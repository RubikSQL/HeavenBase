"""The HTTP request body a Claude thinking call sends on every route, checked at a local fake endpoint.

The calls go through the real Anthropic SDK (gateway="anthropic", Portkey and Bifrost Anthropic
Messages routes) and real LiteLLM (gateway="litellm") to a local HTTP server that records each body
and answers with a documented Messages API response. With thinking on, the Messages API accepts no
temperature (other than its default) and no top_k, takes the effort level in output_config, and
defines no top-level effort, reasoning or extra_body field.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm.gateways._protocols import _anthropic_payload

pytestmark = pytest.mark.fast

BODIES: list = []
EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
    {"type": "message_stop"},
]
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 4, "output_tokens": 5},
}
# Fields the Messages API defines; anything else in a request body is an error there.
MESSAGES_FIELDS = set(
    "model messages max_tokens stream system metadata stop_sequences temperature top_p top_k thinking tools tool_choice "
    "output_config service_tier container inference_geo cache_control".split()
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        BODIES.append(body)
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in EVENTS:
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        else:
            data = json.dumps(MESSAGE).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))
    BODIES.clear()


def _llm(server, gateway, *, model="haiku", preset=None, provider="anthropic", max_tokens=4096, stream_chat=True):
    if gateway == "litellm":
        pytest.importorskip("litellm")
    options = {"preset": preset} if preset else {"model": model}
    if gateway in ("portkey", "bifrost"):
        options["endpoint"] = "anthropic"  # their Anthropic Messages route
    return hb.LLM(**options, provider=provider, gateway=gateway, base_url=server, api_key="sk-placeholder", max_tokens=max_tokens, stream_chat=stream_chat)


def _body(server, gateway, *, model="haiku", preset=None, provider="anthropic", max_tokens=4096, stream_chat=True, **kwargs):
    llm = _llm(server, gateway, model=model, preset=preset, provider=provider, max_tokens=max_tokens, stream_chat=stream_chat)
    assert llm.chat("hi", **kwargs) == "ok"
    assert len(BODIES) == 1
    return BODIES[0]


def _assert_thinking_request(body, thinking):
    assert body["thinking"] == thinking
    assert "temperature" not in body and "top_k" not in body
    assert set(body) <= MESSAGES_FIELDS, sorted(set(body) - MESSAGES_FIELDS)


ADAPTIVE = {"type": "adaptive", "display": "summarized"}
# Claude before 4.6 (haiku 4.5) has no adaptive thinking: think=True takes the smallest manual budget.
MANUAL = {"type": "enabled", "budget_tokens": 1024, "display": "summarized"}


@pytest.mark.slow
@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost", "litellm"])
@pytest.mark.parametrize("model", ["haiku", "sonnet-4.6", "opus"])
@pytest.mark.parametrize("stream_chat", [True, False])
def test_think_turns_claude_thinking_on_without_temperature(server, gateway, model, stream_chat):
    body = _body(server, gateway, model=model, stream_chat=stream_chat, think=True)

    _assert_thinking_request(body, MANUAL if model == "haiku" else ADAPTIVE)


@pytest.mark.parametrize("gateway", ["anthropic", "litellm"])
def test_think_on_a_claude_model_without_adaptive_thinking_needs_room_for_a_budget(server, gateway):
    llm = _llm(server, gateway, model="haiku", max_tokens=1024)

    with pytest.raises(ValueError, match="above 1024"):
        llm.chat("hi", think=True)
    assert BODIES == []


def test_litellm_budget_error_names_what_the_route_accepts(server):
    llm = _llm(server, "litellm", model="opus")

    with pytest.raises(ValueError, match="without reasoning_max_tokens") as raised:
        llm.chat("hi", reasoning_max_tokens=2048)
    assert "reasoning_effort" not in str(raised.value)
    assert BODIES == []


@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost", "litellm"])
def test_manual_budget_turns_claude_thinking_on(server, gateway):
    body = _body(server, gateway, model="sonnet-4.6", reasoning_max_tokens=2048)

    _assert_thinking_request(body, {"type": "enabled", "budget_tokens": 2048, "display": "summarized"})


@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost"])
def test_reasoning_effort_goes_in_output_config(server, gateway):
    body = _body(server, gateway, model="opus", reasoning_effort="low")

    _assert_thinking_request(body, ADAPTIVE)
    assert body["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("preset", ["coder", "coder-pro"])
def test_coder_presets_send_a_valid_thinking_request(server, preset):
    body = _body(server, "anthropic", preset=preset)

    _assert_thinking_request(body, ADAPTIVE)


def test_explicit_native_thinking_drops_the_default_temperature(server):
    body = _body(server, "anthropic", model="haiku", thinking={"type": "enabled", "budget_tokens": 1024})

    _assert_thinking_request(body, {"type": "enabled", "budget_tokens": 1024})


@pytest.mark.parametrize("gateway", ["anthropic", "litellm"])
def test_requests_without_thinking_keep_the_temperature(server, gateway):
    body = _body(server, gateway, model="haiku")

    assert body["temperature"] == 0
    assert "thinking" not in body


def test_litellm_refuses_an_effort_level_it_cannot_forward(server):
    pytest.importorskip("litellm")
    llm = hb.LLM(model="opus", provider="anthropic", gateway="litellm", base_url=server, api_key="sk-ant-placeholder", max_tokens=4096)

    with pytest.raises(ValueError, match="reasoning_effort"):
        llm.chat("hi", reasoning_effort="low")
    assert BODIES == []


@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost"])
def test_extra_body_fields_reach_the_request_body(server, gateway):
    # A field messages.create() has no keyword for travels in the SDK's extra_body.
    body = _body(server, gateway, model="haiku", extra_body={"anthropic_beta_flag": True, "output_config": {"effort": "low"}})

    assert body["anthropic_beta_flag"] is True
    assert body["output_config"] == {"effort": "low"}


def test_extra_body_output_config_merges_with_the_effort(server):
    schema = {"type": "json_schema", "schema": {"type": "object", "properties": {"answer": {"type": "string"}}}}

    body = _body(server, "anthropic", model="opus", reasoning_effort="high", extra_body={"output_config": {"format": schema}})

    assert body["output_config"] == {"effort": "high", "format": schema}


def test_extra_body_output_config_with_another_effort_is_refused(server):
    llm = _llm(server, "anthropic", model="opus")

    with pytest.raises(ValueError, match="output_config.effort"):
        llm.chat("hi", reasoning_effort="high", extra_body={"output_config": {"effort": "low"}})
    assert BODIES == []


# Non-Claude models on the Messages routes keep their 0.1.2.2 request: zai's configured thinking
# setting (thinking.type on Portkey and Bifrost, the native setting on gateway="anthropic") and the
# temperature, default or given.
ZAI_THINKING = {"anthropic": ADAPTIVE, "portkey": {"type": "enabled"}, "bifrost": {"type": "enabled"}}


@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost"])
@pytest.mark.parametrize("temperature", [None, 0.6])
def test_zai_on_the_messages_routes_keeps_its_request(server, gateway, temperature):
    kwargs = {} if temperature is None else {"temperature": temperature}

    # Unstreamed, so the same test also runs on 0.1.2.2, whose stream parser fails on this response's usage.
    body = _body(server, gateway, model="glm-5.2", provider="zai", stream_chat=False, think=True, **kwargs)

    assert body["thinking"] == ZAI_THINKING[gateway]
    assert body["temperature"] == (0 if temperature is None else temperature)


@pytest.mark.parametrize("gateway", ["anthropic", "litellm"])
@pytest.mark.parametrize("think", [None, False])
def test_opus_requests_without_thinking_carry_no_temperature(server, gateway, think):
    # Claude Opus 5, like Sonnet 5 and Fable 5, refuses sampling parameters; its entry sets none.
    kwargs = {} if think is None else {"think": think}

    body = _body(server, gateway, model="opus", **kwargs)

    assert "temperature" not in body and "thinking" not in body


@pytest.mark.parametrize("gateway", ["anthropic", "portkey", "bifrost"])
def test_extra_body_sampling_is_dropped_with_claude_thinking(gateway):
    # The request builder drops them wherever they come from; LLM refuses a caller's value first.
    spec = hb.LLM(model="sonnet-4.6", provider="anthropic", gateway=gateway, endpoint="anthropic", max_tokens=4096).spec
    runtime = LLM_ENGINE.apply(spec, profile=spec.gateway)
    payload = _anthropic_payload(runtime, spec, [{"role": "user", "content": "hi"}], think=True, extra_body={"temperature": 0.5, "top_k": 5})

    _assert_thinking_request(payload, ADAPTIVE)


def test_extra_body_sampling_is_kept_without_thinking(server):
    body = _body(server, "anthropic", model="sonnet-4.6", extra_body={"temperature": 0.5, "top_k": 5})

    assert body["temperature"] == 0.5 and body["top_k"] == 5


@pytest.mark.parametrize("gateway", ["anthropic", "litellm"])
def test_effort_on_a_claude_model_without_effort_is_refused(server, gateway):
    # Claude before 4.6 (except Opus 4.5) has no effort setting, so the request would be refused.
    llm = _llm(server, gateway, model="haiku")

    with pytest.raises(ValueError, match="does not accept an effort level"):
        llm.chat("hi", reasoning_effort="low")
    assert BODIES == []
