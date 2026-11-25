"""In-turn compaction of a Claude thinking tool loop keeps the next request valid.

With thinking on, the Messages API requires the final assistant turn of a request (the assistant
messages after the last user message that is not only tool results) to start with a thinking block.
Claude writes that block in the first round of a turn. The fake client below follows the documented
shapes: round 1 returns a signed thinking block and a tool_use, later rounds return tool_use only.
"""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm.gateways.anthropic import runtime as anthropic_gateway

pytestmark = pytest.mark.fast

SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds"
THINKING = {"type": "thinking", "thinking": "plan", "signature": SIGNATURE}
MEMO = "compacted working memory"


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))


def add(left: int, right: int) -> int:
    return left + right


def _message(content, stop):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku",
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": 400, "output_tokens": 5},
    }


def _starts_turn(payload):
    """Whether the request's final assistant turn is empty (a new turn starts)."""
    last = payload["messages"][-1]
    return last["role"] == "user" and not _only_tool_results(last)


def _only_tool_results(message):
    content = message["content"]
    return isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _reply(payload, calls, thinking):
    if not payload.get("tools"):
        return _message([{"type": "text", "text": "summary: 2+3=5, 3+3=6"}], "end_turn")
    rounds = sum(1 for p in calls if p.get("tools"))
    # A turn's first round thinks; so does the first round after a user text message.
    think = [dict(THINKING)] if thinking and (rounds == 1 or _starts_turn(payload)) else []
    if rounds <= 4:
        use = {"type": "tool_use", "id": f"toolu_{rounds}", "name": "add", "input": {"left": rounds, "right": 3}}
        return _message([*think, use], "tool_use")
    return _message([*think, {"type": "text", "text": "done"}], "end_turn")


def _install(monkeypatch, payloads, thinking):
    class FakeMessages:
        def create(self, **payload):
            payloads.append(payload)
            return _reply(payload, payloads, thinking)

    class FakeAsyncMessages:
        async def create(self, **payload):
            payloads.append(payload)
            return _reply(payload, payloads, thinking)

    class FakeClient:
        messages = FakeMessages()

    class FakeAsyncClient:
        messages = FakeAsyncMessages()

    def runtime(spec):
        return LLM_ENGINE.apply(spec, profile=spec.gateway)

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), runtime(spec)))
    monkeypatch.setattr(anthropic_gateway, "_anthropic_async_client", lambda spec, mode="anthropic": (FakeAsyncClient(), runtime(spec)))


def _final_assistant_turn(messages):
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user" and not _only_tool_results(m))
    return [m for m in messages[last_user + 1 :] if m["role"] == "assistant"]


def _assert_valid_thinking_request(payload):
    assert payload["thinking"]["type"] != "disabled"
    turn = _final_assistant_turn(payload["messages"])
    assert not turn or turn[0]["content"][0]["type"] in {"thinking", "redacted_thinking"}, [b.get("type") for b in turn[0]["content"]]


def _llm(stream_chat):
    # Non-streamed provider rounds keep the fake simple; the compaction rule is the same either way.
    return hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=2048, stream_chat=stream_chat)


def _loop_payloads(payloads):
    return [p for p in payloads if p.get("tools")]


@pytest.mark.parametrize("call", ["chat", "stream"])
def test_every_request_of_a_compacted_thinking_loop_is_valid(monkeypatch, call):
    payloads = []
    _install(monkeypatch, payloads, thinking=True)
    llm = _llm(False)

    if call == "stream":
        text = "".join(llm.stream("add things", think=True, tools=[add], compact_ratio=0.00001, include="text"))
    else:
        text = llm.chat("add things", think=True, tools=[add], compact_ratio=0.00001)

    assert text == "done"
    assert any(not p.get("tools") for p in payloads), "compaction ran"
    assert any(MEMO in str(m["content"]) for p in _loop_payloads(payloads) for m in p["messages"])
    for payload in _loop_payloads(payloads):
        _assert_valid_thinking_request(payload)


def test_async_compacted_thinking_loop_is_valid(monkeypatch):
    import asyncio

    payloads = []
    _install(monkeypatch, payloads, thinking=True)

    text = asyncio.run(_llm(False).achat("add things", think=True, tools=[add], compact_ratio=0.00001))

    assert text == "done"
    assert any(MEMO in str(m["content"]) for p in _loop_payloads(payloads) for m in p["messages"])
    for payload in _loop_payloads(payloads):
        _assert_valid_thinking_request(payload)


def test_loops_without_thinking_keep_the_memo_before_the_latest_rounds(monkeypatch):
    payloads = []
    _install(monkeypatch, payloads, thinking=False)

    assert _llm(False).chat("add things", tools=[add], compact_ratio=0.00001) == "done"

    compacted = [p for p in _loop_payloads(payloads) if any(MEMO in str(m["content"]) for m in p["messages"])]
    assert compacted
    messages = compacted[0]["messages"]
    assert messages[0]["content"] == "add things" and MEMO in str(messages[1]["content"])


def _install_interleaved(monkeypatch, payloads):
    """Every tool round thinks (interleaved thinking), so the kept rounds carry thinking blocks."""

    def reply(payload):
        if not payload.get("tools"):
            return _message([{"type": "text", "text": "summary: 1+3=4, 2+3=5"}], "end_turn")
        rounds = sum(1 for p in payloads if p.get("tools"))
        think = [{"type": "thinking", "thinking": f"step {rounds}", "signature": f"{SIGNATURE}{rounds}"}]
        if rounds <= 4:
            use = {"type": "tool_use", "id": f"toolu_{rounds}", "name": "add", "input": {"left": rounds, "right": 3}}
            return _message([*think, use], "tool_use")
        return _message([*think, {"type": "text", "text": "done"}], "end_turn")

    class FakeMessages:
        def create(self, **payload):
            payloads.append(payload)
            return reply(payload)

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), LLM_ENGINE.apply(spec, profile=spec.gateway)))


def test_a_compacted_request_sends_no_thinking_block_before_its_last_user_text(monkeypatch):
    # Removing rounds changes what a later block followed; the history-editing check of newer
    # Claude models refuses such a block when it is sent back, so the kept rounds go without them.
    payloads = []
    _install_interleaved(monkeypatch, payloads)

    assert _llm(False).chat("add things", think=True, tools=[add], compact_ratio=0.00001) == "done"

    compacted = [p for p in _loop_payloads(payloads) if any(MEMO in str(m["content"]) for m in p["messages"])]
    assert compacted
    for payload in compacted:
        messages = payload["messages"]
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user" and not _only_tool_results(m))
        earlier = [b for m in messages[:last_user] if isinstance(m["content"], list) for b in m["content"] if isinstance(b, dict)]
        assert not any(b.get("type") == "thinking" for b in earlier)
        _assert_valid_thinking_request(payload)
