"""A call interrupted by a signal, such as Ctrl-C or an alarm, leaves no lock held and no count of its connections.

Python runs a signal handler when a Python function starts and after a C function returns. Each case interrupts a call at
the first of these points in the code under test, then at the second, and so on, and checks after each that another
thread still uses the database. The cases run in processes of their own, which end also when a lock stays held.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import event

import heavenbase as hb
from heavenbase.backends.sql import base as sql_base
from heavenbase.database import registry as database_registry
from heavenbase.database.registry import DatabaseEngineRegistry
from heavenbase.utils.config import ConfigManager

pytestmark = [pytest.mark.full, pytest.mark.slow]

_CODE = {database_registry.__file__, sql_base.__file__}
_THREADING = threading.__file__
# Seconds another thread has for one call. Only an interrupt that left a lock held reaches it.
_STUCK = 15


class _Interrupt(BaseException):
    """What a signal handler raises."""


class InterruptNote(hb.Entity):
    identifier = "interrupt-note"
    title = hb.field(hb.ShortText)


def _interrupted(call, nth):
    """Run ``call`` and interrupt it at its ``nth`` interruption point in the code under test; return whether it has one."""
    seen = 0

    def profile(frame, kind, argument):
        nonlocal seen
        if kind not in ("call", "c_return"):
            return
        if kind == "call" and frame.f_lasti >= 0 and sys.version_info < (3, 12):
            return  # an error of the profiler at a resumed generator would skip its handlers; a signal handler's does not
        code = frame
        while code is not None and code.f_code.co_filename == _THREADING:
            code = code.f_back
        if code is None or code.f_code.co_filename not in _CODE:
            return
        seen += 1
        if seen == nth:
            sys.setprofile(None)
            raise _Interrupt

    sys.setprofile(profile)
    try:
        call()
    except _Interrupt:
        pass
    finally:
        sys.setprofile(None)
    return seen >= nth


def _elsewhere(call):
    """Run ``call`` on another thread; return why it did not succeed, if it did not."""
    outcome = ["stuck"]

    def run():
        try:
            call()
            outcome[0] = None
        except Exception as error:
            outcome[0] = repr(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_STUCK)
    return outcome[0]


def _context(root):
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )


def _facade(context, path, provider):
    # Without a pool of its own SQLAlchemy has no connection to lose when an interrupt cuts off its checkin.
    return hb.Database(
        database=hb.utils.path_to_file_uri(path), provider=provider, pool={"pool_class": "null"}, resolver=context.modules(), config=context.config
    )


def _leftovers(db):
    """Return what an interrupted call left behind in the engine registry."""
    registry = DatabaseEngineRegistry
    left = []
    if registry._holds_any_connection() or registry._holds_connection(db.spec._database_key, database_registry._thread_token()):
        left.append("a connection counts as in use")
    if registry._connecting:
        left.append("a connection counts as being opened")
    if any(registry._is_closing(key) for key in tuple(registry._closing)):
        left.append("the file counts as being dropped")
    return left


def _case_statement(context, root, provider):
    db, other = _facade(context, root / f"a.{provider}", provider), _facade(context, root / f"a.{provider}", provider)
    db.execute("CREATE TABLE t (v INTEGER)", readonly=False)

    def statement():
        assert db.execute("SELECT COUNT(*) FROM t", readonly=True).to_lists() == [[0]]

    return statement, lambda: other.execute("SELECT COUNT(*) FROM t", readonly=True).to_lists(), lambda: _leftovers(db)


def _case_closing_file(context, root):
    db, other = _facade(context, root / "a.duckdb", "duckdb"), _facade(context, root / "a.duckdb", "duckdb")
    db.execute("CREATE TABLE t (v INTEGER)", readonly=False)

    def drop():
        with DatabaseEngineRegistry._closing_duckdb_file(db.spec):
            pass

    return drop, lambda: other.execute("SELECT COUNT(*) FROM t", readonly=True).to_lists(), lambda: _leftovers(db)


def _case_gate(context, root):
    gate = database_registry._OpenCloseGate()

    def hold():
        with gate.held():
            gate.close(int)

    def use():
        with gate.held():
            pass

    return hold, use, lambda: ["a close is left deferred"] if gate._deferred else []


def _case_backend_read(context, root):
    pool = {"pool_class": "queue", "pool_size": 2, "max_overflow": 1, "pool_timeout": 5}
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(root / "a.db"), "pool": pool}}
    ws = hb.HeavenBase("interrupted", context=context, detached=True, backends=backends)
    ws.register(InterruptNote)
    ws.upsert(InterruptNote, {"object_id": "a", "title": "first"})
    backend = ws.backends.get("main")
    slots = backend._pool_slots(backend.db)
    assert slots is not None

    def read():
        assert backend.count(InterruptNote.identifier) == 1

    def left():
        # A connection whose checkin SQLAlchemy could not finish is lost to its pool, as in 0.1.2.2.
        backend.db.engine.dispose()
        found = _leftovers(backend.db)
        if backend._read_gate().running:
            found.append("a read counts as running")
        if slots.reads:
            found.append("a read keeps its slot")
        return found

    return read, read, left


_CASES = {
    "sqlite-statement": lambda context, root: _case_statement(context, root, "sqlite"),
    "duckdb-statement": lambda context, root: _case_statement(context, root, "duckdb"),
    "duckdb-closing-file": _case_closing_file,
    "gate": _case_gate,
    "backend-read": _case_backend_read,
}


def _run_case(name, root):
    """Run one case in this process, print what went wrong, and end the process, also when a lock stays held."""
    context = _context(Path(root) / "ctx")
    context._setup_modules()
    call, use, leftovers = _CASES[name](context, Path(root))
    for _ in range(3):
        call()  # open what a first use opens
    problems, nth = [], 1
    while not problems and _interrupted(call, nth):
        stuck = _elsewhere(use)
        problems = [f"point {nth}: {problem}" for problem in ([f"another thread: {stuck}"] if stuck else leftovers())]
        nth += 1
    print("OUTCOME " + json.dumps({"points": nth - 1, "problems": problems}), flush=True)
    os._exit(0)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_an_interrupted_call_leaves_no_lock_held_and_no_connection_counted(tmp_path, home_env, name):
    if name.startswith("duckdb"):
        pytest.importorskip("duckdb_engine")
    path = Path(__file__)
    code = f"import sys; sys.path.insert(0, {str(path.parent)!r}); import {path.stem} as case; case._run_case(*sys.argv[1:])"
    home = tmp_path / "home"
    home.mkdir()
    command = [sys.executable, "-c", code, name, str(tmp_path)]
    child = subprocess.run(command, env=home_env(home), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    lines = [line for line in child.stdout.splitlines() if line.startswith("OUTCOME ")]
    assert lines, child.stdout + child.stderr
    outcome = json.loads(lines[0].removeprefix("OUTCOME "))
    assert outcome["problems"] == [] and outcome["points"] > 5, outcome


def test_a_return_that_is_still_running_is_left_to_its_thread_by_a_drop(tmp_path):
    pytest.importorskip("duckdb_engine")
    user, dropper = _context(tmp_path / "user"), _context(tmp_path / "dropper")
    for context in (user, dropper):
        context._setup_modules()
    path = tmp_path / "shop.duckdb"
    db = hb.Database(database=hb.utils.path_to_file_uri(path), provider="duckdb", resolver=user.modules(), config=user.config)
    db.execute("CREATE TABLE t (v INTEGER)", readonly=False)
    # SQLAlchemy calls an entry not in use before its checkin event, so a slow listener keeps it so while its owner returns it.
    event.listen(db.engine, "invalidate", lambda *arguments: time.sleep(0.3))
    entered, leave = threading.Barrier(5), threading.Event()
    outcomes = []

    def use():
        with db.tx():
            db.execute("SELECT COUNT(*) FROM t")
            entered.wait(60)
            leave.wait(60)

    def keep(call):
        try:
            call()
            outcomes.append("ok")
        except Exception as error:
            outcomes.append(repr(error))

    threads = [threading.Thread(target=keep, args=(use,), daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    entered.wait(60)
    drop = hb.Database(database=hb.utils.path_to_file_uri(path), provider="duckdb", resolver=dropper.modules(), config=dropper.config).drop_database
    threads.append(threading.Thread(target=keep, args=(drop,), daemon=True))
    threads[-1].start()
    time.sleep(0.5)
    leave.set()
    for thread in threads:
        thread.join(60)
    assert outcomes == ["ok"] * 5 and not path.exists()
    for context in (user, dropper):
        context.close()


def test_a_drop_closes_a_connection_whose_return_an_interrupt_cut_off(tmp_path, monkeypatch):
    pytest.importorskip("duckdb_engine")
    monkeypatch.setattr(DatabaseEngineRegistry, "_RETURN_WAIT", 0.5)
    monkeypatch.setattr(DatabaseEngineRegistry, "_DUCKDB_DROP_WAIT", 10.0)
    context = _context(tmp_path / "ctx")
    context._setup_modules()
    path = tmp_path / "shop.duckdb"
    # A static pool keeps its entry, where a queue pool loses one whose return was cut off.
    pool = {"pool_class": "static"}
    db = hb.Database(database=hb.utils.path_to_file_uri(path), provider="duckdb", pool=pool, resolver=context.modules(), config=context.config)
    db.execute("CREATE TABLE t (v INTEGER)", readonly=False)
    cut = [True]

    def interrupt(*arguments):
        if cut.pop():
            raise _Interrupt

    connection = db.engine.raw_connection()
    event.listen(db.engine, "checkin", interrupt, insert=True)
    with pytest.raises(_Interrupt):
        connection.close()
    event.remove(db.engine, "checkin", interrupt)
    assert not DatabaseEngineRegistry._holds_any_connection()
    with DatabaseEngineRegistry._closing_duckdb_file(db.spec):
        pass
    context.close()


def test_an_interrupted_drop_leaves_no_thread_recorded_as_dropping():
    registry = DatabaseEngineRegistry
    nth = 1
    while _interrupted(lambda: _run_drop_scope("database:interrupted"), nth):
        assert registry._drop_threads == {} and registry._droppers == {}, nth
        nth += 1
    assert nth > 3
    with pytest.raises(RuntimeError, match="being dropped"), registry._running_drop("database:nested"):
        with registry._running_drop("database:nested"):
            pass
    assert registry._drop_threads == {} and registry._droppers == {}


def _run_drop_scope(key):
    with DatabaseEngineRegistry._running_drop(key):
        pass
