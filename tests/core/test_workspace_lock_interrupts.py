"""A call that an exception interrupts, as a signal handler's can, leaves the Context and workspace locks usable.

A handler runs where the interpreter checks for signals: as it enters a Python function, and after a call returns. A
profiler raises at each such point of the lock code in turn, one point per run.
"""

from __future__ import annotations

import inspect
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import heavenbase as hb
from heavenbase.context import locks, runtime
from heavenbase.context.reset import reset_default_local_state
from heavenbase.utils.config import ConfigManager

JOIN = 30.0  # a thread that must finish soon
_FILES = (locks.__file__, runtime.__file__)


class Interrupt(BaseException):
    pass


class _InterruptNote(hb.Entity):
    identifier = "lock-interrupt-note"

    name = hb.field(hb.ShortText)


def _interrupt(action, point: int, seen: list[int]) -> bool:
    """Run ``action``, count the points in ``seen`` and raise at the ``point``-th; return whether that reached the caller."""

    def profile(frame, event, arg):
        if frame.f_code.co_filename not in _FILES:
            return
        if event == "call":
            if sys.version_info < (3, 12) and frame.f_code.co_flags & 0x20 and frame.f_lasti >= 0:
                return  # a profiler's exception ends a resumed generator there; a handler's is raised inside its ``try``
        elif event == "return":
            if frame.f_back is None or frame.f_back.f_code.co_filename not in _FILES:
                return  # belongs to the caller of the lock code
        elif event != "c_return":
            return
        seen[0] += 1
        if seen[0] == point + 1:
            raise Interrupt

    hook, sys.unraisablehook = sys.unraisablehook, lambda unraisable: None  # where one goes that ends an unused generator
    sys.setprofile(profile)
    try:
        action()
    except Interrupt:
        return True
    finally:
        sys.setprofile(None)
        sys.unraisablehook = hook
    return False


class _Pair:
    """A Context lock and two workspace locks of its tree."""

    def __init__(self, context=None, first=None) -> None:
        self.context = context or locks._ContextLock()
        self.first = first or locks._WorkspaceLock(self.context)
        self.second = locks._WorkspaceLock(self.context)

    def shutdown_lock(self, deadline: float | None = None):
        return runtime.Context._shutdown_lock(SimpleNamespace(_lock=self.context), "close", deadline)

    def left(self) -> dict:
        """Return everything that the calling thread holds, and every trace of a waiting or finished call."""
        held, context = locks._held(), self.context
        left = {name: getattr(held, name) for name in ("contexts", "exclusive", "shared")}
        left.update({name: getattr(context, name) for name in ("_owner", "_depth", "_reserved", "_watchers", "_drainer")})
        left.update(inner=context._inner._is_owned(), parked=locks._PARKED.get(threading.get_ident()))
        for number, lock in enumerate((self.first, self.second)):
            for name in ("_readers", "_outer_readers", "_pending", "_writer", "_writers_waiting", "_nested_waiting", "_held_back", "_entitled", "_queue"):
                left[f"{number}{name}"] = getattr(lock, name)
        return {name: value for name, value in left.items() if value}


def _read(pair):
    with pair.first.read():
        with pair.first.read():
            pass


def _write(pair):
    with pair.first:
        with pair.first, pair.first.read():
            pass


def _write_from_a_read(pair):
    with pair.first.read():
        with pair.first:
            pass


def _write_from_a_write(pair):
    with pair.first:
        with pair.second, pair.second.read():
            pass


def _hold(pair):
    with pair.context:
        with pair.context, pair.second:
            pass


def _shutdown(pair):
    with pair.shutdown_lock(time.monotonic() + JOIN):
        with pair.shutdown_lock():
            pass


@pytest.mark.fast
@pytest.mark.parametrize("call", [_read, _write, _write_from_a_read, _write_from_a_write, _hold, _shutdown])
def test_an_exception_at_any_point_of_a_lock_call_leaves_nothing_held(call):
    point, points, raised = -1, 0, 0  # point -1 raises nowhere
    while point < points:
        seen = [0]
        pair = _Pair()
        raised += _interrupt(lambda: call(pair), point, seen)
        assert not pair.left(), (point, pair.left())
        points = max(points, seen[0])
        point += 1
    assert raised > 10, (points, raised)  # the sweep reached the lock code


def _at_depth(levels: int, action):
    return action() if levels <= 0 else _at_depth(levels - 1, action)


@pytest.mark.fast
@pytest.mark.parametrize("call", [_read, _write, _write_from_a_read, _write_from_a_write, _hold, _shutdown])
def test_a_lock_call_near_the_recursion_limit_leaves_nothing_held(call):
    depth, limit, refused = len(inspect.stack(0)), sys.getrecursionlimit(), 0
    locks._held()
    rooms = range(5, 160)
    for room in rooms:
        pair = _Pair()
        sys.setrecursionlimit(depth + 300)
        try:
            _at_depth(300 - room, lambda: call(pair))
        except RecursionError:
            refused += 1
        finally:
            sys.setrecursionlimit(limit)
        assert not pair.left(), (room, pair.left())
    assert 0 < refused < len(rooms)


# How another thread holds a lock, how it tells that a thread waits for it, and the call that waits.
_WAITS = {
    "a read for a write": (lambda p: p.first, lambda p: p.first._held_back, _read),
    "a write for a read": (lambda p: p.first.read(), lambda p: p.first._writers_waiting, _write),
    "a write for a write": (lambda p: p.first, lambda p: p.first._writers_waiting, _write),
    "a write for the Context lock": (lambda p: p.context, lambda p: locks._PARKED, _write),
    "a nested write for a read": (lambda p: p.second.read(), lambda p: p.second._writers_waiting, _write_from_a_write),
    "a hold for the Context lock": (lambda p: p.context, lambda p: locks._PARKED, _hold),
    "a shutdown for the Context lock": (lambda p: p.context, lambda p: p.context._reserved, _shutdown),
}


@pytest.mark.slow
@pytest.mark.parametrize("name", _WAITS)
def test_an_exception_at_any_point_of_a_wait_for_a_lock_leaves_nothing_held(name, monkeypatch):
    monkeypatch.setattr(locks, "_WAIT_GUARD", None)  # a guarded wait enters the storage layer's locks
    holds, waits, call = _WAITS[name]

    def hold(pair, entered):
        with holds(pair):
            entered.set()
            end = time.monotonic() + 0.01
            while not waits(pair) and time.monotonic() < end:
                time.sleep(0.0005)

    point, points, raised = -1, 0, 0
    while point < points:
        seen, entered = [0], threading.Event()
        pair = _Pair()
        holder = threading.Thread(target=hold, args=(pair, entered), daemon=True)
        holder.start()
        assert entered.wait(JOIN)
        raised += _interrupt(lambda: call(pair), point, seen)
        holder.join(JOIN)
        assert not holder.is_alive(), point
        assert not pair.left(), (point, pair.left())
        points = max(points, seen[0])
        point += 1
    assert raised > 10, (points, raised)


@pytest.mark.fast
@pytest.mark.parametrize("step", ["_leave_read", "_leave_write", "_leave"])
def test_a_recovery_that_a_second_exception_cuts_short_runs_again(monkeypatch, step):
    # The first exception comes at each point of the call in turn, the second one as the lock is first given back.
    call = {"_leave_read": _read, "_leave_write": _write, "_leave": _hold}[step]
    owner = locks._ContextLock if step == "_leave" else locks._WorkspaceLock
    give_back, armed, rerun = getattr(owner, step), [], 0

    def cut_short(self, *args):
        if armed:
            armed.pop()
            raise Interrupt("second")
        return give_back(self, *args)

    monkeypatch.setattr(owner, step, cut_short)
    point, points = -1, 0
    while point < points:
        seen = [0]
        pair = _Pair()
        armed.append(True)
        _interrupt(lambda: call(pair), point, seen)
        rerun += not armed
        armed.clear()
        assert not pair.left(), (point, pair.left())
        points = max(points, seen[0])
        point += 1
    assert rerun > 10, (points, rerun)


def _workspace(root: Path) -> tuple[hb.Context, hb.HeavenBase]:
    backend = {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"}
    context = hb.Context.load(
        {"version": 1, "root": str(root), "workspace": "default", "backend": backend},
        config=ConfigManager(root=str(root), default={}),
    )
    ws = hb.HeavenBase("interrupted", context=context, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(_InterruptNote)
    return context, ws


def test_an_exception_at_any_point_of_the_locking_of_a_workspace_call_leaves_nothing_held(tmp_path):
    context, ws = _workspace(tmp_path)
    ws.upsert(_InterruptNote, {"object_id": "n1", "name": "one"})
    calls = {
        "read": lambda: ws.count(_InterruptNote),
        "page": lambda: list(ws.iter_rows(_InterruptNote)),
        "write": lambda: ws.upsert(_InterruptNote, {"object_id": "n1", "name": "one"}),
    }
    pair = _Pair(context._lock, ws._lock)
    for name, call in calls.items():
        point, points, raised = -1, 0, 0
        while point < points:
            seen = [0]
            try:
                raised += _interrupt(call, point, seen)
            except Exception:  # the interrupted call may report its own failure
                pass
            assert not pair.left() and not context._readers, (name, point, pair.left(), context._readers)
            points = max(points, seen[0])
            point += 1
        assert raised > 10, (name, points, raised)
    context.close()


def test_a_shutdown_interrupted_as_it_marks_the_context_leaves_it_open(tmp_path, monkeypatch):
    context, ws = _workspace(tmp_path)
    mark = context._mark_shutdown

    def interrupted_mark(**kwargs):
        mark(**kwargs)
        raise KeyboardInterrupt

    for number, shutdown in enumerate((lambda: reset_default_local_state(context), context.close)):
        with monkeypatch.context() as patch:
            patch.setattr(context, "_mark_shutdown", interrupted_mark)
            with pytest.raises(KeyboardInterrupt):
                shutdown()
        ws.upsert(_InterruptNote, {"object_id": f"n{number}", "name": "one"})
        assert ws.count(_InterruptNote) == number + 1
    context.close()
    with pytest.raises(RuntimeError, match="Context is closed"):
        ws.count(_InterruptNote)


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs a timer signal")
def test_interrupted_workspace_calls_leave_the_workspace_and_the_context_usable(tmp_path, home_env):
    script = Path(__file__).with_name("workspace_interrupt_child.py")
    result = subprocess.run([sys.executable, str(script), str(tmp_path)], env=home_env(tmp_path), text=True, capture_output=True, timeout=240)
    assert result.returncode == 0 and result.stdout.startswith("ok "), result.stdout + result.stderr
