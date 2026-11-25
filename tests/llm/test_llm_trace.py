"""Provider-call trace seam: pairing, forwarding, canonical payloads, fail-soft."""

import datetime
import decimal
import enum
import json
import time
import types
import uuid
import warnings

import pytest

import heavenbase as hb
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm.gateways._protocols import _openai_payload
from heavenbase.llm.gateways.anthropic import runtime as anthropic_gateway
from heavenbase.llm.trace import LLMTraceEvent, canonical_request, forward_trace, request_fingerprint, trace_state

pytestmark = pytest.mark.fast


def _investigator(rounds: int = 2, fail_compaction: bool = False):
    """Return a mock provider driving ``rounds`` tool rounds, then answering."""

    def mock_chat(spec, messages, stream=False, **kwargs):
        first = str((messages[0] or {}).get("content") or "")
        if first.startswith("Compress this turn's"):
            if fail_compaction:
                raise RuntimeError("summarizer down")
            payload = {"role": "assistant", "content": "MEMO: early rounds folded"}
        else:
            done = sum(message.get("role") == "tool" for message in messages)
            if done >= rounds or "tools" not in kwargs:
                payload = {"role": "assistant", "content": "final answer"}
            else:
                call = {"id": f"c{done}", "type": "function", "function": {"name": "count", "arguments": f'{{"step":{done}}}'}}
                payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        if stream:
            delta = dict(payload)
            if "tool_calls" in payload:
                delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([{"choices": [{"delta": delta}], "usage": {"total_tokens": 1}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    def count(step: int) -> str:
        """Return a bulky marker so tiny compact ratios overflow."""
        return "R" * 800 + f"#{step}"

    return mock_chat, count


def _llm(mock_chat):
    return hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)


def test_trace_pairs_every_provider_call():
    """Each provider call emits one request before I/O and one paired response after."""
    events = []
    mock_chat, count = _investigator()
    result = _llm(mock_chat).chat("investigate", include=["text"], reduce=False, tools=[count], trace=events.append)
    assert result["text"] == "final answer"
    kinds = [(event.kind, event.seq) for event in events]
    assert kinds == [("request", 0), ("response", 0), ("request", 1), ("response", 1), ("request", 2), ("response", 2)]  # 3 rounds, all paired
    first = events[0].payload
    assert first["messages"][0]["content"] == "investigate"
    assert [tool["function"]["name"] for tool in first["tools"]] == ["count"]  # the offered tool surface rides the request event
    assert first["route"]["gateway"] == "mock" and first["route"]["preset"] == "mock"
    later = events[4].payload["messages"]
    assert any(message.get("role") == "tool" for message in later)  # round requests carry the accumulated transcript
    assert events[1].payload["message"]["tool_calls"]  # the response event carries the assistant message verbatim
    assert events[1].payload["usage"] == {"total_tokens": 1}


def test_trace_captures_anthropic_gateway_input(monkeypatch):
    """The request event records the normalized input before Anthropic conversion."""
    events = []
    wire = {}

    def create(**payload):
        assert [event.kind for event in events] == ["request"]
        wire.update(payload)
        return {"role": "assistant", "content": [{"type": "text", "text": "done"}]}

    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode: (client, spec.engine.apply(spec, profile=mode)))
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", cache=False, stream_chat=False)
    tools = [{"type": "function", "function": {"name": "count", "parameters": {"type": "object"}}}]

    assert llm.chat("q", system="Be brief.", tools=tools, tool_choice="required", trace=events.append) == "done"

    trace = events[0].payload
    user_message = {"role": "user", "content": "q"}
    assert trace["messages"] == [{"role": "system", "content": "Be brief."}, user_message]
    assert trace["options"]["tool_choice"] == "required"
    assert wire["system"] == "Be brief."
    assert wire["messages"] == [user_message]
    assert wire["tool_choice"] == {"type": "any"}


def test_trace_forwards_compaction_calls_under_shared_sequence():
    """In-turn compaction summaries trace as turn_compaction events on the outer counter."""
    events = []
    mock_chat, count = _investigator(rounds=5)
    _llm(mock_chat).chat("investigate", tools=[count], compact_ratio=0.005, trace=events.append)
    seqs = [event.seq for event in events if event.kind == "request"]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))  # one shared monotonic sequence across rounds and summaries
    compaction = [event for event in events if event.call == "turn_compaction"]
    assert {event.kind for event in compaction} == {"request", "response"}
    assert compaction[0].payload["messages"][0]["content"].startswith("Compress this turn's")  # the summary prompt itself is a traced request
    memo_rounds = [
        event
        for event in events
        if event.kind == "request"
        and event.call == "round"
        and any("[compacted working memory" in str(m.get("content") or "") for m in event.payload["messages"])
    ]
    assert memo_rounds  # the provider view after compaction is reconstructable from the round request alone


def test_trace_records_compaction_failure_and_tool_stripping():
    """A failed summary traces its error, and the wrap-up request shows the stripped tool surface."""
    events = []
    mock_chat, count = _investigator(rounds=5, fail_compaction=True)
    result = _llm(mock_chat).chat("investigate", include=["text"], reduce=False, tools=[count], compact_ratio=0.005, trace=events.append)
    assert result["text"] == "final answer"  # the fallback wraps up instead of failing
    failed = [event for event in events if event.call == "turn_compaction"]
    assert [event.kind for event in failed] == ["request", "error"]  # the summarizer's own request/error pair is on record
    assert "summarizer down" in failed[1].payload["error"]
    wrap_up = events[-2]
    assert wrap_up.kind == "request" and wrap_up.payload["tools"] == []  # tools stripped for the wrap-up call
    assert any("[context is nearly full" in str(m.get("content") or "") for m in wrap_up.payload["messages"])


def test_trace_stream_loop_matches_chat_loop():
    """The stream tool loop emits the same event sequence as the blocking loop."""
    blocking, streaming = [], []
    mock_chat, count = _investigator()
    _llm(mock_chat).chat("investigate", tools=[count], trace=blocking.append)
    mock_chat2, count2 = _investigator()
    list(_llm(mock_chat2).chat("investigate", include=["text", "delta"], reduce=False, tools=[count2], stream=True, trace=streaming.append))
    assert [(e.kind, e.call, e.seq) for e in streaming] == [(e.kind, e.call, e.seq) for e in blocking]
    assert [request_fingerprint(e.payload) for e in streaming if e.kind == "request"] == [
        request_fingerprint(e.payload) for e in blocking if e.kind == "request"
    ]  # byte-identical canonical requests


@pytest.mark.anyio
@pytest.mark.parametrize("async_stream", [False, True])
async def test_chat_stream_dispatch_accepts_overrides_without_trace(async_stream):
    class LegacyStreamLLM(hb.LLM):
        def stream(self, messages, include=None, reduce=True, system=None, images=None, tools=None, on_round=None):
            yield messages

        async def astream(self, messages, include=None, reduce=True, system=None, images=None, tools=None):
            yield messages

    llm = LegacyStreamLLM(preset="mock", cache=False)
    if async_stream:
        chunks = [chunk async for chunk in await llm.achat("hello", stream=True)]
    else:
        chunks = list(llm.chat("hello", stream=True))
    assert chunks == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("async_chat", [False, True])
async def test_untraced_compaction_accepts_legacy_chat_overrides(async_chat):
    class LegacyChatLLM(hb.LLM):
        def chat(self, messages, *, cache):
            assert cache is False
            return "measured evidence"

        async def achat(self, messages, *, cache):
            assert cache is False
            return "measured evidence"

    llm = LegacyChatLLM(preset="mock", cache=False)
    rounds = [[{"role": "tool", "content": str(index)}] for index in range(3)]
    if async_chat:
        compacted, wrap_up = await llm._acompact_turn_rounds([], rounds, 0)
    else:
        compacted, wrap_up = llm._compact_turn_rounds([], rounds, 0)
    assert not wrap_up
    assert compacted[1:] == rounds[1:]
    assert "measured evidence" in compacted[0][0]["content"]


@pytest.mark.anyio
async def test_async_tool_stream_accepts_legacy_chat_override_without_trace():
    class LegacyChatLLM(hb.LLM):
        async def achat(self, messages, include=None, reduce=True, stream=False, system=None, images=None, tools=None):
            return messages

    def count() -> int:
        """Count the supplied items."""
        return 1

    llm = LegacyChatLLM(preset="mock", cache=False)
    assert [chunk async for chunk in llm.astream("hello", tools=[count])] == ["hello"]


def test_trace_error_event_on_provider_failure():
    """A raising provider emits the paired error event, then the exception propagates."""
    events = []

    def broken(spec, messages, stream=False, **kwargs):
        raise RuntimeError("provider down")

    with pytest.raises(Exception, match="provider down"):
        _llm(broken).chat("q", trace=events.append)
    assert [event.kind for event in events] == ["request", "error"]  # the request is on record before the failure
    assert "provider down" in events[1].payload["error"]


def test_trace_callback_failure_never_fails_the_call():
    """An observer bug warns once and the traced call still completes."""

    def boom(event):
        raise RuntimeError("observer bug")

    mock_chat, count = _investigator()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _llm(mock_chat).chat("investigate", include=["text"], reduce=False, tools=[count], trace=boom)
    assert result["text"] == "final answer"  # tracing is an observer, never a failure source
    assert any("trace callback failed" in str(item.message) for item in caught)


@pytest.mark.slow
def test_trace_cache_hit_emits_nothing():
    """A cache-served reply reaches no provider, so it traces no events."""
    calls = {"n": 0}

    def once(spec, messages, stream=False, **kwargs):
        calls["n"] += 1
        payload = {"role": "assistant", "content": "cached answer"}
        if stream:
            return iter([{"choices": [{"delta": dict(payload)}], "usage": {"total_tokens": 1}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    llm = hb.LLM(preset="mock", mock_chat=once, mock_pause=(0, 0), cache=True)
    first, second = [], []
    assert llm.chat("same question", trace=first.append) == "cached answer"
    assert llm.chat("same question", trace=second.append) == "cached answer"
    assert calls["n"] == 1 and len(first) == 2 and second == []  # no provider saw the second call, so nothing is traced


def test_request_fingerprint_is_order_insensitive():
    """Fingerprints depend on canonical content, not dict insertion order."""
    a = {"messages": [{"role": "user", "content": "q"}], "tools": [], "options": {"x": 1, "y": 2}, "route": {"gateway": "mock"}}
    b = {"route": {"gateway": "mock"}, "options": {"y": 2, "x": 1}, "tools": [], "messages": [{"role": "user", "content": "q"}]}
    assert request_fingerprint(a) == request_fingerprint(b)
    assert request_fingerprint(a) != request_fingerprint({**a, "options": {"x": 1, "y": 3}})  # content changes change the fingerprint


def test_request_fingerprint_converts_object_keys_before_sorting():
    """Mixed keys hash identically to the round-tripped JSON object."""
    messages = [{"role": "user", "content": "q"}]
    spec = LLM_ENGINE.resolve({"preset": "mock"})
    mixed = {1: 5, "2": 3, None: 0}
    payload = canonical_request(messages, {"extra_body": {"items": [mixed]}}, spec)
    reordered = canonical_request(messages, {"extra_body": {"items": [dict(reversed(list(mixed.items())))]}}, spec)
    restored = json.loads(json.dumps(payload))

    assert request_fingerprint(payload) == request_fingerprint(restored) == request_fingerprint(reordered)
    restored["options"]["extra_body"]["items"][0]["1"] = 6
    assert request_fingerprint(payload) != request_fingerprint(restored)
    assert request_fingerprint({"options": {1: 5, "1": 6}}) == request_fingerprint({"options": {"1": 6}})


def test_request_fingerprint_escapes_lone_surrogates():
    """Escaped surrogate code points are accepted and distinct from literal escape text."""
    payload = {"messages": [{"role": "user", "content": "\ud800"}]}
    restored = json.loads(json.dumps(payload))
    literal = {"messages": [{"role": "user", "content": r"\ud800"}]}

    assert request_fingerprint(payload) == request_fingerprint(restored)
    assert request_fingerprint(payload) != request_fingerprint(literal)


def test_trace_fingerprint_preserves_semantic_options_used_by_gateway_compiler():
    """Canonical traces retain safe controls that compile into distinct provider payloads."""
    base = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    enabled = base.with_args({"think": True, "reasoning_max_tokens": 128, "reasoning_exclude": True})
    disabled = base.with_args({"think": False})
    messages = [{"role": "user", "content": "q"}]

    enabled_trace = canonical_request(messages, enabled.args, enabled, streamable=True)
    disabled_trace = canonical_request(messages, disabled.args, disabled, streamable=True)
    enabled_wire = _openai_payload(LLM_ENGINE.apply(enabled, profile=enabled.gateway), spec=enabled, messages=messages, stream=True)
    disabled_wire = _openai_payload(LLM_ENGINE.apply(disabled, profile=disabled.gateway), spec=disabled, messages=messages, stream=True)

    keys = ("think", "reasoning_max_tokens", "reasoning_exclude", "stream")
    assert {key: enabled_trace["options"][key] for key in keys} == {
        "think": True,
        "reasoning_max_tokens": 128,
        "reasoning_exclude": True,
        "stream": True,
    }
    assert enabled_wire["extra_body"]["reasoning"] == {"enabled": True, "max_tokens": 128, "exclude": True}
    assert disabled_wire["extra_body"]["reasoning"] == {"enabled": False}
    assert request_fingerprint(enabled_trace) != request_fingerprint(disabled_trace)


def test_forward_trace_renumbers_on_the_outer_counter():
    """Forwarded sub-call events share the outer sequence and the given call tag."""
    outer = []
    state = trace_state(outer.append)
    state["seq"] = 5
    sub = forward_trace(state, "replay_compaction")
    sub(LLMTraceEvent(kind="request", call="round", seq=0, payload={"messages": []}))
    sub(LLMTraceEvent(kind="response", call="round", seq=0, payload={}))
    assert [(event.kind, event.call, event.seq) for event in outer] == [("request", "replay_compaction", 5), ("response", "replay_compaction", 5)]
    assert state["seq"] == 6  # the outer counter advanced exactly once


@pytest.mark.anyio
async def test_trace_async_loop_matches_sync():
    """achat traces the same paired sequence as chat."""
    events = []
    mock_chat, count = _investigator()
    result = await _llm(mock_chat).achat("investigate", include=["text"], reduce=False, tools=[count], trace=events.append)
    assert result["text"] == "final answer"
    assert [(event.kind, event.seq) for event in events] == [("request", 0), ("response", 0), ("request", 1), ("response", 1), ("request", 2), ("response", 2)]


def test_trace_options_exclude_credentials_and_wiring():
    """Client construction and credential channels never reach a trace payload."""
    events = []
    mock_chat, count = _investigator()
    llm = hb.LLM(
        preset="mock",
        mock_chat=mock_chat,
        mock_pause=(0, 0),
        cache=False,
        api_key="sk-secret",
        auth_token="tok-secret",
        base_url="https://user:pw@gateway.example",
        extra_headers={"authorization": "Bearer zzz"},
    )
    llm.chat("investigate", include=["text"], reduce=False, tools=[count], trace=events.append)
    options = events[0].payload["options"]
    forbidden = {"api_key", "auth_token", "base_url", "extra_headers", "headers", "http_proxy", "https_proxy", "mock_chat", "mock_pause", "mock"}
    assert not forbidden & set(options)  # the log can never hold credentials or client wiring
    assert "sk-secret" not in str(events[0].payload)  # nor anywhere else in the payload
    assert options.get("stream") in (True, False)  # the effective transport mode is part of the canonical request


def test_trace_options_redact_secret_looking_values_at_any_depth():
    """Keys that gateways accept inside extra_query/extra_body never reach a trace payload."""
    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    schema = {"type": "object", "properties": {"api_key": {"type": "string"}, "token_count": {"type": "integer"}}}
    cfg = {
        "temperature": 0.2,
        "max_tokens": 512,
        "user": "alice",
        "access_token": "zz9-top",
        "Authorization": "Bearer zz9-header",
        "extra_query": {"api-version": "2024-06-01", "api-key": "zz9-query", "sig": "plain"},
        "extra_body": {
            "provider": {"order": ["a", "b"]},
            "auth": {"type": "bearer", "value": "zz9-nested"},
            "client_secret": "zz9-client",
            "keys": ["zz9-list-a", "zz9-list-b"],
            "thinking": {"budget_tokens": 1024},
            "callback_url": "https://bot:zz9-pw@hook.example/cb?token=zz9-url&mode=1",
            "docs_url": "https://docs.example/page?mode=1",
        },
        "response_format": {"type": "json_schema", "json_schema": {"name": "creds", "schema": schema}},
    }
    options = canonical_request([{"role": "user", "content": "q"}], cfg, spec)["options"]

    assert "zz9" not in json.dumps(options)  # no credential-looking value survives, at any depth
    assert options["access_token"] == options["Authorization"] == "***"
    assert options["extra_query"] == {"api-version": "2024-06-01", "api-key": "***", "sig": "plain"}
    assert options["extra_body"]["auth"] == options["extra_body"]["client_secret"] == options["extra_body"]["keys"] == "***"
    assert options["extra_body"]["callback_url"].startswith("https://***:***@hook.example/cb?") and "mode=1" in options["extra_body"]["callback_url"]
    # Request-shaping values stay exact: numbers under token-ish names, ordinary text and URLs, and model-visible schemas.
    assert options["temperature"] == 0.2 and options["max_tokens"] == 512 and options["user"] == "alice"
    assert options["extra_body"]["thinking"] == {"budget_tokens": 1024} and options["extra_body"]["provider"] == {"order": ["a", "b"]}
    assert options["extra_body"]["docs_url"] == "https://docs.example/page?mode=1"
    assert options["response_format"]["json_schema"]["schema"] == schema


def test_trace_fingerprint_distinguishes_requests_that_differ_in_non_secret_options():
    """Non-secret values under credential-looking names are recorded as sent, so their requests stay distinct."""
    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    messages = [{"role": "user", "content": "q"}]

    def schema(kind):
        return {
            "type": "object",
            "properties": {"token_count": {"type": kind}, "api_key": {"type": "string"}, "keywords": {"type": "array", "items": {"type": kind}}},
            "$defs": {"AuthToken": {"type": kind}},
            "required": ["token_count"],
        }

    def anthropic(kind):
        return {"format": {"type": "json_schema", "schema": schema(kind)}}

    pairs = [
        ({"prompt_cache_key": "tenant-A"}, {"prompt_cache_key": "tenant-B"}),
        ({"extra_body": {"stop_token_ids": [128001]}}, {"extra_body": {"stop_token_ids": (2, 3)}}),
        ({"extra_body": {"guided_json": schema("integer")}}, {"extra_body": {"guided_json": schema("string")}}),
        ({"extra_body": {"structured_outputs": {"json": schema("integer")}}}, {"extra_body": {"structured_outputs": {"json": schema("string")}}}),
        ({"output_config": anthropic("integer")}, {"output_config": anthropic("string")}),
        ({"extra_body": {"output_config": anthropic("integer")}}, {"extra_body": {"output_config": anthropic("string")}}),
        ({"extra_body": {"response_schema": schema("integer")}}, {"extra_body": {"response_schema": schema("string")}}),
        ({"text": {"format": {"type": "json_schema", "schema": schema("integer")}}}, {"text": {"format": {"type": "json_schema", "schema": schema("string")}}}),
        # Property names also key dependentRequired and draft-07 dependencies; OpenAPI example is a schema keyword.
        (
            {"extra_body": {"guided_json": {"type": "object", "dependentRequired": {"api_key": ["a"]}}}},
            {"extra_body": {"guided_json": {"type": "object", "dependentRequired": {"api_key": ["b"]}}}},
        ),
        ({"extra_body": {"guided_json": {"dependencies": {"api_key": ["a"]}}}}, {"extra_body": {"guided_json": {"dependencies": {"api_key": ["b"]}}}}),
        (
            {"extra_body": {"guided_json": {"dependencies": {"api_key": {"required": ["a"]}}}}},
            {"extra_body": {"guided_json": {"dependencies": {"api_key": {"required": ["b"]}}}}},
        ),
        (
            {"extra_body": {"guided_json": {"properties": {"api_key": {"example": "a"}}}}},
            {"extra_body": {"guided_json": {"properties": {"api_key": {"example": "b"}}}}},
        ),
    ]
    for first, second in pairs:
        a, b = canonical_request(messages, first, spec), canonical_request(messages, second, spec)
        assert a["options"] == json.loads(json.dumps(first)) and b["options"] == json.loads(json.dumps(second))  # recorded as sent
        assert request_fingerprint(a) != request_fingerprint(b)


def test_trace_options_replace_every_credential_capable_value_under_secret_names():
    """Under a credential-looking name only nulls, booleans, numbers and flat containers of them survive."""
    import array

    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    cfg = {
        "extra_body": {
            "api_keys": {"zz9-key-as-mapping-key": "primary"},
            "tokens": {("zz9-tuple-key",): 1},
            "auth": {"Bearer zz9-auth": {"scope": 1}},
            "secret_key": array.array("b", [1, 2, 3]),
            "secret_bytes": bytearray(b"zz9"),
            "rotation_keys": ("zz9-b", 3),
            "token_set": {b"zz9-bytes", 7},
            "session_token": _Opaque(),
            "stop_token_ids": [1, 2.5, None, True],
            "allowed_token_ids": {3, 1},
            "max_tokens": 64,
            "budget_token_hint": decimal.Decimal("0.5"),
            "metadata": {"properties": {"api_key": {"value": "zz9-not-a-schema"}}, "json_schema": {"api_key": "zz9-nested"}},
            "guided_json": {"properties": {"api_key": {"type": "string", "default": "https://bot:zz9-pw@hook.example/cb"}}},
        },
        "extra_query": {"response_format": "https://user:zz9-pw@h.example/x?token=zz9-q"},
    }
    options = canonical_request([{"role": "user", "content": "q"}], cfg, spec)["options"]

    assert "zz9" not in json.dumps(options)
    body = options["extra_body"]
    for name in ("api_keys", "tokens", "auth", "secret_key", "secret_bytes", "rotation_keys", "token_set", "session_token"):
        assert body[name] == "***", name
    assert body["stop_token_ids"] == [1, 2.5, None, True] and body["allowed_token_ids"] == [1, 3] and body["max_tokens"] == 64
    assert body["budget_token_hint"] == "0.5"
    # A credential container under a "properties" key is no schema; schema names are exempt only at the top level.
    assert body["metadata"] == {"properties": {"api_key": "***"}, "json_schema": {"api_key": "***"}}
    # A real sub-schema keeps its property name; its strings still lose URL user info.
    assert body["guided_json"]["properties"]["api_key"] == {"type": "string", "default": "https://***:***@hook.example/cb"}
    assert options["extra_query"]["response_format"].startswith("https://***:***@h.example/x?token=")

    # A mapping with any non-schema key is no schema, so its credential-named property keeps the credential rule.
    containers = {
        "extra_body": {"properties": {"api_key": {"type": "bearer", "value": "zz9-SECRET"}}},
        "vendor": {"properties": {"auth": {"title": "x", "bearer": "zz9-B"}}},
        "hook": {"dependentRequired": {"api_key": ["zz9-list"]}, "endpoint": "x"},
    }
    options = canonical_request([{"role": "user", "content": "q"}], containers, spec)["options"]
    assert "zz9" not in json.dumps(options)
    assert options == {
        "extra_body": {"properties": {"api_key": "***"}},
        "vendor": {"properties": {"auth": "***"}},
        "hook": {"dependentRequired": {"api_key": "***"}, "endpoint": "x"},
    }


def test_trace_fingerprint_follows_prompt_cache_key_on_live_chat_calls():
    """The live per-call path records the prompt-cache key the provider receives."""
    seen = []

    def mock_chat(spec, messages, stream=False, **kwargs):
        seen.append(kwargs.get("prompt_cache_key"))
        payload = {"role": "assistant", "content": "ok"}
        if stream:
            return iter([{"choices": [{"delta": payload}], "usage": {"total_tokens": 1}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    fingerprints = []
    for tenant in ("tenant-A", "tenant-B"):
        events = []
        llm.chat("hello", include=["text"], trace=events.append, prompt_cache_key=tenant)
        (request,) = [event for event in events if event.kind == "request"]
        assert request.payload["options"]["prompt_cache_key"] == tenant
        fingerprints.append(request_fingerprint(request.payload))
    assert seen == ["tenant-A", "tenant-B"] and fingerprints[0] != fingerprints[1]


def test_trace_options_redaction_applies_to_live_chat_calls():
    """A client constructed with secret-bearing extra options still traces without them."""
    events = []
    mock_chat, count = _investigator()
    llm = hb.LLM(
        preset="mock",
        mock_chat=mock_chat,
        mock_pause=(0, 0),
        cache=False,
        extra_query={"api-key": "zz9-query"},
        extra_body={"secret_token": "zz9-body", "temperature_hint": 1},
    )
    llm.chat("investigate", include=["text"], reduce=False, tools=[count], trace=events.append)
    assert events and "zz9" not in json.dumps([event.payload for event in events], default=str)


class _Mode(enum.Enum):
    FAST = "fast"


class _Opaque:
    """A non-JSON object whose text embeds the credential it carries."""

    def __str__(self):
        return "Plugin(api_key='zz9-opaque')"


class _Unprintable:
    def __str__(self):
        raise RuntimeError("no text form")


def test_trace_options_redact_inside_tuples_and_beside_non_json_leaves():
    """Redaction reaches every container type and the str() text of non-JSON leaves."""
    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    stamp = datetime.datetime(2026, 1, 1)
    cfg = {
        "extra_body": {
            "plugins": ({"id": "web", "api_key": "zz9-tuple"},),
            "api_key": "zz9-beside-datetime",
            "ts": stamp,
            "mode": _Mode.FAST,
            "trace_uuid": uuid.UUID(int=1),
            "tags": frozenset({"b", "a"}),
            "plugin": _Opaque(),
            "odd": _Unprintable(),
        },
        "extra_query": {"token": "zz9-beside-decimal", "w": decimal.Decimal("0.5")},
        "stop": ("END", "https://bot:zz9-pw@hook.example/cb"),
        "metadata": types.MappingProxyType({"client_secret": "zz9-proxy", "n": 1}),
    }
    options = canonical_request([{"role": "user", "content": "q"}], cfg, spec)["options"]

    assert "zz9" not in json.dumps(options)  # nothing secret survives, whatever the container or leaf type
    body = options["extra_body"]
    assert body["plugins"] == [{"id": "web", "api_key": "***"}]  # tuples are walked and stored as JSON arrays
    assert body["api_key"] == "***" and body["ts"] == str(stamp) and body["mode"] == str(_Mode.FAST)  # one odd leaf no longer swallows its siblings
    assert body["trace_uuid"] == str(uuid.UUID(int=1)) and body["tags"] == ["a", "b"]
    assert body["plugin"] == "***" and body["odd"] == "<_Unprintable>"
    assert options["extra_query"] == {"token": "***", "w": "0.5"}
    assert options["stop"] == ["END", "https://***:***@hook.example/cb"]
    assert options["metadata"] == {"client_secret": "***", "n": 1}


class _Endpoint:
    """A non-JSON leaf whose repr embeds a credential-bearing URL."""

    def __init__(self, url):
        self.url = url

    def __str__(self):
        return f"Endpoint(url={self.url!r})"


def test_trace_options_redact_urls_embedded_in_text():
    """A URL inside a repr, a bytes leaf or prose loses its user info like a bare URL."""
    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    cfg = {
        "extra_body": {
            "hook": _Endpoint("https://bot:zz9pw@hook.example/cb"),
            "raw": b"https://bot:zz9bytes@h.example/x",
            "note": "call back at https://bot:zz9prose@h.example/x?token=zz9tok&page=2 later",
            "store": "redis://:zz9redis@cache.example:6379/0",
            "plain": "https://bot:zz9plain@hook.example/cb",
            "doc": "see https://docs.example/page",
        },
    }
    options = canonical_request([{"role": "user", "content": "q"}], cfg, spec)["options"]

    assert "zz9" not in json.dumps(options)
    body = options["extra_body"]
    assert body["hook"] == "Endpoint(url='https://***:***@hook.example/cb')"
    assert body["raw"] == "b'https://***:***@h.example/x'"
    assert body["note"] == "call back at https://***:***@h.example/x?token=%2A%2A%2A&page=2 later"
    assert body["store"] == "redis://***:***@cache.example:6379/0"
    assert body["plain"] == "https://***:***@hook.example/cb"
    assert body["doc"] == "see https://docs.example/page"  # a URL without secrets is kept byte for byte


def test_trace_options_redact_joined_and_nested_urls_each_on_their_own():
    """URLs joined by punctuation or nested in another URL's path or query each lose their user info."""
    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    cfg = {
        "extra_body": {
            "comma": "https://a:zz9c1@h1.example,https://b:zz9c2@h2.example/x",
            "semicolon": "primary=https://a:zz9s1@h1.example;replica=https://b:zz9s2@h2.example",
            "pipe": "https://a:zz9p1@h1.example|https://b:zz9p2@h2.example",
            "redirect": "https://login.example/start?next=https://bot:zz9n1@h.example/cb",
            "redirect_query": "https://login.example/start?token=zz9q&next=https://bot:zz9n2@h.example/cb",
            "path": "https://proxy.example/fetch/https://b:zz9n3@h2.example/x",
            "digit_glued": "1https://a:zz9d@h.example",
            "ipv6": "https://u:zz9v6@[::1]:8080/x",
            "case": "https://u:zz9case@Host.Example/x",
        },
    }
    options = canonical_request([{"role": "user", "content": "q"}], cfg, spec)["options"]

    assert "zz9" not in json.dumps(options)
    body = options["extra_body"]
    assert body["comma"] == "https://***:***@h1.example,https://***:***@h2.example/x"
    assert body["semicolon"] == "primary=https://***:***@h1.example;replica=https://***:***@h2.example"
    assert body["pipe"] == "https://***:***@h1.example|https://***:***@h2.example"
    assert body["redirect"] == "https://login.example/start?next=https://***:***@h.example/cb"
    assert body["redirect_query"] == "https://login.example/start?token=%2A%2A%2A&next=https://***:***@h.example/cb"
    assert body["path"] == "https://proxy.example/fetch/https://***:***@h2.example/x"
    assert body["digit_glued"] == "1https://***:***@h.example"
    assert body["ipv6"] == "https://***:***@[::1]:8080/x"  # the host is kept exactly as written
    assert body["case"] == "https://***:***@Host.Example/x"


def test_trace_options_redact_a_password_holding_an_unescaped_slash_query_or_fragment_mark():
    """A ``/``, ``?`` or ``#`` inside a password ends the parsed authority early; the user info still runs to its ``@``."""
    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    cfg = {
        "extra_body": {
            "slash": "postgres://user:zz9pa/ss@db.example/app",
            "query": "dsn=mysql://user:zz9pa?ss@db.example:3306/app?ssl=1",
            "fragment": "https://user:zz9pa#ss@h.example/x",
            "slash_and_at": "postgres://user:zz9a/b@zz9c@db.example/app",
            "slash_then_path_at": "postgres://user:zz9pa/ss@db.example/app/@x",
            "port_then_at": "https://h.example:8080/@name",
            "plain_port": "https://h.example:8080/x",
            "ipv6": "http://[::1]:8080/@name",
            "port_then_query_at": "https://h.example:8080/p?email=a@b.example",
            "slash_in_user": "postgres://us/er:zz9pass@db.example/app",
            "at_then_slash": "postgres://user:p@ss/word@db.example/app",
            "registry": "https://user:zz9tok@registry.example/@scope/pkg",
            "user_info_then_path_at": "https://user:zz9pw@host.example/p/x@y?q=1",
            "ipv6_user_info_then_path_at": "https://user:zz9pw@[::1]:5432/db@x",
            "at_in_user": "postgres://us@er/zz9x:zz9pass@db.example/app",
            "path_at_no_colon": "https://h.example/@name",
            "query_at_no_colon": "https://h.example/p?email=a@b.example",
        },
    }
    options = canonical_request([{"role": "user", "content": "q"}], cfg, spec)["options"]

    assert "zz9" not in json.dumps(options)
    # the documented limit: an "@" in the query or fragment after a host:port also takes the host
    assert options["extra_body"]["port_then_query_at"] == "https://***:***@b.example"
    # user info holding "/" before its ":", or "@" before a "/", runs to the last "@" before the query
    assert options["extra_body"]["slash_in_user"] == "postgres://***:***@db.example/app"
    # well-formed user info keeps its host when an "@" follows in an ordinary path ...
    assert options["extra_body"]["registry"] == "https://***:***@registry.example/@scope/pkg"
    assert options["extra_body"]["user_info_then_path_at"] == "https://***:***@host.example/p/x@y?q=1"
    assert options["extra_body"]["ipv6_user_info_then_path_at"] == "https://***:***@[::1]:5432/db@x"
    # ... so the documented limit: a password holding "@" and then "/" with no ":" after it is found up to its first "@"
    assert options["extra_body"]["at_then_slash"] == "postgres://***:***@ss/word@db.example/app"
    assert options["extra_body"]["at_in_user"] == "postgres://***:***@db.example/app"
    # an "@" with no ":" before it is a path or query value, not user info
    assert options["extra_body"]["path_at_no_colon"] == "https://h.example/@name"
    assert options["extra_body"]["query_at_no_colon"] == "https://h.example/p?email=a@b.example"
    body = options["extra_body"]
    assert body["slash"] == "postgres://***:***@db.example/app"
    assert body["query"] == "dsn=mysql://***:***@db.example:3306/app?ssl=1"
    assert body["fragment"] == "https://***:***@h.example/x"
    assert body["slash_and_at"] == "postgres://***:***@db.example/app"  # the user info runs to the last "@"
    assert body["slash_then_path_at"] == "postgres://***:***@x"  # an "@" in the path after such user info is redacted through
    assert body["port_then_at"] == "https://***:***@name"  # cannot be told from a password holding "/", so it is redacted
    assert body["plain_port"] == "https://h.example:8080/x"
    assert body["ipv6"] == "http://[::1]:8080/@name"


def test_trace_url_scan_is_linear_in_the_option_length():
    """A long scheme-character run beside a URL does not make redaction quadratic."""
    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    text = "a" * 400_000 + " https://u:zz9@docs.example/x " + "b.c-" * 100_000 + "://" + "d" * 100_000
    started = time.perf_counter()
    options = canonical_request([{"role": "user", "content": "q"}], {"extra_body": {"text": text}}, spec)["options"]
    assert time.perf_counter() - started < 5  # the quadratic scan took minutes on inputs this size
    assert "zz9" not in options["extra_body"]["text"]


def test_trace_options_redaction_covers_per_call_tuples_and_non_json_leaves():
    """The live per-call path stores the same redacted form the canonical serializer returns."""
    mock_chat, count = _investigator()
    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    for extra_body in ({"plugins": ({"id": "web", "api_key": "zz9-live-tuple"},)}, {"api_key": "zz9-live-dt", "ts": datetime.datetime(2026, 1, 1)}):
        events = []
        llm.chat("investigate", include=["text"], reduce=False, tools=[count], trace=events.append, extra_body=extra_body)
        assert events and "zz9" not in json.dumps([event.payload for event in events], default=str)


def test_trace_options_survive_self_referencing_values():
    """A cyclic option is cut at the repeat instead of failing the traced call."""
    spec = LLM_ENGINE.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai"})
    loop: dict = {"api_key": "zz9-loop"}
    loop["self"] = loop
    options = canonical_request([{"role": "user", "content": "q"}], {"extra_body": loop}, spec)["options"]
    assert options["extra_body"] == {"api_key": "***", "self": "<cycle>"}


def test_trace_types_are_exported_from_the_llm_package():
    import heavenbase.llm as llm_package
    import heavenbase.llm.trace as trace_module

    assert llm_package.LLMTraceEvent is trace_module.LLMTraceEvent
    assert llm_package.TraceCallback is trace_module.TraceCallback
    assert {"LLMTraceEvent", "TraceCallback"} <= set(llm_package.__all__)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["chat", "achat", "stream", "astream"])
async def test_untraced_tool_rounds_accept_legacy_compaction_hook(mode):
    calls = []

    class LegacyCompactionLLM(hb.LLM):
        def _compact_turn_rounds(self, prepared, rounds, cap_tokens, usage_base=None):
            calls.append("sync")
            return rounds, False

        async def _acompact_turn_rounds(self, prepared, rounds, cap_tokens, usage_base=None):
            calls.append("async")
            return rounds, False

    mock_chat, count = _investigator()
    llm = LegacyCompactionLLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False)
    kwargs = {"tools": [count], "compact_ratio": 0.005}
    if mode == "chat":
        result = llm.chat("investigate", **kwargs)
    elif mode == "achat":
        result = await llm.achat("investigate", **kwargs)
    elif mode == "stream":
        result = "".join(llm.stream("investigate", **kwargs))
    else:
        result = "".join([chunk async for chunk in llm.astream("investigate", **kwargs)])
    assert result == "final answer"
    assert calls == ["sync" if mode in {"chat", "stream"} else "async"] * 2
