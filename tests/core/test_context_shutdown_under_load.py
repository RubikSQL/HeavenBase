"""Context.close() and reset_default_local_state() end while other threads keep starting operations.

The Context is marked first, so a new resolver operation is refused, as in 0.1.2.2, and the running ones end. Without
the mark, operations that overlap without a gap would keep a close or reset waiting until it gave up.
"""

from concurrent.futures import Future
import threading
import time

import pytest

import heavenbase as hb
from heavenbase.context import runtime
from heavenbase.context.reset import reset_default_local_state
from heavenbase.utils.config import ConfigManager

_RECORD = {
    "name": "Example",
    "desc": "Test registration.",
    "source": "inline",
    "target": "definition",
    "active": True,
    "meta": {"schema_version": 1, "definition": {"ok": True}},
}


class TrafficDoc(hb.Entity):
    identifier = "shutdown-traffic-doc"

    region = hb.field(hb.ShortText).store(to="main")
    units = hb.field(hb.Integer).store(to="main")


def _context(root):
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )
    context._setup_modules(force=True)
    return context


def _on_thread(call, name=None):
    result = Future()

    def run():
        try:
            result.set_result(call())
        except BaseException as error:
            result.set_exception(error)

    threading.Thread(target=run, name=name, daemon=True).start()
    return result


def _shut_down(context, shutdown):
    if shutdown == "close":
        context.close()
    else:
        reset_default_local_state(context)


def _until(condition, seconds):
    """Wait until ``condition`` holds and return whether it did in ``seconds``."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


class _Load:
    """Threads that run ``call`` again and again until the load stops; a refused call pauses."""

    def __init__(self, threads, call, stagger=0.0):
        self._stop = threading.Event()
        self._call = call
        self._stagger = stagger
        self._threads = [threading.Thread(target=self._run, args=(index,), daemon=True) for index in range(threads)]
        for thread in self._threads:
            thread.start()

    def _run(self, index):
        self._stop.wait(index * self._stagger)
        while not self._stop.is_set():
            try:
                self._call(index)
            except Exception:  # noqa: BLE001 - the call is refused during and after the shutdown
                time.sleep(0.005)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stop.set()
        for thread in self._threads:
            thread.join(20)
        assert not any(thread.is_alive() for thread in self._threads)


@pytest.mark.full
@pytest.mark.slow
@pytest.mark.parametrize("shutdown", ["close", "reset"])
def test_a_shutdown_ends_while_threads_run_filter_queries_without_a_pause(tmp_path, monkeypatch, shutdown):
    monkeypatch.setattr(runtime, "_SHUTDOWN_WAIT", 8.0, raising=False)
    context = _context(tmp_path / "ctx")
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "t.db")}}
    ws = hb.HeavenBase("traffic", context=context, detached=True, backends=backends)
    ws.register(TrafficDoc)
    ws.upsert_many(TrafficDoc, [{"object_id": f"d{n}", "region": ("east", "west")[n % 2], "units": n % 9} for n in range(40)])

    def query(index):
        ws.query(TrafficDoc).where((TrafficDoc.units <= 5) & (TrafficDoc.region == "east")).execute().rows()

    query(0)
    with _Load(8, query):  # ordinary queries take resolver leases on their own (operator and handler lookups)
        time.sleep(0.5)
        began = time.monotonic()
        _shut_down(context, shutdown)
        assert time.monotonic() - began < 5


@pytest.mark.full
@pytest.mark.slow
@pytest.mark.parametrize("shutdown", ["close", "reset"])
def test_a_shutdown_ends_while_threads_run_overlapping_module_operations(tmp_path, monkeypatch, shutdown):
    monkeypatch.setattr(runtime, "_SHUTDOWN_WAIT", 8.0, raising=False)
    context = _context(tmp_path / "ctx")
    modules = context.modules()

    def slow_aliases():
        time.sleep(0.4)  # a slow part of the operation, inside its lease
        yield from ()

    def register(index):
        modules.register("test_kind", f"thing-{index}-{time.monotonic_ns()}", _RECORD, aliases=slow_aliases())
        time.sleep(0.02)

    with _Load(2, register, stagger=0.2):  # the operations of the two threads overlap without a gap
        time.sleep(0.8)
        assert modules._authority_users
        began = time.monotonic()
        _shut_down(context, shutdown)
        assert time.monotonic() - began < 5


def _running_operation(context, work):
    """Start a registration that runs ``work`` inside its lease; return the service, its Future and two events."""
    modules = context.modules()
    entered, proceed = threading.Event(), threading.Event()

    def aliases():
        entered.set()
        assert proceed.wait(15)
        work(modules)
        yield "probe-alias"

    modules.register("test_kind", "base", _RECORD)
    return modules, _on_thread(lambda: modules.register("test_kind", "probe", _RECORD, aliases=aliases())), entered, proceed


@pytest.mark.slow
@pytest.mark.parametrize("shutdown", ["close", "reset"])
def test_new_operations_are_refused_while_a_shutdown_waits_and_work_again_when_it_is_refused(tmp_path, monkeypatch, shutdown):
    monkeypatch.setattr(runtime, "_SHUTDOWN_WAIT", 0.6, raising=False)
    context = _context(tmp_path / "ctx")
    modules, registration, entered, proceed = _running_operation(context, lambda modules: None)
    try:
        assert entered.wait(10)
        refused = _on_thread(lambda: _shut_down(context, shutdown))
        assert _until(lambda: context._closing or context._resetting, 10)  # it has marked the Context and waits
        with pytest.raises(RuntimeError, match="retired Context authority generation"):
            modules.resolve("test_kind", "base")
        with pytest.raises(RuntimeError, match=f"{shutdown} is in progress"):
            context.backend()
        with pytest.raises(RuntimeError, match="resolver operations are active"):
            refused.result(10)
        assert not context._closing and not context._resetting and not context._closed
        assert modules.resolve("test_kind", "base") == {"ok": True}  # operations are admitted again
        context.backend()
    finally:
        proceed.set()
    registration.result(10)
    context.close()
    assert context._closed


@pytest.mark.slow
def test_a_close_lets_the_running_operation_and_its_nested_operations_finish(tmp_path):
    context = _context(tmp_path / "ctx")
    nested = []
    modules, registration, entered, proceed = _running_operation(context, lambda modules: nested.append(modules.resolve("test_kind", "base")))
    try:
        assert entered.wait(10)
        closing = _on_thread(context.close)
        assert _until(lambda: context._closing, 10)  # the close waits for the operation
    finally:
        proceed.set()
    registration.result(10)
    closing.result(10)
    assert nested == [{"ok": True}]  # the nested operation ran while the close waited
    assert context._closed


@pytest.mark.slow
@pytest.mark.parametrize("shutdown", ["close", "reset"])
def test_a_shutdown_from_a_resolver_operation_is_refused_before_it_waits_for_the_context_lock(tmp_path, shutdown):
    context = _context(tmp_path / "ctx")
    modules = context.modules()
    holding, release = threading.Event(), threading.Event()

    def hold_the_context_lock():
        with context._lock:
            holding.set()
            release.wait(6)  # longer than a refused call takes, so a call that waits for the lock is seen

    def aliases():
        began = time.monotonic()
        try:
            _shut_down(context, shutdown)
        except RuntimeError as error:
            outcome.append((str(error), time.monotonic() - began))
        yield "probe-alias"

    outcome = []
    holder = _on_thread(hold_the_context_lock)
    try:
        assert holding.wait(10)
        modules.register("test_kind", "probe", _RECORD, aliases=aliases())
    finally:
        release.set()
    holder.result(10)
    context.close()
    assert len(outcome) == 1
    message, seconds = outcome[0]
    assert "active resolver operations" in message
    assert seconds < 1
