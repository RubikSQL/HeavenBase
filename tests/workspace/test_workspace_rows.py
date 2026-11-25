"""Workspace row command protocol contracts."""

from collections import Counter
from contextlib import contextmanager
import dataclasses
import re
import sys
import threading
import types
import weakref

import heavenbase as hb
import pytest

from heavenbase.backends.base import Backend
from heavenbase.execution.contracts import RowOp
from heavenbase.workspace.catalog.rows import CATALOG_DEFAULTS, build_catalog_row
from heavenbase.workspace.rows.inputs import DeleteTarget, RowPatch, UpsertRow
from heavenbase.workspace.rows.lookup import RowMatch
from heavenbase.workspace.rows.writer import DeletePlan, DeleteRoute, UpsertPlan


class CommandNote(hb.Entity):
    """Small entity used to exercise workspace row command values."""

    identifier = "command-note"

    title = hb.field(hb.ShortText)
    status = hb.field(hb.ShortText).default("draft")


class DecodedNote(hb.Entity):
    """Entity whose fields need the row codec, so a stored fragment differs visibly from its logical row."""

    identifier = "decoded-note"

    title = hb.field(hb.ShortText)
    meta = hb.field(hb.Json).default({})
    seen = hb.field(hb.Timestamp)
    vec = hb.field(hb.Vector[4])
    note = hb.field(hb.ShortText).optional()


class OtherNote(hb.Entity):
    """Second entity that may share an ``object_id`` with ``DecodedNote``."""

    identifier = "other-note"

    title = hb.field(hb.ShortText)


_SEEN = "2026-01-01T00:00:00Z"
_SEEN_MS = 1767225600000


def _command_workspace(identifier):
    workspace = hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "inmem"}})
    workspace.register(CommandNote)
    return workspace


def _decoded_row(index):
    return {"object_id": f"d{index}", "title": f"Title {index}", "meta": {"n": index}, "seen": _SEEN, "vec": [0.1, 0.2, 0.3, float(index)]}


def _sqlite_workspace(tmp_path, identifier):
    database = hb.utils.path_to_file_uri(tmp_path / f"{identifier}.db")
    workspace = hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}})
    workspace.register(DecodedNote)
    workspace.register(OtherNote)
    return workspace


@contextmanager
def _captured_sql(workspace):
    """Record every SQL statement the ``main`` backend executes inside the block."""
    from sqlalchemy import event

    engine = workspace.backends.get("main").db.engine
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_row_inputs_produce_isolated_named_commands():
    """Upsert and set normalization should replace positional tuples with named values."""
    workspace = _command_workspace("row-command-inputs")
    source = {"object_id": "n1", "title": "First"}
    upsert = workspace._row_inputs.upsert_row(CommandNote, source, {})
    source["title"] = "Changed"

    assert isinstance(upsert, UpsertRow)
    assert upsert.entity_cls is CommandNote
    assert upsert.data == {"object_id": "n1", "title": "First"}

    workspace.upsert(CommandNote, upsert.data)
    single = workspace._row_inputs.set_patch({"object_id": "n1", "status": "active"}, None, entity=CommandNote)
    batch = workspace._row_inputs.set_patches([{"object_id": "n1", "title": "Updated"}], entity=CommandNote)

    assert isinstance(single, RowPatch)
    assert single.object_id == "n1"
    assert single.patch == {"object_id": "n1", "status": "active"}
    assert len(batch) == 1 and isinstance(batch[0], RowPatch)
    assert batch[0].object_id == "n1"


def test_row_op_isolates_json_rows_without_per_element_deepcopy(monkeypatch):
    """A RowOp and its replacement row are private copies, built without one deepcopy per vector element."""
    import copy
    import datetime

    calls = []
    original = copy.deepcopy
    monkeypatch.setattr(copy, "deepcopy", lambda value, memo=None: calls.append(type(value)) or original(value, memo))

    vector = [float(index) for index in range(2048)]
    source = {"title": "t", "embedding": vector, "meta": {"tags": ["a"]}}
    op = RowOp("command-note", "n1", source)
    vector.append(-1.0)
    source["meta"]["tags"].append("b")
    assert op.values["embedding"] == vector[:-1] and op.values["meta"] == {"tags": ["a"]}

    row = op.to_row()
    row["embedding"].append(-2.0)
    row["meta"]["tags"].append("c")
    assert op.values["embedding"] == vector[:-1] and op.values["meta"] == {"tags": ["a"]}
    assert row["object_id"] == "n1"
    assert op.to_row(fields=["title"]) == {"title": "t", "object_id": "n1"}
    assert calls == []

    stamped = RowOp("command-note", "n2", {"when": datetime.datetime(2026, 9, 11)})
    assert stamped.values["when"] == datetime.datetime(2026, 9, 11)
    assert calls[:1] == [datetime.datetime]


def test_catalog_row_defaults_are_fresh_containers_without_deepcopy(monkeypatch):
    """Every Catalog row gets its own default containers; scalar defaults are shared, nothing is deep-copied.

    ``copy.deepcopy`` is replaced both on ``copy`` and under every name a loaded
    HeavenBase module bound with ``from ... import deepcopy``: patching only the
    ``copy`` attribute would miss such an import-time binding, which is how
    0.1.2.2 deep-copied every default.
    """
    import copy
    import sys

    calls = []
    original = copy.deepcopy

    def counting(value, memo=None):
        calls.append(type(value))
        return original(value, memo)

    monkeypatch.setattr(copy, "deepcopy", counting)
    for name, module in list(sys.modules.items()):
        if (name == "heavenbase" or name.startswith("heavenbase.")) and getattr(module, "__dict__", {}).get("deepcopy") is original:
            monkeypatch.setattr(module, "deepcopy", counting)

    first = build_catalog_row("ws", CommandNote.schema(), "n1", 255)
    second = build_catalog_row("ws", CommandNote.schema(), "n2", 255)

    assert calls == []
    assert {key: first[key] for key in CATALOG_DEFAULTS} == CATALOG_DEFAULTS
    for key, default in CATALOG_DEFAULTS.items():
        if isinstance(default, (list, dict)):
            assert first[key] is not default and first[key] is not second[key]
    first["keywords"].append("k")
    first["metadata"]["x"] = 1
    assert CATALOG_DEFAULTS["keywords"] == [] and CATALOG_DEFAULTS["metadata"] == {} and second["keywords"] == []
    assert first["target_id"] == "n1" and first["target_entity"] == "command-note" and first["ws"] == "ws"


def test_row_lookup_returns_named_match():
    """Object-id lookup should expose entity, identity, and row by name."""
    workspace = _command_workspace("row-command-match")
    workspace.upsert(CommandNote, {"object_id": "n1", "title": "First"})

    match = workspace._row_lookup.id_match("n1", entity=CommandNote)

    assert isinstance(match, RowMatch)
    assert match.entity_cls is CommandNote
    assert match.object_id == "n1"
    assert match.row["title"] == "First"


class ShortCodeCountry(hb.Entity):
    """Entity whose ids are short codes, so longer ids fall outside its object_id domain."""

    identifier = "short-code-country"

    object_id = hb.field(hb.Identifier(max_chars=8)).pk()
    name = hb.field(hb.ShortText)


class LongIdRecord(hb.Entity):
    """Entity whose ids may be longer than the default Identifier limit."""

    identifier = "long-id-record"

    object_id = hb.field(hb.Identifier(max_chars=200)).pk()
    title = hb.field(hb.ShortText)


@pytest.mark.fast
def test_an_entity_less_lookup_treats_an_id_outside_one_entity_domain_as_absent_there():
    """An id one entity's object_id domain rejects is absent from that entity, not an error for the whole lookup."""
    workspace = hb.HeavenBase("row-lookup-id-domains", detached=True, backends={"main": {"type": "inmem"}})
    workspace.register(ShortCodeCountry)
    workspace.register(CommandNote)
    workspace.register(LongIdRecord, catalog="none")
    long_id = "r" * 100
    workspace.upsert(ShortCodeCountry, {"object_id": "fr", "name": "France"})
    workspace.upsert(CommandNote, {"object_id": "report-2026-q3", "title": "Q3"})
    workspace.upsert(LongIdRecord, {"object_id": long_id, "title": "Long"})

    # longer than ShortCodeCountry's 8 characters, within CommandNote's default 63
    assert workspace.get("report-2026-q3")["title"] == "Q3"
    assert workspace.get("missing-12ch") is None
    assert [row and row["object_id"] for row in workspace.get_many(["fr", "report-2026-q3", "missing-12ch"])] == ["fr", "report-2026-q3", None]
    assert workspace.exists("report-2026-q3") is True and workspace.exists("missing-12ch") is False
    assert workspace.exists_many(["fr", "report-2026-q3", "missing-12ch"]) == [True, True, False]
    # longer than Catalog's and CommandNote's 63 characters, within LongIdRecord's 200, under catalog="none"
    assert workspace.get(long_id)["title"] == "Long"
    assert workspace.get_many([long_id, "fr"])[0]["title"] == "Long"
    assert workspace.exists(long_id) is True and workspace.exists_many([long_id, "x" * 150]) == [True, False]
    workspace.set({"object_id": long_id, "title": "Longer"})
    assert workspace.get(long_id, entity=LongIdRecord)["title"] == "Longer"
    assert workspace.delete(long_id) is True
    assert workspace.exists(long_id) is False and workspace.get(long_id) is None
    # an id that no present entity can hold is still rejected, as with entity=..., naming the widest domain's limit
    with pytest.raises(ValueError, match=r"^Identifier exceeds 200 characters$"):
        workspace.get("x" * 201)
    with pytest.raises(ValueError, match=r"^Identifier exceeds 200 characters$"):
        workspace.exists_many(["fr", "x" * 201])
    with pytest.raises(ValueError, match=r"^Identifier exceeds 200 characters$"):
        workspace.get_many(["fr", "x" * 150, "y" * 250])
    with pytest.raises(ValueError, match=r"^Identifier exceeds 200 characters$"):
        workspace.delete("y" * 250)
    with pytest.raises(ValueError, match="cannot be empty"):
        workspace.get("")


@pytest.mark.slow
@pytest.mark.parametrize(("method", "backend_method"), [("exists_many", "exists_many"), ("get_many", "get_many")])
def test_entityless_bulk_reads_reject_invalid_ids_before_backend_io(monkeypatch, method, backend_method):
    workspace = _command_workspace("row-bulk-id-validation")
    workspace.register(LongIdRecord, catalog="none")

    def fail(*args, **kwargs):
        raise RuntimeError("unexpected backend read")

    monkeypatch.setattr(workspace.backends.get("main"), backend_method, fail)
    try:
        with pytest.raises(ValueError, match="Identifier cannot be empty"):
            getattr(workspace, method)(["n1", "", "x" * 201])
        with pytest.raises(ValueError, match="Identifier exceeds 200 characters"):
            getattr(workspace, method)(["n1", "x" * 201, ""])
    finally:
        workspace.release()


@pytest.mark.fast
@pytest.mark.parametrize(("method", "backend_method", "missing"), [("exists_many", "exists_many", False), ("get_many", "get_many", None)])
def test_entityless_bulk_reads_release_each_normalized_id_batch(monkeypatch, method, backend_method, missing):
    class TrackedId(str):
        pass

    workspace = hb.HeavenBase("row-bulk-id-memory", detached=True, backends={"main": {"type": "inmem"}})
    ids = range(1000, 1100)
    normalized_refs = []
    try:
        for index in range(8):
            workspace.register({"name": f"MemoryNote{index}", "entity_id": f"memory-note-{index}", "fields": {"title": {"type": "short-text"}}}, catalog="none")

        lookup = workspace._row_lookup
        normalize = lookup.normalize_object_id

        def track(schema, object_id):
            value = TrackedId(normalize(schema, object_id))
            normalized_refs.append(weakref.ref(value))
            return value

        backend = workspace.backends.get("main")
        read = getattr(backend, backend_method)
        live_counts = []

        def record(entity_id, object_ids, **options):
            live_counts.append(sum(ref() is not None for ref in normalized_refs))
            return read(entity_id, object_ids, **options)

        monkeypatch.setattr(lookup, "normalize_object_id", track)
        monkeypatch.setattr(backend, backend_method, record)
        assert getattr(workspace, method)(ids) == [missing] * len(ids)
        assert live_counts and max(live_counts) <= 2 * len(ids)
    finally:
        workspace.release()


@pytest.mark.slow
def test_entityless_bulk_reads_bound_retained_matches_when_every_id_is_ambiguous(monkeypatch):
    class TrackedId(str):
        pass

    workspace = hb.HeavenBase("row-ambiguous-id-memory", detached=True, backends={"main": {"type": "inmem"}})
    ids = range(1000, 1010)
    normalized_refs = []
    names = [f"memory-note-{index}" for index in range(12)]
    try:
        for index, name in enumerate(names):
            workspace.register({"name": f"MemoryNote{index}", "entity_id": name, "fields": {"title": {"type": "short-text"}}}, catalog="none")
            workspace.upsert_many(name, [{"object_id": object_id, "title": name} for object_id in ids])

        lookup = workspace._row_lookup
        normalize = lookup.normalize_object_id
        backend = workspace.backends.get("main")
        read = backend.get_many
        live_counts = []

        def track(schema, object_id):
            value = TrackedId(normalize(schema, object_id))
            normalized_refs.append(weakref.ref(value))
            return value

        def record(entity_id, object_ids, **options):
            live_counts.append(sum(ref() is not None for ref in normalized_refs))
            return read(entity_id, object_ids, **options)

        monkeypatch.setattr(lookup, "normalize_object_id", track)
        monkeypatch.setattr(backend, "get_many", record)
        message = f"object_id 1000 exists in multiple entities: {', '.join(names[:8])}, +4 more; pass entity=... to disambiguate"
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            workspace.get_many(ids, unique=False)
        assert live_counts and max(live_counts) <= 2 * len(ids)
    finally:
        workspace.release()


@pytest.mark.slow
def test_entity_known_reads_touch_only_the_entity_table(tmp_path):
    """Naming the entity must read its routed table directly and never consult Catalog."""
    workspace = _sqlite_workspace(tmp_path, "row-entity-known-reads")
    workspace.upsert_many(DecodedNote, [_decoded_row(1), _decoded_row(2)])
    workspace.upsert(OtherNote, {"object_id": "d1", "title": "Other"})

    with _captured_sql(workspace) as control:
        catalog_path = workspace.get("d2")
    assert any("sys_catalog" in statement for statement in control)  # the entity-less path is what the capture must see
    with pytest.raises(ValueError, match="exists in multiple entities"):
        workspace.get("d1")

    with _captured_sql(workspace) as statements:
        by_class = workspace.get_many(["d2", "d1", "missing"], entity=DecodedNote)
        by_identifier = workspace.get_many(["d2", "d1", "missing"], entity="decoded-note")
        read = workspace.read_rows(DecodedNote, ["d2", "d1", "missing"])
        single = workspace.get("d2", entity=DecodedNote)
        by_instance = workspace.get("d1", entity=DecodedNote.from_dict(_decoded_row(9)))
        other = workspace.get("d1", entity=OtherNote)

    assert by_class == by_identifier == read
    assert read[0] == single == catalog_path
    assert read[1] == by_instance
    assert by_instance["title"] == "Title 1"
    assert read[2] is None
    assert other == {"object_id": "d1", "title": "Other"}
    assert len(statements) == 6
    assert all(statement.startswith("SELECT") for statement in statements)
    assert not any("sys_catalog" in statement for statement in statements)
    assert all("FROM decoded_note" in statement for statement in statements[:5])
    assert "FROM other_note" in statements[5]


@pytest.mark.slow
def test_projected_lookup_uses_storage_projection_in_a_sql_backend_subclass(tmp_path):
    class CustomSQLiteBackend(hb.backends.SQLiteBackend):
        pass

    backend = CustomSQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "custom-sqlite.db"))
    workspace = hb.HeavenBase("row-custom-sqlite-projection", detached=True, backends={"main": backend})
    try:
        workspace.register(DecodedNote, catalog="none")
        workspace.upsert(DecodedNote, _decoded_row(1))
        with _captured_sql(workspace) as statements:
            assert workspace.get("d1", entity=DecodedNote, columns=["title"]) == {"object_id": "d1", "title": "Title 1"}
        assert len(statements) == 1
        assert statements[0].startswith("SELECT decoded_note.object_id, decoded_note.title FROM decoded_note ")
    finally:
        workspace.release()


@pytest.mark.fast
def test_read_rows_and_iter_rows_decode_exactly_like_get(tmp_path):
    """Both entity-first reads must return the codec-decoded logical row, not the stored fragment."""
    workspace = _sqlite_workspace(tmp_path, "row-entity-decoded-reads")
    ids = ["d1", "d2", "d3"]
    workspace.upsert_many(DecodedNote, [_decoded_row(index) for index in (1, 2, 3)])
    fragment = workspace.backends.get("main").get_many(DecodedNote.schema().entity_id, ["d1"])[0]
    expected = [workspace.get(object_id, entity=DecodedNote) for object_id in ids]

    with _captured_sql(workspace) as statements:
        streamed = list(workspace.iter_rows(DecodedNote, batch_size=2))

    assert isinstance(fragment["meta"], str)  # the stored fragment carries encoded JSON text
    assert expected[0] == {"object_id": "d1", "title": "Title 1", "meta": {"n": 1}, "seen": _SEEN_MS, "vec": [0.1, 0.2, 0.3, 1.0], "note": None}
    assert workspace.read_rows(DecodedNote, ids) == expected
    assert streamed == expected
    assert len(statements) == 3  # two keyset pages plus the empty page that ends the scan
    assert all("FROM decoded_note" in statement and "LIMIT" in statement for statement in statements)
    assert not any("sys_catalog" in statement for statement in statements)


@pytest.mark.fast
def test_iter_rows_admits_writes_between_pages_and_is_not_a_snapshot(tmp_path):
    """The read lock is released per page: a consumer may write, and only rows ahead of the cursor appear."""
    workspace = _sqlite_workspace(tmp_path, "row-iter-not-snapshot")
    workspace.upsert_many(DecodedNote, [_decoded_row(1), _decoded_row(3)])

    rows = workspace.iter_rows(DecodedNote, batch_size=1)
    assert next(rows)["object_id"] == "d1"
    workspace.upsert(DecodedNote, _decoded_row(4))  # ahead of the cursor: admitted now, seen later
    workspace.upsert(DecodedNote, _decoded_row(0))  # behind the cursor: never seen by this iteration

    assert [row["object_id"] for row in rows] == ["d3", "d4"]
    assert workspace.count(DecodedNote) == 4


@pytest.mark.fast
@pytest.mark.parametrize("split", [False, True])
def test_iter_rows_decodes_optional_field_added_between_pages(split):
    workspace = hb.HeavenBase("row-iter-add-field", detached=True, backends={"main": {"type": "inmem"}, "zaux": {"type": "inmem"}})

    class PagedNote(hb.Entity):
        identifier = "paged-note"

        title = hb.field(hb.ShortText).store(to="zaux" if split else "main")
        body = hb.field(hb.ShortText).optional().store(to="main")

    class ExtendedNote(hb.Entity):
        identifier = "paged-note"

        title = hb.field(hb.ShortText).store(to="zaux" if split else "main")
        body = hb.field(hb.ShortText).optional().store(to="main")
        payload = hb.field(hb.Json).optional().store(to="main")

    try:
        workspace.register(PagedNote, catalog="none")
        workspace.upsert_many(PagedNote, [{"object_id": key, "title": key, "body": key} for key in ("a", "b")])
        rows = workspace.iter_rows(PagedNote, batch_size=1)
        assert next(rows)["object_id"] == "a"

        workspace.alter_entity(ExtendedNote, confirm="paged-note")
        workspace.upsert("paged-note", {"object_id": "b", "title": "b", "body": "b", "payload": {"a": 1}})

        expected = workspace.get("b", entity="paged-note")
        assert expected["payload"] == {"a": 1}
        assert list(rows) == [expected]
    finally:
        workspace.release()


@pytest.mark.slow
@pytest.mark.parametrize("batch_size", [1, 2, 3, 128])
def test_iter_rows_keeps_object_id_order_when_only_a_later_route_holds_a_row(batch_size):
    """A row whose fragment only a later route holds takes its ``object_id`` place, once, in bounded pages."""
    workspace = hb.HeavenBase(f"row-iter-route-order-{batch_size}", detached=True, backends={"main": {"type": "inmem"}, "zaux": {"type": "inmem"}})

    class SplitNote(hb.Entity):
        identifier = "split-order-note"

        title = hb.field(hb.ShortText).store(to="zaux")
        body = hb.field(hb.ShortText).optional()

    workspace.register(SplitNote, catalog="none")
    for object_id in ("r1", "r3", "r5"):
        workspace.upsert(SplitNote, {"object_id": object_id, "title": object_id, "body": "both"})
    workspace.backends.get("zaux").upsert([RowOp(SplitNote.identifier, object_id, {"object_id": object_id, "title": object_id}) for object_id in ("r0", "r4")])

    assert workspace.get("r0", entity=SplitNote)["title"] == "r0"
    assert [row["object_id"] for row in workspace.iter_rows(SplitNote, batch_size=batch_size)] == ["r0", "r1", "r3", "r4", "r5"]
    assert [row.get("body") for row in workspace.iter_rows(SplitNote, batch_size=batch_size)] == [None, "both", "both", None, "both"]
    pages = list(workspace._row_store.iter_row_pages(SplitNote.identifier, batch_size=batch_size))
    assert all(0 < len(page) <= batch_size for page in pages)
    assert [row["object_id"] for page in pages for row in page] == ["r0", "r1", "r3", "r4", "r5"]


class SplitRouteNote(hb.Entity):
    """Entity whose ``title`` lives on a second route, so a complete row has a fragment on both routes."""

    identifier = "split-route-note"

    title = hb.field(hb.ShortText).store(to="zaux")
    body = hb.field(hb.ShortText).optional()


def _split_workspace(identifier):
    workspace = hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "inmem"}, "zaux": {"type": "inmem"}})
    workspace.register(SplitRouteNote, catalog="none")
    return workspace


def _write_partial_rows(workspace, object_ids):
    """Store rows on the second route only, as a multi-backend write that failed part-way leaves them."""
    rows = [RowOp(SplitRouteNote.identifier, object_id, {"object_id": object_id, "title": object_id}) for object_id in object_ids]
    workspace.backends.get("zaux").upsert(rows)


def _count_backend_reads(monkeypatch, workspace):
    calls = Counter()
    for name in ("main", "zaux"):
        backend = workspace.backends.get(name)
        for method in ("scan_page", "iter_scan", "get_many", "exists_many"):

            def counted(*args, _read=getattr(backend, method), _key=f"{name}.{method}", **kwargs):
                calls[_key] += 1
                return _read(*args, **kwargs)

            monkeypatch.setattr(backend, method, counted)
    return calls


@pytest.mark.fast
@pytest.mark.parametrize("portable", [False, True])
def test_iter_rows_reads_one_page_per_route_for_each_page_it_yields(monkeypatch, portable):
    """The first row and an early close cost one page per route; a full scan reads every route once, with no lookups.

    When every route holds every row, the merged pages already carry all
    fragments. A route that inherits the portable ``scan_page`` enumerates
    itself once per page it serves, so it is read once for the first row.
    """
    workspace = _split_workspace(f"row-iter-page-cost-{int(portable)}")
    workspace.upsert_many(SplitRouteNote, [{"object_id": f"r{index:04d}", "title": f"t{index}", "body": "b"} for index in range(400)])
    if portable:
        zaux = workspace.backends.get("zaux")
        monkeypatch.setattr(zaux, "scan_page", types.MethodType(Backend.scan_page, zaux))
    calls = _count_backend_reads(monkeypatch, workspace)

    rows = workspace.iter_rows(SplitRouteNote, batch_size=16)
    assert next(rows) == {"object_id": "r0000", "title": "t0", "body": "b"}
    rows.close()
    assert calls == Counter({"main.scan_page": 1, "zaux.scan_page": 1, **({"zaux.iter_scan": 1} if portable else {})})

    calls.clear()
    assert [row["object_id"] for row in workspace.iter_rows(SplitRouteNote, batch_size=16)] == [f"r{index:04d}" for index in range(400)]
    assert calls["main.scan_page"] == calls["zaux.scan_page"] == 400 // 16 + 1
    assert calls["main.get_many"] == calls["zaux.get_many"] == calls["main.exists_many"] == calls["zaux.exists_many"] == 0


@pytest.mark.slow
@pytest.mark.parametrize("route, cap, batch_size", [("main", 2, 4), ("main", 1, 3), ("zaux", 2, 4), ("zaux", 3, 4), ("zaux", 1, 2)])
def test_iter_rows_yields_every_row_when_a_route_returns_short_pages(monkeypatch, route, cap, batch_size):
    """A route whose ``scan_page`` returns fewer than ``batch_size`` rows before its end loses no row.

    ``Backend.scan_page`` promises at most ``batch_size`` rows and ends the
    scan only with an empty page, so a short page is not that route's last.
    """
    workspace = _split_workspace(f"row-iter-short-page-{route}-{cap}-{batch_size}")
    complete = [f"r{index:02d}" for index in range(1, 12)]
    workspace.upsert_many(SplitRouteNote, [{"object_id": object_id, "title": object_id, "body": "both"} for object_id in complete])
    _write_partial_rows(workspace, ["r00", "r05a", "r12"])
    backend = workspace.backends.get(route)
    native = backend.scan_page

    def short_page(entity_id, *, after, batch_size, columns=None):
        return native(entity_id, after=after, batch_size=min(cap, batch_size), columns=columns)

    monkeypatch.setattr(backend, "scan_page", short_page)
    expected = sorted([*complete, "r00", "r05a", "r12"])
    rows = list(workspace.iter_rows(SplitRouteNote, batch_size=batch_size))
    assert [row["object_id"] for row in rows] == expected
    assert all(row == workspace.get(row["object_id"], entity=SplitRouteNote) for row in rows)
    pages = list(workspace._row_store.iter_row_pages(SplitRouteNote.identifier, batch_size=batch_size))
    assert all(0 < len(page) <= batch_size for page in pages)
    assert [row["object_id"] for page in pages for row in page] == expected


@pytest.mark.slow
@pytest.mark.parametrize("batch_size", [1, 2, 3])
def test_iter_rows_yields_a_row_repaired_between_pages_once(batch_size):
    """A partial row completed while the scan is paused is yielded once, with the repaired fields."""
    workspace = _split_workspace(f"row-iter-repair-once-{batch_size}")
    for object_id in ("r1", "r3", "r5"):
        workspace.upsert(SplitRouteNote, {"object_id": object_id, "title": object_id, "body": "both"})
    _write_partial_rows(workspace, ["r0", "r4"])

    seen = []
    for row in workspace.iter_rows(SplitRouteNote, batch_size=batch_size):
        seen.append((row["object_id"], row.get("body")))
        if row["object_id"] == "r1":
            workspace.upsert(SplitRouteNote, {"object_id": "r4", "title": "r4", "body": "repaired"})
    assert seen == [("r0", None), ("r1", "both"), ("r3", "both"), ("r4", "repaired"), ("r5", "both")]


@pytest.mark.slow
@pytest.mark.parametrize("batch_size", [1, 2, 3, 128])
def test_iter_rows_yields_each_row_once_when_a_route_orders_ids_otherwise(monkeypatch, batch_size):
    """A route ordering ids by a case-insensitive collation still yields each row once, fully hydrated."""
    workspace = _split_workspace(f"row-iter-collation-{batch_size}")
    complete = ["a", "B", "c", "D", "e", "F"]
    workspace.upsert_many(SplitRouteNote, [{"object_id": object_id, "title": object_id, "body": "both"} for object_id in complete])
    _write_partial_rows(workspace, ["g", "H"])
    zaux = workspace.backends.get("zaux")
    native = zaux.scan_page

    def case_insensitive_page(entity_id, *, after, batch_size, columns=None):
        def key(object_id):
            return (object_id.lower(), object_id)

        rows = sorted(native(entity_id, after=None, batch_size=10_000, columns=columns), key=lambda row: key(row["object_id"]))
        return [row for row in rows if after is None or key(row["object_id"]) > key(after)][:batch_size]

    monkeypatch.setattr(zaux, "scan_page", case_insensitive_page)
    rows = list(workspace.iter_rows(SplitRouteNote, batch_size=batch_size))
    assert sorted(row["object_id"] for row in rows) == sorted([*complete, "g", "H"])
    assert all(row["title"] == row["object_id"] for row in rows)
    assert {row["object_id"]: row.get("body") for row in rows} == {**{object_id: "both" for object_id in complete}, "g": None, "H": None}


@pytest.mark.fast
def test_iter_rows_validates_eagerly_and_checks_liveness_per_page():
    """Bad input fails at the call; a retired workspace fails at the next page; early close is clean."""
    workspace = _command_workspace("row-iter-eager")
    workspace.upsert_many(CommandNote, [{"object_id": "n1", "title": "One"}, {"object_id": "n2", "title": "Two"}])

    for batch_size in (0, -1, True, 2.0):
        with pytest.raises(ValueError, match="batch_size must be a positive integer"):
            workspace.iter_rows(CommandNote, batch_size=batch_size)

    rows = workspace.iter_rows(CommandNote, batch_size=1)
    assert next(rows)["object_id"] == "n1"
    rows.close()

    rows = workspace.iter_rows(CommandNote, batch_size=1)
    assert next(rows)["object_id"] == "n1"
    workspace._authority_token = "retired"
    with pytest.raises(RuntimeError, match="retired Context authority generation"):
        next(rows)


@pytest.mark.fast
def test_iter_rows_holds_no_backend_lock_between_pages_so_another_thread_may_write():
    """A reader paused between pages and a writer on another thread must both complete.

    The in-memory backend's ``iter_scan`` generator keeps the backend lock across
    ``yield``; paging through ``scan_page`` instead means the paused reader owns no
    backend lock, so the writer's workspace-write-then-backend order cannot invert.
    """
    workspace = _command_workspace("row-iter-threads")
    workspace.upsert_many(CommandNote, [{"object_id": f"n{index}", "title": str(index)} for index in range(3)])
    rows = workspace.iter_rows(CommandNote, batch_size=1)
    assert next(rows)["object_id"] == "n0"  # the reader now sits between pages
    outcome = {}

    def write():
        outcome["written"] = workspace.upsert(CommandNote, {"object_id": "n9", "title": "late"})

    def read_rest():
        outcome["rest"] = [row["object_id"] for row in rows]

    for target in (write, read_rest):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), f"{target.__name__} deadlocked against the paused iteration"
    assert outcome == {"written": "n9", "rest": ["n1", "n2", "n9"]}


class KeywordNote(hb.Entity):
    """Entity with a projectable name and a keyword array that the sparse-GRAM adapter indexes by default."""

    identifier = "keyword-note"

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).default([])


@contextmanager
def _captured_commits(workspace):
    """Record every transaction commit the ``main`` backend performs inside the block."""
    from sqlalchemy import event

    engine = workspace.backends.get("main").db.engine
    commits = []

    def record(conn):
        commits.append(conn)

    event.listen(engine, "commit", record)
    try:
        yield commits
    finally:
        event.remove(engine, "commit", record)


def _statement_tables(statements):
    """Return the ``(verb, table)`` shape of captured statements, ignoring bound values."""
    shapes = []
    for statement in statements:
        words = statement.split()
        verb = words[0]
        table = words[2] if verb in ("INSERT", "DELETE") else words[words.index("FROM") + 1] if "FROM" in words else "?"
        shapes.append((verb, table))
    return shapes


@pytest.mark.fast
def test_registration_options_decide_the_catalog_and_gram_statements_of_a_write(tmp_path):
    """Default writes project and index; identity skips projection and index; none skips Catalog; gram=False skips the field index."""
    workspace = _sqlite_workspace(tmp_path, "row-write-options")
    keyword_row = {"object_id": "k1", "name": "Named", "keywords": ["redis query engine"]}

    workspace.register(KeywordNote)
    with _captured_sql(workspace) as full:
        workspace.upsert(KeywordNote, keyword_row)
    assert _statement_tables(full) == [
        ("SELECT", "sys_catalog"),
        ("DELETE", "keyword_note"),
        ("INSERT", "keyword_note"),  # its condition checks that the shared index state is the one registration read
        ("SELECT", "hb_gram_keywords"),
        ("DELETE", "hb_gram_keywords"),
        ("INSERT", "hb_gram_keywords"),
        ("INSERT", "hb_gram_postings"),
        ("DELETE", "sys_catalog"),
        ("INSERT", "sys_catalog"),
        ("SELECT", "hb_gram_keywords"),
        ("DELETE", "hb_gram_keywords"),
        ("INSERT", "hb_gram_keywords"),  # the same-name projection copies ``keywords`` into the Catalog row
        ("INSERT", "hb_gram_postings"),
    ]
    workspace.delete((KeywordNote, "k1"))

    workspace.register(KeywordNote, catalog="identity", gram=False, allow_replace=True)
    with _captured_sql(workspace) as identity:
        workspace.upsert(KeywordNote, keyword_row)
    # the identity row keeps the stored row's lifecycle, so the batch reads it by id first: one SELECT per batch; the write
    # of the entity whose field is left without the index checks that no other registration indexes it, with one
    # fingerprint read because this registration retired the field's index. The Catalog row the lookup did not find is
    # written without keyword maintenance, and one read afterwards finds no keyword row another writer committed for it.
    assert _statement_tables(identity) == [
        ("SELECT", "sys_catalog"),
        ("DELETE", "keyword_note"),
        ("INSERT", "keyword_note"),
        ("SELECT", "hb_gram_meta"),
        ("DELETE", "sys_catalog"),
        ("INSERT", "sys_catalog"),
        ("SELECT", "hb_gram_keywords"),
    ]
    catalog_row = workspace.get("catalog:keyword-note:k1", entity=hb.Catalog)
    assert (catalog_row["target_id"], catalog_row["target_entity"], catalog_row["name"], catalog_row["keywords"]) == ("k1", "keyword-note", "", [])
    # a rewrite over a stored Catalog row keeps index maintenance, in the row's own transaction, so keyword rows that
    # another writer left on it, even after the lookup, cannot outlive the rewrite. The row statements check the index
    # state, so no fingerprint read precedes selecting and deleting keyword rows.
    with _captured_sql(workspace) as identity_rewrite:
        workspace.upsert(KeywordNote, keyword_row)
    assert _statement_tables(identity_rewrite) == [
        ("SELECT", "sys_catalog"),
        ("DELETE", "keyword_note"),
        ("INSERT", "keyword_note"),
        ("DELETE", "sys_catalog"),
        ("INSERT", "sys_catalog"),
        ("SELECT", "hb_gram_keywords"),
        ("DELETE", "hb_gram_keywords"),
    ]
    with _captured_sql(workspace) as identity_delete:
        assert workspace.delete((KeywordNote, "k1")) is True
    # Both deletes check for shared indexes, including ones created after registration, in their DELETE statements. A
    # Catalog delete keeps index maintenance whatever the mode, so a keyword row can never outlive its
    # Catalog row.
    assert _statement_tables(identity_delete) == [
        ("SELECT", "keyword_note"),
        ("DELETE", "keyword_note"),
        ("DELETE", "sys_catalog"),
        ("SELECT", "hb_gram_keywords"),
        ("DELETE", "hb_gram_keywords"),
    ]

    workspace.register(KeywordNote, catalog="none", allow_replace=True)
    with _captured_sql(workspace) as none:
        workspace.upsert(KeywordNote, keyword_row)
    assert _statement_tables(none) == [("DELETE", "keyword_note"), ("INSERT", "keyword_note")]
    assert workspace.get("catalog:keyword-note:k1", entity=hb.Catalog) is None
    assert workspace.get("k1", entity=KeywordNote)["keywords"] == ["redis query engine"]

    workspace.register(DecodedNote, catalog="full", gram=False, allow_replace=True)
    with _captured_sql(workspace) as plain:
        workspace.upsert(DecodedNote, _decoded_row(1))
    assert _statement_tables(plain)[:3] == [("SELECT", "sys_catalog"), ("DELETE", "decoded_note"), ("INSERT", "decoded_note")]
    assert ("INSERT", "sys_catalog") in _statement_tables(plain)
    assert ("INSERT", "hb_gram_keywords") not in _statement_tables(plain)


@pytest.mark.fast
def test_atomic_upsert_commits_entity_and_catalog_rows_in_one_transaction(tmp_path):
    """``atomic=True`` issues one backend write call, so SQL commits once; the default commits entity rows first."""
    workspace = _sqlite_workspace(tmp_path, "row-write-atomic")

    with _captured_sql(workspace) as phased, _captured_commits(workspace) as phased_commits:
        workspace.upsert_many(DecodedNote, [_decoded_row(1), _decoded_row(2)])
    assert len(phased_commits) == 2
    assert _statement_tables(phased).index(("INSERT", "decoded_note")) < _statement_tables(phased).index(("INSERT", "sys_catalog"))

    with _captured_sql(workspace) as atomic, _captured_commits(workspace) as atomic_commits:
        workspace.upsert_many(DecodedNote, [_decoded_row(3), _decoded_row(4)], atomic=True)
        workspace.upsert(DecodedNote, _decoded_row(5), atomic=True)
    assert len(atomic_commits) == 2
    assert _statement_tables(atomic)[:7] == [
        ("SELECT", "sys_catalog"),  # the two stored Catalog rows of the batch are read in one lookup
        ("DELETE", "decoded_note"),
        # the DELETE finds no stored row, so the first new row is inserted on its own by an INSERT that checks the
        # keyword-index state, and the second one follows
        ("INSERT", "decoded_note"),
        ("INSERT", "decoded_note"),
        ("DELETE", "sys_catalog"),
        ("INSERT", "sys_catalog"),
        ("INSERT", "sys_catalog"),
    ]
    assert workspace.read_rows(DecodedNote, ["d3", "d4", "d5"]) == [workspace.get(f"d{index}", entity=DecodedNote) for index in (3, 4, 5)]
    assert workspace.audit()["catalog"]["ok"] is True


@pytest.mark.fast
def test_atomic_upsert_refuses_writes_that_span_backends():
    """The one-transaction guarantee cannot cover two backends, so the request fails before any write."""
    workspace = hb.HeavenBase("row-write-atomic-span", detached=True, backends={"main": {"type": "inmem"}, "zaux": {"type": "inmem"}})

    class SpanNote(hb.Entity):
        identifier = "span-note"

        title = hb.field(hb.ShortText).store(to="zaux")

    workspace.register(SpanNote)
    with pytest.raises(ValueError, match="atomic upsert requires one backend for entity and Catalog rows; routed backends: \\['main', 'zaux'\\]"):
        workspace.upsert(SpanNote, {"object_id": "s1", "title": "Split"}, atomic=True)
    assert workspace.count(SpanNote) == 0
    assert workspace.upsert(SpanNote, {"object_id": "s1", "title": "Split"}) == "s1"

    workspace.register(SpanNote, catalog="none", allow_replace=True)
    assert workspace.upsert(SpanNote, {"object_id": "s2", "title": "Only aux"}, atomic=True) == "s2"
    assert workspace.get("s2", entity=SpanNote)["title"] == "Only aux"


@pytest.mark.slow
@pytest.mark.parametrize("backend", ["json", "pickle"])
def test_atomic_upsert_refuses_a_backend_whose_write_call_is_not_atomic(tmp_path, backend):
    """A File backend saves one record file at a time, so ``atomic=True`` is refused before any write.

    Accepting it would let a failed Catalog record save leave the entity record on disk without its
    Catalog row, which breaks the "both land or neither does" contract.
    """
    path = hb.utils.path_to_file_uri(tmp_path / "rows")
    workspace = hb.HeavenBase(f"row-write-atomic-{backend}", detached=True, backends={"main": {"type": backend, "path": path}})
    workspace.register(CommandNote)

    refusal = re.escape(
        "atomic upsert requires a backend whose write call applies all of its rows or none; "
        f"backend 'main' of type '{backend}' does not claim the 'atomic-upsert' tag"
    )
    with pytest.raises(ValueError, match=refusal):
        workspace.upsert(CommandNote, {"object_id": "n1", "title": "One"}, atomic=True)
    with pytest.raises(ValueError, match=refusal):
        workspace.upsert_many(CommandNote, [{"object_id": "n2", "title": "Two"}, {"object_id": "n3", "title": "Three"}], atomic=True)
    assert workspace.count(CommandNote) == 0

    assert workspace.upsert_many(CommandNote, [{"object_id": "n2", "title": "Two"}]) == ["n2"]
    assert workspace.get("n2", entity=CommandNote)["title"] == "Two"
    assert workspace.audit()["catalog"]["ok"] is True
    assert workspace.backends.get("main").tag("atomic-upsert").value is False


class AtomicIngressNote(hb.Entity):
    identifier = "row-write-atomic-ingress-note"

    title = hb.field(hb.ShortText)


@pytest.mark.slow
def test_refused_atomic_upsert_establishes_no_entity_from_a_metaschema_row(tmp_path):
    """The refusal runs before a definition-shaped MetaSchema row registers its Entity, so a refused call writes nothing."""
    path = hb.utils.path_to_file_uri(tmp_path / "rows")
    workspace = hb.HeavenBase("row-write-atomic-ingress", detached=True, backends={"main": {"type": "json", "path": path}})
    entity_id = AtomicIngressNote.identifier
    row = {"object_id": f"manual:{entity_id}", "kind": "entity", "subject_id": entity_id, "meta": AtomicIngressNote.schema().to_dict()}

    with pytest.raises(ValueError, match="does not claim the 'atomic-upsert' tag"):
        workspace.upsert(hb.MetaSchema, row, atomic=True)
    with pytest.raises(ValueError, match="does not claim the 'atomic-upsert' tag"):
        workspace.upsert_many(hb.MetaSchema, [row], atomic=True)
    assert entity_id not in workspace.entities
    assert workspace.get_many([f"entity:{entity_id}", f"manual:{entity_id}"], entity=hb.MetaSchema) == [None, None]

    workspace.upsert(hb.MetaSchema, row)
    assert entity_id in workspace.entities


class SparseNote(hb.Entity):
    identifier = "row-write-sparse-note"

    title = hb.field(hb.ShortText)
    note = hb.field(hb.ShortText).optional()
    side = hb.field(hb.ShortText).store(to="zside").optional()


class SparseMainNote(hb.Entity):
    identifier = "row-write-sparse-main-note"

    title = hb.field(hb.ShortText)
    note = hb.field(hb.ShortText).optional()
    side = hb.field(hb.ShortText).optional()


@pytest.mark.slow
@pytest.mark.parametrize("atomic", [False, True], ids=["plain", "atomic"])
@pytest.mark.parametrize("order", ["stored-first", "omitted-first"])
def test_sql_batch_stores_each_row_as_given_when_rows_omit_different_optional_fields(order, atomic):
    """A SQL batch whose rows omit different optional fields neither fails nor drops another row's value.

    The plain write also routes ``side`` to a second backend; the atomic one keeps every field on one backend.
    """
    sqlite = {"type": "sqlite", "database": ":memory:"}
    entity = SparseMainNote if atomic else SparseNote
    backends = {"main": dict(sqlite)} if atomic else {"main": dict(sqlite), "zside": dict(sqlite)}
    workspace = hb.HeavenBase(f"row-write-sparse-{order}-{str(atomic).lower()}", detached=True, backends=backends)
    workspace.register(entity)
    rows = [{"object_id": "a", "title": "A", "note": "n", "side": "s"}, {"object_id": "b", "title": "B"}]

    workspace.upsert_many(entity, rows if order == "stored-first" else rows[::-1], atomic=atomic)

    stored = workspace.get_many(["a", "b"], entity=entity)
    assert [(row["title"], row.get("note"), row.get("side")) for row in stored] == [("A", "n", "s"), ("B", None, None)]
    assert workspace.audit()["catalog"]["ok"] is True


class _Uncopyable:
    """Row value whose copy fails, standing in for any failure while a backend builds a later row of its batch."""

    def __deepcopy__(self, memo):
        raise RuntimeError("cannot copy this value")


@pytest.mark.fast
def test_inmem_upsert_applies_no_row_of_a_failed_batch():
    """``inmem`` claims ``atomic-upsert``, so a batch that fails on a later row leaves its earlier rows unapplied."""
    workspace = _command_workspace("row-write-atomic-inmem")
    workspace.upsert(CommandNote, {"object_id": "keep", "title": "Kept"})
    backend = workspace.backends.get("main")
    entity_id = CommandNote.schema().entity_id
    before = backend.get_many(entity_id, ["keep", "new", "bad"])

    failing = RowOp(entity_id, "bad", {"title": "placeholder"})
    failing.values = {"title": _Uncopyable()}
    with pytest.raises(RuntimeError, match="cannot copy this value"):
        backend.upsert([RowOp(entity_id, "keep", op="delete"), RowOp(entity_id, "new", {"title": "New"}), failing])

    assert backend.get_many(entity_id, ["keep", "new", "bad"]) == before
    assert before[0]["title"] == "Kept" and before[1:] == [None, None]
    assert backend.tag("atomic-upsert").value is True


def _refuse_inserts(workspace, entity, condition="1"):
    """Make the ``main`` SQLite backend reject every INSERT into ``entity``'s table that matches ``condition``."""
    from sqlalchemy import text

    backend = workspace.backends.get("main")
    table = backend.tables[entity.schema().entity_id].name
    with backend.db.engine.begin() as conn:
        conn.execute(text(f"CREATE TRIGGER refuse_{table} BEFORE INSERT ON \"{table}\" WHEN {condition} BEGIN SELECT RAISE(ABORT, 'insert refused'); END"))


@pytest.mark.fast
def test_atomic_upsert_on_sql_keeps_no_entity_row_when_its_catalog_row_fails(tmp_path):
    """On SQL ``atomic=True`` is one transaction: a failed Catalog insert also rolls back the entity row before it.

    Without ``atomic`` the entity row commits in its own call first, so it stays without its Catalog row.
    """
    workspace = _sqlite_workspace(tmp_path, "row-write-atomic-rollback")
    workspace.upsert(DecodedNote, _decoded_row(1))
    _refuse_inserts(workspace, hb.Catalog)

    with pytest.raises(Exception, match="insert refused"):
        workspace.upsert_many(DecodedNote, [_decoded_row(2), _decoded_row(3)], atomic=True)
    with pytest.raises(Exception, match="insert refused"):
        workspace.upsert(DecodedNote, {**_decoded_row(1), "title": "Changed"}, atomic=True)
    assert workspace.get_many(["d1", "d2", "d3"], entity=DecodedNote) == [workspace.get("d1", entity=DecodedNote), None, None]
    assert workspace.get("d1", entity=DecodedNote)["title"] == "Title 1"

    with pytest.raises(Exception, match="insert refused"):
        workspace.upsert(DecodedNote, _decoded_row(4))
    assert workspace.get("d4", entity=DecodedNote)["title"] == "Title 4"


@pytest.mark.slow
@pytest.mark.parametrize(
    "engine_options",
    [
        {"isolation_level": "AUTOCOMMIT"},
        {"execution_options": {"isolation_level": "AUTOCOMMIT"}},
        {"connect_args": {"isolation_level": None}},
        pytest.param(
            {"connect_args": {"autocommit": True}},
            marks=pytest.mark.skipif(sys.version_info < (3, 12), reason="sqlite3 autocommit= needs Python 3.12"),
        ),
    ],
    ids=["isolation-level", "execution-options", "sqlite3-autocommit", "sqlite3-autocommit-true"],
)
def test_sql_backend_whose_engine_autocommits_makes_no_atomic_upsert_claim(tmp_path, engine_options):
    """An autocommit engine commits each statement, so ``atomic=True`` is refused instead of half-applied.

    Accepting it would let the entity row commit and stay when its Catalog insert then fails.
    """
    database = hb.utils.path_to_file_uri(tmp_path / "autocommit.db")
    config = {"type": "sqlite", "name": "main", "database": database, **engine_options}
    workspace = hb.HeavenBase("row-write-atomic-autocommit", detached=True, backends={"main": config})
    workspace.register(DecodedNote)
    _refuse_inserts(workspace, hb.Catalog)

    claim = workspace.backends.get("main").tag("atomic-upsert")
    assert (claim.value, claim.source) == (False, "construction")
    with pytest.raises(ValueError, match="backend 'main' of type 'sqlite' does not claim the 'atomic-upsert' tag"):
        workspace.upsert(DecodedNote, _decoded_row(1), atomic=True)
    with pytest.raises(ValueError, match="backend 'main' of type 'sqlite' does not claim the 'atomic-upsert' tag"):
        workspace.upsert_many(DecodedNote, [_decoded_row(2)], atomic=True)
    assert workspace.count(DecodedNote) == 0

    plain = _sqlite_workspace(tmp_path, "row-write-atomic-transactional").backends.get("main").tag("atomic-upsert")
    assert (plain.value, plain.source) == (True, "record")


_SERVER_ENDPOINT = {"database": "heavenbase", "host": "localhost", "username": "root", "password": "password"}


@pytest.mark.fast
@pytest.mark.parametrize(
    "backend_class, options, claimed",
    [
        ("MySQLBackend", {}, True),
        ("MySQLBackend", {"params": {"autocommit": True}}, False),
        ("MySQLBackend", {"params": {"autocommit": "false"}}, False),
        ("MySQLBackend", {"connect_args": {"autocommit": 1}}, False),
        ("MySQLBackend", {"pool": {"connect_args": {"autocommit": "true"}}}, False),
        ("MySQLBackend", {"connect_args": {"autocommit": False}}, True),
        ("MySQLBackend", {"connect_args": {"autocommit": 0}}, True),
        ("MySQLBackend", {"params": {"autocommit": True}, "connect_args": {"autocommit": False}}, True),
        ("MySQLBackend", {"connect_args": {"autocommit": None}}, False),
        ("MySQLBackend", {"pragmas": ["SET autocommit=1"]}, False),
        ("MySQLBackend", {"pragmas": ["SET @@SESSION.AUTOCOMMIT = ON"]}, False),
        ("MySQLBackend", {"pragmas": ["SET sql_mode = 'ANSI'"]}, True),
        ("MsSQLBackend", {}, True),
        ("MsSQLBackend", {"params": {"autocommit": True}}, False),
        ("MsSQLBackend", {"pragmas": ["SET IMPLICIT_TRANSACTIONS OFF"]}, False),
    ],
    ids=[
        "mysql-plain",
        "mysql-params-true",
        "mysql-params-false-string",
        "mysql-connect-args-one",
        "mysql-pool-connect-args-string",
        "mysql-connect-args-false",
        "mysql-connect-args-zero",
        "mysql-connect-args-overrides-params",
        "mysql-connect-args-none",
        "mysql-pragma-autocommit",
        "mysql-pragma-session-autocommit",
        "mysql-pragma-unrelated",
        "mssql-plain",
        "mssql-params-true",
        "mssql-pragma-implicit-transactions",
    ],
)
def test_sql_backend_reads_autocommit_from_the_arguments_its_dbapi_receives(backend_class, options, claimed):
    """``params`` reach the DBAPI through the URL query and ``connect_args`` override them.

    pymysql and pymssql turn on autocommit for any value that is true in Python, so a URL string, even
    ``"false"``, does, while ``False`` or ``0`` given through ``connect_args`` does not. ``None`` leaves the
    mode to the server default, which is autocommit on MySQL, and a pragma naming ``autocommit`` or
    ``implicit_transactions`` runs after the driver set the mode, so either withdraws the claim.
    """
    backend = getattr(hb.backends, backend_class)("server", **_SERVER_ENDPOINT, **options)
    claim = backend.tag("atomic-upsert")
    assert (claim.value, claim.source) == ((True, "record") if claimed else (False, "construction"))


@pytest.mark.fast
def test_failed_atomic_metaschema_write_keeps_the_entity_class_it_registered(tmp_path):
    """``atomic=True`` covers the rows of the call: a definition-shaped MetaSchema row registers its Entity first.

    The registration publishes that class's own MetaSchema row before the write, so a failed write of the
    caller's row leaves the class registered and its published row stored.
    """
    database = hb.utils.path_to_file_uri(tmp_path / "metaschema.db")
    workspace = hb.HeavenBase("row-write-atomic-metaschema", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}})
    entity_id = AtomicIngressNote.identifier
    row = {"object_id": f"manual:{entity_id}", "kind": "entity", "subject_id": entity_id, "meta": AtomicIngressNote.schema().to_dict()}
    _refuse_inserts(workspace, hb.MetaSchema, "NEW.object_id LIKE 'manual:%'")

    with pytest.raises(Exception, match="insert refused"):
        workspace.upsert(hb.MetaSchema, row, atomic=True)
    assert entity_id in workspace.entities
    published, written = workspace.get_many([f"entity:{entity_id}", f"manual:{entity_id}"], entity=hb.MetaSchema)
    assert published is not None and written is None


@pytest.mark.fast
def test_atomic_flag_must_be_a_bool():
    """Without a field named ``atomic`` the keyword is the transaction flag, and a non-bool flag is refused."""
    workspace = hb.HeavenBase("row-write-atomic-flag", detached=True, backends={"main": {"type": "inmem"}})

    workspace.register(CommandNote)
    with pytest.raises(
        TypeError, match="atomic must be a bool, got str; CommandNote declares no field named 'atomic', so the upsert keyword is the one-transaction flag"
    ):
        workspace.upsert(CommandNote, {"object_id": "f1", "title": "Flag"}, atomic="yes")
    with pytest.raises(TypeError, match="^atomic must be a bool, got int$"):
        workspace.upsert_many(CommandNote, [{"object_id": "f2", "title": "Flag"}], atomic=1)
    assert workspace.count(CommandNote) == 0
    assert workspace.upsert(CommandNote, {"object_id": "f1", "title": "Flag"}, atomic=True) == "f1"
    assert workspace.get("f1", entity=CommandNote)["title"] == "Flag"


@pytest.mark.fast
def test_atomic_flag_error_names_a_foreign_bool_type_by_module():
    """A NumPy-style boolean scalar is refused with its qualified type, never the self-contradicting ``got bool``.

    NumPy 2 names ``type(np.True_)`` ``bool``; the synthetic type below has that
    exact name and module, so the check runs whether or not NumPy is installed.
    """
    workspace = hb.HeavenBase("row-write-atomic-foreign-bool", detached=True, backends={"main": {"type": "inmem"}})
    workspace.register(CommandNote)
    foreign_bool = type("bool", (), {"__module__": "numpy", "__bool__": lambda self: True})()
    flags = [foreign_bool]
    try:
        import numpy
    except ImportError:
        pass
    else:
        flags.append(numpy.True_)

    for flag in flags:
        label = f"{type(flag).__module__}.{type(flag).__qualname__}"
        with pytest.raises(TypeError, match=f"^atomic must be a bool, got {re.escape(label)}; CommandNote declares no field named 'atomic'"):
            workspace.upsert(CommandNote, {"object_id": "f1", "title": "Flag"}, atomic=flag)
        with pytest.raises(TypeError, match=f"^atomic must be a bool, got {re.escape(label)}$"):
            workspace.upsert_many(CommandNote, [{"object_id": "f2", "title": "Flag"}], atomic=flag)
    assert workspace.count(CommandNote) == 0


@pytest.mark.slow
@pytest.mark.parametrize("backend", ["inmem", "sqlite"])
def test_atomic_keyword_is_written_when_the_entity_declares_an_atomic_field(tmp_path, backend):
    """A field named ``atomic`` passed as a keyword is written, as in 0.1.2.2, never taken as the flag and dropped."""
    database = hb.utils.path_to_file_uri(tmp_path / "rows.db")
    config = {"type": "sqlite", "name": "main", "database": database} if backend == "sqlite" else {"type": "inmem"}
    workspace = hb.HeavenBase(f"row-write-atomic-field-{backend}", detached=True, backends={"main": config})

    class DefaultedFlag(hb.Entity):
        identifier = "defaulted-flag"

        title = hb.field(hb.ShortText)
        atomic = hb.field(hb.Boolean).default(False)

    class RequiredFlag(hb.Entity):
        identifier = "required-flag"

        title = hb.field(hb.ShortText)
        atomic = hb.field(hb.Boolean)

    class OptionalLabel(hb.Entity):
        identifier = "optional-label"

        title = hb.field(hb.ShortText)
        atomic = hb.field(hb.ShortText).optional()

    workspace.register(DefaultedFlag)
    workspace.register(RequiredFlag)
    workspace.register(OptionalLabel)

    assert workspace.upsert(DefaultedFlag, object_id="d1", title="t", atomic=True) == "d1"
    assert workspace.get("d1", entity=DefaultedFlag)["atomic"] is True
    assert workspace.upsert(RequiredFlag, object_id="r1", title="t", atomic=True) == "r1"
    assert workspace.get("r1", entity=RequiredFlag)["atomic"] is True
    assert workspace.upsert(DefaultedFlag(object_id="d2", title="t", atomic=False), atomic=True) == "d2"
    assert workspace.get("d2", entity=DefaultedFlag)["atomic"] is True
    assert workspace.upsert(OptionalLabel, {"object_id": "o1", "title": "t"}, atomic="yes") == "o1"
    assert workspace.get("o1", entity=OptionalLabel)["atomic"] == "yes"
    assert workspace.upsert(OptionalLabel, {"object_id": "o1", "title": "t", "atomic": "kept"}, atomic=None) == "o1"
    assert workspace.get("o1", entity=OptionalLabel)["atomic"] is None

    assert workspace.upsert_many(DefaultedFlag, [{"object_id": "d3", "title": "t", "atomic": True}], atomic=True) == ["d3"]
    assert workspace.get("d3", entity=DefaultedFlag)["atomic"] is True


@pytest.mark.fast
def test_overwrite_flag_must_be_a_bool():
    """Without a field named ``overwrite`` the keyword is the replace flag, and a non-bool flag is refused."""
    workspace = hb.HeavenBase("row-write-overwrite-flag", detached=True, backends={"main": {"type": "inmem"}})

    workspace.register(CommandNote)
    with pytest.raises(
        TypeError, match="overwrite must be a bool, got str; CommandNote declares no field named 'overwrite', so the upsert keyword is the replace flag"
    ):
        workspace.upsert(CommandNote, {"object_id": "f1", "title": "Flag"}, overwrite="no")
    with pytest.raises(TypeError, match="overwrite must be a bool, got int"):
        workspace.upsert(CommandNote, {"object_id": "f1", "title": "Flag"}, overwrite=0)
    assert workspace.count(CommandNote) == 0
    assert workspace.upsert(CommandNote, {"object_id": "f1", "title": "Flag"}, overwrite=False) == "f1"
    with pytest.raises(ValueError, match="already exists"):
        workspace.upsert(CommandNote, {"object_id": "f1", "title": "Again"}, overwrite=False)
    assert workspace.upsert(CommandNote, {"object_id": "f1", "title": "Again"}, overwrite=True) == "f1"
    assert workspace.get("f1", entity=CommandNote)["title"] == "Again"


@pytest.mark.slow
@pytest.mark.parametrize("backend", ["inmem", "sqlite"])
def test_overwrite_keyword_is_written_when_the_entity_declares_an_overwrite_field(tmp_path, backend):
    """A field named ``overwrite`` passed as a keyword is written, never taken as the replace flag and dropped."""
    database = hb.utils.path_to_file_uri(tmp_path / "rows.db")
    config = {"type": "sqlite", "name": "main", "database": database} if backend == "sqlite" else {"type": "inmem"}
    workspace = hb.HeavenBase(f"row-write-overwrite-field-{backend}", detached=True, backends={"main": config})

    class PolicyRow(hb.Entity):
        identifier = "policy-row"

        title = hb.field(hb.ShortText)
        overwrite = hb.field(hb.Boolean).default(True)

    class RequiredPolicy(hb.Entity):
        identifier = "required-policy"

        title = hb.field(hb.ShortText)
        overwrite = hb.field(hb.Boolean)
        atomic = hb.field(hb.Boolean)

    workspace.register(PolicyRow)
    workspace.register(RequiredPolicy)

    assert workspace.upsert(PolicyRow, object_id="p1", title="t", overwrite=False) == "p1"
    assert workspace.get("p1", entity=PolicyRow)["overwrite"] is False
    assert workspace.upsert(PolicyRow, object_id="p1", title="again", overwrite=False) == "p1"
    stored = workspace.get("p1", entity=PolicyRow)
    assert (stored["title"], stored["overwrite"]) == ("again", False)
    assert workspace.upsert(PolicyRow(object_id="p2", title="t", overwrite=True), overwrite=False) == "p2"
    assert workspace.get("p2", entity=PolicyRow)["overwrite"] is False
    assert workspace.upsert(RequiredPolicy, object_id="r1", title="t", overwrite=False, atomic=False) == "r1"
    stored = workspace.get("r1", entity=RequiredPolicy)
    assert (stored["overwrite"], stored["atomic"]) == (False, False)

    with pytest.raises(ValueError, match="already exists"):
        workspace.upsert_many(PolicyRow, [{"object_id": "p1", "title": "flag", "overwrite": True}], overwrite=False)
    assert workspace.get("p1", entity=PolicyRow)["title"] == "again"


@pytest.mark.fast
def test_upsert_keyword_fields_never_bind_internal_write_options():
    """A field whose name is an internal write option, passed as a keyword, is written like any other field."""
    workspace = hb.HeavenBase("row-write-internal-names", detached=True, backends={"main": {"type": "inmem"}})

    class ScopedNote(hb.Entity):
        identifier = "scoped-note"

        title = hb.field(hb.ShortText)
        allowed_entities = hb.field(hb.ShortText).optional()
        values = hb.field(hb.ShortText).optional()

    workspace.register(ScopedNote)
    assert workspace.upsert(ScopedNote, object_id="s1", title="t", allowed_entities="scope", values="v") == "s1"
    stored = workspace.get("s1", entity=ScopedNote)
    assert (stored["allowed_entities"], stored["values"]) == ("scope", "v")


@pytest.mark.fast
def test_entity_known_unique_governs_routed_fragment_conflicts_only():
    """With the entity named, ``unique`` decides only whether disagreeing routes raise."""
    workspace = hb.HeavenBase("row-entity-unique", detached=True, backends={"main": {"type": "inmem"}, "zaux": {"type": "inmem"}})

    class ReplicatedNote(hb.Entity):
        identifier = "replicated-note"

        title = hb.field(hb.ShortText).store(to="main").store(to="zaux")

    workspace.register(ReplicatedNote)
    workspace.upsert(ReplicatedNote, {"object_id": "n1", "title": "Same"})
    assert workspace.read_rows(ReplicatedNote, ["n1"]) == [{"object_id": "n1", "title": "Same"}]

    workspace.backends.get("zaux").upsert([RowOp(ReplicatedNote.schema().entity_id, "n1", {"title": "Drift"})])

    with pytest.raises(ValueError, match="not unique across routed backends"):
        workspace.get("n1", entity=ReplicatedNote)
    with pytest.raises(ValueError, match="not unique across routed backends"):
        workspace.read_rows(ReplicatedNote, ["n1"])
    assert workspace.get("n1", entity=ReplicatedNote, unique=False) == {"object_id": "n1", "title": "Same"}
    assert workspace.get_many(["n1"], entity=ReplicatedNote, unique=False) == [{"object_id": "n1", "title": "Same"}]


@pytest.mark.slow
@pytest.mark.parametrize(
    ("number", "equivalent", "conflicting"),
    [
        (10**400, 10**400, 10**400 + 1),
        (2**53 + 1, 2**53 + 1, 2**53),
        (2**53 + 1, 2**53 + 1, float(2**53)),
        (float(2**53), 2**53, 2**53 + 1),
        (1, 1.0000005, 1.000002),
    ],
    ids=["beyond-float-range", "beyond-float-precision", "integer-and-float", "float-and-integer", "near-integer"],
)
def test_replicated_json_preserves_integer_precision_and_float_tolerance(number, equivalent, conflicting):
    workspace = hb.HeavenBase("row-replicated-json", detached=True, backends={"main": {"type": "inmem"}, "zaux": {"type": "inmem"}})

    class ReplicatedJson(hb.Entity):
        identifier = "replicated-json"

        payload = hb.field(hb.Json).store(to="main").store(to="zaux")

    try:
        workspace.register(ReplicatedJson, catalog="none")
        workspace.upsert(ReplicatedJson, {"object_id": "n1", "payload": {"n": [number]}})
        dtype = ReplicatedJson.schema().get("payload").dtype
        route = workspace.backends.get("zaux")
        route.upsert([RowOp(ReplicatedJson.identifier, "n1", {"payload": dtype.encode({"n": [equivalent]})})])
        assert workspace.get("n1", entity=ReplicatedJson)["payload"] == {"n": [number]}

        route.upsert([RowOp(ReplicatedJson.identifier, "n1", {"payload": dtype.encode({"n": [conflicting]})})])
        with pytest.raises(ValueError, match="not unique across routed backends"):
            workspace.get("n1", entity=ReplicatedJson)
    finally:
        workspace.release()


def test_writer_returns_named_upsert_and_delete_plans():
    """Writer planning should expose routed work without positional result tuples."""
    workspace = _command_workspace("row-command-plans")
    workspace.upsert(CommandNote, {"object_id": "n1", "title": "First"})
    row = workspace._row_inputs.upsert_row(CommandNote, {"object_id": "n2", "title": "Second"}, {})

    upsert = workspace._writer.prepare_upsert_rows([row], overwrite=True)
    target = workspace._row_inputs.delete_target((CommandNote, "n1"))
    missing = workspace._row_inputs.delete_target((CommandNote, "missing"))
    delete = workspace._writer.prepare_delete_rows([target, missing])

    assert isinstance(upsert, UpsertPlan)
    assert upsert.object_ids == ["n2"]
    assert upsert.entity_classes == [CommandNote]
    assert set(upsert.entity_ops) == {"main"}
    assert isinstance(target, DeleteTarget)
    assert isinstance(delete, DeletePlan)
    assert delete.deleted == [True, False]
    assert all(isinstance(route, DeleteRoute) for route in delete.object_ids_by_route)


@pytest.mark.fast
@pytest.mark.parametrize("catalog", ["full", "identity"])
def test_delete_routes_name_only_a_backend_and_entity_and_catalog_deletes_keep_strategies(catalog):
    """A delete route carries no strategy flag: every delete, Catalog rows included, keeps strategy maintenance."""
    workspace = hb.HeavenBase(f"row-delete-routes-{catalog}", detached=True, backends={"main": {"type": "inmem"}})
    workspace.register(CommandNote, catalog=catalog)
    workspace.upsert(CommandNote, {"object_id": "n1", "title": "First"})

    plan = workspace._writer.prepare_delete_rows([workspace._row_inputs.delete_target((CommandNote, "n1"))])
    ops = [op for backend_ops in workspace._writer.delete_ops(plan).values() for op in backend_ops]

    assert [field.name for field in dataclasses.fields(DeleteRoute)] == ["backend_name", "entity_id"]
    assert {route.entity_id for route in plan.object_ids_by_route} == {CommandNote.identifier, hb.Catalog.schema().entity_id}
    assert ops and all(op.op == "delete" and op.strategies for op in ops)


@pytest.mark.parametrize(
    "command",
    (
        "upsert",
        "upsert_many",
        "get",
        "get_many",
        "read_rows",
        "iter_rows",
        "exists",
        "exists_many",
        "count",
        "ids",
        "rows",
        "set",
        "set_many",
        "delete",
        "delete_many",
        "describe",
        "refresh",
    ),
)
def test_class_targeted_operations_never_register_an_unknown_class(command):
    """Row and maintenance operations should require an existing Entity class."""

    class UnknownCommandNote(hb.Entity):
        identifier = f"unknown-command-note-{command}"

        title = hb.field(hb.ShortText)

    workspace = hb.HeavenBase(f"row-command-unregistered-{command}", detached=True, backends={"main": {"type": "inmem"}})
    row = UnknownCommandNote.from_dict({"object_id": "n1", "title": "First"})
    operations = {
        "upsert": lambda: workspace.upsert(row),
        "upsert_many": lambda: workspace.upsert_many(UnknownCommandNote, [row]),
        "get": lambda: workspace.get("n1", entity=UnknownCommandNote),
        "get_many": lambda: workspace.get_many(["n1"], entity=UnknownCommandNote),
        "read_rows": lambda: workspace.read_rows(UnknownCommandNote, ["n1"]),
        "iter_rows": lambda: workspace.iter_rows(UnknownCommandNote),
        "exists": lambda: workspace.exists("n1", entity=UnknownCommandNote),
        "exists_many": lambda: workspace.exists_many(["n1"], entity=UnknownCommandNote),
        "count": lambda: workspace.count(UnknownCommandNote),
        "ids": lambda: workspace.ids(UnknownCommandNote),
        "rows": lambda: workspace.rows(UnknownCommandNote),
        "set": lambda: workspace.set(row),
        "set_many": lambda: workspace.set_many([row]),
        "delete": lambda: workspace.delete(row),
        "delete_many": lambda: workspace.delete_many([row]),
        "describe": lambda: workspace.describe(UnknownCommandNote, desc="Unknown"),
        "refresh": lambda: workspace.refresh(UnknownCommandNote),
    }
    before_entities = set(workspace.entities)
    before_storage_plans = set(workspace.storage_plans)

    with pytest.raises(KeyError, match="has no registered Entity class"):
        operations[command]()

    assert set(workspace.entities) == before_entities
    assert set(workspace.storage_plans) == before_storage_plans


@pytest.mark.parametrize("command", ("upsert_many", "get_many", "read_rows", "exists_many"))
def test_empty_class_targeted_batches_still_require_the_explicit_class(command):
    """An empty payload must not bypass validation of its declared Entity class."""

    class UnknownEmptyNote(hb.Entity):
        identifier = f"unknown-empty-note-{command}"

        title = hb.field(hb.ShortText)

    workspace = hb.HeavenBase(f"row-command-empty-unregistered-{command}", detached=True, backends={"main": {"type": "inmem"}})
    operations = {
        "upsert_many": lambda: workspace.upsert_many(UnknownEmptyNote, []),
        "get_many": lambda: workspace.get_many([], entity=UnknownEmptyNote),
        "read_rows": lambda: workspace.read_rows(UnknownEmptyNote, []),
        "exists_many": lambda: workspace.exists_many([], entity=UnknownEmptyNote),
    }

    with pytest.raises(KeyError, match="has no registered Entity class"):
        operations[command]()


def test_upsert_many_validates_its_default_class_before_typed_rows():
    """Typed rows must not cause an invalid explicit default selector to be ignored."""

    class PresentBatchNote(hb.Entity):
        identifier = "present-batch-note"

        title = hb.field(hb.ShortText)

    class UnknownBatchDefault(hb.Entity):
        identifier = "unknown-batch-default"

        title = hb.field(hb.ShortText)

    workspace = hb.HeavenBase("row-command-invalid-batch-default", detached=True, backends={"main": {"type": "inmem"}})
    workspace.register(PresentBatchNote)
    row = PresentBatchNote.from_dict({"object_id": "n1", "title": "First"})

    with pytest.raises(KeyError, match="has no registered Entity class"):
        workspace.upsert_many(UnknownBatchDefault, [row])

    assert workspace.rows(PresentBatchNote) == []
