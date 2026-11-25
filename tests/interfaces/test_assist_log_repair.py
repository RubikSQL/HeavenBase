"""Review regressions for durable assistant turn and transcript consistency."""

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Event

import pytest

import heavenbase as hb
from heavenbase.api.assist_log import AssistSessionLog, AssistTurnLog
from heavenbase.api.http import build_app
from heavenbase.api.service import DashboardAPI
from heavenbase.llm.base import LLM
from heavenbase.llm.trace import LLMTraceEvent
from heavenbase.utils import hash_id

import test_assist_session_log
from test_assist_log_consistency import _observe_wait

logged_api = test_assist_session_log.logged_api
pytestmark = pytest.mark.fast
_WAIT = 30


def _session(recorder, conversation):
    return recorder._entity("agent-session").load(hash_id("assist-log", conversation), ws=recorder.workspace())


def _transcript(recorder, conversation):
    return [message["content"] for message in _session(recorder, conversation).messages(ws=recorder.workspace())]


@pytest.mark.slow
def test_parent_and_child_dashboard_requests_join_the_same_turn(logged_api, monkeypatch):
    import heavenbase.llm as llm_module

    parent_api, log_ws = logged_api
    parent_api.assist("warm up", conversation_id="warm")
    child = parent_api.context.child(parent_api.ws_id)
    child_api = DashboardAPI(context=child)
    child_api.open(parent_api.ws_id)
    recorder = AssistSessionLog(child_api.cm, context=child)
    recorder.workspace()
    child_api._assist_session_log = recorder
    entered, release, child_admission, child_request = (Event() for _ in range(4))
    calls = []
    persist = AssistTurnLog._persist_event
    begin = recorder.begin_turn
    identity = {"conversation_id": "parent-child", "turn_id": "t"}

    def provider(spec, messages, **kwargs):
        answer = "one" if any(message["content"] == "one" for message in messages) else "two"
        calls.append(answer)
        return {"choices": [{"message": {"role": "assistant", "content": answer}}]}

    def pause_write(turn, event):
        if turn.conversation_id == identity["conversation_id"] and event.kind == "request":
            marker = entered if turn._owner is parent_api._assist_session_log else child_request
            marker.set()
            assert release.wait(_WAIT)
        persist(turn, event)

    def mark_child_admission(*args, **kwargs):
        child_admission.set()
        return begin(*args, **kwargs)

    monkeypatch.setattr(recorder, "begin_turn", mark_child_admission)
    monkeypatch.setattr(AssistTurnLog, "_persist_event", pause_write)
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: LLM(preset="mock", mock_chat=provider, mock_pause=(0, 0), cache=False))

    def call(api, marker):
        return api.assist("question", history=[{"role": "user", "content": marker}, {"role": "assistant", "content": "earlier"}], **identity)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(call, parent_api, "one")
            try:
                assert entered.wait(_WAIT)
                second = pool.submit(call, child_api, "two")
                assert child_admission.wait(_WAIT)
                child_request.wait(1)
            finally:
                release.set()
            replies = [first.result(_WAIT), second.result(_WAIT)]
        requests = recorder._trace_rows(recorder.workspace(), conversation_id=identity["conversation_id"], kind="request")
        assert ([reply["text"] for reply in replies], calls, len(requests)) == (["one", "one"], ["one"], 1)
        assert all("capture_warning" not in reply for reply in replies)
        assert _transcript(recorder, identity["conversation_id"]) == ["question", "one"]
    finally:
        child.close()


@pytest.mark.slow
def test_parent_and_child_retries_keep_the_first_successful_close(logged_api, monkeypatch):
    api, _ = logged_api
    api.assist("warm up", conversation_id="warm")
    parent = api._assist_session_log
    child = api.context.child(api.ws_id)
    identity = {"conversation_id": "peer-retries", "turn_id": "t"}
    try:
        peer = AssistSessionLog(api.cm, context=child)
        parent.begin_turn("question", **identity).close(error="temporary provider failure")
        first = parent.begin_turn("question", **identity)
        waiting = _observe_wait(parent, monkeypatch)
        with ThreadPoolExecutor(max_workers=1) as pool:
            retry = pool.submit(peer.begin_turn, "question", **identity)
            try:
                assert waiting.wait(_WAIT)
            finally:
                first.close(text="first answer")
            second = retry.result(_WAIT)
        assert first.capture == second.capture == "complete"
        assert second.replay()["text"] == "first answer"
        assert _transcript(peer, identity["conversation_id"]) == ["question", "first answer"]
    finally:
        child.close()


@pytest.mark.slow
def test_new_turn_recovers_after_restore_publication_fails(logged_api, monkeypatch):
    api, _ = logged_api
    conversation = {"conversation_id": "restore-retry"}
    old = api.assist("old", turn_id="old", **conversation)
    recorder = api._assist_session_log
    ws = recorder.workspace()
    ws.delete_many([(recorder._entity("agent-session"), hash_id("assist-log", conversation["conversation_id"]))])
    upsert = ws.upsert
    failures = []

    def fail_one_restore(entity, row, *args, **kwargs):
        if entity.identifier == "agent-session" and row["state"].get("turn_ids") == ["old"] and not failures:
            failures.append(row["object_id"])
            raise RuntimeError("restore publication failed")
        return upsert(entity, row, *args, **kwargs)

    monkeypatch.setattr(ws, "upsert", fail_one_restore)
    new = api.assist("new", turn_id="new", **conversation)
    before = _transcript(recorder, conversation["conversation_id"])
    replay = api.assist("new", turn_id="new", **conversation)
    assert len(failures) == 1 and replay["text"] == new["text"]
    assert before == _transcript(recorder, conversation["conversation_id"]) == ["old", old["text"], "new", new["text"]]


@pytest.mark.slow
def test_replay_retries_stale_message_cleanup_after_publication(logged_api, monkeypatch):
    api, _ = logged_api
    conversation = {"conversation_id": "cleanup-retry"}
    api.assist("initial", turn_id="initial", **conversation)
    recorder = api._assist_session_log
    ws = recorder.workspace()
    upsert = ws.upsert

    def fail_append(entity, row, *args, **kwargs):
        if entity.identifier == "agent-session" and row["state"].get("last_turn_id") == "missing":
            raise RuntimeError("append publication failed")
        return upsert(entity, row, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ws, "upsert", fail_append)
        assert api.assist("missing", turn_id="missing", **conversation)["capture_warning"] == "incomplete:projection:RuntimeError"
    api.assist("later", turn_id="later", **conversation)

    def fail_cleanup(targets):
        assert targets and all(entity.identifier == "agent-message" for entity, _ in targets)
        raise RuntimeError("stale cleanup failed")

    with monkeypatch.context() as patch:
        patch.setattr(ws, "delete_many", fail_cleanup)
        assert api.assist("missing", turn_id="missing", **conversation)["capture_warning"] == "incomplete:projection:RuntimeError"
    assert "capture_warning" not in api.assist("missing", turn_id="missing", **conversation)
    session = _session(recorder, conversation["conversation_id"])
    message = recorder._entity("agent-message")
    actual = ws.query(message).where(message.session_id == session.object_id).select("object_id").execute().rows()
    assert len(session.message_ids) == 6
    assert {row["object_id"] for row in actual} == set(session.message_ids)


@pytest.mark.slow
def test_disconnected_sse_duplicate_does_not_start_a_provider_retry(logged_api, monkeypatch):
    import heavenbase.llm as llm_module

    api, _ = logged_api
    api.assist("warm up", conversation_id="warm")
    recorder = api._assist_session_log
    active, release, duplicate_waiting, duplicate_finished = (Event() for _ in range(4))
    calls = []
    wait = recorder._lock.wait

    def provider(spec, messages, stream=False, **kwargs):
        early = not active.is_set()
        calls.append(early)
        if early:
            active.set()
            assert release.wait(_WAIT)
        raise RuntimeError("provider unavailable")

    def mark_wait(*args, **kwargs):
        duplicate_waiting.set()
        return wait(*args, **kwargs)

    class ObservedAPI(DashboardAPI):
        def assist_stream(self, *args, **kwargs):
            try:
                yield from super().assist_stream(*args, **kwargs)
            finally:
                duplicate_finished.set()

    duplicate = ObservedAPI(context=api.context)
    duplicate.open(api.ws_id)
    duplicate._assist_session_log = recorder
    app = build_app(duplicate)
    monkeypatch.setattr(recorder._lock, "wait", mark_wait)
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: LLM(preset="mock", mock_chat=provider, mock_pause=(0, 0), cache=False))
    identity = {"conversation_id": "disconnect", "turn_id": "t"}

    async def disconnect():
        messages = []
        request = b'{"question":"question","conversation_id":"disconnect","turn_id":"t"}'
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/assist/stream",
            "query_string": b"",
            "headers": [(b"host", b"localhost"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("localhost", 80),
        }

        async def receive():
            nonlocal request
            if request:
                body, request = request, b""
                return {"type": "http.request", "body": body, "more_body": False}
            while not duplicate_waiting.is_set():
                await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        assert messages[0]["status"] == 200

    with ThreadPoolExecutor(max_workers=1) as pool:
        working = pool.submit(api.assist, "question", **identity)
        try:
            assert active.wait(_WAIT)
            asyncio.run(asyncio.wait_for(disconnect(), timeout=_WAIT))
        finally:
            release.set()
        with pytest.raises(ValueError, match="provider unavailable"):
            working.result(_WAIT)
    assert duplicate_finished.wait(_WAIT)
    assert calls == [True]


@pytest.mark.slow
def test_unsealed_retry_shaped_literal_does_not_reserve_parent_attempt(logged_api):
    api, _ = logged_api
    api.assist("warm up", conversation_id="warm")
    recorder = api._assist_session_log
    conversation = {"conversation_id": "literal"}
    trace = recorder._entity("agent-trace")
    # An earlier process wrote the request but never sealed a turn summary.
    recorder.workspace().upsert(trace, trace.event("literal", "chapter-a2", 0, "request", payload={"marker": "literal"}))
    api.assist("parent", turn_id="chapter", **conversation)
    requests = recorder._trace_rows(recorder.workspace(), "turn_id", kind="request", **conversation)
    assert {row["turn_id"] for row in requests} == {"chapter-a2", "chapter"}


@pytest.mark.slow
@pytest.mark.parametrize("first_timestamp", [1000, 2000], ids=["tied", "reversed"])
def test_legacy_rebuild_respects_intact_transcript_order(logged_api, first_timestamp):
    api, _ = logged_api
    conversation = "legacy-ties"
    for turn_id in ("a", "b"):
        api.assist(turn_id, conversation_id=conversation, turn_id=turn_id)
    recorder = api._assist_session_log
    ws = recorder.workspace()
    trace = recorder._entity("agent-trace")
    rows = recorder._trace_rows(ws, "turn_id", "ws_id", "payload", "conversation_id", "kind", "seq", "call", conversation_id=conversation, kind="turn")
    for row in rows:
        row["payload"].pop("close_order", None)
        row["seq"] = 0
        row["payload"]["closed_at"] = first_timestamp if row["turn_id"] == "a" else 1000
        ws.upsert(trace, row)
    assert _session(recorder, conversation).state["turn_ids"] == ["a", "b"]
    recorder.rebuild_session(conversation)
    assert _session(recorder, conversation).state["turn_ids"] == ["a", "b"]


def _stored_reads(log, monkeypatch):
    """Record each row the log's SQL backend returns that carries a stored payload or message text."""
    db = log.backends.get("main").db
    real = db.orm_execute
    read: list = []

    def recording(statement, *args, **kwargs):
        response = real(statement, *args, **kwargs)
        rows = response.to_dicts() if response.ok else []
        read.extend(row for row in rows if row.get("payload") or row.get("content_text"))
        return response

    monkeypatch.setattr(db, "orm_execute", recording)
    return read


@pytest.mark.slow
def test_retention_and_attempt_detection_read_ids_not_stored_payloads(logged_api, monkeypatch):
    """Eviction and retry-attempt detection read row ids and coordinates; payloads stay in storage."""
    api, _ = logged_api
    hb.CM_HVNB.set(f"{test_assist_session_log._ASSIST}.session_log_max_sessions", 1)
    for index in range(3):
        api.assist(f"question {index}", conversation_id="conv-old", turn_id=f"turn-{index}")
    recorder = api._assist_session_log
    log, ws_id = recorder.workspace(), str(api.ws_id or "")
    read = _stored_reads(log, monkeypatch)
    turn = recorder.begin_turn("new question", conversation_id="conv-new", turn_id="turn-1", ws_id=ws_id)
    assert read == []  # the evicted conversation's requests, responses and messages were deleted unread
    assert recorder._trace_rows(log, conversation_id="conv-old") == []  # and they are gone
    turn.llm_trace()(LLMTraceEvent(kind="request", call="round", seq=0, payload={"messages": [{"role": "user", "content": "x" * 4096}]}))
    turn.close(error="provider down")
    read.clear()
    retry = recorder.begin_turn("new question", conversation_id="conv-new", turn_id="turn-1", ws_id=ws_id)
    assert retry._attempt == 2
    assert {row.get("kind") for row in read} <= {"turn"}  # only the turn summary admission needs; the request row is unread
    retry.close(text="answer")


@pytest.mark.slow
def test_other_conversations_wait_for_the_eviction_delete_but_not_its_planning(logged_api, monkeypatch):
    """Other conversations run while a pass plans; its bulk delete is a workspace write they wait for."""
    api, _ = logged_api
    api.assist("old question", conversation_id="conv-old", turn_id="turn-1")
    api.assist("idle question", conversation_id="conv-idle", turn_id="turn-1")
    recorder = api._assist_session_log
    log, ws_id = recorder.workspace(), str(api.ws_id or "")
    busy = recorder.begin_turn("busy question", conversation_id="conv-busy", turn_id="turn-1", ws_id=ws_id)
    later_busy = recorder.begin_turn("later question", conversation_id="conv-later", turn_id="turn-1", ws_id=ws_id)
    hb.CM_HVNB.set(f"{test_assist_session_log._ASSIST}.session_log_max_sessions", 4)  # conv-new evicts the idle, oldest conv-old
    planning, plan_resume, deleting, delete_resume = Event(), Event(), Event(), Event()
    real_targets, real_delete_many = recorder._retention_targets, log._crud.delete_many

    def held_targets(*args, **kwargs):
        planning.set()
        plan_resume.wait(_WAIT)
        return real_targets(*args, **kwargs)

    def held_storage_delete(targets, *args, **kwargs):
        # Inside Workspace.delete_many, under the workspace write lock, as a slow physical delete runs.
        targets = list(targets)
        if len(targets) > 1:  # the retention batch, not a single-row cleanup
            deleting.set()
            delete_resume.wait(_WAIT)
        return real_delete_many(targets, *args, **kwargs)

    def idle_turn():
        recorder.begin_turn("idle again", conversation_id="conv-idle", turn_id="turn-2", ws_id=ws_id).close(text="idle answer")

    monkeypatch.setattr(recorder, "_retention_targets", held_targets)
    monkeypatch.setattr(log._crud, "delete_many", held_storage_delete)
    with ThreadPoolExecutor(max_workers=4) as pool:
        admission = pool.submit(recorder.begin_turn, "new question", conversation_id="conv-new", turn_id="turn-1", ws_id=ws_id)
        try:
            assert planning.wait(_WAIT)
            evicted = pool.submit(recorder.begin_turn, "old again", conversation_id="conv-old", turn_id="turn-2", ws_id=ws_id)
            # While the pass reads and plans, other conversations seal and start turns as usual.
            pool.submit(busy.close, text="busy answer").result(timeout=_WAIT)
            pool.submit(idle_turn).result(timeout=_WAIT)
            plan_resume.set()
            assert deleting.wait(_WAIT)
            # Its bulk delete holds the workspace write lock, so another conversation's seal waits for it.
            sealing = pool.submit(later_busy.close, text="later answer")
            with pytest.raises(FutureTimeout):
                sealing.result(timeout=2)
            assert not evicted.done()  # a turn on the conversation being evicted waits for the pass
        finally:
            plan_resume.set()
            delete_resume.set()
        admission.result(timeout=_WAIT).close(text="new answer")
        sealing.result(timeout=_WAIT)
        later = evicted.result(timeout=_WAIT)
    assert busy.capture == "complete" and later_busy.capture == "complete"
    assert later._attempt == 1  # it starts as a new conversation: the evicted turn row is gone
    later.close(text="old answer again")
    assert not recorder._active and not recorder._writes.running


@pytest.mark.slow
def test_rebuild_keeps_the_usage_the_appends_merged(logged_api):
    """Numbers add up; any other usage value keeps the first non-empty one, live or rebuilt."""
    api, _ = logged_api
    api.assist("warm up", conversation_id="conv-warm", turn_id="turn-0")
    recorder = api._assist_session_log
    log, ws_id = recorder.workspace(), str(api.ws_id or "")
    usages = [
        {"total_tokens": 30, "prompt_tokens_details": {"cached_tokens": 10}, "model": "chat-a"},
        {"total_tokens": 40, "prompt_tokens_details": {"cached_tokens": 25}, "model": "chat-b"},
    ]
    for index, usage in enumerate(usages):
        recorder.begin_turn(f"q{index}", conversation_id="conv-usage", turn_id=f"turn-{index}", ws_id=ws_id).close(text=f"a{index}", usage=usage)
    appended = dict(_session(recorder, "conv-usage").usage)
    assert appended == {"total_tokens": 70, "prompt_tokens_details": {"cached_tokens": 10}, "model": "chat-a"}
    assert dict(recorder.rebuild_session("conv-usage").usage) == appended
    assert dict(_session(recorder, "conv-usage").usage) == appended
    assert log is recorder.workspace()


@pytest.mark.slow
def test_rebuild_adds_a_usage_count_that_follows_a_null_count(logged_api):
    """A count reported as null and later as a number is the number, live and rebuilt."""
    api, _ = logged_api
    api.assist("warm up", conversation_id="conv-warm", turn_id="turn-0")
    recorder = api._assist_session_log
    ws_id = str(api.ws_id or "")
    usages = [{"total_tokens": None, "model": "chat-a"}, {"total_tokens": 40, "model": "chat-b"}, {"total_tokens": 2}]
    for index, usage in enumerate(usages):
        recorder.begin_turn(f"q{index}", conversation_id="conv-null", turn_id=f"turn-{index}", ws_id=ws_id).close(text=f"a{index}", usage=usage)
    appended = dict(_session(recorder, "conv-null").usage)
    assert appended == {"total_tokens": 42, "model": "chat-a"}
    assert dict(recorder.rebuild_session("conv-null").usage) == appended


@pytest.mark.slow
@pytest.mark.parametrize("projection", ["live", "rebuild", "repair"])
def test_blank_answer_counts_usage_without_adding_messages(logged_api, monkeypatch, projection):
    """A blank successful turn contributes its provider usage exactly once."""
    api, _ = logged_api
    turn_usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}

    def answer_with_usage(text):
        def mock_chat(spec, messages, stream=False, **kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": dict(turn_usage)}

        return LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False, stream_chat=False)

    conversation = "deliveries"
    question = "Any corrections?"
    assert api.assist("Were the deliveries checked?", conversation_id=conversation, turn_id="checked", llm=answer_with_usage("Yes."))["text"] == "Yes."
    recorder = api._assist_session_log
    workspace = recorder.workspace()
    expected_ids = _session(recorder, conversation).message_ids

    def refuse_projection(*args, **kwargs):
        raise RuntimeError("projection write interrupted")

    with monkeypatch.context() as patch:
        if projection == "repair":
            patch.setattr(hb.Session, "append_messages", refuse_projection)
        assert api.assist(question, conversation_id=conversation, turn_id="corrections", llm=answer_with_usage(""))["text"] == ""

    trace = recorder._entity("agent-trace")
    turn_rows = [row["payload"] for row in workspace.rows(trace) if row["conversation_id"] == conversation and row["kind"] == "turn"]
    assert len(turn_rows) == 2
    assert all(not payload.get("error") and payload["usage"] == turn_usage for payload in turn_rows)
    if projection == "rebuild":
        recorder.rebuild_session(conversation)
    elif projection == "repair":
        assert api.assist(question, conversation_id=conversation, turn_id="corrections")["text"] == ""

    expected_usage = {key: 2 * count for key, count in turn_usage.items()}
    session = _session(recorder, conversation)
    assert session.usage == expected_usage
    assert session.message_ids == expected_ids
    assert session.turn_count == 1
    assert _transcript(recorder, conversation) == ["Were the deliveries checked?", "Yes."]
    assert api.assist(question, conversation_id=conversation, turn_id="corrections")["text"] == ""
    assert _session(recorder, conversation).usage == expected_usage
