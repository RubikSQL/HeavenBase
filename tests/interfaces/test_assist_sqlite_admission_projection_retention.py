"""SQLite regressions for assistant admission, projection, and retention."""

import pytest

import heavenbase as hb
from heavenbase.api.assist_log import AssistSessionLog
from heavenbase.utils import hash_id

import test_assist_session_log

logged_api = test_assist_session_log.logged_api
pytestmark = pytest.mark.fast


def _log(api):
    api._assist_session_log = AssistSessionLog(api.cm, context=api.context)
    return api._assist_session_log


def _transcript(recorder, conversation):
    ws = recorder.workspace()
    session = recorder._entity("agent-session").load(hash_id("assist-log", conversation), ws=ws)
    return [message["content"] for message in session.messages(ws=ws)]


@pytest.mark.slow
def test_restore_lookup_failure_does_not_evict_another_conversation(logged_api, monkeypatch):
    api, _ = logged_api
    hb.CM_HVNB.set("heavenbase.dashboard.assistant.session_log_max_sessions", 1)
    recorder = _log(api)
    recorder.begin_turn("earlier", conversation_id="lost", turn_id="one").close(text="first")
    ws = recorder.workspace()
    session = recorder._entity("agent-session")
    ws.delete_many([(session, hash_id("assist-log", "lost"))])
    recorder.begin_turn("unrelated", conversation_id="other", turn_id="one").close(text="stay")
    trace_rows = recorder._trace_rows
    failures = []

    def fail_once(*args, **kwargs):
        if kwargs.get("conversation_id") == "lost" and not kwargs.get("turn_id") and not failures:
            failures.append(True)
            raise OSError("trace lookup unavailable")
        return trace_rows(*args, **kwargs)

    monkeypatch.setattr(recorder, "_trace_rows", fail_once)
    recorder.begin_turn("follow-up", conversation_id="lost", turn_id="two").close(text="second")
    assert failures
    assert _transcript(recorder, "other") == ["unrelated", "stay"]
    assert _transcript(recorder, "lost") == ["earlier", "first", "follow-up", "second"]


@pytest.mark.slow
def test_completed_retry_recovers_from_one_failed_read(logged_api, monkeypatch):
    import heavenbase.llm as llm_module
    from heavenbase.llm.base import LLM

    api, _ = logged_api
    recorder = _log(api)
    calls = []

    def provider(*args, **kwargs):
        calls.append(len(calls) + 1)
        return {"choices": [{"message": {"role": "assistant", "content": f"answer {calls[-1]}"}}]}

    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: LLM(preset="mock", mock_chat=provider, mock_pause=(0, 0), cache=False))
    first = api.assist("question", conversation_id="retry", turn_id="one")
    ws = recorder.workspace()
    trace = recorder._entity("agent-trace")
    key = trace.event_id("retry", "one", 0, "turn")
    get = ws.get
    failures = []

    def fail_once(object_id, *args, **kwargs):
        if object_id == key and not failures:
            failures.append(True)
            raise OSError("turn lookup unavailable")
        return get(object_id, *args, **kwargs)

    monkeypatch.setattr(ws, "get", fail_once)
    replay = api.assist("question", conversation_id="retry", turn_id="one")
    assert failures and calls == [1]
    assert first["text"] == replay["text"] == "answer 1"
    assert len(recorder._trace_rows(ws, conversation_id="retry", kind="request")) == 1

    def unavailable(object_id, *args, **kwargs):
        if object_id == key:
            raise OSError("turn lookup unavailable")
        return get(object_id, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ws, "get", unavailable)
        with pytest.raises(ValueError, match="could not read the turn"):
            api.assist("question", conversation_id="retry", turn_id="one")
    assert api.assist("question", conversation_id="retry", turn_id="one")["text"] == "answer 1"
    assert calls == [1]


@pytest.mark.slow
def test_interrupted_admission_releases_turn(logged_api, monkeypatch):
    api, _ = logged_api
    recorder = _log(api)
    ws = recorder.workspace()
    upsert = ws.upsert
    failures = []

    def interrupt_once(entity, row, *args, **kwargs):
        if entity.identifier == "agent-session" and not failures:
            failures.append(True)
            raise KeyboardInterrupt
        return upsert(entity, row, *args, **kwargs)

    monkeypatch.setattr(ws, "upsert", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        api.assist("question", conversation_id="interrupt", turn_id="one")
    assert not recorder._trace_rows(ws, conversation_id="interrupt")
    result = api.assist("question", conversation_id="interrupt", turn_id="one")
    assert result["text"] == "answer after 1 rounds"


@pytest.mark.slow
@pytest.mark.parametrize("length", [4096, 65536])
def test_large_answer_does_not_block_transcript_rebuild(logged_api, length):
    api, _ = logged_api
    recorder = _log(api)
    answers = ["short", "a" * length, "last"]
    for index, answer in enumerate(answers):
        recorder.begin_turn(f"q{index}", conversation_id="long", turn_id=f"t{index}").close(text=answer)
    recorder.workspace().delete_many([(recorder._entity("agent-session"), hash_id("assist-log", "long"))])
    recorder.rebuild_session("long")
    assert _transcript(recorder, "long") == ["q0", answers[0], "q1", answers[1], "q2", answers[2]]


@pytest.mark.slow
def test_sealing_does_not_read_prior_turn_payloads(logged_api, monkeypatch):
    api, _ = logged_api
    recorder = _log(api)
    for index in range(4):
        recorder.begin_turn(f"q{index}", conversation_id="bounded", turn_id=f"t{index}").close(text="a" * 4000)
    recorder = _log(api)  # the next seal must work with a fresh recorder
    last = recorder.begin_turn("last question", conversation_id="bounded", turn_id="last")
    backend = recorder.workspace().backends.get("main")
    trace_table = backend.tables["agent-trace"]
    execute = backend.db.orm_execute
    fetched = []

    def observe(statement, *args, **kwargs):
        result = execute(statement, *args, **kwargs)
        if getattr(statement, "is_select", False) and trace_table in statement.get_final_froms() and "payload" in statement.selected_columns:
            fetched.extend(row["turn_id"] for row in result.to_dicts() if row.get("kind") == "turn")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(backend.db, "orm_execute", observe)
        last.close(text="last answer")
    assert fetched == []
    assert last.capture == "complete"


@pytest.mark.slow
def test_missing_seal_counter_recovers_order_from_existing_turns(logged_api):
    api, _ = logged_api
    recorder = _log(api)
    recorder.begin_turn("Were the March invoices paid?", conversation_id="invoices", turn_id="march").close(text="They were paid on time.")
    recorder.begin_turn("Were the April invoices paid?", conversation_id="invoices", turn_id="april").close(text="One is still open.")
    ws = recorder.workspace()
    ws.delete_many([("agent-log-seal", hash_id("assist-log-seal", "invoices"))])

    recorder = _log(api)
    recorder.begin_turn("Were the May invoices paid?", conversation_id="invoices", turn_id="may").close(text="All were paid.")
    turns = recorder._trace_rows(ws, "seq", "turn_id", conversation_id="invoices", kind="turn")
    assert sorted((row["seq"], row["turn_id"]) for row in turns) == [(1, "march"), (2, "april"), (3, "may")]


@pytest.mark.slow
def test_failed_summary_write_can_leave_a_gap_in_seal_order(logged_api, monkeypatch):
    api, _ = logged_api
    recorder = _log(api)
    turn = recorder.begin_turn("Were the invoices checked?", conversation_id="accounts", turn_id="review")
    ws = recorder.workspace()
    upsert = ws.upsert

    def fail_summary(entity, row, *args, **kwargs):
        if getattr(entity, "identifier", entity) == "agent-trace" and row["kind"] == "turn":
            raise OSError("summary write unavailable")
        return upsert(entity, row, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ws, "upsert", fail_summary)
        turn.close(text="The invoices were checked.")
    assert turn.capture == "incomplete:turn:OSError"

    retry = recorder.begin_turn("Were the invoices checked?", conversation_id="accounts", turn_id="review")
    assert retry.replay() is None
    retry.close(text="The invoices reconcile.")
    turns = recorder._trace_rows(ws, "seq", conversation_id="accounts", kind="turn")
    assert [row["seq"] for row in turns] == [2]
    assert _transcript(recorder, "accounts") == ["Were the invoices checked?", "The invoices reconcile."]


@pytest.mark.slow
@pytest.mark.parametrize("phase", ["retention lookup", "session publication"])
def test_interrupted_admission_does_not_cost_a_retained_conversation(logged_api, monkeypatch, phase):
    api, _ = logged_api
    hb.CM_HVNB.set("heavenbase.dashboard.assistant.session_log_max_sessions", 2)
    recorder = _log(api)
    recorder.begin_turn("Summarize July shipments.", conversation_id="july", turn_id="summary").close(text="July shipments arrived on schedule.")
    recorder.begin_turn("Summarize August shipments.", conversation_id="august", turn_id="summary").close(text="August shipments increased.")
    ws = recorder.workspace()
    session = recorder._entity("agent-session")
    query, upsert = ws.query, ws.upsert

    def interrupt_lookup(entity):
        if entity is session:
            raise KeyboardInterrupt
        return query(entity)

    def interrupt_publication(entity, row, *args, **kwargs):
        result = upsert(entity, row, *args, **kwargs)
        if entity is session:
            raise KeyboardInterrupt
        return result

    with monkeypatch.context() as patch:
        if phase == "retention lookup":
            patch.setattr(ws, "query", interrupt_lookup)
        else:
            patch.setattr(ws, "upsert", interrupt_publication)
        with pytest.raises(KeyboardInterrupt):
            recorder.begin_turn("Summarize September shipments.", conversation_id="september", turn_id="summary")

    recorder.begin_turn("Summarize October shipments.", conversation_id="october", turn_id="summary").close(text="October shipments were steady.")
    sessions = ws.query(session).select("state").execute().rows()
    assert {row["state"]["conversation_id"] for row in sessions} == {"august", "october"}
    assert recorder._trace_rows(ws, conversation_id="august", kind="turn")
    assert not recorder._trace_rows(ws, conversation_id="july")
    assert ws.get(hash_id("assist-log-seal", "july"), entity="agent-log-seal") is None


@pytest.mark.slow
def test_blank_completed_replay_rebuilds_a_missing_session(logged_api):
    api, _ = logged_api
    recorder = _log(api)
    recorder.begin_turn("Were the deliveries checked?", conversation_id="deliveries", turn_id="checked").close(text="All deliveries were checked.")
    recorder.begin_turn("Any corrections?", conversation_id="deliveries", turn_id="corrections").close(text="  ")
    ws = recorder.workspace()
    session = recorder._entity("agent-session")
    ws.delete_many([(session, hash_id("assist-log", "deliveries"))])

    retry = recorder.begin_turn("Any corrections?", conversation_id="deliveries", turn_id="corrections")
    assert retry.replay()["text"] == "  "
    assert _transcript(recorder, "deliveries") == ["Were the deliveries checked?", "All deliveries were checked."]


@pytest.mark.slow
def test_admission_can_stay_over_the_cap_when_every_session_is_active(logged_api):
    api, _ = logged_api
    hb.CM_HVNB.set("heavenbase.dashboard.assistant.session_log_max_sessions", 1)
    recorder = _log(api)
    first = recorder.begin_turn("Review the January invoices.", conversation_id="january", turn_id="review")
    try:
        second = recorder.begin_turn("Review the February invoices.", conversation_id="february", turn_id="review")
        try:
            ws = recorder.workspace()
            sessions = ws.query(recorder._entity("agent-session")).select("state").execute().rows()
            assert {row["state"]["conversation_id"] for row in sessions} == {"january", "february"}
        finally:
            second.close(text="February invoices reconciled.")
    finally:
        first.close(text="January invoices reconciled.")


@pytest.mark.slow
def test_retention_counts_in_flight_conversations(logged_api):
    api, _ = logged_api
    hb.CM_HVNB.set("heavenbase.dashboard.assistant.session_log_max_sessions", 2)
    recorder = _log(api)
    recorder.begin_turn("first", conversation_id="pinned", turn_id="one").close(text="stay")
    recorder.begin_turn("second", conversation_id="idle", turn_id="one").close(text="evict")
    turn = recorder.begin_turn("still active", conversation_id="pinned", turn_id="two")
    try:
        recorder.begin_turn("third", conversation_id="new", turn_id="one").close(text="admitted")
        ws = recorder.workspace()
        sessions = ws.query(recorder._entity("agent-session")).select("state").execute().rows()
        assert {row["state"]["conversation_id"] for row in sessions} == {"pinned", "new"}
        assert not recorder._trace_rows(ws, conversation_id="idle")
    finally:
        turn.close(text="finished")
