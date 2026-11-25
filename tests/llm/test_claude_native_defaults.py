"""Effective native Claude limits and LiteLLM sampling before provider I/O."""

import pytest

import heavenbase as hb
from heavenbase.llm.gateways._protocols import _anthropic_payload
from heavenbase.llm.gateways.litellm import runtime as litellm_gateway

pytestmark = pytest.mark.fast
PROMPT = [{"role": "user", "content": "Answer briefly."}]


def _payload(**kwargs):
    spec = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic").spec
    return _anthropic_payload(spec.engine.apply(spec), spec, PROMPT, **kwargs)


@pytest.mark.slow
@pytest.mark.parametrize("options,budget", [({"think": True}, 1024), ({"reasoning_max_tokens": 2048}, 2048)])
def test_implicit_native_limit_leaves_room_for_the_answer(options, budget):
    payload = _payload(**options)
    assert payload["thinking"]["budget_tokens"] == budget
    assert payload["max_tokens"] == budget + 1024


def test_native_limit_remains_1024_without_thinking():
    assert _payload(think=False)["max_tokens"] == 1024


@pytest.mark.parametrize("options", [{"max_tokens": 1024}, {"max_completion_tokens": 1024}, {"extra_body": {"max_tokens": 1024}}])
def test_explicit_native_limit_at_the_budget_is_rejected(options):
    with pytest.raises(ValueError, match="max_tokens"):
        _payload(think=True, **options)


def test_native_output_cap_reads_the_override_that_will_be_sent():
    payload = _payload(think=True, max_tokens=1024, extra_body={"max_tokens": 3072})
    assert payload["max_tokens"] == 3072


@pytest.mark.parametrize("sampling", [{"temperature": 0.3}, {"top_k": 5}])
def test_litellm_refuses_caller_sampling_in_extra_body(monkeypatch, sampling):
    def provider_io(spec):
        pytest.fail("Caller sampling should be refused before the LiteLLM transport runs")

    monkeypatch.setattr(litellm_gateway, "_litellm", provider_io)
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="litellm")
    with pytest.raises(ValueError, match=next(iter(sampling))):
        llm.chat("Answer briefly.", think=True, extra_body=sampling)
