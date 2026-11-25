"""Durable assistant session log: write-ahead trace rows, projection, doctrine."""

import re
import warnings

import pytest

import heavenbase as hb
from heavenbase.api.assist_log import AssistSessionLog
from heavenbase.api.service import DashboardAPI
import heavenbase.extensions.agent_log as agent_log_package
from heavenbase.llm.trace import LLMTraceEvent
from heavenbase.utils import path_to_file_uri
from heavenbase.workspace.registry import WorkspaceSpec

pytestmark = pytest.mark.fast

_ASSIST = "heavenbase.dashboard.assistant"


def _investigating_mock(rounds: int = 1):
    """Return a mock provider that runs ``rounds`` tool rounds, then answers."""

    def mock_chat(spec, messages, stream=False, **kwargs):
        done = sum(message.get("role") == "tool" for message in messages)
        if done >= rounds or "tools" not in kwargs:
            payload = {"role": "assistant", "content": f"answer after {done} rounds"}
        else:
            call = {"id": f"c{done}", "type": "function", "function": {"name": "list_entities", "arguments": "{}"}}
            payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        if stream:
            delta = dict(payload)
            if "tool_calls" in payload:
                delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([{"choices": [{"delta": delta}], "usage": {"total_tokens": 3}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 3}}

    return mock_chat


@pytest.fixture()
def logged_api(monkeypatch, tmp_path):
    """DashboardAPI with session logging on, a mock provider, and full cleanup."""
    import heavenbase.llm as llm_module

    suffix = re.sub(r"[^a-z0-9]", "", tmp_path.name.lower())[-8:] or "x"
    log_ws = f"assist-log-{suffix}"
    data_ws = f"assist-data-{suffix}"
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_workspace", log_ws)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "sqlite", "name": "main", "database": path_to_file_uri(tmp_path / "session-log.db")}})
    real_llm = llm_module.LLM
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: real_llm(preset="mock", mock_chat=_investigating_mock(), mock_pause=(0, 0), cache=False))
    ws = hb.HeavenBase(data_ws, backends={"main": {"type": "inmem"}})
    WorkspaceSpec.register(data_ws, config={"backends": {"main": {"type": "inmem"}}})
    api = DashboardAPI()
    api.open(data_ws)
    try:
        yield api, log_ws
    finally:
        for key in ("session_log", "session_log_workspace", "session_log_backend", "session_log_max_sessions"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")
        for name in (log_ws, data_ws):
            try:
                hb.HeavenBase.drop(name)
            except Exception:
                pass
            try:
                WorkspaceSpec.unregister(name)
            except Exception:
                pass
        del ws


def _log_rows(log_ws):
    log = hb.HeavenBase(log_ws)
    return log, log.rows(agent_log_package.AgentTrace), log.rows(hb.Session)


@pytest.mark.slow
def test_assist_logs_write_ahead_trace_rows_and_projection(logged_api):
    api, log_ws = logged_api
    reply = api.assist("what entities exist?", conversation_id="conv-A", turn_id="turn-1")
    assert reply["text"] == "answer after 1 rounds"
    log, traces, sessions = _log_rows(log_ws)
    assert {"agent", "agent-log"} <= set(log.extensions())  # the log workspace opts into the trace entity explicitly
    coords = sorted((row["kind"], row["seq"], row["call"]) for row in traces)
    assert coords == [("request", 0, "round"), ("request", 1, "round"), ("response", 0, "round"), ("response", 1, "round"), ("turn", 1, "")]
    request = next(row for row in traces if row["kind"] == "request" and row["seq"] == 0)
    assert request["payload"]["messages"][0]["role"] == "system"  # the exact assembled request head is on record
    assert request["payload"]["messages"][-1]["content"] == "what entities exist?"
    assert any(tool["function"]["name"] == "list_entities" for tool in request["payload"]["tools"])  # the offered tool surface rides along
    second = next(row for row in traces if row["kind"] == "request" and row["seq"] == 1)
    assert any(message.get("role") == "tool" for message in second["payload"]["messages"])  # each round's provider view is its own row
    turn = next(row for row in traces if row["kind"] == "turn")
    assert turn["payload"]["capture"] == "complete" and turn["payload"]["question"] == "what entities exist?"
    assert turn["payload"]["usage"] if "usage" in turn["payload"] else True
    session = hb.Session.load(sessions[0]["object_id"], ws=log)
    assert [(m["role"], m["content"]) for m in session.messages(ws=log)] == [
        ("user", "what entities exist?"),
        ("assistant", "answer after 1 rounds"),
    ]  # the readable projection mirrors the finished turn


@pytest.mark.slow
def test_assist_retry_with_same_turn_id_replays_without_provider_io(logged_api, monkeypatch):
    api, log_ws = logged_api
    first = api.assist("q1", conversation_id="conv-A", turn_id="turn-1")
    _, before, _ = _log_rows(log_ws)
    import heavenbase.llm as llm_module

    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: (_ for _ in ()).throw(AssertionError("completed replay reached provider construction")))
    replay = api.assist("q1", conversation_id="conv-A", turn_id="turn-1")  # e.g. a client retry after a dropped response
    streamed = list(api.assist_stream("q1", conversation_id="conv-A", turn_id="turn-1"))
    log, after, sessions = _log_rows(log_ws)
    assert replay["text"] == first["text"] and streamed == [{"kind": "done", **replay}]
    assert len(after) == len(before)  # deterministic event ids make the retry a no-op
    session = hb.Session.load(sessions[0]["object_id"], ws=log)
    assert len(session.messages(ws=log)) == 2  # the projection did not double either
    with pytest.raises(ValueError, match="different question or workspace"):
        api.assist("different question", conversation_id="conv-A", turn_id="turn-1")


@pytest.mark.slow
@pytest.mark.parametrize("stream_first", [False, True], ids=["blocking-first", "streaming-first"])
def test_assist_retry_without_conversation_id_reuses_trace_and_transcript(logged_api, monkeypatch, stream_first):
    api, log_ws = logged_api
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_max_sessions", 2)
    api.assist("other conversation", conversation_id="other", turn_id="unrelated")
    turn_id = "retry-" + "x" * 100  # Same identity even after storage normalization.
    if stream_first:
        first = list(api.assist_stream("q1", turn_id=turn_id))[-1]
        assert first["kind"] == "done"
    else:
        first = api.assist("q1", turn_id=turn_id)
    _, before, sessions_before = _log_rows(log_ws)
    assert len(sessions_before) == 2
    original = next(row for row in sessions_before if row["state"]["conversation_id"] != "other")
    _forbid_provider(monkeypatch)
    api._assist_session_log = None  # A fresh recorder must find the persisted turn.
    replay = api.assist("q1", turn_id=turn_id)
    assert replay["text"] == first["text"]
    assert list(api.assist_stream("q1", turn_id=turn_id)) == [{"kind": "done", **replay}]
    log, after, sessions_after = _log_rows(log_ws)
    assert {row["object_id"] for row in before} == {row["object_id"] for row in after}
    assert {row["object_id"] for row in sessions_before} == {row["object_id"] for row in sessions_after}
    session = hb.Session.load(original["object_id"], ws=log)
    assert [message["content"] for message in session.messages(ws=log)] == ["q1", first["text"]]


@pytest.mark.slow
def test_assist_explicit_off_touches_no_workspace(monkeypatch, tmp_path):
    import heavenbase.llm as llm_module

    data_ws = "assist-plain-" + (re.sub(r"[^a-z0-9]", "", tmp_path.name.lower())[-8:] or "x")
    real_llm = llm_module.LLM
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: real_llm(preset="mock", mock_chat=_investigating_mock(), mock_pause=(0, 0), cache=False))
    hb.HeavenBase(data_ws, backends={"main": {"type": "inmem"}})
    WorkspaceSpec.register(data_ws, config={"backends": {"main": {"type": "inmem"}}})
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", False)  # the single opt-out key disables the whole subsystem
    api = DashboardAPI()
    api.open(data_ws)
    try:
        reply = api.assist("hello", conversation_id="conv-A", turn_id="turn-1")
        assert reply["text"]
        assert api._assist_session_log is None  # the recorder is never constructed
        assert "assistant-log" not in {spec.id for spec in WorkspaceSpec.list()}  # and no log workspace appears
    finally:
        hb.CM_HVNB.unset(f"{_ASSIST}.session_log")
        hb.HeavenBase.drop(data_ws)
        WorkspaceSpec.unregister(data_ws)


@pytest.mark.slow
def test_assist_default_off_on_a_pristine_context_and_opt_in_records(monkeypatch, tmp_path):
    """Stock config never logs; the single `session_log: true` key opts a deployment in."""
    import heavenbase.llm as llm_module
    from heavenbase.utils.config import ConfigManager
    from heavenbase.utils import path_to_file_uri as to_uri
    from heavenbase.context.registry import HeavenBaseRegistryStore

    real_llm = llm_module.LLM
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: real_llm(preset="mock", mock_chat=_investigating_mock(), mock_pause=(0, 0), cache=False))
    # A pristine ConfigManager over its own registry: no test-session layers,
    # no tombstones — exactly what a stock install resolves.
    backend = hb.backends.SQLiteBackend("stock-config", to_uri(tmp_path / "config.db"), ws_id="stock-config")
    registry = hb.Registry.load("stock-config", store=HeavenBaseRegistryStore(backend))
    cm = ConfigManager(package="heavenbase", scope="heavenbase", root=str(tmp_path), registry=registry, refresh_interval=999.0)
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=cm,
    )
    try:
        assert AssistSessionLog.enabled(cm) is False  # no key set anywhere: the packaged default is OFF
        api = DashboardAPI(context=context)
        reply = api.assist("stock question", conversation_id="conv-stock", turn_id="turn-1")
        assert reply["text"]
        assert api._assist_session_log is None  # the recorder is never constructed
        assert "assistant-log" not in {spec.id for spec in WorkspaceSpec.list(context=context)}  # and no log workspace appears
        cm.set(f"{_ASSIST}.session_log", True)
        cm.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "sqlite", "name": "main", "database": to_uri(tmp_path / "session-log.db")}})
        assert AssistSessionLog.enabled(cm) is True
        reply = api.assist("logged question", conversation_id="conv-stock", turn_id="turn-2")
        assert reply["text"]
        recorder = api._assist_session_log
        log = recorder.workspace()  # the stock assistant-log workspace inside this pristine context
        trace_entity = recorder._entity("agent-trace")
        rows = [row for row in log.rows(trace_entity) if row.get("conversation_id") == "conv-stock"]
        assert {row["kind"] for row in rows} >= {"turn"}  # the opted-in turn landed durably
    finally:
        context.close()


@pytest.mark.slow
def test_assist_recorder_failure_never_fails_the_turn(logged_api, monkeypatch):
    api, log_ws = logged_api
    from heavenbase.api import assist_log as assist_log_module

    monkeypatch.setattr(assist_log_module.AssistSessionLog, "workspace", lambda self: (_ for _ in ()).throw(RuntimeError("log store down")))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reply = api.assist("still answered?", conversation_id="conv-A", turn_id="turn-1")
    assert reply["text"] == "answer after 1 rounds"  # the question is never hostage to the log
    assert any("session log" in str(item.message) for item in caught)


def test_validate_refuses_credentials_at_rest():
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "sqlite", "name": "main", "database": "file://%/assistant/x.db"}})
    hb.CM_HVNB.set(f"{_ASSIST}.share_credentials", True)
    try:
        with pytest.raises(ValueError, match="credentials at rest"):
            AssistSessionLog.validate(hb.CM_HVNB)
        hb.CM_HVNB.set(f"{_ASSIST}.session_log_allow_credentials", True)
        AssistSessionLog.validate(hb.CM_HVNB)  # the explicit three-key opt-in is accepted
    finally:
        for key in ("session_log", "session_log_backend", "share_credentials", "session_log_allow_credentials"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")


def test_validate_refuses_non_durable_backends():
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    try:
        hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "inmem"}})
        with pytest.raises(ValueError, match="in-memory"):
            AssistSessionLog.validate(hb.CM_HVNB)
        hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "sqlite", "name": "main", "database": ":memory:"}})
        with pytest.raises(ValueError, match="memory"):
            AssistSessionLog.validate(hb.CM_HVNB)
        hb.CM_HVNB.unset(f"{_ASSIST}.session_log_backend")
        with pytest.raises(ValueError, match="declare"):
            AssistSessionLog.validate(hb.CM_HVNB)
    finally:
        for key in ("session_log", "session_log_backend"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")


@pytest.mark.slow
def test_session_cap_evicts_oldest_conversation_with_all_rows(logged_api):
    api, log_ws = logged_api
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_max_sessions", 1)
    api.assist("first conversation", conversation_id="conv-A", turn_id="turn-1")
    api.assist("second conversation", conversation_id="conv-B", turn_id="turn-1")
    log, traces, sessions = _log_rows(log_ws)
    assert [row["title"] for row in sessions] == ["second conversation"]  # admission evicted the oldest whole session
    assert {row["conversation_id"] for row in traces} == {"conv-B"}  # together with its trace rows
    assert not [row for row in log.rows(hb.Message) if row["session_id"] not in {sessions[0]["object_id"]}]  # and its message rows


@pytest.mark.slow
def test_replay_compaction_call_is_traced(logged_api, monkeypatch):
    api, log_ws = logged_api
    hb.CM_HVNB.set(f"{_ASSIST}.history_chars", 200)  # force the measured path over a tiny threshold
    try:
        history = [
            {"role": "user", "content": "old question " + "x" * 200},
            {"role": "assistant", "content": "old answer " + "y" * 200},
            {"role": "user", "content": "newer question " + "z" * 200},
            {"role": "assistant", "content": "newer answer " + "w" * 200},
        ]
        api.assist("current?", history=history, conversation_id="conv-A", turn_id="turn-9")
        _, traces, _ = _log_rows(log_ws)
        replay = [row for row in traces if row["call"] == "replay_compaction" and row["turn_id"] == "turn-9"]
        assert {row["kind"] for row in replay} == {"request", "response"}  # the summary model call is part of the turn's record
        summary_request = next(row for row in replay if row["kind"] == "request")
        assert summary_request["payload"]["messages"][0]["content"].startswith("Compress the earlier turns")
    finally:
        hb.CM_HVNB.unset(f"{_ASSIST}.history_chars")


@pytest.mark.slow
def test_assist_stream_logs_the_turn_and_survives_disconnect(logged_api):
    api, log_ws = logged_api
    events = list(api.assist_stream("stream me", conversation_id="conv-S", turn_id="turn-1"))
    assert events[-1]["kind"] == "done" and events[-1]["text"] == "answer after 1 rounds"
    assert "capture_warning" not in events[-1]  # a healthy recording adds no warning
    _, traces, _ = _log_rows(log_ws)
    stream_rows = sorted((row["kind"], row["seq"]) for row in traces if row["turn_id"] == "turn-1")
    assert stream_rows == [("request", 0), ("request", 1), ("response", 0), ("response", 1), ("turn", 1)]
    finished = next(row for row in traces if row["kind"] == "turn" and row["turn_id"] == "turn-1")
    assert "error" not in finished["payload"]  # a finished stream closes cleanly

    # A client disconnect tears the generator down mid-stream: the write-ahead
    # request row is already durable and the turn row records the interruption.
    stream = api.assist_stream("cut me off", conversation_id="conv-S", turn_id="turn-2")
    next(stream)
    stream.close()
    _, traces, _ = _log_rows(log_ws)
    cut = {row["kind"]: row for row in traces if row["turn_id"] == "turn-2"}
    assert "request" in cut  # the provider-bound request survived the disconnect
    assert cut["turn"]["payload"]["error"].startswith("interrupted")


def test_assist_request_models_accept_conversation_identity():
    from heavenbase.api.models import AssistRequest, AssistStreamRequest

    for model in (AssistRequest, AssistStreamRequest):
        parsed = model.model_validate({"question": "q", "conversation_id": "conv-A", "turn_id": "turn-1"})
        assert parsed.conversation_id == "conv-A" and parsed.turn_id == "turn-1"
        assert model.model_validate({"question": "q"}).conversation_id is None  # old clients stay valid


@pytest.mark.slow
def test_log_reconstructs_every_provider_view_byte_for_byte(monkeypatch, tmp_path):
    """D4 acceptance: each provider call is rebuildable from its request row alone."""
    import heavenbase.llm as llm_module

    suffix = re.sub(r"[^a-z0-9]", "", tmp_path.name.lower())[-8:] or "x"
    log_ws, data_ws = f"assist-inv-{suffix}", f"assist-invd-{suffix}"
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_workspace", log_ws)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "sqlite", "name": "main", "database": path_to_file_uri(tmp_path / "inv.db")}})
    hb.CM_HVNB.set(f"{_ASSIST}.compact_ratio", 0.001)  # force repeated in-turn compaction
    provider_views = []

    def observing_mock(spec, messages, stream=False, **kwargs):
        provider_views.append(([dict(message) for message in messages], [dict(tool) for tool in kwargs.get("tools") or []]))
        first = str((messages[0] or {}).get("content") or "")
        done = sum(message.get("role") == "tool" for message in messages)
        if first.startswith("Compress this turn's"):
            payload = {"role": "assistant", "content": "MEMO: folded"}
        elif done >= 3 or "tools" not in kwargs:
            payload = {"role": "assistant", "content": "final"}
        else:
            call = {
                "id": f"c{len(provider_views)}",
                "type": "function",
                "function": {"name": "sample_rows", "arguments": '{"entity_id":"agent-session","limit":2}'},
            }
            payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        if stream:
            delta = dict(payload)
            if "tool_calls" in payload:
                delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([{"choices": [{"delta": delta}], "usage": {"total_tokens": 500}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 500}}

    real_llm = llm_module.LLM
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: real_llm(preset="mock", mock_chat=observing_mock, mock_pause=(0, 0), cache=False))
    hb.HeavenBase(data_ws, backends={"main": {"type": "inmem"}})
    WorkspaceSpec.register(data_ws, config={"backends": {"main": {"type": "inmem"}}})
    api = DashboardAPI()
    api.open(data_ws)
    try:
        api.assist("dig deep", conversation_id="conv-I", turn_id="turn-1")
        _, traces, _ = _log_rows(log_ws)
        requests = sorted((row for row in traces if row["kind"] == "request"), key=lambda row: row["seq"])
        assert len(requests) == len(provider_views)  # every provider call has its write-ahead row — none missing, none extra
        assert {row["call"] for row in requests} >= {"round", "turn_compaction"}  # incl. the compaction summaries themselves
        for row, (seen_messages, seen_tools) in zip(requests, provider_views):
            assert row["payload"]["messages"] == seen_messages  # the provider view rebuilds from the log row alone, byte for byte
            assert row["payload"]["tools"] == seen_tools  # including the exact tool surface offered on that call
        compacted = [row for row in requests if any("[compacted working memory" in str(m.get("content") or "") for m in row["payload"]["messages"])]
        assert compacted  # the post-compaction provider views — v1's blind spot — are first-class rows
    finally:
        for key in ("session_log", "session_log_workspace", "session_log_backend", "compact_ratio"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")
        for name in (log_ws, data_ws):
            try:
                hb.HeavenBase.drop(name)
            except Exception:
                pass
            try:
                WorkspaceSpec.unregister(name)
            except Exception:
                pass


@pytest.mark.slow
def test_rebuild_session_recovers_the_projection_from_trace_rows(logged_api):
    api, log_ws = logged_api
    api.assist("first", conversation_id="conv-R", turn_id="turn-1")
    api.assist(
        "second",
        history=[{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer after 1 rounds"}],
        conversation_id="conv-R",
        turn_id="turn-2",
    )
    recorder = api._assist_session_log
    log = recorder.workspace()
    session = recorder._open_session(log, "conv-R", "first")
    session.set_messages([], source="damage", ws=log)  # simulate a damaged projection
    assert session.messages(ws=log) == []
    rebuilt = recorder.rebuild_session("conv-R")
    assert [(m["role"], m["content"]) for m in rebuilt.messages(ws=log)] == [
        ("user", "first"),
        ("assistant", "answer after 1 rounds"),
        ("user", "second"),
        ("assistant", "answer after 1 rounds"),
    ]  # the trace rows alone recover the readable transcript
    with pytest.raises(ValueError, match="no trace rows"):
        recorder.rebuild_session("conv-unknown")


def _conversation_trace_count(log_ws, conversation_id):
    _, traces, _ = _log_rows(log_ws)
    return sum(row["conversation_id"] == conversation_id for row in traces)


@pytest.mark.slow
def test_rebuild_after_a_lost_projection_row_keeps_the_trace(logged_api):
    """Rebuilding a conversation whose session row is gone recreates the row and deletes no trace row."""
    api, log_ws = logged_api
    api.assist("q1", conversation_id="conv-L", turn_id="turn-1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    before = _conversation_trace_count(log_ws, "conv-L")
    session = recorder._open_session(log, "conv-L", "q1")
    started = session.state["started_at"]
    log.delete_many([(recorder._entity("agent-session"), session.object_id)])  # the projection row is lost
    rebuilt = recorder.rebuild_session("conv-L")
    assert [(m["role"], m["content"]) for m in rebuilt.messages(ws=log)] == [("user", "q1"), ("assistant", "answer after 1 rounds")]
    assert _conversation_trace_count(log_ws, "conv-L") == before  # the authority survived its own recovery
    _, traces, _ = _log_rows(log_ws)
    closed = next(row["payload"]["closed_at"] for row in traces if row["conversation_id"] == "conv-L" and row["kind"] == "turn")
    assert started <= rebuilt.state["started_at"] == closed  # the restored row keeps the conversation's age, dated from its first turn
    again = recorder.rebuild_session("conv-L")  # and stays rebuildable
    assert [m["content"] for m in again.messages(ws=log)] == ["q1", "answer after 1 rounds"]
    assert not recorder._active  # the rebuild left no retention pin behind


@pytest.mark.slow
def test_rebuild_at_the_cap_deletes_no_conversation_and_the_next_admission_evicts_by_age(logged_api):
    """Restoring a row admits no conversation: the rebuild deletes no conversation, the next admission evicts the oldest."""
    api, log_ws = logged_api
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_max_sessions", 2)
    api.assist("q1", conversation_id="c1", turn_id="turn-1")
    api.assist("q2", conversation_id="c2", turn_id="turn-1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    log.delete_many([(recorder._entity("agent-session"), recorder._open_session(log, "c1", "q1").object_id)])  # c1's row is lost
    api.assist("q3", conversation_id="c3", turn_id="turn-1")  # c1 is not counted, so the cap of 2 still holds c2 and c3
    before = {name: _conversation_trace_count(log_ws, name) for name in ("c1", "c2", "c3")}
    assert all(before.values())

    rebuilt = recorder.rebuild_session("c1")
    _, traces, sessions = _log_rows(log_ws)
    assert {name: _conversation_trace_count(log_ws, name) for name in ("c1", "c2", "c3")} == before  # the rebuild deleted no conversation
    assert sorted(row["state"]["conversation_id"] for row in sessions) == ["c1", "c2", "c3"]  # one over the cap until the next admission
    ages = {row["state"]["conversation_id"]: row["state"]["started_at"] for row in sessions}
    assert rebuilt.state["started_at"] == ages["c1"] < ages["c2"] < ages["c3"]  # the restored conversation keeps its age

    api.assist("q4", conversation_id="c4", turn_id="turn-1")
    _, traces, sessions = _log_rows(log_ws)
    assert {row["conversation_id"] for row in traces} == {"c3", "c4"}  # the next admission evicted the two oldest, restored c1 included
    assert sorted(row["state"]["conversation_id"] for row in sessions) == ["c3", "c4"]


@pytest.mark.slow
def test_each_rebuild_can_add_one_conversation_over_the_cap(logged_api):
    """Two rebuilt conversations put the log two over the cap; the next admission evicts back to it."""
    api, log_ws = logged_api
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_max_sessions", 2)
    api.assist("q1", conversation_id="c1", turn_id="turn-1")
    api.assist("q2", conversation_id="c2", turn_id="turn-1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    Session = recorder._entity("agent-session")
    log.delete_many([(Session, recorder._open_session(log, "c1", "q1").object_id)])
    api.assist("q3", conversation_id="c3", turn_id="turn-1")
    log.delete_many([(Session, recorder._open_session(log, "c2", "q2").object_id)])
    api.assist("q4", conversation_id="c4", turn_id="turn-1")

    recorder.rebuild_session("c1")
    recorder.rebuild_session("c2")
    _, traces, sessions = _log_rows(log_ws)
    assert sorted(row["state"]["conversation_id"] for row in sessions) == ["c1", "c2", "c3", "c4"]  # the cap plus two
    assert {row["conversation_id"] for row in traces} == {"c1", "c2", "c3", "c4"}

    api.assist("q5", conversation_id="c5", turn_id="turn-1")
    _, traces, sessions = _log_rows(log_ws)
    assert sorted(row["state"]["conversation_id"] for row in sessions) == ["c4", "c5"]  # back to the cap
    assert {row["conversation_id"] for row in traces} == {"c4", "c5"}


@pytest.mark.slow
def test_rebuild_replaces_only_its_own_message_rows(logged_api):
    """A rebuild deletes the conversation's stale message rows and no other conversation's rows."""
    api, log_ws = logged_api
    api.assist("q1", conversation_id="c1", turn_id="turn-1")
    api.assist("q2", conversation_id="c2", turn_id="turn-1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    session = recorder._open_session(log, "c1", "q1")
    session.set_messages([{"role": "user", "content": f"stale {n}"} for n in range(3)], ws=log)
    stale = set(recorder._open_session(log, "c1", "q1").message_ids)
    other = set(recorder._open_session(log, "c2", "q2").message_ids)
    traces_before = len(_log_rows(log_ws)[1])

    rebuilt = recorder.rebuild_session("c1")
    message_ids = {row["object_id"] for row in log.query(recorder._entity("agent-message")).select("object_id").execute().rows()}
    assert stale and not stale & message_ids  # the conversation's own stale message rows were replaced
    assert other <= message_ids  # another conversation's message rows are untouched
    assert len(_log_rows(log_ws)[1]) == traces_before  # no trace row was deleted
    assert [m["content"] for m in rebuilt.messages(ws=log)] == ["q1", "answer after 1 rounds"]


@pytest.mark.slow
@pytest.mark.parametrize("damage", ["deleted", "source"])
def test_admission_never_deletes_the_trace_of_a_conversation_under_the_cap(logged_api, damage):
    """A missing or re-labelled projection row does not make a conversation's trace rows garbage."""
    api, log_ws = logged_api
    api.assist("q1", conversation_id="conv-D", turn_id="turn-1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    before = _conversation_trace_count(log_ws, "conv-D")
    session = recorder._open_session(log, "conv-D", "q1")
    if damage == "deleted":
        log.delete_many([(recorder._entity("agent-session"), session.object_id)])
    else:
        session.set_messages([], source="damage", ws=log)  # the damage the shipped rebuild test applies
    api.assist("newcomer", conversation_id="conv-N", turn_id="turn-1")  # admission runs the retention pass (default cap 256)
    assert _conversation_trace_count(log_ws, "conv-D") == before
    rebuilt = recorder.rebuild_session("conv-D")
    assert [m["content"] for m in rebuilt.messages(ws=log)] == ["q1", "answer after 1 rounds"]


@pytest.mark.slow
def test_a_relabelled_projection_still_counts_toward_the_cap(logged_api):
    """Retention identifies its projection rows by id, so a changed source neither hides nor orphans one."""
    api, log_ws = logged_api
    api.assist("old", conversation_id="conv-1", turn_id="turn-1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    recorder._open_session(log, "conv-1", "old").set_messages([], source="damage", ws=log)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_max_sessions", 1)
    api.assist("new", conversation_id="conv-2", turn_id="turn-1")
    _, traces, sessions = _log_rows(log_ws)
    assert {row["conversation_id"] for row in traces} == {"conv-2"}  # the oldest whole session went, trace rows included
    assert [row["title"] for row in sessions] == ["new"]


@pytest.mark.slow
def test_oversize_conversation_id_still_logs_under_a_stable_hash(logged_api):
    api, log_ws = logged_api
    long_id = "c" * 80  # legal per the 128-char protocol bound, over the 63-char storage bound
    reply = api.assist("normalize me", conversation_id=long_id, turn_id="t" * 80)
    assert "capture_warning" not in reply  # nothing was silently dropped
    _, traces, sessions = _log_rows(log_ws)
    assert traces and sessions  # the turn logged in full under the hash-normalized identity
    assert {row["conversation_id"] for row in traces} != {long_id}  # stored under the stable hash, not the oversize literal
    reply2 = api.assist("normalize me", conversation_id=long_id, turn_id="t" * 80)  # same client ids: still an idempotent replay
    assert reply2["text"]
    _, after, _ = _log_rows(log_ws)
    assert len(after) == len(traces)


@pytest.mark.slow
def test_non_duplicate_write_failures_mark_capture(logged_api):
    api, log_ws = logged_api
    recorder = api._assist_session_log or api._assist_turn_log("warm", None, None, "").__class__  # ensure recorder exists
    api.assist("warm up", conversation_id="conv-V", turn_id="turn-0")
    recorder = api._assist_session_log
    turn = recorder.begin_turn("q", conversation_id="conv-V", turn_id="turn-1")

    class BrokenWS:
        def upsert(self, *args, **kwargs):
            raise ValueError("Identifier exceeds 63 characters")  # a validation error, NOT a duplicate

        def get(self, *args, **kwargs):
            return None

    turn._ws = BrokenWS()
    turn._persist_event(LLMTraceEvent(kind="request", call="round", seq=0, payload={}))
    assert turn.capture.startswith("incomplete:")  # validation failures are never mistaken for idempotent replays
    turn.close(text="answer")  # and close still never raises


@pytest.mark.slow
def test_streaming_fallback_retry_supersedes_the_interrupted_turn(logged_api):
    api, log_ws = logged_api
    stream = api.assist_stream("flaky question", conversation_id="conv-F", turn_id="turn-1")
    next(stream)
    stream.close()  # transport died mid-turn: a provisional interrupted turn row lands
    _, traces, _ = _log_rows(log_ws)
    provisional = next(row for row in traces if row["kind"] == "turn" and row["turn_id"] == "turn-1")
    assert provisional["payload"]["error"].startswith("interrupted")
    reply = api.assist("flaky question", conversation_id="conv-F", turn_id="turn-1")  # the GUI fallback reuses the turn id
    assert reply["text"] == "answer after 1 rounds"
    log, traces, sessions = _log_rows(log_ws)
    final = next(row for row in traces if row["kind"] == "turn" and row["turn_id"] == "turn-1")
    assert "error" not in final["payload"] and final["payload"]["text"] == reply["text"]  # the answer the user got is the record
    assert final["payload"]["superseded"].startswith("interrupted") and final["payload"]["attempt"] == 2
    attempt_two = [row for row in traces if row["turn_id"] == "turn-1-a2"]
    assert attempt_two  # the retry's provider calls live in their own attempt namespace — attempts never interleave
    session = hb.Session.load(sessions[0]["object_id"], ws=log)
    assert [m["content"] for m in session.messages(ws=log)][-1] == reply["text"]  # and the transcript carries the turn


@pytest.mark.slow
@pytest.mark.parametrize("mismatch", ["question", "workspace"])
def test_assist_retry_rejects_a_different_identity_for_a_failed_turn(logged_api, monkeypatch, mismatch):
    import heavenbase.llm as llm_module

    api, log_ws = logged_api
    provider = llm_module.LLM

    def unavailable(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_module, "LLM", unavailable)
    identity = {"conversation_id": "failed", "turn_id": "turn-1"}
    question = "original question"
    with pytest.raises(RuntimeError, match="provider down"):
        api.assist(question, **identity)
    _, before, _ = _log_rows(log_ws)
    failed = next(row for row in before if row["kind"] == "turn")
    assert failed["payload"]["question"] == question and "provider down" in failed["payload"]["error"]

    _forbid_provider(monkeypatch)
    original_ws = api.ws_id
    if mismatch == "workspace":
        monkeypatch.setattr(api, "ws_id", "another-workspace")
    retry_question = "different question" if mismatch == "question" else question
    with pytest.raises(ValueError, match="different question or workspace"):
        api.assist(retry_question, **identity)
    (stream_error,) = api.assist_stream(retry_question, **identity)
    assert stream_error["kind"] == "error" and "different question or workspace" in stream_error["text"]
    _, after, _ = _log_rows(log_ws)
    assert after == before

    monkeypatch.setattr(api, "ws_id", original_ws)
    monkeypatch.setattr(llm_module, "LLM", provider)
    reply = api.assist(question, **identity)
    log, traces, sessions = _log_rows(log_ws)
    sealed = next(row for row in traces if row["kind"] == "turn")
    assert sealed["payload"]["attempt"] == 2
    assert sealed["payload"]["superseded"] == failed["payload"]["error"]
    assert "error" not in sealed["payload"]
    session = hb.Session.load(sessions[0]["object_id"], ws=log)
    assert [message["content"] for message in session.messages(ws=log)] == [question, reply["text"]]


@pytest.mark.slow
def test_a_waiting_retry_cannot_supersede_a_different_failed_turn(logged_api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from test_assist_log_consistency import _observe_wait

    api, log_ws = logged_api
    recorder = AssistSessionLog(api.cm, context=api.context)
    identity = {"conversation_id": "failed", "turn_id": "turn-1", "ws_id": str(api.ws_id or "")}
    original = recorder.begin_turn("original question", **identity)
    waiting = _observe_wait(recorder, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(recorder.begin_turn, "different question", **identity)
        try:
            assert waiting.wait(30)
        finally:
            original.close(error="provider down")
        with pytest.raises(ValueError, match="different question or workspace"):
            retry.result(timeout=30)
    _, traces, _ = _log_rows(log_ws)
    recorded = next(row for row in traces if row["kind"] == "turn")
    assert recorded["payload"]["question"] == "original question"
    assert recorded["payload"]["error"] == "provider down"


@pytest.mark.slow
def test_retry_after_unsealed_request_uses_fresh_attempt_namespace(logged_api):
    """A hard interruption with no turn row cannot make the retry's provider calls disappear."""
    api, log_ws = logged_api
    restarted = AssistSessionLog(api.cm, context=api.context)
    ws = restarted.workspace()
    trace = restarted._entity("agent-trace")
    ws.upsert(trace, trace.event("conv-C", "turn-1", 0, "request", ws_id=str(api.ws_id or ""), payload={"marker": "attempt-one"}))
    second = restarted.begin_turn("crash window", conversation_id="conv-C", turn_id="turn-1", ws_id=str(api.ws_id or ""))
    second.llm_trace()(LLMTraceEvent(kind="request", call="round", seq=0, payload={"marker": "attempt-two"}))
    second.close(error="retry stopped")

    _, traces, _ = _log_rows(log_ws)
    requests = {row["turn_id"]: row["payload"]["marker"] for row in traces if row["kind"] == "request" and row["conversation_id"] == "conv-C"}
    assert requests == {"turn-1": "attempt-one", "turn-1-a2": "attempt-two"}


@pytest.mark.slow
def test_post_boot_credential_flip_stops_recording_with_a_warning(logged_api):
    api, log_ws = logged_api
    api.assist("safe turn", conversation_id="conv-P", turn_id="turn-1")
    _, before, _ = _log_rows(log_ws)
    hb.CM_HVNB.set(f"{_ASSIST}.share_credentials", True)
    try:
        reply = api.assist("unsafe turn", conversation_id="conv-P", turn_id="turn-2")
        assert reply["text"]  # the question still answers
        assert reply["capture_warning"].startswith("incomplete:workspace")  # but the refusal is visible, not silent
        _, after, _ = _log_rows(log_ws)
        assert len(after) == len(before)  # and nothing was persisted while the combination was unsafe
    finally:
        hb.CM_HVNB.unset(f"{_ASSIST}.share_credentials")


@pytest.mark.slow
def test_eviction_never_touches_an_active_conversation(logged_api):
    api, log_ws = logged_api
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_max_sessions", 1)
    api.assist("held conversation", conversation_id="conv-H", turn_id="turn-1")
    recorder = api._assist_session_log
    with recorder._lock:
        recorder._active["conv-H"] = 1  # simulate an in-flight turn holding the conversation open
    try:
        api.assist("newcomer", conversation_id="conv-N", turn_id="turn-1")
        _, traces, sessions = _log_rows(log_ws)
        conversations = {row["conversation_id"] for row in traces}
        assert "conv-H" in conversations  # the live conversation's write-ahead record survived admission pressure
    finally:
        with recorder._lock:
            recorder._active.pop("conv-H", None)


def test_enabled_warns_on_truthy_non_boolean_and_stays_off():
    import heavenbase.api.assist_log as assist_log_module

    hb.CM_HVNB.set(f"{_ASSIST}.session_log", "true")
    assist_log_module._WARNED_FAILURES.clear()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert AssistSessionLog.enabled(hb.CM_HVNB) is False  # strict literal: logging stays off
            AssistSessionLog.validate(hb.CM_HVNB)  # boot does not raise on it
            assert AssistSessionLog(hb.CM_HVNB).begin_turn("q", conversation_id="c", turn_id="t") is None  # no recorder, so no capture_warning
        # but the misconfiguration is loud, once per process
        assert sum("literal boolean" in str(item.message) for item in caught) == 1
    finally:
        hb.CM_HVNB.unset(f"{_ASSIST}.session_log")


def test_validate_refuses_memory_alias_backend():
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "memory"}})
    try:
        with pytest.raises(ValueError, match="in-memory"):
            AssistSessionLog.validate(hb.CM_HVNB)
    finally:
        for key in ("session_log", "session_log_backend"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "InMem"},
        {"type": " INMEM "},
        {"type": "Memory"},
        {"type": "sqlite", "name": "main", "url": "sqlite://"},
        {"type": "sqlite", "name": "main", "url": "sqlite:///:memory:"},
        {"type": "SQLite", "name": "main", "url": " sqlite+pysqlite:///:memory: "},
        {"type": "sqlite", "name": "main", "in_memory": True},
        {"type": "surreal", "url": "mem://"},
        {"type": "surreal", "url": "memory://"},
        {"type": "lance", "uri": "memory://log"},
        {"type": "chroma"},
        {"type": "ChromaBackend"},
        {"type": " Chroma "},
    ],
)
def test_validate_refuses_every_in_memory_declaration(entry):
    """Every shipped spelling of an in-memory store is refused at boot, whatever its case or form."""
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": entry})
    try:
        with pytest.raises(ValueError, match="in-memory"):
            AssistSessionLog.validate(hb.CM_HVNB)
    finally:
        for key in ("session_log", "session_log_backend"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "sqlite", "name": "main", "database": "file://%/assistant/x.db"},
        {"type": "sqlite", "name": "main", "url": "sqlite:////var/lib/hb/session-log.db"},
        {"type": "chroma", "path": "file://%/assistant/chroma"},
        {"type": "ChromaBackend", "path": "file://%/assistant/chroma"},
        {"type": "surreal", "url": "ws://localhost:8000/rpc"},
    ],
)
def test_validate_accepts_durable_declarations(entry):
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": entry})
    try:
        AssistSessionLog.validate(hb.CM_HVNB)
    finally:
        for key in ("session_log", "session_log_backend"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")


@pytest.mark.slow
def test_workspace_refuses_a_declaration_that_resolves_in_memory(tmp_path):
    """A database preset that resolves to memory never opens as the log, and leaves nothing that blocks the durable fix."""
    suffix = re.sub(r"[^a-z0-9]", "", tmp_path.name.lower())[-8:] or "x"
    log_ws = f"assist-memlog-{suffix}"
    hb.CM_HVNB.set("heavenbase.db.presets.w3tmem", {"provider": "sqlite", "in_memory": True})
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_workspace", log_ws)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "sqlite", "name": "main", "preset": "w3tmem"}})
    try:
        AssistSessionLog.validate(hb.CM_HVNB)  # nothing in the declaration itself names memory
        with pytest.raises(ValueError, match="in-memory"):
            AssistSessionLog(hb.CM_HVNB).workspace()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            recorder = AssistSessionLog(hb.CM_HVNB).begin_turn("q", conversation_id="conv-M", turn_id="turn-1")
        assert recorder is not None and "workspace" in str(recorder.capture)  # the turn says it went unrecorded
        with pytest.raises(KeyError):
            WorkspaceSpec.load(log_ws)  # the refused contract is not left registered
        # The operator follows the refusal: a durable declaration now opens under the same name.
        hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "sqlite", "name": "main", "database": path_to_file_uri(tmp_path / "log.db")}})
        recorder = AssistSessionLog(hb.CM_HVNB).begin_turn("q", conversation_id="conv-M", turn_id="turn-2")
        assert recorder is not None and recorder.capture == "complete"
    finally:
        for key in ("session_log", "session_log_backend", "session_log_workspace"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")
        hb.CM_HVNB.unset("heavenbase.db.presets.w3tmem")
        for action in (lambda: hb.HeavenBase.drop(log_ws), lambda: WorkspaceSpec.unregister(log_ws)):
            try:
                action()
            except Exception:
                pass


@pytest.mark.slow
def test_workspace_keeps_a_registration_it_did_not_create(tmp_path):
    """A pre-existing in-memory registration is refused but never unregistered by the log."""
    suffix = re.sub(r"[^a-z0-9]", "", tmp_path.name.lower())[-8:] or "x"
    log_ws = f"assist-premem-{suffix}"
    hb.CM_HVNB.set("heavenbase.db.presets.w3tmem2", {"provider": "sqlite", "in_memory": True})
    declaration = {"main": {"type": "sqlite", "name": "main", "preset": "w3tmem2"}}
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_workspace", log_ws)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", declaration)
    # Register exactly the declaration the log reads: the set merges into the packaged default entry.
    WorkspaceSpec.register(log_ws, config={"backends": AssistSessionLog._durable_backends(hb.CM_HVNB)})
    try:
        with pytest.raises(ValueError, match=f"hb ws unset {log_ws}"):
            AssistSessionLog(hb.CM_HVNB).workspace()
        assert WorkspaceSpec.load(log_ws).id == log_ws  # someone else's registration stays
    finally:
        for key in ("session_log", "session_log_backend", "session_log_workspace"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")
        hb.CM_HVNB.unset("heavenbase.db.presets.w3tmem2")
        for action in (lambda: hb.HeavenBase.drop(log_ws), lambda: WorkspaceSpec.unregister(log_ws)):
            try:
                action()
            except Exception:
                pass


_FRESH_HOME_VALIDATE = """
from heavenbase.api.assist_log import AssistSessionLog


class Config:
    def __init__(self, entry):
        self.values = {
            "heavenbase.dashboard.assistant.session_log": True,
            "heavenbase.dashboard.assistant.session_log_backend": {"main": entry},
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


entries = [
    {"type": "memory"},
    {"type": "Memory"},
    {"type": "InMemBackend"},
    {"type": "sqlite", "name": "main", "url": "sqlite://"},
    {"type": "sqlite", "name": "main", "url": "sqlite:///:memory:"},
    {"type": "ChromaBackend"},
]
for entry in entries:
    try:
        AssistSessionLog.validate(Config(entry))
        print("ACCEPTED", entry)
    except ValueError:
        print("REFUSED", entry)
"""


@pytest.mark.full
def test_validate_refuses_in_memory_declarations_in_a_home_never_bootstrapped(tmp_path, home_env):
    """Boot validation indexes the packaged modules itself, so an unbootstrapped HOME refuses every alias and URL form."""
    import subprocess
    import sys

    env = home_env(tmp_path)
    result = subprocess.run([sys.executable, "-c", _FRESH_HOME_VALIDATE], check=True, env=env, capture_output=True, text=True)
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 6 and all(line.startswith("REFUSED") for line in lines), result.stdout


@pytest.mark.slow
def test_turn_bookkeeping_reads_only_its_own_conversation_without_whole_table_scans(logged_api, monkeypatch):
    """Admission, attempt detection, eviction and rebuild never list or decode the whole trace table."""
    api, log_ws = logged_api
    api.assist("other conversation", conversation_id="conv-O", turn_id="turn-1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    trace_entity = recorder._entity("agent-trace")
    queried: list = []
    filtered: list = []
    real_query = log.query
    real_trace_rows = recorder._trace_rows

    def spying_query(entity):
        if getattr(entity, "identifier", entity) == "agent-trace":
            queried.append(entity)
        return real_query(entity)

    def spying_trace_rows(ws, *columns, conversation_id=None, **kwargs):
        if conversation_id is None:
            raise AssertionError("the session log queried trace rows without a conversation_id filter")
        filtered.append(conversation_id)
        return real_trace_rows(ws, *columns, conversation_id=conversation_id, **kwargs)

    monkeypatch.setattr(recorder, "_trace_rows", spying_trace_rows)

    real_rows, real_iter_rows = log.rows, log.iter_rows

    def guarded(real):
        def read(entity=None, *args, **kwargs):
            if getattr(entity, "identifier", entity) == "agent-trace":
                raise AssertionError("the session log read the whole trace table")
            return real(entity, *args, **kwargs)

        return read

    monkeypatch.setattr(log, "query", spying_query)
    monkeypatch.setattr(log, "rows", guarded(real_rows))
    monkeypatch.setattr(log, "iter_rows", guarded(real_iter_rows))
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_max_sessions", 8)  # a new conversation's admission runs the retention pass
    api.assist("first", conversation_id="conv-Q", turn_id="turn-1")
    api.assist("second", conversation_id="conv-Q", turn_id="turn-2")
    retried = recorder.begin_turn("third", conversation_id="conv-Q", turn_id="turn-2b", ws_id=str(api.ws_id or ""))
    retried.close(error="stopped")
    again = recorder.begin_turn("third", conversation_id="conv-Q", turn_id="turn-2b", ws_id=str(api.ws_id or ""))
    assert again._attempt == 2  # attempt detection still sees the earlier provisional turn row
    again.close(error="stopped again")
    rebuilt = recorder.rebuild_session("conv-Q")
    assert [m["content"] for m in rebuilt.messages(ws=log)] == ["first", "answer after 1 rounds", "second", "answer after 1 rounds"]
    assert queried and len(queried) == len(filtered)  # every trace read went through a conversation-filtered, projected query
    assert "conv-O" not in filtered  # the retention pass under its cap never read another conversation's rows

    selected = real_trace_rows(log, "turn_id", conversation_id="conv-Q", kind="turn")
    assert {row["turn_id"] for row in selected} == {"turn-1", "turn-2", "turn-2b"}  # the filter keeps conv-O out
    assert all(set(row) <= {"object_id", "turn_id"} for row in selected)  # and the projection leaves payloads unread
    assert trace_entity.identifier == "agent-trace"


@pytest.mark.slow
@pytest.mark.parametrize("failure", ["model", "progress"])
def test_a_turn_that_fails_before_the_model_call_releases_its_conversation(logged_api, monkeypatch, failure):
    """Every early failure closes the turn, so its conversation is not pinned against retention forever."""
    api, log_ws = logged_api
    import heavenbase.llm as llm_module

    api.assist("warm up", conversation_id="conv-W", turn_id="turn-0")
    recorder = api._assist_session_log
    kwargs = {}
    if failure == "model":
        monkeypatch.setattr(llm_module, "LLM", lambda **kw: (_ for _ in ()).throw(ValueError("unknown preset")))
        expected = "unknown preset"
    else:
        api._assist_progress["busy"] = []  # another call already owns this progress id
        kwargs["progress_id"] = "busy"
        expected = "already active"
    with pytest.raises(ValueError, match=expected):
        api.assist("doomed", conversation_id="conv-E", turn_id="turn-1", **kwargs)
    api._assist_progress.pop("busy", None)
    assert "conv-E" not in recorder._active  # the conversation is free for retention again
    _, traces, _ = _log_rows(log_ws)
    turn = next(row for row in traces if row["kind"] == "turn" and row["conversation_id"] == "conv-E")
    assert expected in turn["payload"]["error"]  # and the failure is on record as an errored turn


class _StubConfig:
    """Config manager stub holding one enabled session-log backend declaration."""

    def __init__(self, entry):
        self.values = {f"{_ASSIST}.session_log": True, f"{_ASSIST}.session_log_backend": {"main": entry}}

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_validate_indexes_the_packaged_modules_at_most_once(monkeypatch):
    """URLs no dialect resolves miss on every turn; only the first miss indexes the packaged modules."""
    import weakref

    from heavenbase.api import assist_log
    from heavenbase.context.runtime import Context

    monkeypatch.setattr(assist_log, "_MODULE_SETUPS", weakref.WeakKeyDictionary())
    calls: list = []
    real_setup = Context._setup_modules

    def counted_setup(self, force=False):
        calls.append(force)
        return real_setup(self, force=force)

    monkeypatch.setattr(Context, "_setup_modules", counted_setup)
    durable = [
        {"type": "sqlite", "name": "main", "database": "file://%/assistant/session-log.db"},  # the packaged default
        {"type": "redis", "url": "redis://localhost:6379/0"},
        {"type": "mongodb", "uri": "mongodb://localhost:27017/log"},
        {"type": "sqlite", "name": "main", "url": "sqlite:////tmp/hb-log.db"},
    ]
    for _ in range(5):
        for entry in durable:
            AssistSessionLog.validate(_StubConfig(entry))
    for entry in ({"type": "Memory"}, {"type": "sqlite", "name": "main", "url": "sqlite://"}):
        with pytest.raises(ValueError, match="in-memory"):
            AssistSessionLog.validate(_StubConfig(entry))
    assert len(calls) <= 1, calls


def test_module_setup_on_a_miss_runs_once_and_concurrent_misses_wait_for_it(monkeypatch):
    """Threads that miss while the modules are indexed wait for that one setup instead of answering early."""
    import threading
    import time
    import weakref

    from heavenbase.api import assist_log
    from heavenbase.context.runtime import Context

    monkeypatch.setattr(assist_log, "_MODULE_SETUPS", weakref.WeakKeyDictionary())
    indexed = threading.Event()
    calls: list = []

    def slow_setup(self, force=False):
        calls.append(force)
        time.sleep(0.2)
        indexed.set()
        return True

    monkeypatch.setattr(Context, "_setup_modules", slow_setup)

    def lookup():
        if not indexed.is_set():
            raise LookupError("not indexed yet")
        return "resolved"

    results: list = []
    threads = [threading.Thread(target=lambda: results.append(assist_log._with_modules(lookup))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == ["resolved"] * 4 and len(calls) == 1

    def never():
        raise LookupError("Unknown database dialect 'file'")

    for _ in range(3):
        with pytest.raises(LookupError):
            assist_log._with_modules(never)
    assert len(calls) == 1


def _transcript(recorder, conversation_id):
    log = recorder.workspace()
    return [(m["role"], m["content"]) for m in recorder._open_session(log, conversation_id, "").messages(ws=log)]


def _forbid_provider(monkeypatch):
    import heavenbase.llm as llm_module

    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: (_ for _ in ()).throw(AssertionError("completed replay reached provider construction")))


def _fail_projection(monkeypatch, recorder, *, on_call):
    """Make the recorder's ``on_call``-th next ``_open_session`` call raise once."""
    real = recorder._open_session
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == on_call:
            raise RuntimeError("projection store hiccup")
        return real(*args, **kwargs)

    monkeypatch.setattr(recorder, "_open_session", flaky)
    return lambda: monkeypatch.setattr(recorder, "_open_session", real)


@pytest.mark.slow
@pytest.mark.parametrize("retry", ["assist", "assist_stream"])
def test_a_retry_restores_a_completed_turn_the_transcript_missed(logged_api, monkeypatch, retry):
    """A failed projection append is visible on the reply, and a retry of that turn restores it in order."""
    api, log_ws = logged_api
    api.assist("q1", conversation_id="conv-M", turn_id="turn-1")
    recorder = api._assist_session_log
    restore = _fail_projection(monkeypatch, recorder, on_call=2)  # call 1 is admission, call 2 the close-time append
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        second = api.assist("q2", conversation_id="conv-M", turn_id="turn-2")
    restore()
    assert second["capture_warning"] == "incomplete:projection:RuntimeError"  # the missing transcript entry is not silent
    api.assist("q3", conversation_id="conv-M", turn_id="turn-3")  # a later turn lands after the gap
    answer = "answer after 1 rounds"
    assert _transcript(recorder, "conv-M") == [("user", "q1"), ("assistant", answer), ("user", "q3"), ("assistant", answer)]

    _forbid_provider(monkeypatch)
    if retry == "assist":
        replay = api.assist("q2", conversation_id="conv-M", turn_id="turn-2")
    else:
        events = list(api.assist_stream("q2", conversation_id="conv-M", turn_id="turn-2"))
        assert [event["kind"] for event in events] == ["done"]
        replay = events[0]
    assert replay["text"] == answer and "capture_warning" not in replay  # the trace was complete and the transcript is whole again
    expected = [("user", "q1"), ("assistant", answer), ("user", "q2"), ("assistant", answer), ("user", "q3"), ("assistant", answer)]
    assert _transcript(recorder, "conv-M") == expected  # restored in the order the turns completed
    api.assist("q2", conversation_id="conv-M", turn_id="turn-2")
    api.assist("q3", conversation_id="conv-M", turn_id="turn-3")
    assert _transcript(recorder, "conv-M") == expected  # further retries never double it


@pytest.mark.slow
def test_a_retry_after_a_crash_between_the_turn_row_and_the_projection_restores_the_turn(logged_api, monkeypatch):
    """A restarted process restores a completed turn whose projection append never ran."""
    api, log_ws = logged_api
    api.assist("q1", conversation_id="conv-K", turn_id="turn-1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    answer = "answer after 1 rounds"
    turn = recorder.begin_turn("q2", conversation_id="conv-K", turn_id="turn-2", ws_id=str(api.ws_id or ""))
    turn.close(text=answer)
    session = recorder._open_session(log, "conv-K", "q1")
    # The process died after the turn row and before the append: the projection still ends at turn-1.
    session.set_messages(
        [{"role": "user", "content": "q1"}, {"role": "assistant", "content": answer}], state={"last_turn_id": "turn-1", "turn_ids": ["turn-1"]}, ws=log
    )

    api._assist_session_log = AssistSessionLog(api.cm, context=api.context)  # the restarted process has a fresh recorder
    _forbid_provider(monkeypatch)
    replay = api.assist("q2", conversation_id="conv-K", turn_id="turn-2")
    assert replay["text"] == answer and "capture_warning" not in replay
    assert _transcript(api._assist_session_log, "conv-K") == [("user", "q1"), ("assistant", answer), ("user", "q2"), ("assistant", answer)]


@pytest.mark.slow
def test_a_retry_that_recreates_a_lost_session_row_deletes_no_conversation_and_keeps_the_age_order(logged_api, monkeypatch):
    """A restored row is dated by its first turn and admits no conversation, so retention keeps evicting oldest first."""
    api, log_ws = logged_api
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_max_sessions", 2)
    api.assist("a1", conversation_id="conv-A", turn_id="t-a1")
    api.assist("b1", conversation_id="conv-B", turn_id="t-b1")
    recorder = api._assist_session_log
    log = recorder.workspace()
    log.delete_many([(recorder._entity("agent-session"), recorder._open_session(log, "conv-A", "a1").object_id)])  # conv-A's row is lost
    api.assist("c1", conversation_id="conv-C", turn_id="t-c1")  # conv-A is not counted, so the cap of 2 holds conv-B and conv-C
    before = {name: _conversation_trace_count(log_ws, name) for name in ("conv-A", "conv-B", "conv-C")}
    assert all(before.values())

    import heavenbase.llm as llm_module

    mocked = llm_module.LLM
    _forbid_provider(monkeypatch)
    replay = api.assist("a1", conversation_id="conv-A", turn_id="t-a1")
    assert replay["text"] == "answer after 1 rounds" and "capture_warning" not in replay
    _, traces, sessions = _log_rows(log_ws)
    assert {name: _conversation_trace_count(log_ws, name) for name in ("conv-A", "conv-B", "conv-C")} == before  # no conversation evicted
    assert sorted(row["state"]["conversation_id"] for row in sessions) == ["conv-A", "conv-B", "conv-C"]  # one over the cap
    ages = {row["state"]["conversation_id"]: row["state"]["started_at"] for row in sessions}
    assert ages["conv-A"] < ages["conv-B"] < ages["conv-C"]  # the restored conversation keeps its age
    assert _transcript(recorder, "conv-A") == [("user", "a1"), ("assistant", "answer after 1 rounds")]

    monkeypatch.setattr(llm_module, "LLM", mocked)
    api.assist("d1", conversation_id="conv-D", turn_id="t-d1")
    _, traces, sessions = _log_rows(log_ws)
    assert sorted(row["state"]["conversation_id"] for row in sessions) == ["conv-C", "conv-D"]  # the two oldest went, restored conv-A first
    assert {row["conversation_id"] for row in traces} == {"conv-C", "conv-D"}


@pytest.mark.slow
def test_a_replay_that_cannot_restore_the_transcript_carries_a_capture_warning(logged_api, monkeypatch):
    api, log_ws = logged_api
    api.assist("q1", conversation_id="conv-N", turn_id="turn-1")
    recorder = api._assist_session_log
    restore = _fail_projection(monkeypatch, recorder, on_call=2)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        api.assist("q2", conversation_id="conv-N", turn_id="turn-2")
    restore()
    log = recorder.workspace()
    upsert = log.upsert

    def fail_projection(entity, row, *args, **kwargs):
        if entity.identifier == "agent-session":
            raise RuntimeError("projection store down")
        return upsert(entity, row, *args, **kwargs)

    monkeypatch.setattr(log, "upsert", fail_projection)
    _forbid_provider(monkeypatch)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        replay = api.assist("q2", conversation_id="conv-N", turn_id="turn-2")
    assert replay["text"] == "answer after 1 rounds"  # the stored answer is still served
    assert replay["capture_warning"] == "incomplete:projection:RuntimeError"  # but the transcript gap is not hidden


@pytest.mark.slow
def test_a_waiting_retry_restores_the_original_transcript_entry(logged_api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from test_assist_log_consistency import _observe_wait

    api, log_ws = logged_api
    api.assist("q1", conversation_id="conv-D", turn_id="turn-1")
    recorder = api._assist_session_log
    ws_id = str(api.ws_id or "")
    first = recorder.begin_turn("q2", conversation_id="conv-D", turn_id="turn-2", ws_id=ws_id)
    restore = _fail_projection(monkeypatch, recorder, on_call=1)
    waiting = _observe_wait(recorder, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(recorder.begin_turn, "q2", conversation_id="conv-D", turn_id="turn-2", ws_id=ws_id)
        try:
            assert waiting.wait(30)
        finally:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                first.close(text="first answer")
        second = retry.result(timeout=30)
    restore()
    assert first.capture == "incomplete:projection:RuntimeError"
    assert second.replay()["text"] == "first answer"
    assert second.capture == "complete"
    answer = "answer after 1 rounds"
    assert _transcript(recorder, "conv-D") == [("user", "q1"), ("assistant", answer), ("user", "q2"), ("assistant", "first answer")]
    assert not recorder._active


@pytest.mark.slow
def test_an_unexpected_close_failure_marks_the_turn_capture(logged_api, monkeypatch):
    api, log_ws = logged_api
    api.assist("warm up", conversation_id="conv-U", turn_id="turn-0")
    recorder = api._assist_session_log
    turn = recorder.begin_turn("q", conversation_id="conv-U", turn_id="turn-1", ws_id=str(api.ws_id or ""))
    monkeypatch.setattr(recorder, "_entity", lambda name: (_ for _ in ()).throw(RuntimeError("registry gone")))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        turn.close(text="answer")  # never raises
    assert turn.capture == "incomplete:turn:RuntimeError"
    assert "conv-U" not in recorder._active
