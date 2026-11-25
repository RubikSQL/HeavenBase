"""Regression coverage for Context shutdown from backend and resolver callbacks."""

from concurrent import futures
from concurrent.futures import Future
import threading
import time

import pytest

import heavenbase as hb
from heavenbase.context import load_bootstrap
from heavenbase.context.reset import reset_default_local_state
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


class Note(hb.Entity):
    identifier = "r9-context-note"
    name = hb.field(hb.ShortText)


def make_context(root, *, sqlite=False):
    backend = (
        load_bootstrap(home_path=root / "missing.yaml").backend
        if sqlite
        else {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"}
    )
    return hb.Context.load(
        {"version": 1, "root": str(root), "workspace": "default", "backend": backend},
        config=ConfigManager(root=str(root), default={}),
    )


def make_workspace(context, name):
    workspace = hb.HeavenBase(name, context=context, detached=True, backends={"main": {"type": "inmem"}})
    workspace.register(Note)
    workspace.upsert(Note, {"object_id": "n1", "name": "one"})
    return workspace


def on_thread(call):
    result = Future()

    def run():
        try:
            result.set_result(call())
        except BaseException as error:
            result.set_exception(error)

    threading.Thread(target=run, daemon=True).start()
    return result


def _ended(future, seconds):
    """Return whether ``future`` ends within ``seconds``."""
    try:
        future.exception(seconds)
    except futures.TimeoutError:
        return False
    return True


def _until(condition, seconds):
    """Return whether ``condition()`` becomes true within ``seconds``."""
    end = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= end:
            return False
        time.sleep(0.01)
    return True


@pytest.mark.slow
def test_two_workspace_readers_cannot_deadlock_by_closing_opposite_roots(tmp_path, monkeypatch):
    left = make_context(tmp_path / "left")
    right = make_context(tmp_path / "right")
    left_ws = make_workspace(left, "left")
    right_ws = make_workspace(right, "right")
    entered = threading.Barrier(2)

    def install_callback(workspace, other):
        backend = workspace.backends.get("main")
        get_many = backend.get_many

        def close_other(*args, **kwargs):
            entered.wait(5)
            with pytest.raises(RuntimeError, match="active workspace read"):
                other.close()
            return get_many(*args, **kwargs)

        monkeypatch.setattr(backend, "get_many", close_other)

    install_callback(left_ws, right)
    install_callback(right_ws, left)
    left_read = on_thread(lambda: left_ws.get("n1", entity=Note))
    right_read = on_thread(lambda: right_ws.get("n1", entity=Note))
    assert left_read.result(8)["name"] == "one"
    assert right_read.result(8)["name"] == "one"
    left.close()
    right.close()


@pytest.mark.slow
def test_read_can_close_an_idle_child_from_before_reset(tmp_path, monkeypatch):
    context = make_context(tmp_path, sqlite=True)
    old_child = context.child("old")
    reset_default_local_state(context)
    workspace = make_workspace(context.child("current"), "current")
    backend = workspace.backends.get("main")
    get_many = backend.get_many

    def close_old(*args, **kwargs):
        old_child.close()
        return get_many(*args, **kwargs)

    monkeypatch.setattr(backend, "get_many", close_old)
    try:
        assert workspace.get("n1", entity=Note)["name"] == "one"
        with pytest.raises(RuntimeError, match="Context is closed"):
            old_child.backend()
    finally:
        old_child.close()
        context.close()


@pytest.mark.slow
def test_write_callback_can_close_an_idle_child(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    idle_child = context.child("idle")
    workspace = make_workspace(context, "current")
    backend = workspace.backends.get("main")
    upsert = backend.upsert

    def close_idle(*args, **kwargs):
        idle_child.close()
        return upsert(*args, **kwargs)

    monkeypatch.setattr(backend, "upsert", close_idle)
    try:
        workspace.upsert(Note, {"object_id": "n2", "name": "two"})
        assert workspace.get("n2", entity=Note)["name"] == "two"
        with pytest.raises(RuntimeError, match="Context is closed"):
            idle_child.backend()
    finally:
        idle_child.close()
        context.close()


def test_resolver_callback_does_not_wait_for_close_that_waits_for_its_lease(tmp_path):
    context = make_context(tmp_path)
    modules = context.modules()
    entered, proceed = threading.Event(), threading.Event()

    def aliases():
        with pytest.raises(RuntimeError, match="active resolver operations"):
            context.close()  # its own resolver operation would never end
        entered.set()
        assert proceed.wait(10)
        assert context.close() is None  # the close on the other thread is in progress
        yield "probe-alias"

    record = {
        "name": "Example",
        "desc": "Test registration.",
        "source": "inline",
        "target": "definition",
        "active": True,
        "meta": {"schema_version": 1, "definition": {"ok": True}},
    }
    registration = on_thread(lambda: modules.register("test_kind", "probe-operation", record, aliases=aliases()))
    assert entered.wait(5)
    closing = on_thread(context.close)
    try:
        assert not closing.done() and not _ended(closing, 0.3)  # the close waits for the resolver operation
    finally:
        proceed.set()
    registration.result(8)
    closing.result(10)
    with pytest.raises(RuntimeError, match="Context is closed"):
        context.backend()


@pytest.mark.slow
def test_reset_that_deletes_local_storage_waits_for_a_read(tmp_path, monkeypatch):
    context = make_context(tmp_path, sqlite=True)
    workspace = make_workspace(context, "current")
    backend = workspace.backends.get("main")
    get_many = backend.get_many
    entered, release = threading.Event(), threading.Event()

    def pause_read(*args, **kwargs):
        if threading.current_thread().name == "paused-reader":
            entered.set()
            assert release.wait(10)
        return get_many(*args, **kwargs)

    monkeypatch.setattr(backend, "get_many", pause_read)
    read = Future()

    def read_once():
        try:
            read.set_result(workspace.get("n1", entity=Note))
        except BaseException as error:
            read.set_exception(error)

    threading.Thread(target=read_once, name="paused-reader", daemon=True).start()
    try:
        assert entered.wait(5)
        resetting = on_thread(lambda: reset_default_local_state(context))
        assert _until(lambda: context._resetting, 10)  # the reset has started
        assert not _ended(resetting, 0.3)  # and waits for the read
        with pytest.raises(RuntimeError, match="reset is in progress"):
            workspace.get("n1", entity=Note)  # a new read fails meanwhile
    finally:
        release.set()
    assert read.result(5)["name"] == "one"
    resetting.result(10)
    with pytest.raises(RuntimeError, match="retired"):
        workspace.get("n1", entity=Note)
    context.close()


@pytest.mark.slow
def test_reset_drops_a_hidden_workspace_without_holding_context_locks(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    store = context._hidden_store({"workspace": "private", "backends": {"main": {"type": "inmem"}}})
    workspace = store.workspace
    workspace.register(Note)
    workspace.upsert(Note, {"object_id": "n1", "name": "one"})
    backend = workspace.backends.get("main")
    drop = backend.drop
    dropped = []

    closing = []

    def close_during_drop():
        assert context.close() is None
        closing.append(on_thread(context.close))
        assert not _ended(closing[0], 0.3)  # a close on another thread waits for the reset
        drop()
        dropped.append(True)

    monkeypatch.setattr(backend, "drop", close_during_drop)
    reset_default_local_state(context)
    assert dropped == [True]
    assert store.retired
    closing[0].result(10)  # then it closes the Context
    assert context._closed
    with pytest.raises(RuntimeError, match="retired|Context is closed"):
        workspace.get("n1", entity=Note)
    context.close()
