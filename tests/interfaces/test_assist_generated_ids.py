"""Conversation and turn ids the assistant session log mints for requests without them."""

import pytest

import heavenbase as hb
import heavenbase.api.assist_log as assist_log_module
from heavenbase.llm.base import LLM

import test_assist_session_log

logged_api = test_assist_session_log.logged_api
pytestmark = pytest.mark.fast

# Windows CPython before 3.13 advances time.time() once per system tick of
# about 15.6 ms, so two requests in one tick read the same clock value.
_ONE_CLOCK_TICK = 1_700_000_000.0


@pytest.fixture()
def coarse_clock_api(logged_api, monkeypatch):
    """Session-logged API whose log clock never advances, and its provider calls."""
    import heavenbase.llm as llm_module

    calls = []

    def provider(spec, messages, stream=False, **kwargs):
        seen = [message.get("content") for message in messages if message.get("role") == "user"]
        answer = f"answer to {seen[-1]} after {seen[:-1]}"
        calls.append(answer)
        return {"choices": [{"message": {"role": "assistant", "content": answer}}]}

    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: LLM(preset="mock", mock_chat=provider, mock_pause=(0, 0), cache=False))
    monkeypatch.setattr(assist_log_module, "time", lambda: _ONE_CLOCK_TICK)
    api, log_ws = logged_api
    return api, log_ws, calls


def _sessions(log_ws):
    log = hb.HeavenBase(log_ws)
    return [hb.Session.load(row["object_id"], ws=log) for row in log.rows(hb.Session)], log


@pytest.mark.slow
def test_requests_without_ids_open_separate_conversations_within_one_clock_tick(coarse_clock_api):
    api, log_ws, calls = coarse_clock_api
    first = api.assist("how many rows?", history=[{"role": "user", "content": "ctx-A"}, {"role": "assistant", "content": "ok"}])
    second = api.assist("how many rows?", history=[{"role": "user", "content": "ctx-B"}, {"role": "assistant", "content": "ok"}])
    assert len(calls) == 2  # the second request is answered, not replayed from the first
    assert "ctx-A" in first["text"] and "ctx-B" in second["text"]
    assert "capture_warning" not in first and "capture_warning" not in second
    sessions, log = _sessions(log_ws)
    assert len({session.object_id for session in sessions}) == 2
    assert sorted([message["content"] for message in session.messages(ws=log)] for session in sessions) == sorted(
        [["how many rows?", first["text"]], ["how many rows?", second["text"]]]
    )


@pytest.mark.slow
def test_turns_without_turn_id_stay_separate_within_one_clock_tick(coarse_clock_api):
    api, log_ws, calls = coarse_clock_api
    first = api.assist("first question", conversation_id="conv-0")
    second = api.assist("second question", conversation_id="conv-0")
    assert len(calls) == 2
    assert first["text"].startswith("answer to first question") and second["text"].startswith("answer to second question")
    assert "capture_warning" not in first and "capture_warning" not in second
    sessions, log = _sessions(log_ws)
    assert [[message["content"] for message in session.messages(ws=log)] for session in sessions] == [
        ["first question", first["text"], "second question", second["text"]]
    ]
