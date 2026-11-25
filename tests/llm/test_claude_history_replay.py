"""Exercise real SDK request and response transformations without provider calls."""

import asyncio
from importlib import import_module
import json

import anthropic
import heavenbase as hb
import httpx
import openai
import pytest

from heavenbase.llm.gateways._protocols import _anthropic_payload, _openai_payload

pytestmark = pytest.mark.fast

CLAUDE = "claude-haiku-4-5"
SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds"
DETAIL = {"index": 0, "type": "reasoning.text", "format": "anthropic-claude-v1", "text": "Check the sum.", "signature": SIGNATURE}
THINKING = {"type": "thinking", "thinking": "Check the sum.", "signature": SIGNATURE}
CALL = {"id": "call_add", "type": "function", "function": {"name": "add", "arguments": '{"left":2,"right":3}'}}
CITATION = {
    "type": "char_location",
    "cited_text": "The invoice total is five dollars.",
    "document_index": 0,
    "document_title": "Invoice",
    "start_char_index": 0,
    "end_char_index": 34,
}


def _module(gateway):
    return import_module(f"heavenbase.llm.gateways.{gateway}.runtime")


def _chat_response(model, message):
    return {
        "id": "chatcmpl-review",
        "model": model,
        "object": "chat.completion",
        "created": 100,
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": "stop"}],
    }


def _sse(events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"


def _completion_chunk(delta, finish=None):
    return {
        "id": "chatcmpl-review",
        "model": CLAUDE,
        "object": "chat.completion.chunk",
        "created": 100,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def test_openrouter_history_does_not_send_signed_blocks_to_openai(monkeypatch):
    block = _completion_chunk({"reasoning_details": [DETAIL]})
    calls = []

    def reply(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, text=_sse([block, _completion_chunk({"content": "The sum is five."}, "stop")]))
        return httpx.Response(200, json=_chat_response("gpt-5.6-sol", {"content": "Confirmed."}))

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        client = openai.OpenAI(api_key="placeholder", http_client=http, max_retries=0)
        monkeypatch.setattr(_module("openai"), "_openai_client", lambda spec, route: (client, spec.engine.apply(spec)))
        claude = hb.LLM(model="haiku", provider="openrouter", gateway="openai", cache=False)
        history = claude.chat("Add two and three.", think=True, include="messages")
        gpt = hb.LLM(model="sol", provider="openai", gateway="openai", cache=False)
        assert gpt.chat([*history, "Confirm that result."], stream_chat=False) == "Confirmed."
    assert next(message for message in history if message["role"] == "assistant")["reasoning_details"] == [DETAIL]
    assert all("reasoning_details" not in message for message in calls[-1]["messages"])


def test_portkey_blocking_keeps_citations_in_normalized_message_and_tool_history(monkeypatch):
    text = "The invoice total is five dollars."
    text_block = {"type": "text", "text": text, "citations": [CITATION]}
    original = [THINKING, text_block, {"type": "text", "text": "The invoice was paid."}]
    calls = []

    def reply(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            response = _chat_response(CLAUDE, {"content": text + " The invoice was paid.", "content_blocks": original, "tool_calls": [CALL]})
            response["choices"][0]["finish_reason"] = "tool_calls"
        else:
            response = _chat_response(CLAUDE, {"content": "The sum is five."})
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        client = openai.OpenAI(api_key="placeholder", http_client=http, max_retries=0)
        monkeypatch.setattr(_module("portkey"), "_openai_client", lambda spec, route: (client, spec.engine.apply(spec)))
        llm = hb.LLM(model="haiku", provider="anthropic", gateway="portkey", cache=False, max_tokens=4096)
        history = llm.chat("Summarize the invoice with citations.", stream_chat=False, think=True, tools=[add], include="messages")
    message = next(message for message in history if message["role"] == "assistant")
    assert message["thinking_blocks"] == [THINKING]
    assert message["content_blocks"] == original
    replayed = next(message for message in calls[1]["messages"] if message["role"] == "assistant")
    assert replayed["content_blocks"] == original


USAGE_START = {"input_tokens": 25, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 12, "service_tier": "standard"}
USAGE_FINAL = {**USAGE_START, "output_tokens": 15}
RESULT = {
    "id": "msg_review",
    "type": "message",
    "role": "assistant",
    "model": CLAUDE,
    "content": [{"type": "text", "text": "The sum is five."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": USAGE_FINAL,
}
EVENTS = [
    {"type": "message_start", "message": {**RESULT, "content": [], "stop_reason": None, "usage": USAGE_START}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "The sum is five."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"input_tokens": 25, "output_tokens": 15}},
    {"type": "message_stop"},
]


def _messages_reply(request):
    if json.loads(request.content).get("stream"):
        frames = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in EVENTS)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=frames)
    return httpx.Response(200, json=RESULT)


def _assert_usage_chunks(chunks):
    nonempty = [chunk["usage"] for chunk in chunks if chunk["usage"]]
    assert [(part.get("prompt_tokens", 0), part.get("completion_tokens", 0), part.get("total_tokens", 0)) for part in nonempty if "total_tokens" in part] == [
        (25, 1, 26),
        (0, 14, 14),
    ]
    assert chunks[-1]["usage"] == {}


def test_messages_real_sdk_sync_and_async_usage_matches_blocking(monkeypatch):
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", cache=False, max_tokens=64)
    with httpx.Client(transport=httpx.MockTransport(_messages_reply)) as http:
        client = anthropic.Anthropic(api_key="placeholder", http_client=http, max_retries=0)
        monkeypatch.setattr(_module("anthropic"), "_anthropic_client", lambda spec, route: (client, spec.engine.apply(spec)))
        prompt = "Add two and three."
        blocking = llm.chat(prompt, stream_chat=False, include="usage")
        streamed = llm.chat(prompt, include="usage")
        assert all(streamed[key] == value for key, value in blocking.items())
        assert streamed["total_tokens"] == 40
        _assert_usage_chunks(list(llm.stream(prompt, include=["usage", "messages"], reduce=False)))

    async def verify():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_messages_reply)) as http:
            client = anthropic.AsyncAnthropic(api_key="placeholder", http_client=http, max_retries=0)
            monkeypatch.setattr(_module("anthropic"), "_anthropic_async_client", lambda spec, route: (client, spec.engine.apply(spec)))
            blocking = await llm.achat(prompt, stream_chat=False, include="usage")
            streamed = await llm.achat(prompt, include="usage")
            assert all(streamed[key] == value for key, value in blocking.items())
            assert streamed["total_tokens"] == 40
            _assert_usage_chunks([chunk async for chunk in llm.astream(prompt, include=["usage", "messages"], reduce=False)])

    asyncio.run(verify())


@pytest.mark.slow
@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost"])
def test_messages_explicit_limit_alias(gateway):
    spec = hb.LLM(model="haiku", provider="anthropic", gateway=gateway, endpoint="anthropic").spec
    payload = _anthropic_payload(
        spec.engine.apply(spec), spec, [{"role": "user", "content": "Answer briefly."}], reasoning_max_tokens=2048, max_completion_tokens=2500
    )
    assert payload["max_tokens"] == 2500 and "max_completion_tokens" not in payload


@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost"])
def test_messages_extra_body_explicit_limit_alias(gateway):
    spec = hb.LLM(model="haiku", provider="anthropic", gateway=gateway, endpoint="anthropic").spec
    payload = _anthropic_payload(
        spec.engine.apply(spec), spec, [{"role": "user", "content": "Answer briefly."}], reasoning_max_tokens=2048, extra_body={"max_completion_tokens": 2500}
    )
    assert payload["max_tokens"] == 2500
    assert "max_completion_tokens" not in payload and "max_completion_tokens" not in payload.get("extra_body", {})


def add(left: int, right: int) -> int:
    """Add two integers."""
    return left + right


def test_portkey_replays_default_on_claude_thinking(monkeypatch):
    calls = []

    def reply(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            deltas = [
                {"content_blocks": [{"index": 0, "delta": {"thinking": "Check the sum."}}]},
                {"content_blocks": [{"index": 0, "delta": {"signature": SIGNATURE}}]},
                {"tool_calls": [{"id": "call_add", "index": 0, "type": "function", "function": {"name": "add", "arguments": '{"left":2,"right":3}'}}]},
            ]
            chunks = [_completion_chunk(delta) for delta in deltas] + [_completion_chunk({}, "tool_calls")]
            return httpx.Response(200, text=_sse(chunks))
        return httpx.Response(200, text=_sse([_completion_chunk({"content": "The sum is five."}, "stop")]))

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        client = openai.OpenAI(api_key="placeholder", http_client=http, max_retries=0)
        monkeypatch.setattr(_module("portkey"), "_openai_client", lambda spec, route: (client, spec.engine.apply(spec)))
        llm = hb.LLM(model="sonnet", provider="anthropic", gateway="portkey", cache=False, max_tokens=4096)
        assert llm.chat("Add two and three.", tools=[add]) == "The sum is five."
    assert "thinking" not in calls[0]
    assistant = next(message for message in calls[1]["messages"] if message["role"] == "assistant")
    assert assistant["content_blocks"] == [THINKING]


TEXT_BLOCK = {"type": "text", "text": "The invoice total is five dollars.", "citations": [CITATION]}
REPLAY_FIELDS = {"reasoning_details", "thinking_blocks", "content_blocks", "reasoning_content"}
SOURCES = {
    "openrouter": {"reasoning_details": [DETAIL], "reasoning_content": DETAIL["text"]},
    "bifrost-anthropic": {"reasoning_details": [{key: value for key, value in DETAIL.items() if key != "format"}]},
    "portkey-anthropic": {"content_blocks": [THINKING, TEXT_BLOCK], "thinking_blocks": [THINKING]},
    "anthropic-messages": {"thinking_blocks": [THINKING], "reasoning_content": THINKING["thinking"]},
    "litellm-anthropic": {"thinking_blocks": [THINKING]},
    "deepseek": {"reasoning_content": "Use the calculator."},
}


@pytest.mark.slow
@pytest.mark.parametrize(
    "gateway,provider,model,accepted",
    [
        ("openai", "openai", "sol", set()),
        ("openai", "anthropic", "haiku", set()),
        ("portkey", "openai", "sol", set()),
        ("bifrost", "openai", "sol", set()),
        ("litellm", "openai", "sol", set()),
        ("openai", "openrouter", "haiku", {"reasoning_details", "reasoning_content"}),
        ("portkey", "openrouter", "haiku", {"reasoning_details", "reasoning_content"}),
        ("bifrost", "openrouter", "haiku", {"reasoning_details", "reasoning_content"}),
        ("litellm", "openrouter", "haiku", {"reasoning_details", "reasoning_content"}),
        ("bifrost", "anthropic", "haiku", {"reasoning_details"}),
        ("portkey", "anthropic", "haiku", {"content_blocks"}),
        ("litellm", "anthropic", "haiku", {"thinking_blocks"}),
        ("openai", "deepseek", "dsv4", {"reasoning_content"}),
    ],
)
def test_chat_completions_replay_uses_the_destination_schema(gateway, provider, model, accepted):
    spec = hb.LLM(model=model, provider=provider, gateway=gateway, max_tokens=4096).spec
    runtime = spec.engine.apply(spec)
    for source, sidecars in SOURCES.items():
        assistant = {"role": "assistant", "content": "The total is five.", "tool_calls": [CALL], **sidecars}
        expected = {key: value for key, value in sidecars.items() if key in accepted}
        if gateway == "bifrost" and provider == "anthropic" and "reasoning_details" in expected:
            expected["reasoning_details"] = [{key: value for key, value in detail.items() if key != "format"} for detail in expected["reasoning_details"]]
        if "content_blocks" in accepted and "thinking_blocks" in sidecars and "content_blocks" not in sidecars:
            expected["content_blocks"] = [THINKING, {"type": "text", "text": assistant["content"]}]
        wire = _openai_payload(runtime, spec=spec, messages=[assistant])["messages"][0]
        assert {key: wire[key] for key in REPLAY_FIELDS & wire.keys()} == expected, source
        assert wire["tool_calls"] == [CALL] and wire["content"] == assistant["content"], source
        assert assistant == {"role": "assistant", "content": "The total is five.", "tool_calls": [CALL], **sidecars}


@pytest.mark.slow
@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost"])
def test_messages_replay_uses_native_content_blocks(gateway):
    spec = hb.LLM(model="haiku", provider="anthropic", gateway=gateway, endpoint="anthropic", max_tokens=4096).spec
    for source, sidecars in SOURCES.items():
        assistant = {"role": "assistant", "content": "The total is five.", "tool_calls": [CALL], **sidecars}
        wire = _anthropic_payload(spec.engine.apply(spec), spec, [assistant], think=True)["messages"][0]
        assert not REPLAY_FIELDS.intersection(wire), source
        expected_thinking = sidecars.get("thinking_blocks", [])
        assert wire["content"][: len(expected_thinking)] == expected_thinking, source
        assert wire["content"][len(expected_thinking)] == {"type": "text", "text": "The total is five."}, source
        assert wire["content"][-1]["id"] == CALL["id"], source


def test_portkey_combines_normalized_thinking_with_original_content_blocks():
    spec = hb.LLM(model="haiku", provider="anthropic", gateway="portkey", max_tokens=4096).spec
    runtime = spec.engine.apply(spec)
    history = [{"role": "assistant", "content": TEXT_BLOCK["text"], "thinking_blocks": [THINKING], "content_blocks": [TEXT_BLOCK]}]
    assert _openai_payload(runtime, spec=spec, messages=history)["messages"][0]["content_blocks"] == [THINKING, TEXT_BLOCK]
    assert _openai_payload(runtime, spec=spec, messages=history, think=False)["messages"][0]["content_blocks"] == [TEXT_BLOCK]


def test_openai_extra_body_history_also_uses_the_destination_schema():
    spec = hb.LLM(model="sol", provider="openai", gateway="openai").spec
    assistant = {"role": "assistant", "content": "The total is five.", "reasoning_details": [DETAIL]}
    payload = _openai_payload(spec.engine.apply(spec), spec=spec, messages=[assistant], extra_body={"messages": [assistant]})
    assert payload["extra_body"]["messages"] == [{"role": "assistant", "content": "The total is five."}]


def test_portkey_compaction_keeps_citations_and_strips_signed_blocks():
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="portkey", max_tokens=4096)
    assistant = {"role": "assistant", "thinking_blocks": [THINKING], "content_blocks": [THINKING, TEXT_BLOCK]}
    kept, _ = llm._compacted_turn_rounds([[assistant], [assistant], [assistant]], "The invoice total was verified.")
    assert kept[0][0]["content_blocks"] == [TEXT_BLOCK]
    assert "thinking_blocks" not in kept[0][0]
    assert kept[-1][-1]["role"] == "user"
