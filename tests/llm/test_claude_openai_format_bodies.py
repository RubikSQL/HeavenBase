"""Claude thinking on the OpenAI-format routes, checked at a local fake endpoint that records each body.

Documented request formats:
- OpenRouter: a ``reasoning`` object (``enabled``, ``effort`` or ``max_tokens``).
- Anthropic's OpenAI-compatibility endpoint: a top-level ``thinking`` of type ``enabled`` with
  ``budget_tokens``; ``adaptive`` is refused and ``reasoning_effort`` is ignored.
- Portkey's OpenAI format: a top-level ``thinking`` passed on to Anthropic.
- Bifrost's OpenAI format: ``reasoning.max_tokens``, sent to Claude as an ``enabled`` budget.
With thinking on, Claude accepts no ``temperature`` other than its default and no ``top_k``.
Non-Claude models keep their 0.1.2.2 request bodies.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache

pytestmark = pytest.mark.fast

BODIES: list = []
CHUNKS = [
    {"id": "c1", "object": "chat.completion.chunk", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}]},
    {"id": "c1", "object": "chat.completion.chunk", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
]
COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        BODIES.append(body)
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in CHUNKS:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            data = json.dumps(COMPLETION).encode()
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
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))
    BODIES.clear()


def _llm(server, gateway, provider, model, stream_chat=True, **options):
    return hb.LLM(model=model, provider=provider, gateway=gateway, base_url=server, api_key="sk-placeholder", stream_chat=stream_chat, **options)


def _body(server, gateway, provider, model, stream_chat=True, options=None, **kwargs):
    assert _llm(server, gateway, provider, model, stream_chat=stream_chat, **(options or {})).chat("hi", **kwargs) == "ok"
    assert len(BODIES) == 1
    return BODIES[0]


def _no_sampling(body):
    assert "temperature" not in body and "top_k" not in body
    assert "temperature" not in body.get("extra_body", {})


# --- OpenRouter (the default route for Claude models) ---------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("gateway", ["openai", "portkey", "bifrost"])
@pytest.mark.parametrize("model", ["haiku", "sonnet-4.6"])
@pytest.mark.parametrize(
    "kwargs, reasoning",
    [
        ({"think": True}, {"enabled": True}),
        ({"reasoning_effort": "low"}, {"enabled": True, "effort": "low"}),
        ({"reasoning_max_tokens": 2048}, {"enabled": True, "max_tokens": 2048}),
    ],
)
def test_openrouter_claude_thinking_sends_reasoning_without_temperature(server, gateway, model, kwargs, reasoning):
    body = _body(server, gateway, "openrouter", model, **kwargs)

    assert body["reasoning"] == reasoning
    _no_sampling(body)


@pytest.mark.slow
def test_openrouter_claude_without_thinking_keeps_its_request(server):
    body = _body(server, "openai", "openrouter", "haiku", think=False)

    assert body["reasoning"] == {"enabled": False}
    assert body["temperature"] == 0


# --- Anthropic's OpenAI-compatibility endpoint (provider="anthropic", gateway="openai") -----------

ENABLED = {"type": "enabled", "budget_tokens": 1024}


@pytest.mark.slow
@pytest.mark.parametrize("stream_chat", [True, False])
@pytest.mark.parametrize("model", ["haiku", "sonnet-4.6", "opus"])
def test_anthropic_compat_think_sends_an_enabled_budget(server, model, stream_chat):
    body = _body(server, "openai", "anthropic", model, stream_chat=stream_chat, options={"max_tokens": 4096}, think=True)

    assert body["thinking"] == ENABLED
    assert "reasoning" not in body and "reasoning_effort" not in body
    _no_sampling(body)


@pytest.mark.slow
def test_anthropic_compat_budget(server):
    body = _body(server, "openai", "anthropic", "sonnet-4.6", options={"max_tokens": 4096}, reasoning_max_tokens=2048)

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    _no_sampling(body)


def test_anthropic_compat_without_thinking_sends_no_thinking(server):
    body = _body(server, "openai", "anthropic", "haiku", think=False)

    assert "thinking" not in body and "reasoning" not in body
    assert body["temperature"] == 0


# --- Portkey's OpenAI format (provider="anthropic", gateway="portkey") ---------------------------


@pytest.mark.slow
@pytest.mark.parametrize(
    "model, thinking",
    [
        ("haiku", {"type": "enabled", "budget_tokens": 1024, "display": "summarized"}),
        ("sonnet-4.6", {"type": "adaptive", "display": "summarized"}),
        ("opus", {"type": "adaptive", "display": "summarized"}),
    ],
)
def test_portkey_openai_format_sends_claude_thinking(server, model, thinking):
    body = _body(server, "portkey", "anthropic", model, options={"max_tokens": 4096}, think=True)

    assert body["thinking"] == thinking
    assert "reasoning" not in body
    _no_sampling(body)


# --- Bifrost's OpenAI format (provider="anthropic", gateway="bifrost") ---------------------------


@pytest.mark.parametrize("model", ["haiku", "sonnet-4.6"])
def test_bifrost_openai_format_sends_a_reasoning_budget(server, model):
    body = _body(server, "bifrost", "anthropic", model, options={"max_tokens": 4096}, think=True)

    assert body["reasoning"] == {"max_tokens": 1024}
    _no_sampling(body)


def test_bifrost_openai_format_refuses_an_adaptive_only_model(server):
    with pytest.raises(ValueError, match="endpoint='anthropic'"):
        _llm(server, "bifrost", "anthropic", "opus", max_tokens=4096).chat("hi", think=True)
    assert BODIES == []


@pytest.mark.parametrize("gateway", ["openai", "portkey", "bifrost"])
def test_effort_is_refused_where_the_route_cannot_send_it(server, gateway):
    with pytest.raises(ValueError, match="reasoning_effort"):
        _llm(server, gateway, "anthropic", "sonnet-4.6", max_tokens=4096).chat("hi", reasoning_effort="low")
    assert BODIES == []


# --- Non-Claude models keep their 0.1.2.2 bodies --------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("gateway", ["openai", "portkey", "bifrost"])
@pytest.mark.parametrize("temperature", [None, 0.6])
def test_non_claude_models_keep_their_request(server, gateway, temperature):
    kwargs = {} if temperature is None else {"temperature": temperature}

    body = _body(server, gateway, "openrouter", "ds-flash", stream_chat=False, think=True, **kwargs)

    assert body["reasoning"] == {"enabled": True}
    assert body["temperature"] == (0 if temperature is None else temperature)


# --- A temperature the caller passes ----------------------------------------------------------------


@pytest.mark.parametrize("gateway, provider", [("openai", "openrouter"), ("openai", "anthropic"), ("anthropic", "anthropic")])
def test_a_caller_temperature_with_claude_thinking_is_refused(server, gateway, provider):
    with pytest.raises(ValueError, match="temperature=0.3"):
        _llm(server, gateway, provider, "sonnet-4.6", max_tokens=4096).chat("hi", think=True, temperature=0.3)
    with pytest.raises(ValueError, match="top_k=5"):
        _llm(server, gateway, provider, "sonnet-4.6", max_tokens=4096).chat("hi", think=True, extra_body={"top_k": 5})
    with pytest.raises(ValueError, match="temperature=0.2"):
        _llm(server, gateway, provider, "sonnet-4.6", max_tokens=4096, temperature=0.2).chat("hi", think=True)
    assert BODIES == []


def test_a_caller_temperature_of_one_or_without_thinking_is_sent(server):
    with_thinking = _body(server, "openai", "openrouter", "sonnet-4.6", think=True, temperature=1)
    BODIES.clear()
    without = _body(server, "openai", "openrouter", "sonnet-4.6", temperature=0.3)

    assert "temperature" not in with_thinking  # 1 is Claude's default with thinking
    assert without["temperature"] == 0.3


def test_default_args_given_to_llm_count_as_configuration(server):
    # Components such as the dashboard assistant pass their configured defaults as default_args.
    body = _body(server, "openai", "openrouter", "sonnet-4.6", options={"default_args": {"temperature": 0.2}}, think=True)

    assert body["reasoning"] == {"enabled": True}
    _no_sampling(body)


@pytest.mark.parametrize("gateway", ["openai", "portkey"])
def test_the_budget_check_reads_max_completion_tokens_too(server, gateway):
    llm = _llm(server, gateway, "anthropic", "haiku")

    with pytest.raises(ValueError, match="max_completion_tokens"):
        llm.chat("hi", think=True, max_completion_tokens=1000)
    assert BODIES == []

    body = _body(server, gateway, "anthropic", "haiku", think=True, max_completion_tokens=4096)
    assert body["thinking"]["budget_tokens"] == 1024 and body["max_completion_tokens"] == 4096


def test_a_constructor_extra_body_temperature_is_refused_even_with_a_top_level_one(server):
    # extra_body wins over top-level fields in the sent request, so the constructor's 0.2 is what would go out.
    llm = _llm(server, "openai", "openrouter", "sonnet-4.6", extra_body={"temperature": 0.2})

    with pytest.raises(ValueError, match="temperature=0.2"):
        llm.chat("hi", think=True, temperature=1)
    assert BODIES == []


@pytest.mark.slow
def test_a_per_call_extra_body_replaces_the_constructor_one(server):
    # The per-call extra_body replaces the constructor's, so its temperature is not sent and nothing is refused.
    body = _body(server, "openai", "openrouter", "sonnet-4.6", options={"extra_body": {"temperature": 0.2}}, think=True, extra_body={"user": "u1"})

    assert body["user"] == "u1"
    _no_sampling(body)
