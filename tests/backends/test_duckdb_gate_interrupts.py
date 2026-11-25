"""An interrupt inside the DuckDB write gate of the keyword-index check: the gate's lock is free and nothing stays held."""

import os
import sys
import threading
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

import heavenbase as hb
from heavenbase.backends.sql.strategies import sparse_gram

pytestmark = pytest.mark.fast


class Stopped(BaseException):
    pass


def _interrupted_at(count, fn):
    """Run ``fn`` and raise ``Stopped`` as call or return number ``count`` in the gate happens; return whether it came to that."""
    left = [count]
    files = (sparse_gram.__file__, threading.__file__)

    def profile(frame, event, arg):
        if event != "c_call" and frame.f_code.co_filename in files:
            left[0] -= 1
            if left[0] < 0:
                sys.setprofile(None)
                raise Stopped

    sys.setprofile(profile)
    try:
        fn()
    except BaseException:
        if left[0] >= 0:
            raise
    finally:
        sys.setprofile(None)
    return left[0] < 0


def _connection():
    return SimpleNamespace(info={}, in_transaction=lambda: True)


def _another_thread_can_change_the_index(gates):
    connection, done = _connection(), []
    thread = threading.Thread(target=lambda: done.append(gates.hold(connection, "key", exclusive=True, what="it")), daemon=True)
    thread.start()
    thread.join(10)
    return bool(done)


def _transactions(gates, connection):
    gates._on_begin(connection)
    gates._on_execute(connection)
    gates.hold(connection, "key", exclusive=False, what="it")
    gates.hold(connection, "key", exclusive=True, what="it")
    gates._on_commit(connection)
    gates._on_begin(connection)
    gates.hold(connection, "key", exclusive=True, what="it")
    gates.release(connection, "key")
    gates.hold(connection, "key", exclusive=False, what="it")
    gates._on_rollback(connection)


def _change(gates, connection):
    gates.hold(connection, "key", exclusive=True, what="it")


@contextmanager
def _a_writer_ends_soon(gates):
    """Hold the gate shared in another thread until a change waits for it, or the body ends."""
    writer, entered, over = _connection(), threading.Event(), threading.Event()

    def write():
        gates.hold(writer, "key", exclusive=False, what="it")
        entered.set()
        while not gates._gates["key"][4] and not over.wait(0.005):
            pass
        over.wait(0.02)
        gates._on_rollback(writer)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    assert entered.wait(10)
    try:
        yield
    finally:
        over.set()
        thread.join(10)
        assert not thread.is_alive()


@pytest.mark.parametrize("calls, around", [(_transactions, nullcontext), (_change, _a_writer_ends_soon)])
def test_an_interrupt_at_any_call_in_the_gate_leaves_it_free(calls, around):
    count = 0
    while True:
        gates, connection = sparse_gram._DuckDBWriteGates(), _connection()
        with around(gates):
            interrupted = _interrupted_at(count, lambda: calls(gates, connection))
        gates._on_pool_event(None, SimpleNamespace(info=connection.info))  # the pool takes the connection back
        assert _another_thread_can_change_the_index(gates), count
        if not interrupted:
            break
        count += 1
    assert count > 10


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_fork_while_another_thread_is_inside_the_gate():
    gates = sparse_gram._DUCKDB_GATES
    inside, go = threading.Event(), threading.Event()

    def hold():
        with gates._lock:
            inside.set()
            go.wait(20)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert inside.wait(5)
    pid = os.fork()
    if pid == 0:
        os._exit(0 if gates._lock.acquire(timeout=3) and not gates._gates else 7)
    _, status = os.waitpid(pid, 0)
    go.set()
    thread.join(5)
    assert os.waitstatus_to_exitcode(status) == 0


class Note(hb.Entity):
    identifier = "duck-gate-interrupt-note"
    title = hb.field(hb.ShortText)


@pytest.mark.full
@pytest.mark.slow
def test_another_file_answers_after_an_interrupt_in_the_gate(tmp_path):
    pytest.importorskip("duckdb_engine")
    opened = []
    for name in ("one", "two"):
        opened.append(hb.HeavenBase(name, detached=True, backends={"main": {"type": "duckdb", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}.db")}}))
        opened[-1].register(Note)
        opened[-1].upsert(Note, {"object_id": "n", "title": name})
    one, two = opened

    def profile(frame, event, arg):
        if event == "return" and frame.f_code.co_name == "__enter__" and frame.f_back and frame.f_back.f_code.co_filename == sparse_gram.__file__:
            sys.setprofile(None)
            raise Stopped

    sys.setprofile(profile)
    try:
        one.get("n", Note)
    except BaseException:
        pass
    finally:
        sys.setprofile(None)
    answered = []
    thread = threading.Thread(target=lambda: answered.append(two.get("n", Note)["title"]), daemon=True)
    thread.start()
    thread.join(30)
    assert answered == ["two"]
    for ws in opened:
        ws.release()
