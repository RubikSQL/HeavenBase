"""Context.close() and reset_default_local_state() wait, for a bounded time, for the work of other threads.

In 0.1.2.2, close and reset waited without a limit for the Context lock and
for other threads' running resolver operations. A close or reset by a thread
that holds no HeavenBase lock now waits for a hold of the Context lock, for
root resolver operations that are already running and, when the reset
removes storage, for workspace reads already running, for at most
``_SHUTDOWN_WAIT`` seconds in all, and then raises with the Context left open.
A caller holding a Context or workspace lock is still refused at once.
"""

from concurrent import futures
from concurrent.futures import Future
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import heavenbase as hb
from heavenbase.context import locks, runtime
from heavenbase.context.reset import reset_default_local_state
from heavenbase.utils.config import ConfigManager

_RACE_CHILD = Path(__file__).with_name("context_shutdown_race_child.py")


class WaitNote(hb.Entity):
    identifier = "shutdown-wait-note"

    name = hb.field(hb.ShortText)


def _context(root):
    backend = {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"}
    return hb.Context.load(
        {"version": 1, "root": str(root), "workspace": "default", "backend": backend},
        config=ConfigManager(root=str(root), default={}),
    )


def _workspace(context, name):
    ws = hb.HeavenBase(name, context=context, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(WaitNote)
    ws.upsert(WaitNote, {"object_id": "n1", "name": "one"})
    return ws


def _on_thread(call, name=None):
    result = Future()

    def run():
        try:
            result.set_result(call())
        except BaseException as error:
            result.set_exception(error)

    threading.Thread(target=run, name=name, daemon=True).start()
    return result


def _ended(future, seconds):
    """Return whether ``future`` ends within ``seconds``."""
    try:
        future.exception(seconds)
    except futures.TimeoutError:
        return False
    return True


def _pause(monkeypatch, backend, method, thread_name):
    """Make ``backend.method`` wait, on the thread named ``thread_name``, until the returned event is set."""
    original = getattr(backend, method)
    entered, release = threading.Event(), threading.Event()

    def paused(*args, **kwargs):
        if threading.current_thread().name == thread_name:
            entered.set()
            assert release.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(backend, method, paused)
    return entered, release


@pytest.mark.slow
def test_reset_waits_for_an_active_writer(tmp_path, monkeypatch):
    context = _context(tmp_path)
    ws = _workspace(context, "writer")
    entered, release = _pause(monkeypatch, ws.backends.get("main"), "upsert", "paused-writer")
    write = _on_thread(lambda: ws.upsert(WaitNote, {"object_id": "n2", "name": "two"}), name="paused-writer")
    try:
        assert entered.wait(10)
        resetting = _on_thread(lambda: reset_default_local_state(context))
        assert not _ended(resetting, 0.3)  # the reset waits for the write
    finally:
        release.set()
    write.result(10)
    resetting.result(15)
    with pytest.raises(RuntimeError, match="retired"):
        ws.count(WaitNote)  # the reset retired the workspace after the write ended
    context.close()


@pytest.mark.slow
def test_reset_gives_up_on_a_read_that_outlasts_its_wait_and_leaves_the_context_open(tmp_path, monkeypatch):
    context = _context(tmp_path)
    ws = _workspace(context, "reader")
    context._hidden_store({"workspace": "wait-private", "backends": {"main": {"type": "inmem"}}})  # a reset drops it
    entered, release = _pause(monkeypatch, ws.backends.get("main"), "get_many", "paused-reader")
    monkeypatch.setattr(runtime, "_SHUTDOWN_WAIT", 0.5, raising=False)
    read = _on_thread(lambda: ws.get("n1", entity=WaitNote), name="paused-reader")
    try:
        assert entered.wait(10)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="during active workspace reads"):
            reset_default_local_state(context)
        assert time.monotonic() - started >= 0.4  # it waited before it gave up
        assert not context._resetting
        assert ws.count(WaitNote) == 1  # new calls work again
    finally:
        release.set()
    assert read.result(10)["name"] == "one"
    reset_default_local_state(context)  # with no read running, the reset goes ahead
    with pytest.raises(RuntimeError, match="retired"):
        ws.count(WaitNote)
    context.close()


@pytest.mark.fast
def test_close_and_reset_waiting_for_the_context_lock_are_checked_by_the_wait_guard(tmp_path, monkeypatch):
    context = _context(tmp_path)
    holding, release = threading.Event(), threading.Event()

    def hold_the_context_lock():
        with context._lock:
            holding.set()
            assert release.wait(15)

    checks = []

    def refuse(*_):
        checks.append(True)
        raise RuntimeError("refused by the wait guard")

    holder = _on_thread(hold_the_context_lock)
    try:
        assert holding.wait(10)
        monkeypatch.setattr(locks, "_WAIT_GUARD", (lambda: True, refuse))
        for shutdown in (context.close, lambda: reset_default_local_state(context)):
            started = time.monotonic()
            with pytest.raises(RuntimeError, match="refused by the wait guard"):
                _on_thread(shutdown).result(10)
            assert time.monotonic() - started < 5  # not after the whole wait
        assert checks
    finally:
        release.set()
    holder.result(10)
    monkeypatch.setattr(locks, "_WAIT_GUARD", None)
    context.close()
    assert context._closed


@pytest.mark.fast
def test_a_close_gets_the_context_lock_from_threads_that_take_it_back_to_back(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setattr(runtime, "_SHUTDOWN_WAIT", 10.0, raising=False)
    stop = threading.Event()
    started = threading.Barrier(5, timeout=10)

    def take_back_to_back():
        started.wait()
        while not stop.is_set():
            with context._lock:
                time.sleep(0.0005)  # release it, then take it again at once

    takers = [_on_thread(take_back_to_back) for _ in range(4)]
    try:
        started.wait()
        begin = time.monotonic()
        context.close()  # the lock is reserved for the close, so the takers let it go first
        assert time.monotonic() - begin < 5
        assert context._closed
    finally:
        stop.set()
    for taker in takers:
        taker.result(10)


@pytest.mark.fast
def test_a_close_from_inside_a_read_is_refused_at_once(tmp_path):
    context = _context(tmp_path)
    ws = _workspace(context, "inside")
    started = time.monotonic()
    with ws._lock.read():
        with pytest.raises(RuntimeError, match="active workspace read"):
            context.close()
        with pytest.raises(RuntimeError, match="active workspace read"):
            reset_default_local_state(context)
    assert time.monotonic() - started < 5
    context.close()


@pytest.mark.full
@pytest.mark.slow
def test_resets_and_a_close_that_wait_for_reads_and_writes_do_not_deadlock(tmp_path):
    done = subprocess.run([sys.executable, str(_RACE_CHILD), str(tmp_path / "root")], capture_output=True, text=True, timeout=300)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert done.stdout.strip().splitlines()[-1].startswith("ok "), done.stdout
