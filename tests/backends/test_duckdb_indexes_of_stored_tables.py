"""A DuckDB table that has held rows gets no index on a column none covers, so later processes can write the file."""

import os
import subprocess
import sys

import pytest

import heavenbase as hb

pytest.importorskip("duckdb_engine")

# Each process registers one definition, overwrites rows and ends without Context.close().
_CHILD = """
import os, sys, warnings
from pathlib import Path
warnings.simplefilter("ignore")
import heavenbase as hb
from heavenbase.backends.sql.strategies.sparse_gram import _SQLGramIndex

path, step, how = sys.argv[1:4]


def account(fields):
    namespace = {"identifier": "duckdb-grow-account", "__module__": "grow", "name": hb.field(hb.ShortText).required(), "email": hb.field(hb.ShortText).optional()}
    namespace["tags"] = hb.field(hb.Array[hb.ShortText]).default([])
    namespace.update({name: hb.field(hb.ShortText).optional() for name in fields if name not in ("unique", "composite", "noindex")})
    if "unique" in fields:
        namespace["email"] = namespace["email"].unique()
    if "noindex" in fields:
        namespace["email"] = namespace["email"].index(enabled=False)
    if "composite" in fields:
        namespace["__constraints__"] = [hb.unique("name", "email")]
    return type("Account", (hb.Entity,), namespace)


def write(ws, entity):
    for n in range(3):
        # deletes first: overwriting a row of a table whose unique index DuckDB replayed is a DuckDB limit of its own
        ws.delete((entity, str(n)))
        ws.upsert(entity, {"object_id": str(n), "name": f"{step}-{n}", "email": f"e{n}-{os.getpid()}@x.org", "tags": [f"red apple {n}"]})


if step == "0.1.2.2":
    # 0.1.2.2 made the keyword table without the index on the keyword length
    define_tables = _SQLGramIndex._define_tables

    def without_length(self):
        define_tables(self)
        self.keywords.indexes.discard(self.width_index)

    _SQLGramIndex._define_tables = without_length

ws = hb.HeavenBase("grow", detached=True, backends={"main": {"type": "duckdb", "database": hb.utils.path_to_file_uri(Path(path))}})
if step == "live":
    first = account(())
    ws.register(first)
    write(ws, first)
    entity = account(("x1",))
    ws.register(entity, allow_replace=True)
elif step == "live-composite":
    first = account(())
    ws.register(first)
    write(ws, first)
    entity = account(("composite",))
    ws.register(entity, allow_replace=True)
elif step == "alter":
    first = account(())
    ws.register(first)
    write(ws, first)
    entity = ws.alter_entity(account(("x1", "x2")), confirm=first.identifier)
else:
    entity = account({"0": (), "0.1.2.2": (), "1": ("x1",), "2": ("x1", "x2"), "n": ("noindex",)}.get(step) or ({"u": ("unique",), "c": ("composite",)}.get(step[0], ())))
    if step.endswith("!"):
        try:
            ws.register(entity, allow_replace=True)
            raise SystemExit("the constraint was accepted")
        except ValueError as error:
            assert "unique constraint" in str(error)
        entity = account(("noindex",))
    ws.register(entity, allow_replace=True)
if step in ("u", "c", "live-composite"):
    backend = ws.backends.get("main")
    columns = ("email",) if step == "u" else ("name", "email")
    assert backend.db.index_column_sets(backend.tables[entity.identifier])[columns] is True
write(ws, entity)
assert sorted(ws.ids(entity)) == ["0", "1", "2"]
assert {row["name"] for row in ws.rows(entity)} == {f"{step}-{n}" for n in range(3)}
assert ws.query(entity).where(entity.tags.contained_in("a red apple 1")).execute().ids == ["1"]
if "x1" in entity.schema().fields:
    ws.upsert(entity, {"object_id": "2", "name": f"{step}-2", "x1": "found"})
    assert ws.query(entity).where(entity.x1 == "found").execute().ids == ["2"]
sys.stdout.flush()
if how == "kill":
    os._exit(0)
"""


@pytest.mark.slow
@pytest.mark.parametrize("how", ["exit", "kill"])
@pytest.mark.parametrize(
    "steps",
    [
        ("0", "1", "1", "1"),
        ("0", "0", "1", "1"),
        ("0", "1", "0", "1", "2"),
        ("live", "1", "1"),
        ("alter", "2", "2"),
        ("0.1.2.2", "0", "0", "0"),
        ("0", "c", "c", "c"),
        ("0", "0", "c", "c", "c"),
        ("0", "u", "u", "u"),
        ("live-composite", "c", "c"),
        ("n", "u!", "u!", "n"),
        ("n", "c!", "n"),
    ],
    ids="-".join,
)
def test_a_file_stays_writable_for_later_processes_after_a_definition_grew(tmp_path, home_env, steps, how):
    env = home_env(tmp_path / "home")
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    for position, step in enumerate(steps):
        command = [sys.executable, "-c", _CHILD, str(tmp_path / "grow.duckdb"), step, how]
        result = subprocess.run(command, env=env, cwd=tmp_path, text=True, capture_output=True, timeout=180)
        assert result.returncode == 0, f"process {position} ({step}): {result.returncode}\n{result.stdout}{result.stderr[-3000:]}"


def _account(**fields):
    return type("Account", (hb.Entity,), {"identifier": "duckdb-stored-account", "__module__": __name__, "name": hb.field(hb.ShortText).required(), **fields})


def _workspace(database):
    return hb.HeavenBase("stored", detached=True, backends={"main": {"type": "duckdb", "database": database}})


def _indexes(ws):
    backend = ws.backends.get("main")
    return sorted((",".join(item["column_names"]), bool(item["unique"])) for item in backend.db.table_indexes(backend.tables["duckdb-stored-account"]))


@pytest.mark.slow
def test_a_table_that_has_held_rows_gets_no_index_and_refuses_a_new_unique_constraint(tmp_path):
    database = hb.utils.path_to_file_uri(tmp_path / "stored.duckdb")
    plain = {"email": hb.field(hb.ShortText).optional(), "note": hb.field(hb.ShortText).optional().index(enabled=False)}
    ws = _workspace(database)
    try:
        # before the first row a definition may still grow with its indexes
        ws.register(_account())
        ws.register(_account(**plain), allow_replace=True)
        stored = [("email", False), ("name", False)]
        assert _indexes(ws) == stored
        entity = ws.entities["duckdb-stored-account"]
        ws.upsert(entity, {"object_id": "1", "name": "Ada", "email": "a@x.org"})
        ws.delete((entity, "1"))
        ws.upsert(entity, {"object_id": "1", "name": "Ada", "email": "a@x.org"})
    finally:
        ws.release()
    for attempt in range(2):
        ws = _workspace(database)
        try:
            for constraint in ({"code": hb.field(hb.ShortText).optional().unique()}, {"note": hb.field(hb.ShortText).optional().unique()}):
                with pytest.raises(ValueError, match="unique constraint"):
                    ws.register(_account(**{**plain, **constraint}), allow_replace=True)
            assert "code" not in ws.backends.get("main").db.columns("duckdb_stored_account")
            grown = _account(**{**plain, "note": hb.field(hb.ShortText).optional(), "city": hb.field(hb.ShortText).optional()})
            ws.register(grown, allow_replace=True)
            assert _indexes(ws) == stored
            ws.upsert(grown, {"object_id": "1", "name": "Ada", "email": "a@x.org", "city": f"Rome {attempt}"})
            assert ws.query(grown).where(grown.city == f"Rome {attempt}").execute().ids == ["1"]
        finally:
            ws.release()
    ws = _workspace(database)
    try:
        constrained = _account(**{**plain, "email": hb.field(hb.ShortText).optional().unique()})
        ws.register(constrained, allow_replace=True)
        assert _indexes(ws) == [("email", False), ("email", True), ("name", False)]
        with pytest.raises(Exception, match="(?i)duplicate"):
            ws.upsert(constrained, {"object_id": "2", "name": "Bob", "email": "a@x.org"})
    finally:
        ws.release()


@pytest.mark.slow
def test_a_unique_constraint_a_stored_table_has_is_registered_again(tmp_path):
    database = hb.utils.path_to_file_uri(tmp_path / "unique.duckdb")
    for attempt in range(3):
        ws = _workspace(database)
        try:
            entity = _account(email=hb.field(hb.ShortText).optional().unique())
            ws.register(entity, allow_replace=bool(attempt % 2))
            ws.upsert(entity, {"object_id": "1", "name": "Ada", "email": "a@x.org"})
            with pytest.raises(Exception, match="(?i)duplicate"):
                ws.upsert(entity, {"object_id": "2", "name": "Bob", "email": "a@x.org"})
            assert _indexes(ws) == [("email", True), ("name", False)]
        finally:
            ws.release()


@pytest.mark.slow
def test_an_in_memory_duckdb_table_still_gets_the_index_of_a_new_field():
    ws = hb.HeavenBase("memory", detached=True, backends={"main": {"type": "duckdb", "database": ":memory:"}})
    try:
        first = _account()
        ws.register(first)
        ws.upsert(first, {"object_id": "1", "name": "Ada"})
        ws.register(_account(email=hb.field(hb.ShortText).optional()), allow_replace=True)
        assert _indexes(ws) == [("email", False), ("name", False)]
    finally:
        ws.release()
