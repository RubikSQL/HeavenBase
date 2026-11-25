"""Replay signed reasoning from Chat Completions through a streamed tool loop."""

from __future__ import annotations

import json
from importlib import import_module

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast

SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds"
REDACTED = "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT2Xlxh0L5L8rLVyIwxtE3rAFBa8cr3qpP"
PLAN = "Check the sum."
TOOL_CALL = {"id": "call_add", "index": 0, "type": "function", "function": {"name": "add", "arguments": '{"left":2,"right":3}'}}


def add(left: int, right: int) -> int:
    """Add two integers."""
    return left + right


def _reasoning(gateway, provider):
    if gateway == "portkey" and provider == "anthropic":
        fragments = [
            {"content_blocks": [{"index": 0, "delta": {"thinking": "Check "}}]},
            {"content_blocks": [{"index": 0, "delta": {"thinking": "the sum."}}]},
            {"content_blocks": [{"index": 0, "delta": {"signature": SIGNATURE}}]},
            {"content_blocks": [{"index": 1, "delta": {"data": REDACTED}}]},
        ]
        blocks = [
            {"type": "thinking", "thinking": PLAN, "signature": SIGNATURE},
            {"type": "redacted_thinking", "data": REDACTED},
        ]
        return fragments, "content_blocks", blocks
    first = {"index": 0, "type": "reasoning.text"}
    second = {"index": 1, "type": "reasoning.encrypted"}
    if provider == "openrouter":
        first.update(format="anthropic-claude-v1")
        second.update(format="anthropic-claude-v1")
    fragments = [
        {"reasoning": "Check ", "reasoning_details": [{**first, "text": "Check "}]},
        {"reasoning": "the sum.", "reasoning_details": [{**first, "text": "the sum."}]},
        {"reasoning_details": [{**first, "signature": SIGNATURE}]},
        {"reasoning_details": [{**second, "data": REDACTED}]},
    ]
    return fragments, "reasoning_details", [{**first, "text": PLAN, "signature": SIGNATURE}, {**second, "data": REDACTED}]


def _sse(deltas, finish):
    chunks = [
        {
            "id": "chatcmpl-add",
            "object": "chat.completion.chunk",
            "created": 100,
            "model": "claude-haiku-4-5",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }
        for delta, reason in [*((part, None) for part in deltas), ({}, finish)]
    ]
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


@pytest.mark.parametrize("gateway, provider", [("openai", "openrouter"), ("portkey", "openrouter"), ("bifrost", "anthropic"), ("portkey", "anthropic")])
def test_streamed_tool_loop_replays_signed_reasoning(monkeypatch, gateway, provider):
    httpx = pytest.importorskip("httpx")
    openai = pytest.importorskip("openai")
    fragments, key, expected = _reasoning(gateway, provider)
    calls = []

    def reply(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["messages"][-1]["role"] == "tool":
            return httpx.Response(200, text=_sse([{"content": "The sum is 5."}], "stop"))
        return httpx.Response(200, text=_sse([*fragments, {"tool_calls": [TOOL_CALL]}], "tool_calls"))

    module = import_module(f"heavenbase.llm.gateways.{gateway}.runtime")
    llm = hb.LLM(model="haiku", gateway=gateway, provider=provider, max_tokens=4096, cache=False)
    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        client = openai.OpenAI(api_key="placeholder", http_client=http, max_retries=0)
        monkeypatch.setattr(module, "_openai_client", lambda spec, route: (client, spec.engine.apply(spec)))
        assert llm.chat("Add 2 and 3.", tools=[add], think=True) == "The sum is 5."

    assistant = next(message for message in calls[1]["messages"] if message["role"] == "assistant")
    assert assistant[key] == expected
    assert assistant["tool_calls"][0]["id"] == "call_add"
    assert "thinking_blocks" not in assistant
