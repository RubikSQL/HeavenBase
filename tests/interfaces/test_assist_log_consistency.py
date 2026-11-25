"""Data consistency regressions for assistant turn recording."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import heavenbase as hb
from heavenbase.llm.base import LLM
from heavenbase.llm.trace import LLMTraceEvent
from heavenbase.utils import hash_id

import test_assist_session_log

logged_api = test_assist_session_log.logged_api
pytestmark = pytest.mark.fast
_WAIT = 30


def _transcript(recorder, conversation):
    session = recorder._entity("agent-session").load(hash_id("assist-log", conversation), ws=recorder.workspace())
    return [message["content"] for message in session.messages(ws=recorder.workspace())]


@pytest.mark.slow
@pytest.mark.parametrize("retry_stream", [False, True], ids=["blocking", "streaming"])
def test_overlapping_dashboard_turn_replays_one_answer(logged_api, monkeypatch, retry_stream):
    import heavenbase.llm as llm_module
    from heavenbase.api.assist_log import AssistTurnLog

    api, _ = logged_api
    api.assist("warm up", conversation_id="warm")
    recorder = api._assist_session_log
    first_request, let_first_write, second_admitting, second_request = Event(), Event(), Event(), Event()
    second_history_started = Event()
    persist = AssistTurnLog._persist_event
    begin = recorder.begin_turn
    identity = {"conversation_id": "overlap", "turn_id": "t"}

    def begin_turn(*args, **kwargs):
        if second_history_started.is_set():
            second_admitting.set()
        return begin(*args, **kwargs)

    def pause_write(turn, event):
        if turn.conversation_id == "overlap" and event.kind == "request":
            history = [message["content"] for message in event.payload["messages"]]
            if "history one" in history:
                first_request.set()
                assert let_first_write.wait(_WAIT)
            if "history two" in history:
                second_request.set()
                assert let_first_write.wait(_WAIT)
        persist(turn, event)

    provider_calls = []

    def provider(spec, messages, stream=False, **kwargs):
        answer = "one" if any(message.get("content") == "history one" for message in messages) else "two"
        provider_calls.append(answer)
        if stream:
            return iter([{"choices": [{"delta": {"role": "assistant", "content": answer}}]}])
        return {"choices": [{"message": {"role": "assistant", "content": answer}}]}

    monkeypatch.setattr(recorder, "begin_turn", begin_turn)
    monkeypatch.setattr(AssistTurnLog, "_persist_event", pause_write)
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: LLM(preset="mock", mock_chat=provider, mock_pause=(0, 0), cache=False))

    def retry():
        second_history_started.set()
        history = [{"role": "user", "content": "history two"}, {"role": "assistant", "content": "previous"}]
        if retry_stream:
            events = list(api.assist_stream("question", history=history, **identity))
            return events[-1], [event["text"] for event in events if event["kind"] == "text"]
        return api.assist("question", history=history, **identity), []

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            api.assist, "question", history=[{"role": "user", "content": "history one"}, {"role": "assistant", "content": "previous"}], **identity
        )
        try:
            assert first_request.wait(_WAIT)
            second = pool.submit(retry)
            assert second_admitting.wait(_WAIT)
            second_request.wait(1)
        finally:
            let_first_write.set()
        first_reply, (second_reply, second_chunks) = first.result(_WAIT), second.result(_WAIT)
    requests = recorder._trace_rows(recorder.workspace(), "payload", conversation_id="overlap", kind="request")
    assert (first_reply["text"], second_reply["text"], second_chunks, provider_calls, len(requests)) == ("one", "one", [], ["one"], 1)
    assert "capture_warning" not in first_reply and "capture_warning" not in second_reply
    assert _transcript(recorder, "overlap") == ["question", "one"]


@pytest.mark.slow
def test_concurrent_retries_choose_one_authoritative_answer(logged_api, monkeypatch):
    api, _ = logged_api
    api.assist("warm up", conversation_id="warm")
    recorder = api._assist_session_log
    identity = {"conversation_id": "retry", "turn_id": "t", "ws_id": str(api.ws_id)}
    recorder.begin_turn("question", **identity).close(error="temporary provider failure")
    first = recorder.begin_turn("question", **identity)
    waiting = _observe_wait(recorder, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(recorder.begin_turn, "question", **identity)
        try:
            assert waiting.wait(_WAIT)
        finally:
            first.close(text="first answer")
        second = retry.result(_WAIT)
    assert first.capture == second.capture == "complete"
    assert second.replay()["text"] == "first answer"
    assert _transcript(recorder, "retry") == ["question", "first answer"]


def _observe_wait(recorder, monkeypatch):
    waiting = Event()
    wait = recorder._lock.wait

    def observe(*args, **kwargs):
        waiting.set()
        return wait(*args, **kwargs)

    monkeypatch.setattr(recorder._lock, "wait", observe)
    return waiting


@pytest.mark.slow
def test_non_ascii_digit_is_an_ordinary_part_of_a_turn_id(logged_api):
    api, _ = logged_api
    api.assist("other", conversation_id="unicode", turn_id="chapter-a²")
    reply = api.assist("question", conversation_id="unicode", turn_id="chapter")
    assert reply["text"] and "capture_warning" not in reply
    recorder = api._assist_session_log
    assert _transcript(recorder, "unicode") == ["other", reply["text"], "question", reply["text"]]


@pytest.mark.slow
def test_admitted_attempts_keep_both_request_traces(logged_api):
    api, _ = logged_api
    api.assist("warm up", conversation_id="warm")
    recorder = api._assist_session_log
    identity = {"conversation_id": "attempts", "turn_id": "t"}
    for marker, outcome in (("first", {"error": "failed"}), ("second", {"text": "answer"})):
        turn = recorder.begin_turn("question", **identity)
        turn.llm_trace()(LLMTraceEvent(kind="request", call="round", seq=0, payload={"marker": marker}))
        turn.close(**outcome)
    rows = recorder._trace_rows(recorder.workspace(), "payload", conversation_id="attempts", kind="request")
    assert {row["payload"]["marker"] for row in rows} == {"first", "second"}


@pytest.mark.slow
def test_rebuild_keeps_transcript_order_after_overlapping_closes(logged_api, monkeypatch):
    api, _ = logged_api
    api.assist("warm up", conversation_id="warm")
    recorder = api._assist_session_log
    first = recorder.begin_turn("first", conversation_id="order", turn_id="first")
    waiting = _observe_wait(recorder, monkeypatch)

    def close_second():
        second = recorder.begin_turn("second", conversation_id="order", turn_id="second")
        second.close(text="second answer")
        return second.capture

    with ThreadPoolExecutor(max_workers=1) as pool:
        other = pool.submit(close_second)
        try:
            assert waiting.wait(_WAIT)
        finally:
            first.close(text="first answer")
        assert first.capture == other.result(_WAIT) == "complete"
    before = _transcript(recorder, "order")
    assert before == ["first", "first answer", "second", "second answer"]
    recorder.rebuild_session("order")
    assert _transcript(recorder, "order") == before


@pytest.mark.slow
def test_rebuild_keeps_completion_order_after_a_clock_rollback(logged_api, monkeypatch):
    from heavenbase.api import assist_log

    api, _ = logged_api
    conversation = {"conversation_id": "clock"}
    api.assist("first", turn_id="first", **conversation)
    api._assist_session_log = None
    monkeypatch.setattr(assist_log, "time", lambda: 1)
    api.assist("second", turn_id="second", **conversation)
    recorder = api._assist_session_log
    before = _transcript(recorder, "clock")
    recorder.rebuild_session("clock")
    assert _transcript(recorder, "clock") == before


@pytest.mark.slow
@pytest.mark.parametrize("repair", [False, True], ids=["eviction", "repair"])
def test_projection_cleans_unlisted_messages_by_session_id(logged_api, monkeypatch, repair):
    api, _ = logged_api
    hb.CM_HVNB.set("heavenbase.dashboard.assistant.session_log_max_sessions", 1)
    api.assist("initial", conversation_id="orphan", turn_id="initial")
    recorder = api._assist_session_log
    ws = recorder.workspace()
    message = recorder._entity("agent-message")
    ref = hash_id("assist-log", "orphan")
    upsert = ws.upsert

    def fail_append(entity, row, *args, **kwargs):
        if entity.identifier == "agent-session" and row["state"].get("last_turn_id") in {"failed-one", "failed-two"}:
            raise RuntimeError("simulated session write failure after message writes")
        return upsert(entity, row, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ws, "upsert", fail_append)
        for turn in ("failed-one", "failed-two"):
            reply = api.assist(turn, conversation_id="orphan", turn_id=turn)
            assert reply["capture_warning"] == "incomplete:projection:RuntimeError"
    api.assist("later", conversation_id="orphan", turn_id="later")
    if repair:
        api.assist("failed-one", conversation_id="orphan", turn_id="failed-one")
        assert len(_transcript(recorder, "orphan")) == 8
    else:
        api.assist("evict", conversation_id="next", turn_id="new")
        assert ws.get(ref, entity=recorder._entity("agent-session")) is None
        assert not recorder._trace_rows(ws, conversation_id="orphan")
    rows = ws.query(message).where(message.session_id == ref).select("object_id").execute().rows()
    assert len(rows) == (8 if repair else 0)


@pytest.mark.slow
def test_new_turn_restores_a_missing_session_before_appending(logged_api):
    api, _ = logged_api
    hb.CM_HVNB.set("heavenbase.dashboard.assistant.session_log_max_sessions", 2)
    old = api.assist("old", conversation_id="missing", turn_id="old")
    recorder = api._assist_session_log
    ws = recorder.workspace()
    ws.delete_many([(recorder._entity("agent-session"), hash_id("assist-log", "missing"))])
    api.assist("other one", conversation_id="other-one", turn_id="other")
    api.assist("other two", conversation_id="other-two", turn_id="other")
    new = api.assist("new", conversation_id="missing", turn_id="new")
    expected = ["old", old["text"], "new", new["text"]]
    before_replay = _transcript(recorder, "missing")
    assert api.assist("new", conversation_id="missing", turn_id="new")["text"] == new["text"]
    assert before_replay == _transcript(recorder, "missing") == expected
    assert all(recorder._trace_rows(ws, conversation_id=name) for name in ("other-one", "other-two"))


@pytest.mark.slow
def test_replay_repairs_a_projected_turn_whose_answer_differs_from_the_trace(logged_api):
    api, _ = logged_api
    identity = {"conversation_id": "mismatch", "turn_id": "t"}
    api.assist("question", **identity)
    recorder = api._assist_session_log
    ws = recorder.workspace()
    trace = recorder._entity("agent-trace")
    row = ws.get(trace.event_id("mismatch", "t", 0, "turn"), entity=trace)
    row["payload"]["text"] = "authoritative answer"
    ws.upsert(trace, row)

    assert api.assist("question", **identity)["text"] == "authoritative answer"
    assert _transcript(recorder, "mismatch") == ["question", "authoritative answer"]


@pytest.mark.slow
def test_retry_event_key_is_distinct_from_an_ordinary_suffix_turn(logged_api):
    api, _ = logged_api
    api.assist("warm up", conversation_id="warm")
    recorder = api._assist_session_log
    conversation = {"conversation_id": "suffix"}
    recorder.begin_turn("parent", turn_id="chapter", **conversation).close(error="provider failure")
    for question, key, marker in (("parent", "chapter", "retry"), ("sibling", "chapter-a2", "sibling")):
        turn = recorder.begin_turn(question, turn_id=key, **conversation)
        turn.llm_trace()(LLMTraceEvent(kind="request", call="round", seq=0, payload={"marker": marker}))
        turn.close(text=marker)
    rows = recorder._trace_rows(recorder.workspace(), "payload", kind="request", **conversation)
    assert {row["payload"]["marker"] for row in rows} == {"retry", "sibling"}
