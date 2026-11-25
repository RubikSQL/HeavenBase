"""An agent ``Session`` without an ``object_id`` gets a fresh id, even for sessions created in one clock tick."""

import sys

import pytest

import heavenbase as hb
from heavenbase.utils import hash_id

pytestmark = pytest.mark.fast


@pytest.fixture
def frozen_clock(monkeypatch):
    """Freeze the clock the Session module reads, as a coarse clock does for sessions created in one tick."""
    module = sys.modules[hb.Session.__module__]
    monkeypatch.setattr(module, "time", lambda: 1_700_000_000.0, raising=False)
    return module


@pytest.fixture
def workspace():
    ws = hb.HeavenBase("agent-session-default-id", detached=True, backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")
    try:
        yield ws
    finally:
        ws.release()


def test_sessions_with_the_same_title_in_one_clock_tick_get_distinct_ids(frozen_clock, workspace):
    first = hb.Session(title="Run", agent_ref="helper", summary="first", ws=workspace)
    second = hb.Session(title="Run", agent_ref="helper", summary="second", ws=workspace)

    assert first.object_id != second.object_id
    for session in (first, second):
        workspace.upsert(hb.Session, session.to_dict())
    assert hb.Session.load(first.object_id, ws=workspace).summary == "first"
    assert hb.Session.load(second.object_id, ws=workspace).summary == "second"
    # The default id keeps the hash_id format: a zero-padded decimal digest of the configured length.
    assert first.object_id.isdigit() and len(first.object_id) == len(hash_id("session"))


def test_logged_chat_sessions_with_the_same_title_in_one_clock_tick_are_both_kept(frozen_clock, workspace):
    first = frozen_clock.log_chat_session([{"role": "user", "content": "one"}, {"role": "assistant", "content": "a"}], title="Chat", ws=workspace)
    second = frozen_clock.log_chat_session([{"role": "user", "content": "two"}, {"role": "assistant", "content": "b"}], title="Chat", ws=workspace)

    assert first.object_id != second.object_id
    assert [message["content"] for message in hb.Session.load(first.object_id, ws=workspace).messages(ws=workspace)] == ["one", "a"]
    assert [message["content"] for message in hb.Session.load(second.object_id, ws=workspace).messages(ws=workspace)] == ["two", "b"]


def test_a_session_keeps_the_object_id_it_is_given(workspace):
    assert hb.Session(object_id="session-given", title="Run", ws=workspace).object_id == "session-given"
