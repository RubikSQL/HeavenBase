"""LLM session history and runtime tool composition behavior."""

from __future__ import annotations

import inspect

import pytest

import heavenbase as hb
from heavenbase.llm import LLMSession
from heavenbase.utils import pj


def _db(tmp_path, name: str) -> str:
    return hb.utils.path_to_file_uri(pj(str(tmp_path), name))


def test_stream_with_tools_streams_every_round_live():
    def add(left: int, right: int) -> int:
        return left + right

    def mock_chat(spec, messages, stream=False, **kwargs):
        has_tool = any(message.get("role") == "tool" for message in messages)
        if not has_tool:
            payload = {
                "role": "assistant",
                "content": "",
                "reasoning_content": "plan: use add",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "add", "arguments": '{"left":2,"right":3}'}}],
            }
        else:
            payload = {"role": "assistant", "content": "sum=5", "reasoning_content": "tool said 5"}
        if stream:
            first = {"choices": [{"delta": {"role": "assistant", "reasoning_content": payload["reasoning_content"]}}]}
            second_delta = {"role": "assistant", "content": payload.get("content", "")}
            if "tool_calls" in payload:
                second_delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([first, {"choices": [{"delta": second_delta}], "usage": {"total_tokens": 1}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    chunks = list(llm.stream("use tools", include=["text", "think", "tool_calls", "delta"], reduce=False, tools=[add]))

    # Round-1 reasoning arrives live, before the tool even runs.
    assert chunks[0]["think"] == "plan: use add"
    # The boundary chunk carries the executed tool result in its delta.
    boundary = next(chunk for chunk in chunks if any(message.get("role") == "tool" for message in chunk["delta"]))
    assert boundary["delta"][-1]["content"] == "5"
    # Round-2 reasoning and answer stream after the tool result.
    assert any(chunk["think"] == "tool said 5" for chunk in chunks)
    assert any(chunk["text"] == "sum=5" for chunk in chunks)
    # The synthetic final-delta chunk carries metadata only; consumers must
    # see the streamed answer exactly once.
    assert [chunk["text"] for chunk in chunks if chunk["text"]] == ["sum=5"]
    assert chunks[-1]["text"] == ""
    # The final aggregate delta holds the whole conversation added by the call.
    assert len(chunks[-1]["delta"]) == 3


@pytest.mark.parametrize("max_tool_turns", [0, 1, 2])
def test_stream_tool_turn_limit_matches_chat(max_tool_turns):
    executions = []

    def touch(step: int) -> int:
        executions.append(step)
        return step

    def mock_chat(spec, messages, stream=False, **kwargs):
        step = sum(message.get("role") == "tool" for message in messages) + 1
        call = {"id": f"c{step}", "type": "function", "function": {"name": "touch", "arguments": f'{{"step":{step}}}'}}
        payload = {"role": "assistant", "content": "", "reasoning_content": f"round {step}", "tool_calls": [call]}
        if stream:
            return iter([{"choices": [{"delta": {**payload, "tool_calls": [{"index": 0, **call}]}}], "usage": {"total_tokens": 1}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    chat_rounds = []
    chat_result = llm.chat(
        "keep using tools",
        include=["delta", "tool_calls", "truncated", "tool_turns", "max_tool_turns"],
        reduce=False,
        tools=[touch],
        max_tool_turns=max_tool_turns,
        on_round=lambda response: chat_rounds.append(response.think),
    )

    assert "on_round" in inspect.signature(hb.LLM.chat).parameters
    assert "on_round" in inspect.signature(hb.LLM.stream).parameters
    assert executions == list(range(1, max_tool_turns + 1))
    assert chat_rounds == [f"round {step}" for step in range(1, max_tool_turns + 2)]
    assert [message["role"] for message in chat_result["delta"]] == ["assistant", "tool"] * max_tool_turns + ["assistant"]
    # This mock never stops calling tools, so every budget here is exhausted.
    assert (chat_result["truncated"], chat_result["tool_turns"], chat_result["max_tool_turns"]) == (True, max_tool_turns, max_tool_turns)

    executions.clear()
    stream_rounds = []
    chunks = list(
        llm.stream(
            "keep using tools",
            include=["delta", "tool_calls", "truncated", "tool_turns", "max_tool_turns"],
            reduce=False,
            tools=[touch],
            max_tool_turns=max_tool_turns,
            on_round=lambda response: stream_rounds.append(response.think),
        )
    )

    assert executions == list(range(1, max_tool_turns + 1))
    assert stream_rounds == chat_rounds
    assert chunks[-1]["delta"] == chat_result["delta"]
    # Streaming reaches the same verdict as chat, on the same evidence.
    assert (chunks[-1]["truncated"], chunks[-1]["tool_turns"], chunks[-1]["max_tool_turns"]) == (True, max_tool_turns, max_tool_turns)


def _investigating_mock(rounds_needed: int):
    """Mock that keeps calling a tool until it has run ``rounds_needed`` of them."""

    def mock_chat(spec, messages, stream=False, **kwargs):
        done = sum(message.get("role") == "tool" for message in messages)
        if done >= rounds_needed:
            payload = {"role": "assistant", "content": "every entity counted"}
        else:
            call = {"id": f"c{done}", "type": "function", "function": {"name": "count", "arguments": f'{{"step":{done}}}'}}
            payload = {"role": "assistant", "content": "Let me check the remaining entities.", "tool_calls": [call]}
        if stream:
            delta = dict(payload)
            if "tool_calls" in payload:
                delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([{"choices": [{"delta": delta}], "usage": {"total_tokens": 1}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    return mock_chat


def test_tool_budget_exhaustion_states_itself_in_the_answer():
    """A loop that ran out of rounds must not read like a finished answer."""

    def count(step: int) -> int:
        return step

    llm = hb.LLM(preset="mock", mock_chat=_investigating_mock(12), mock_pause=(0, 0), cache=False)

    # The default projection is a bare string, so the string itself has to say it.
    text = llm.chat("count every entity", tools=[count], max_tool_turns=3)
    assert text.startswith("Let me check the remaining entities.")
    assert "[incomplete: the tool-call budget of 3 round(s) ran out" in text

    flags = llm.chat("count every entity", include=["truncated", "tool_turns", "max_tool_turns", "delta"], reduce=False, tools=[count], max_tool_turns=3)
    assert (flags["truncated"], flags["tool_turns"], flags["max_tool_turns"]) == (True, 3, 3)
    # The history a caller replays or renders carries the same verdict as `text`.
    assert "[incomplete:" in flags["delta"][-1]["content"]

    # LLMSession.send hands back the message itself, so the message must carry it.
    session = LLMSession(llm, tools=[count])
    final = session.send("count every entity", max_tool_turns=2)
    assert "[incomplete: the tool-call budget of 2 round(s) ran out" in final["content"]
    assert session.messages[-1] is final

    # Streaming text callers receive the verdict as its own final chunk rather
    # than losing the annotation inside an aggregate they did not request.
    default_chunks = list(llm.stream("count every entity", tools=[count], max_tool_turns=1))
    text_chunks = list(llm.stream("count every entity", include="text", tools=[count], max_tool_turns=1))
    for chunks in (default_chunks, text_chunks):
        assert chunks[-1].startswith("[incomplete: the tool-call budget of 1 round(s) ran out")
        assert "Let me check the remaining entities." in "".join(chunks[:-1])


def test_explicit_tool_schema_attr_wins_over_signature_derivation():
    import functools

    from heavenbase.llm.base import _CallableToolRunner

    def free_form(**kwargs):
        """Bridge tool with a protocol-supplied schema."""
        return kwargs

    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    free_form.__tool_schema__ = schema
    assert _CallableToolRunner(free_form).to_openai_tool()["function"]["parameters"] == schema

    @functools.wraps(free_form)
    def wrapped(*args, **kwargs):
        return free_form(*args, **kwargs)

    assert _CallableToolRunner(wrapped).input_schema == schema

    def plain(entity: str, limit: int = 5):
        """Signature-derived tool."""

    derived = _CallableToolRunner(plain).input_schema
    assert derived["properties"]["entity"] == {"type": "string"} and derived["required"] == ["entity"]


def test_tool_result_capture_clips_the_captured_message_in_characters():
    def bulky() -> str:
        return "X" * 500

    def mock_chat(spec, messages, stream=False, **kwargs):
        if any(message.get("role") == "tool" for message in messages):
            payload = {"role": "assistant", "content": "done"}
        else:
            call = {"id": "c1", "type": "function", "function": {"name": "bulky", "arguments": "{}"}}
            payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        if stream:
            delta = dict(payload)
            if "tool_calls" in payload:
                delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([{"choices": [{"delta": delta}], "usage": {"total_tokens": 1}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)

    def tool_content(delta):
        return next(message for message in delta if message["role"] == "tool")["content"]

    # Generic LLM calls preserve complete tool results by default.
    whole = tool_content(llm.chat("fetch", include=["delta"], reduce=False, tools=[bulky])["delta"])
    assert "X" * 500 in whole
    # An explicit 0 disables the clip outright.
    assert tool_content(llm.chat("fetch", include=["delta"], reduce=False, tools=[bulky], tool_result_capture_chars=0)["delta"]) == whole

    clipped = tool_content(llm.chat("fetch", include=["delta"], reduce=False, tools=[bulky], tool_result_capture_chars=25)["delta"])
    assert clipped == f"{whole[:25]}\n… [tool result truncated: kept 25 of {len(whole)} characters; make a narrower call for the rest]"

    streamed = list(llm.stream("fetch", include=["delta"], reduce=False, tools=[bulky], tool_result_capture_chars=25))[-1]["delta"]
    assert tool_content(streamed) == clipped  # the stream loop clips identically


@pytest.mark.anyio
async def test_async_tool_loop_honors_capture_and_compaction_controls():
    def bulky() -> str:
        return "X" * 500

    def capture_mock(spec, messages, stream=False, **kwargs):
        if any(message.get("role") == "tool" for message in messages):
            payload = {"role": "assistant", "content": "done"}
        else:
            payload = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bulky", "arguments": "{}"}}],
            }
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    llm = hb.LLM(preset="mock", mock_chat=capture_mock, mock_pause=(0, 0), cache=False)
    captured = await llm.achat("fetch", include=["delta"], reduce=False, tools=[bulky], tool_result_capture_chars=25)
    tool_message = next(message for message in captured["delta"] if message["role"] == "tool")
    assert tool_message["content"].startswith("X" * 25)
    assert "kept 25 of 500 characters" in tool_message["content"]

    mock_chat, count, seen, _ = _compacting_probe()
    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    result = await llm.achat("investigate everything", include=["text", "delta"], reduce=False, tools=[count], compact_ratio=0.005)
    assert result["text"] == "final answer"
    assert sum(message["role"] == "tool" for message in result["delta"]) == 5
    assert any(any("[compacted working memory" in str(message.get("content") or "") for message in messages) for messages, _ in seen)


def _compacting_probe():
    """Mock, tool, and recorders for driving mid-turn compaction offline."""
    seen = []
    state = {"round": 0}

    def count(step: int) -> str:
        return "R" * 800 + f"#{step}"

    def mock_chat(spec, messages, stream=False, **kwargs):
        seen.append(([dict(message) for message in messages], "tools" in kwargs))
        if str((messages[0] or {}).get("content") or "").startswith("Compress this turn's"):
            payload = {"role": "assistant", "content": "MEMO: early rounds folded"}
        elif state["round"] >= 5:
            payload = {"role": "assistant", "content": "final answer"}
        else:
            call = {"id": f"c{state['round']}", "type": "function", "function": {"name": "count", "arguments": f'{{"step":{state["round"]}}}'}}
            state["round"] += 1
            payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        if stream:
            delta = dict(payload)
            if "tool_calls" in payload:
                delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([{"choices": [{"delta": delta}], "usage": {"total_tokens": 1}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    return mock_chat, count, seen, state


def test_context_ceiling_compacts_the_oldest_rounds_mid_turn():
    mock_chat, count, seen, _ = _compacting_probe()
    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    result = llm.chat("investigate everything", include=["text", "delta"], reduce=False, tools=[count], compact_ratio=0.005)
    assert result["text"] == "final answer"  # the loop continued to completion
    # The recorded evidence stays whole: all five executed tool rounds.
    assert sum(message["role"] == "tool" for message in result["delta"]) == 5
    compress = [messages for messages, _ in seen if str(messages[0].get("content") or "").startswith("Compress this turn's")]
    assert len(compress) >= 2  # overflowing again later in the turn compacts again
    assert "R" * 800 in compress[0][0]["content"]  # old rounds' evidence feeds the summary
    compacted = [messages for messages, _ in seen if any("[compacted working memory" in str(message.get("content") or "") for message in messages)]
    view = compacted[0]
    assert view[0]["content"] == "investigate everything"  # the head is immutable
    kept = [message for message in view if message.get("role") == "tool"]
    assert len(kept) == 2 and all("R" * 800 in message["content"] for message in kept)  # newest two rounds ride verbatim

    # The stream loop compacts identically.
    mock_chat, count, seen, _ = _compacting_probe()
    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    chunks = list(llm.stream("investigate everything", include=["text", "delta"], reduce=False, tools=[count], compact_ratio=0.005))
    assert sum(message["role"] == "tool" for message in chunks[-1]["delta"]) == 5
    assert any(any("[compacted working memory" in str(message.get("content") or "") for message in messages) for messages, _ in seen)


def test_context_ceiling_failure_wraps_the_turn_up_without_tools():
    mock_chat, count, seen, state = _compacting_probe()

    def failing_mock(spec, messages, stream=False, **kwargs):
        if str((messages[0] or {}).get("content") or "").startswith("Compress this turn's"):
            seen.append(([dict(message) for message in messages], "tools" in kwargs))
            return {"choices": [{"message": {"role": "assistant", "content": "   "}}], "usage": {"total_tokens": 1}}
        if "tools" not in kwargs:
            seen.append(([dict(message) for message in messages], False))
            return {"choices": [{"message": {"role": "assistant", "content": "wrapped up from evidence"}}], "usage": {"total_tokens": 1}}
        return mock_chat(spec, messages, stream=stream, **kwargs)

    llm = hb.LLM(preset="mock", mock_chat=failing_mock, mock_pause=(0, 0), cache=False)
    result = llm.chat("investigate everything", include=["text"], reduce=False, tools=[count], compact_ratio=0.005)
    assert result["text"] == "wrapped up from evidence"  # never a hard failure
    wrapped = [messages for messages, offered in seen if not offered and not str(messages[0].get("content") or "").startswith("Compress this turn's")]
    assert wrapped  # the remaining rounds ran without tools on offer
    note = "[context is nearly full — answer now from the evidence already gathered]"
    assert any(note in str(message.get("content") or "") for message in wrapped[0])


def test_context_fullness_prefers_real_reported_usage():
    def count(step: int) -> str:
        return f"tiny#{step}"

    def make_mock(prompt_tokens, seen, state):
        def mock_chat(spec, messages, stream=False, **kwargs):
            seen.append([dict(message) for message in messages])
            if str((messages[0] or {}).get("content") or "").startswith("Compress this turn's"):
                return {"choices": [{"message": {"role": "assistant", "content": "MEMO: folded"}}], "usage": {"total_tokens": 1}}
            if state["round"] >= 5:
                payload = {"role": "assistant", "content": "final answer"}
            else:
                call = {"id": f"c{state['round']}", "type": "function", "function": {"name": "count", "arguments": f'{{"step":{state["round"]}}}'}}
                state["round"] += 1
                payload = {"role": "assistant", "content": "", "tool_calls": [call]}
            usage = {"total_tokens": 1}
            if prompt_tokens:
                usage["prompt_tokens"] = prompt_tokens
            return {"choices": [{"message": payload}], "usage": usage}

        return mock_chat

    def compress_calls(seen):
        return [messages for messages in seen if str(messages[0].get("content") or "").startswith("Compress this turn's")]

    # The provider reports a nearly full window; the tiny transcript's own
    # estimate never would. The real number is in charge.
    seen: list = []
    llm = hb.LLM(preset="mock", mock_chat=make_mock(125000, seen, {"round": 0}), mock_pause=(0, 0), cache=False)
    result = llm.chat("investigate", include=["text"], reduce=False, tools=[count], compact_ratio=0.92)
    assert result["text"] == "final answer"
    assert compress_calls(seen)
    # Usage-less responses fall back to the pure estimate (also the cold-start
    # path), which stays far under 92% of the window here.
    quiet: list = []
    llm = hb.LLM(preset="mock", mock_chat=make_mock(None, quiet, {"round": 0}), mock_pause=(0, 0), cache=False)
    assert llm.chat("investigate", include=["text"], reduce=False, tools=[count])["text"] == "final answer"
    assert not compress_calls(quiet)


def test_context_ceiling_off_leaves_the_loop_byte_identical():
    def count(step: int) -> int:
        return step

    llm = hb.LLM(preset="mock", mock_chat=_investigating_mock(2), mock_pause=(0, 0), cache=False)
    baseline = llm.chat("count every entity", include=["delta"], reduce=False, tools=[count])
    explicit = llm.chat("count every entity", include=["delta"], reduce=False, tools=[count], compact_ratio=0)
    assert explicit["delta"] == baseline["delta"]


def test_completed_tool_loop_is_not_marked_incomplete():
    """The marker must be absent whenever the model actually finished."""

    def count(step: int) -> int:
        return step

    llm = hb.LLM(preset="mock", mock_chat=_investigating_mock(2), mock_pause=(0, 0), cache=False)
    finished = llm.chat("count every entity", include=["text", "truncated", "tool_turns", "max_tool_turns"], reduce=False, tools=[count], max_tool_turns=8)

    assert finished["text"] == "every entity counted"
    assert (finished["truncated"], finished["tool_turns"], finished["max_tool_turns"]) == (False, 2, 8)

    # Reaching the budget on the round that finishes is not truncation.
    exact = llm.chat("count every entity", include=["text", "truncated", "tool_turns"], reduce=False, tools=[count], max_tool_turns=2)
    assert (exact["text"], exact["truncated"], exact["tool_turns"]) == ("every entity counted", False, 2)

    # A tool-free answer still reports the budget it never needed.
    idle = hb.LLM(preset="mock", mock_chat=_investigating_mock(0), mock_pause=(0, 0), cache=False)
    quiet = idle.chat("count every entity", include=["truncated", "tool_turns", "max_tool_turns"], reduce=False, tools=[count], max_tool_turns=8)
    assert quiet == {"truncated": False, "tool_turns": 0, "max_tool_turns": 8}


def test_chat_tool_roundtrip_keeps_every_rounds_think():
    def add(left: int, right: int) -> int:
        return left + right

    def mock_chat(spec, messages, stream=False, **kwargs):
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "I should add the numbers with the add tool.",
                            "tool_calls": [{"id": "call_add", "type": "function", "function": {"name": "add", "arguments": '{"left":2,"right":3}'}}],
                        }
                    }
                ],
                "usage": {"total_tokens": 1},
            }
        return {
            "choices": [{"message": {"role": "assistant", "content": "sum=5", "reasoning_content": "The tool returned 5, so the answer is 5."}}],
            "usage": {"total_tokens": 1},
        }

    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    rounds = []
    result = llm.chat("use tools", include=["text", "think"], reduce=False, tools=[add], on_round=lambda response: rounds.append(response.think))

    assert result["text"] == "sum=5"
    # The aggregated chain keeps the tool-planning round, not just the final round.
    assert result["think"] == "I should add the numbers with the add tool.\n\nThe tool returned 5, so the answer is 5."
    # on_round surfaces each completed round live, in order.
    assert rounds == ["I should add the numbers with the add tool.", "The tool returned 5, so the answer is 5."]


def test_chat_tools_accept_callables_toolkits_and_schemas():
    calls = []

    def add(left: int, right: int) -> int:
        return left + right

    def multiply(left: int, right: int) -> int:
        return left * right

    toolkit = hb.Toolkit("math-tools", {"multiply": multiply}, namespace="demo")
    schema = {
        "name": "lookup_note",
        "description": "Schema-only tool definition.",
        "parameters": {"type": "object", "properties": {"note_id": {"type": "string"}}, "required": ["note_id"]},
    }

    def mock_chat(spec, messages, stream=False, **kwargs):
        calls.append({"messages": [dict(message) for message in messages], "tool_names": [item["function"]["name"] for item in kwargs.get("tools") or []]})
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"id": "call_add", "type": "function", "function": {"name": "add", "arguments": '{"left":2,"right":3}'}}],
                        }
                    }
                ],
                "usage": {"total_tokens": 1},
            }
        return {"choices": [{"message": {"role": "assistant", "content": f"sum={messages[-1]['content']}"}}], "usage": {"total_tokens": 1}}

    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    result = llm.chat("use tools", include=["text", "delta"], reduce=False, tools=[add, toolkit, schema])

    assert result["text"] == "sum=5"
    assert [message["role"] for message in result["delta"]] == ["assistant", "tool", "assistant"]
    assert result["delta"][1]["content"] == "5"
    assert calls[0]["tool_names"] == ["add", "multiply", "lookup_note"]
    assert calls[1]["messages"][-1]["role"] == "tool"


def test_session_add_mcp_imports_fastmcp_toolkit_and_executes_delta():
    workspace = hb.HeavenBase("session-mcp-tools", backends={"main": {"type": "inmem"}})
    server_toolkit = workspace.to_mcp(name="session-mcp-tools")

    def mock_chat(spec, messages, stream=False, **kwargs):
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_list",
                                    "type": "function",
                                    "function": {"name": "list_entities", "arguments": '{"include_system":false}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 1},
            }
        return {"choices": [{"message": {"role": "assistant", "content": "listed"}}], "usage": {"total_tokens": 1}}

    session = LLMSession(hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False))
    session.add_mcp(server_toolkit.to_fastmcp(), name="session-mcp-client")

    final = session.send("list entities")

    assert final["content"] == "listed"
    assert [message["role"] for message in session.messages] == ["user", "assistant", "tool", "assistant"]
    assert session.messages[2]["name"] == "list_entities"
    assert "session-mcp-tools" in session.messages[2]["content"]


def test_session_list_tools_supports_workspace_and_external_mcp():
    workspace = hb.HeavenBase("session-mixed-mcp-tools", backends={"main": {"type": "inmem"}})

    def add_numbers(left: int, right: int) -> int:
        """Add two integers."""
        return left + right

    session = LLMSession(hb.LLM(preset="mock", cache=False))
    session.add_mcp(workspace.to_mcp(name="session-workspace", profile="agent").to_fastmcp(), name="session-workspace-client")
    session.add_mcp(hb.Toolkit("session-math", {"add_numbers": add_numbers}, namespace="demo").to_fastmcp(), name="session-math-client")

    assert "list_entities" in session.list_tools()
    assert "query" in session.list_tools()
    assert session.list_tools()[-1] == "add_numbers"


def test_session_add_tool_rejects_duplicate_names_before_send():
    session = LLMSession(hb.LLM(preset="mock", cache=False))
    session.add_tool(
        {
            "name": "lookup_note",
            "description": "Look up a note.",
            "parameters": {"type": "object", "properties": {"note_id": {"type": "string"}}, "required": ["note_id"]},
        }
    )

    with pytest.raises(ValueError, match="Duplicate session tool name"):
        session.add_tool(
            {
                "type": "function",
                "function": {
                    "name": "lookup_note",
                    "description": "Duplicate lookup tool.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
        )

    assert session.list_tools() == ["lookup_note"]


def test_session_add_mcp_rejects_duplicate_external_tool_names():
    workspace = hb.HeavenBase("session-duplicate-mcp-tools", backends={"main": {"type": "inmem"}})

    def query(entity: str) -> dict[str, str]:
        """Colliding tool name used by the external MCP server."""
        return {"entity": entity}

    session = LLMSession(hb.LLM(preset="mock", cache=False))
    session.add_mcp(workspace.to_mcp(name="session-workspace", profile="agent").to_fastmcp(), name="session-workspace-client")

    with pytest.raises(ValueError, match="Duplicate session tool name"):
        session.add_mcp(hb.Toolkit("session-collision", {"query": query}, namespace="demo").to_fastmcp(), name="session-collision-client")

    assert session.list_tools().count("query") == 1


def test_session_exposes_one_validated_history_protocol(tmp_path):
    public = {name for name in LLMSession.__dict__ if not name.startswith("_") and callable(getattr(LLMSession, name, None))}

    assert public == {"add_mcp", "add_tool", "clear", "from_dict", "list_tools", "load", "save", "send", "to_dict", "undo"}
    assert {"add_tools", "append_assistant", "back"}.isdisjoint(LLMSession.__dict__)

    llm = hb.LLM(preset="mock", cache=False)
    session = LLMSession.from_dict({"messages": ["hello"]}, llm=llm)
    path = tmp_path / "session.json"
    session.save(str(path))
    restored = LLMSession.load(str(path), llm=llm)

    assert restored.llm is llm
    assert restored.messages == [{"role": "user", "content": "hello"}]
    assert restored.tools == []
    duplicate = {"name": "same", "parameters": {"type": "object", "properties": {}}}
    with pytest.raises(ValueError, match="Duplicate session tool name"):
        LLMSession(llm=llm, tools=[duplicate, duplicate])
    with pytest.raises(TypeError, match="messages must be a list"):
        LLMSession.from_dict({"messages": {}})
    with pytest.raises(FileNotFoundError):
        LLMSession.load(str(tmp_path / "missing.json"))
    with pytest.raises(TypeError, match="owns option.*include"):
        restored.send("hello", include="text")
    with pytest.raises(TypeError, match="owns option.*tools"):
        restored.send("hello", tools=[])


def test_chat_tool_errors_are_structured_for_agent_context():
    def fail() -> str:
        raise ValueError("bad input")

    def mock_chat(spec, messages, stream=False, **kwargs):
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"id": "call_fail", "type": "function", "function": {"name": "fail", "arguments": "{}"}}],
                        }
                    }
                ],
                "usage": {"total_tokens": 1},
            }
        return {"choices": [{"message": {"role": "assistant", "content": "handled"}}], "usage": {"total_tokens": 1}}

    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    result = llm.chat("use failing tool", include=["delta"], reduce=False, tools=[fail])
    payload = hb.loads_json(result["delta"][1]["content"])

    assert payload == {"ok": False, "error": {"type": "ValueError", "message": "bad input"}}


@pytest.mark.slow
def test_llm_session_mcp_ref_resolution_supports_dot_and_unambiguous_names(tmp_path):
    from heavenbase.toolkit.refs import load_toolkit_ref

    def add(left: int, right: int) -> int:
        return left + right

    workspace = hb.HeavenBase(
        "session-mcp-registry",
        backends={"main": {"type": "sqlite", "name": "main", "database": _db(tmp_path, "session-mcp-registry.db")}},
        detached=True,
    )
    hb.Toolkit("math-tools", {"add": add}, namespace="quickstart", version="1").register(ws=workspace)
    hb.Toolkit("math-tools", {"add": add}, namespace="quickstart", version="2").register(ws=workspace)
    hb.Toolkit("unique-tools", {"add": add}, namespace="only", version="2").register(ws=workspace)
    hb.Toolkit("ambiguous-tools", {"add": add}, namespace="left", version="1").register(ws=workspace)
    hb.Toolkit("ambiguous-tools", {"add": add}, namespace="right", version="1").register(ws=workspace)

    dotted = load_toolkit_ref("quickstart.math-tools:1", ws=workspace)
    unqualified_latest = load_toolkit_ref("math-tools:-1", ws=workspace)
    unique = load_toolkit_ref("unique-tools:2", ws=workspace)

    assert dotted.namespace == "quickstart"
    assert dotted.name == "math-tools"
    assert unqualified_latest.namespace == "quickstart"
    assert unqualified_latest.version == "2"
    assert unique.namespace == "only"
    with pytest.raises(KeyError, match="Ambiguous toolkit name"):
        load_toolkit_ref("ambiguous-tools:1", ws=workspace)


@pytest.mark.slow
def test_registry_negative_versions_select_latest_offsets(tmp_path):
    from heavenbase.toolkit.refs import load_toolkit_ref

    def add(left: int, right: int) -> int:
        return left + right

    registry_db = _db(tmp_path, "negative-version-registry.db")
    registry_config = {"backend": {"main": {"type": "sqlite", "name": "main", "database": registry_db}}}
    capsule_store = hb.capsule_registry(registry_config)
    workspace = hb.HeavenBase(
        "negative-version-registry",
        backends={"main": {"type": "sqlite", "name": "main", "database": _db(tmp_path, "negative-toolkit-registry.db")}},
        detached=True,
    )
    for version in ["1", "2", "3"]:
        hb.Capsule(add, name="capsule-add", namespace="demo", version=version).register(registry=capsule_store)
        hb.Toolkit("math-tools", {"add": add}, namespace="demo", version=version).register(ws=workspace)

    assert hb.Capsule.load(name="capsule-add", namespace="demo", version="-1", registry=capsule_store).manifest.version == "3"
    assert hb.Capsule.load(name="capsule-add", namespace="demo", version="-2", registry=capsule_store).manifest.version == "2"
    assert hb.Toolkit.load(name="math-tools", namespace="demo", version="-1", ws=workspace).version == "3"
    assert hb.Toolkit.describe(name="math-tools", namespace="demo", version="-2", ws=workspace)["version"] == "2"
    assert load_toolkit_ref("demo.math-tools:-1", ws=workspace).version == "3"
    assert load_toolkit_ref("demo/math-tools:-2", ws=workspace).version == "2"
    with pytest.raises(KeyError):
        hb.Toolkit.load(name="math-tools", namespace="demo", version="-4", ws=workspace)
