"""OpenAI-compatible requests leave out the Anthropic-only thinking_blocks message key.

A history that holds a Claude thinking turn keeps thinking_blocks on its assistant messages so the
Anthropic routes and LiteLLM can send them back. OpenAI-compatible APIs do not define the key, so a
request built from such a history on another provider must not carry it.
"""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm.gateways._protocols import _openai_payload
from heavenbase.llm.gateways.anthropic import runtime as anthropic_gateway

pytestmark = pytest.mark.fast

SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds"
REDACTED = "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT2Xlxh0L5L8rLVyIwxtE3rAFBa8cr3qpP"


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))


def add(left: int, right: int) -> int:
    return left + right


def _install(monkeypatch, redacted_only):
    def reply(payload):
        if any(isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"]) for m in payload["messages"]):
            content, stop = [{"type": "text", "text": "sum=5"}], "end_turn"
        else:
            think = [{"type": "redacted_thinking", "data": REDACTED}] if redacted_only else [{"type": "thinking", "thinking": "plan", "signature": SIGNATURE}]
            content, stop = [*think, {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {"left": 2, "right": 3}}], "tool_use"
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku",
            "content": content,
            "stop_reason": stop,
            "usage": {"input_tokens": 4, "output_tokens": 5},
        }

    class FakeMessages:
        def create(self, **payload):
            return reply(payload)

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), LLM_ENGINE.apply(spec, profile=spec.gateway)))


@pytest.mark.parametrize("redacted_only", [False, True])
def test_openai_compatible_requests_never_carry_thinking_blocks(monkeypatch, redacted_only):
    _install(monkeypatch, redacted_only)
    claude = hb.LLM(model="sonnet-4.6", provider="anthropic", gateway="anthropic", max_tokens=64, stream_chat=False)
    history = claude.chat("add 2 and 3", think=True, tools=[add], include="messages")
    assert any("thinking_blocks" in message for message in history)
    spec = hb.LLM(model="dsv4", provider="deepseek", gateway="openai").spec
    runtime = LLM_ENGINE.apply(spec, profile=spec.gateway)

    sent = _openai_payload(runtime, spec=spec, messages=[*history, {"role": "user", "content": "thanks"}])
    litellm = hb.LLM(model="sonnet-4.6", provider="anthropic", gateway="litellm").spec
    kept = _openai_payload(LLM_ENGINE.apply(litellm), spec=litellm, messages=history)

    assistant = [message for message in sent["messages"] if message["role"] == "assistant"]
    assert not any("thinking_blocks" in message for message in sent["messages"])
    assert assistant[0]["tool_calls"] and assistant[-1]["content"] == "sum=5"
    # LiteLLM asks to keep the key: it sends the blocks back to Anthropic itself.
    assert any("thinking_blocks" in message for message in kept["messages"])
