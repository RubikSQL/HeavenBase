"""A transaction whose begin is interrupted or fails after the driver began leaves nothing that later writes run in.

The connection of such a transaction stayed set on its thread. Later writes of the thread took it for the caller's open
transaction and returned normally, but nothing ever committed them.
"""

import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import heavenbase as hb

pytestmark = [pytest.mark.full, pytest.mark.slow]

_ERRORS = {"interrupt": KeyboardInterrupt, "error": RuntimeError}


class BeginNote(hb.Entity):
    identifier = "begin-note"
    title = hb.field(hb.ShortText)


def _workspace(provider, path, name="begin"):
    ws = hb.HeavenBase(name, detached=True, backends={"main": {"type": provider, "name": "main", "database": hb.utils.path_to_file_uri(path)}})
    ws.register(BeginNote)
    return ws


def _interrupted_write(ws, how, nth):
    """Make the ``nth`` transaction of one write raise right after the driver began it; return what the write did."""
    dialect = ws.backends.get("main").db.engine.dialect
    begin, seen = dialect.do_begin, [0]

    def do_begin(connection):
        begin(connection)
        seen[0] += 1
        if seen[0] == nth:
            raise _ERRORS[how]("stopped")

    dialect.do_begin = do_begin
    try:
        ws.upsert(BeginNote, {"object_id": "interrupted", "title": "t"})
    except BaseException as error:
        return type(error).__name__ if seen[0] >= nth else f"unexpected {error!r}"
    finally:
        dialect.do_begin = begin
    return "not interrupted"


def _run(provider, root):
    """Run every case in this process and print what each one saw."""
    seen = {}
    for how in _ERRORS:
        for nth in (1, 2):
            case = f"{how}-{nth}"
            ws = _workspace(provider, Path(root) / f"{case}.{provider}")
            ws.upsert(BeginNote, {"object_id": "first", "title": "t"})
            found = {"write": _interrupted_write(ws, how, nth)}
            # The same thread reads nothing of the interrupted transaction, and its later write commits.
            found["own_read"] = ws.get("interrupted", entity=BeginNote) is None
            ws.upsert(BeginNote, {"object_id": "later", "title": "t"})
            elsewhere = []
            thread = threading.Thread(target=lambda: elsewhere.append(ws.get("later", entity=BeginNote)), daemon=True)
            thread.start()
            thread.join(60)
            found["other_thread"] = bool(elsewhere and elsewhere[0])
            ws.release()
            dropped = _workspace(provider, Path(root) / f"{case}-dropped.{provider}", "dropped")
            _interrupted_write(dropped, how, nth)
            dropped.drop()
            found["dropped"] = not (Path(root) / f"{case}-dropped.{provider}").exists()
            seen[case] = found
    print("OUTCOME " + json.dumps(seen), flush=True)


def _read(provider, root):
    """Print the rows that a new process finds."""
    cases = [f"{how}-{nth}" for how in _ERRORS for nth in (1, 2)]
    rows = {case: sorted(row["object_id"] for row in _workspace(provider, Path(root) / f"{case}.{provider}").rows(BeginNote)) for case in cases}
    print("OUTCOME " + json.dumps(rows), flush=True)


def _child(call, provider, root, home_env):
    path = Path(__file__)
    home = root / "home"
    home.mkdir(exist_ok=True)
    code = f"import sys; sys.path.insert(0, {str(path.parent)!r}); import {path.stem} as case; case.{call}(*sys.argv[1:])"
    command = [sys.executable, "-c", code, provider, str(root)]
    child = subprocess.run(command, env=home_env(home), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    lines = [line for line in child.stdout.splitlines() if line.startswith("OUTCOME ")]
    assert lines, child.stdout + child.stderr
    return json.loads(lines[0].removeprefix("OUTCOME "))


@pytest.mark.parametrize("provider", ["sqlite", "duckdb"])
def test_writes_after_an_interrupted_begin_commit_and_the_workspace_can_be_dropped(tmp_path, home_env, provider):
    if provider == "duckdb":
        pytest.importorskip("duckdb_engine")
    for case, found in _child("_run", provider, tmp_path, home_env).items():
        assert found.pop("write") not in ("not interrupted",) and all(found.values()), (case, found)
    for case, rows in _child("_read", provider, tmp_path, home_env).items():
        assert rows == ["first", "later"], (case, rows)


def test_a_connection_left_set_by_an_interrupted_exit_is_rolled_back(tmp_path):
    db = hb.Database(database=hb.utils.path_to_file_uri(tmp_path / "a.db"), provider="sqlite")
    db.execute("CREATE TABLE t (v INTEGER)", readonly=False)
    tx = db.tx()
    tx.__enter__()
    db.execute("INSERT INTO t VALUES (1)", readonly=False)
    # As an interrupt inside __exit__ leaves it: the transaction is left, its connection still set.
    db._state.entered = None
    assert not db.in_transaction()
    assert db.execute("SELECT COUNT(*) FROM t", readonly=True).to_lists() == [[0]]
    with db.tx():
        db.execute("INSERT INTO t VALUES (2)", readonly=False)
    assert db.execute("SELECT v FROM t", readonly=True).to_lists() == [[2]]
    db.dispose()
