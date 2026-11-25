from importlib.util import find_spec
import os
import sys
import threading
import warnings
from urllib.parse import quote
from uuid import uuid4

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.execution.handlers.seed import seed_handlers
from heavenbase.schema.model import EntitySchema, FieldSchema
from heavenbase.utils import resolve_interpolations
from heavenbase.database.dialect.registry import database_dialects
from heavenbase.utils.files import exists_path, get_file_basename, get_file_dir, pj

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry


class LocalStrategy(hb.StorageStrategy):
    """Durable strategy used by Backend routing integration tests."""


class LateExternalBackend(hb.ext.Backend):
    """Importable external Backend used by Registry-first consumer tests."""

    identifier = "late-ext-row"

    def upsert(self, rows):
        """Retain the latest row operations for the test contract."""
        self.rows = rows

    def scan(self, _entity_id):
        """Return the empty external test store."""
        return []

    @classmethod
    def from_config(cls, name, data, *, resolver=None, config=None):
        """Construct the external Backend through `BackendFactory`."""
        return cls(name, ws_id=data.get("ws_id"), resolver=resolver, config=config)


class ShadowInMemBackend(hb.backends.InMemBackend):
    """Importable subclass used to verify class-owned registration metadata."""


class CustomFactoryBackend(hb.ext.Backend):
    """Backend whose record names an explicit importable factory."""

    identifier = "custom-factory-backend"


class NativeGraphTagBackend(hb.ext.Backend):
    """Importable Backend used to verify static native graph tags."""

    identifier = "native-graph-test"

    def graph_traverse_refs(self, entity_id, start_id, field_name, *, min_hops=1, max_hops=1, direction="out", endpoint_preds=None):
        """Return one deterministic native graph reference."""
        return [(entity_id, "b")]


class BatchedTagBackend(hb.ext.Backend):
    """Importable Backend used to verify open-world static tag claims."""

    identifier = "batched-tag-backend"


def build_custom_factory_backend(name, provider_config, *, resolver, config):
    """Build the custom-factory test Backend from the shared contract.

    Args:
        name: Requested Backend instance name.
        provider_config: Provider-owned construction arguments.
        resolver: Context-owned module authority.
        config: Config authority retained by the backend.

    Returns:
        CustomFactoryBackend: Bound Backend carrying the supplied marker.
    """
    provider_values = dict(provider_config)
    backend = CustomFactoryBackend(
        name,
        ws_id=provider_values.pop("ws_id", None),
        resolver=resolver,
        config=config,
    )
    backend.marker = provider_values.pop("marker")
    if provider_values:
        raise ValueError(f"Unexpected custom factory config: {sorted(provider_values)}")
    return backend


@pytest.fixture(autouse=True)
def _isolate_workspaces():
    before = set(WORKSPACES.keys())
    yield
    for wid in set(WORKSPACES.keys()) - before:
        WORKSPACES.pop(wid, None)


def _require_elasticsearch(url: str) -> None:
    """Require an authenticated Elasticsearch test service."""
    from elasticsearch import Elasticsearch

    client = Elasticsearch(url)
    try:
        if not client.ping():
            raise RuntimeError("ping returned false")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if "HVNB_ELASTICSEARCH_URL" in os.environ:
            pytest.fail(f"configured Elasticsearch test service is unavailable: {detail}", pytrace=False)
        pytest.skip(f"default Elasticsearch endpoint is not an authenticated test service: {detail}")
    finally:
        client.close()


def _github_actions() -> bool:
    value = resolve_interpolations({"value": "${env:GITHUB_ACTIONS,false}"})["value"]
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _db(path, name: str) -> str:
    return hb.utils.path_to_file_uri(path / name)


def _bare_backend(cls, name: str):
    backend = object.__new__(cls)
    backend.name = name
    backend.type = getattr(cls, "identifier", name)
    backend.ws_id = "name-rule-ws"
    backend._collections = {}
    backend._indices = {}
    backend._indexes = {}
    return backend


def test_side_table_keeps_an_absent_list_distinct_from_an_empty_one(tmp_path):
    """A side table stores nothing for either, so the main row has to carry the difference."""

    class SideDoc(hb.Entity):
        identifier = "side-presence-doc"

        tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)

    seen = {}
    for label, spec in (("inmem", {"type": "inmem"}), ("sqlite", {"type": "sqlite", "name": "main", "database": "side-presence"})):
        ws = hb.HeavenBase(f"side-presence-{label}", detached=True, backends={"main": spec})
        ws.register(SideDoc)
        ws.upsert_many(
            SideDoc,
            [{"object_id": "s1", "tags": ["a", "b"]}, {"object_id": "s2", "tags": None}, {"object_id": "s3", "tags": []}],
        )
        seen[label] = {row["object_id"]: row["tags"] for row in ws.query(SideDoc).execute().rows()}
        counts = (
            ws.query_json(
                "side-presence-doc",
                {"aggregate": {"rows": {"count": {"field": "object_id"}}, "present": {"count": {"field": "tags"}}}},
            )
            .execute()
            .rows()
        )
        # one null, and a count that says so — the empty list is a value, not a gap
        assert counts == [{"rows": 3, "present": 2}], label

    assert seen["inmem"] == seen["sqlite"] == {"s1": ["a", "b"], "s2": None, "s3": []}


def test_side_table_presence_marker_leaves_rows_written_without_it_alone():
    """A table written before the marker existed must read exactly as it did."""
    from sqlalchemy import text

    class LegacyDoc(hb.Entity):
        identifier = "side-legacy-doc"

        tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)

    ws = hb.HeavenBase("side-legacy", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": "side-legacy"}})
    ws.register(LegacyDoc)
    ws.upsert_many(LegacyDoc, [{"object_id": "a", "tags": ["x"]}, {"object_id": "b", "tags": []}, {"object_id": "c", "tags": None}])
    assert {row["object_id"]: row["tags"] for row in ws.query(LegacyDoc).execute().rows()} == {"a": ["x"], "b": [], "c": None}

    backend = ws.backends.get("main")
    assert "_present_tags" in backend.tables["side-legacy-doc"].c
    # a row stored before the column existed carries no marker; blank them to reproduce that
    backend.db.orm_execute(text('UPDATE "side_legacy_doc" SET "_present_tags" = NULL'), readonly=False)

    # an absent marker means "present", so every stored value reads as it always did
    assert {row["object_id"]: row["tags"] for row in ws.query(LegacyDoc).execute().rows()} == {"a": ["x"], "b": [], "c": []}


@pytest.mark.parametrize("provider", ["sqlite", "duckdb"])
def test_side_field_added_later_reads_none_for_rows_stored_before_it(tmp_path, provider):
    """A row stored before alter_entity adds a side field reads None, as a row written later without it does."""
    if provider == "duckdb":
        pytest.importorskip("duckdb_engine")

    class LaterDoc(hb.Entity):
        identifier = "side-later-doc"

        title = hb.field(hb.ShortText).optional()

    class LaterDocV2(hb.Entity):
        identifier = "side-later-doc"

        title = hb.field(hb.ShortText).optional()
        items = hb.field(hb.Array[hb.Integer]).store(strategy=hb.SideTable, optional=True)
        tags = hb.field(hb.Array[hb.ShortText]).optional()

    spec = {"type": provider, "database": hb.utils.path_to_file_uri(tmp_path / f"later.{provider}")}
    ws = hb.HeavenBase(f"side-later-{provider}", detached=True, backends={"main": spec})
    try:
        ws.register(LaterDoc)
        ws.upsert(LaterDoc, {"object_id": "old", "title": "t"})
        ws.alter_entity(LaterDocV2, confirm="side-later-doc")
        ws.upsert_many(
            LaterDocV2,
            [{"object_id": "new", "title": "u"}, {"object_id": "full", "items": [1, 2], "tags": []}, {"object_id": "empty", "items": []}],
        )
        rows = ws.get_many(["old", "new", "full", "empty"], entity=LaterDocV2)
        # the side field and the main-table field added beside it agree: absent is None, an empty list is []
        assert {row["object_id"]: (row["items"], row["tags"]) for row in rows} == {
            "old": (None, None),
            "new": (None, None),
            "full": ([1, 2], []),
            "empty": ([], None),
        }
        ws.upsert(LaterDocV2, {"object_id": "old", "title": "t", "items": [3]})
        assert ws.get_many(["old"], entity=LaterDocV2)[0]["items"] == [3]
    finally:
        ws.release()


@pytest.mark.parametrize("provider", ["sqlite", "duckdb"])
@pytest.mark.parametrize("stop_at", ["_mark_side_fields_absent", "_ensure_side_indexes"])
def test_side_field_added_by_an_interrupted_register_still_reads_none_for_older_rows(tmp_path, monkeypatch, provider, stop_at):
    """A register that stops after adding a side field's marker column leaves the next register to mark the older rows."""
    if provider == "duckdb":
        pytest.importorskip("duckdb_engine")
    from heavenbase.backends.sql.base import SQLBackend

    class StopDoc(hb.Entity):
        identifier = "side-stop-doc"

        title = hb.field(hb.ShortText).optional()

    class StopDocV2(hb.Entity):
        identifier = "side-stop-doc"

        title = hb.field(hb.ShortText).optional()
        items = hb.field(hb.Array[hb.Integer]).optional()
        tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)

    spec = {"type": provider, "database": hb.utils.path_to_file_uri(tmp_path / f"stop.{provider}")}
    ws = hb.HeavenBase(f"side-stop-{provider}", detached=True, backends={"main": spec})
    try:
        ws.register(StopDoc)
        ws.upsert_many(StopDoc, [{"object_id": "old", "title": "t"}, {"object_id": "old2"}])
    finally:
        ws.release()

    def stop(self, *args, **kwargs):
        raise RuntimeError("stopped")

    with monkeypatch.context() as patch:
        patch.setattr(SQLBackend, stop_at, stop)
        stopped = hb.HeavenBase(f"side-stop-{provider}-stopped", detached=True, backends={"main": spec})
        try:
            with pytest.raises(RuntimeError, match="stopped"):
                stopped.register(StopDocV2)
        finally:
            stopped.release()
    reopened = hb.HeavenBase(f"side-stop-{provider}-again", detached=True, backends={"main": spec})
    try:
        reopened.register(StopDocV2)
        reopened.upsert(StopDocV2, {"object_id": "new", "items": [1], "tags": ["x"]})
        rows = reopened.get_many(["old", "old2", "new"], entity=StopDocV2)
        assert [(row["object_id"], row["items"], row["tags"]) for row in rows] == [("old", None, None), ("old2", None, None), ("new", [1], ["x"])]
    finally:
        reopened.release()


def test_side_field_marker_added_beside_a_stored_side_table_keeps_its_items(tmp_path):
    """Opening a table written before presence markers existed adds the marker and every stored value reads as it did."""
    from sqlalchemy import text

    class LegacyOpenDoc(hb.Entity):
        identifier = "side-legacy-open-doc"

        tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)

    spec = {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "legacy-open.db")}
    ws = hb.HeavenBase("side-legacy-open", detached=True, backends={"main": spec})
    try:
        ws.register(LegacyOpenDoc)
        ws.upsert_many(LegacyOpenDoc, [{"object_id": "a", "tags": ["x"]}, {"object_id": "b", "tags": []}, {"object_id": "c", "tags": None}])
        # a table from before markers existed has the side table and no marker column
        ws.backends.get("main").db.orm_execute(text('ALTER TABLE "side_legacy_open_doc" DROP COLUMN "_present_tags"'), readonly=False)
    finally:
        ws.release()
    reopened = hb.HeavenBase("side-legacy-open-again", detached=True, backends={"main": spec})
    try:
        reopened.register(LegacyOpenDoc)
        assert {row["object_id"]: row["tags"] for row in reopened.query(LegacyOpenDoc).execute().rows()} == {"a": ["x"], "b": [], "c": []}
    finally:
        reopened.release()


def test_python_fallback_execution_warns_when_configured():
    ws = hb.HeavenBase("fallback-warning-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})
    ws.context.config.set("heavenbase.query.warn_on_python_fallback", True)

    class WarningDoc(hb.Entity):
        identifier = "fallback-warning-doc"

        title = hb.field(hb.ShortText)

    try:
        ws.register(WarningDoc)
        ws.upsert_many(WarningDoc, [{"object_id": "a", "title": "alpha"}, {"object_id": "b", "title": "beta"}])
        with pytest.warns(UserWarning, match="Python scan fallback"):
            assert ws.query(WarningDoc).where(WarningDoc.title == "alpha").execute().ids == ["a"]
    finally:
        ws.context.config.unset("heavenbase.query.warn_on_python_fallback")


def test_backend_execute_defaults_to_replaceable_scan_module():
    """One Backend base should provide scan fallback and permit native overrides."""

    class PortableBackend(hb.ext.Backend):
        def scan(self, entity_id):
            assert entity_id == "portable-doc"
            return [
                {"object_id": "a", "title": "alpha"},
                {"object_id": "b", "title": "beta"},
            ]

    class NativeAwareBackend(PortableBackend):
        def execute(self, fragment):
            self.native_attempts = getattr(self, "native_attempts", 0) + 1
            return super().execute(fragment)

    fragment = hb.ext.QueryFragment("portable", "eq", "title", lambda row: row["title"] == "alpha").bind("portable-doc")
    backend = NativeAwareBackend("portable")

    assert backend.execute(fragment).ids == ["a"]
    assert backend.native_attempts == 1


def test_sql_rowops_preserve_repeated_same_key_sequence(tmp_path):
    """SQL replacement batching preserves upsert/delete order and repeated object ids."""

    class OrderedSQLDoc(hb.Entity):
        identifier = "ordered-sql-doc"

        title = hb.field(hb.ShortText)

    ws = hb.HeavenBase("ordered-sql", backends={"main": hb.backends.SQLiteBackend("main", _db(tmp_path, "ordered.db"))})
    backend = ws.backends.get("main")
    try:
        ws.register(OrderedSQLDoc)
        backend.upsert(
            [
                hb.ext.RowOp(OrderedSQLDoc.identifier, "same", {"title": "first"}),
                hb.ext.RowOp(OrderedSQLDoc.identifier, "same", {"title": "second"}),
                hb.ext.RowOp(OrderedSQLDoc.identifier, "gone", {"title": "temporary"}),
                hb.ext.RowOp(OrderedSQLDoc.identifier, "gone", op="delete"),
                hb.ext.RowOp(OrderedSQLDoc.identifier, "revived", op="delete"),
                hb.ext.RowOp(OrderedSQLDoc.identifier, "revived", {"title": "present"}),
            ]
        )

        assert backend.get_many(OrderedSQLDoc.identifier, ["same", "gone", "revived"]) == [
            {"object_id": "same", "title": "second"},
            None,
            {"object_id": "revived", "title": "present"},
        ]
    finally:
        ws.drop()


def test_sql_side_table_array_exact_compound_items_fall_back():
    class SQLJsonArrayDoc(hb.Entity):
        identifier = "unit-sql-json-array-doc"

        payloads = hb.field(hb.Array[hb.Json]).store(to="sql", strategy=hb.SideTable)

    schema = SQLJsonArrayDoc.schema()
    handlers = seed_handlers()
    ctx = hb.ext.HandlerContext("main", "sqlite", hb.SideTable.identifier)
    fragment = handlers.get(hb.Array, "eq", "sqlite", hb.SideTable.identifier)(schema.get("payloads"), [{"a": 1}], ctx)

    assert fragment.notes["mode"] == "py_scan"
    assert fragment.notes.get("kind") != "sql_array_exact"


def test_sql_inline_array_exact_compiler_is_guarded():
    class SQLInlineArrayDoc(hb.Entity):
        identifier = "unit-sql-inline-array-doc"

        tags = hb.field(hb.Array[hb.ShortText]).store(to="sql", strategy=hb.InlineColumn)
        payloads = hb.field(hb.Array[hb.Json]).store(to="sql", strategy=hb.InlineColumn)

    schema = SQLInlineArrayDoc.schema()
    handlers = seed_handlers()

    postgres_ctx = hb.ext.HandlerContext("main", "postgres", hb.InlineColumn.identifier)
    postgres_eq = handlers.get(hb.Array, "eq", "postgres", hb.InlineColumn.identifier)(schema.get("tags"), ["alpha"], postgres_ctx)
    assert postgres_eq.notes["mode"] == "py_scan"
    assert postgres_eq.notes["fallback_reason"] == "text_semantics_not_proven"

    sqlite_ctx = hb.ext.HandlerContext("main", "sqlite", hb.InlineColumn.identifier)
    tuple_eq = handlers.get(hb.Array, "eq", "sqlite", hb.InlineColumn.identifier)(schema.get("tags"), ("alpha",), sqlite_ctx)
    tuple_in = handlers.get(hb.Array, "in", "sqlite", hb.InlineColumn.identifier)(schema.get("tags"), [("alpha",)], sqlite_ctx)
    compound_eq = handlers.get(hb.Array, "eq", "sqlite", hb.InlineColumn.identifier)(schema.get("payloads"), [{"a": 1}], sqlite_ctx)

    assert tuple_eq.notes["mode"] == "py_scan"
    assert tuple_in.notes["mode"] == "py_scan"
    assert compound_eq.notes["mode"] == "py_scan"


def test_handler_context_keeps_strategy_options_and_near_knobs_explicit():
    ctx = hb.ext.HandlerContext("vec", "milvus", hb.VectorIndex.identifier, {"metric_family": "l2"}, top_k=7, metric="l2")

    assert ctx.backend_identifier == "milvus"
    assert ctx.strategy_options == {"metric_family": "l2"}
    assert ctx.top_k == 7
    assert ctx.metric == "l2"

    with pytest.raises(TypeError, match="strategy_options must be a mapping"):
        hb.ext.HandlerContext("vec", "milvus", hb.VectorIndex.identifier, 7)


def test_query_fragment_exposes_one_shared_execution_protocol():
    """QueryFragment should own cross-layer routing facts while keeping payloads open."""
    native_notes = {"kind": "sql_where"}
    native = hb.ext.QueryFragment("source", "eq", "title", {"sql": "title = ?"}, notes=native_notes)
    scan = hb.ext.QueryFragment("source", "contains", "title", lambda row: "a" in row.get("title", ""))
    logical = hb.ext.QueryFragment("source", "and", "", [native, scan], notes={"kind": "logic"})

    native_notes["kind"] = "changed"
    scan.mark_scan("contains requires a portable predicate")
    logical.bind("doc", backend="main")

    assert native.kind() == "sql_where"
    assert native.mode() == "native"
    assert scan.kind("row_payload") == "row_payload"
    assert scan.mode() == "py_scan"
    assert logical.field == "*"
    assert logical.children() == [native, scan]
    assert logical.uses_scan() is True
    assert logical.fallback_reason() == "contains requires a portable predicate"
    assert logical.entity_id() == native.entity_id() == scan.entity_id() == "doc"
    assert logical.backend == native.backend == scan.backend == "main"

    near = hb.ext.QueryFragment("main", "near", "embedding", {"vector": [1.0], "filter": native})
    assert near.filter_fragment() is native
    assert hb.ext.QueryFragment("main", "near", "embedding", {"vector": [1.0]}).filter_fragment() is None
    assert hb.ext.QueryFragment("main", "near", "embedding", {"vector": [1.0]}, notes={"filter": scan}).filter_fragment() is scan
    with pytest.raises(TypeError, match="filter must be a QueryFragment"):
        hb.ext.QueryFragment("main", "near", "embedding", {"filter": {"eq": 1}}).filter_fragment()

    unsupported = hb.ext.QueryFragment("main", "eq", "title", None, notes={"unsupported_reason": "no handler"})
    assert hb.ext.QueryFragment("main", "or", "*", [native, unsupported]).unsupported_reason() == "no handler"
    assert hb.ext.QueryFragment("main", "eq", "title", None, notes={"unsupported": True}).unsupported_reason() == "Unsupported operation"
    with pytest.raises(ValueError, match="exactly one child"):
        hb.ext.QueryFragment("main", "not", "*", []).children()
    with pytest.raises(TypeError, match="QueryFragment children"):
        hb.ext.QueryFragment("main", "and", "*", ["bad"]).children()
    with pytest.raises(ValueError, match="field must be non-empty"):
        hb.ext.QueryFragment("main", "eq", "", None)


def test_backend_graph_tags_are_registry_backed():
    backend = hb.backends.InMemBackend("plain")

    assert backend.tag("graph-traversal-native").value is False
    assert backend.tag("graph-endpoint-materialization").value == "none"
    assert backend.tag("graph-incoming-lookup-native").value is False
    assert backend.tag("graph-traversal-native").source == "default"
    with pytest.raises(NotImplementedError, match="does not support native GraphEdge traversal"):
        backend.graph_traverse_refs("doc", "a", "cites")

    NativeGraphTagBackend.register(
        tags={
            "graph-traversal-native": True,
            "graph-endpoint-pushdown": True,
            "graph-endpoint-materialization": "identity_anchor",
            "graph-incoming-lookup-native": True,
        }
    )
    try:
        native = NativeGraphTagBackend("graph")

        assert native.tag("graph-traversal-native").value is True
        assert native.tag("graph-traversal-native").source == "record"
        assert native.graph_traverse("doc", "a", "cites") == ["b"]
        assert native.tag("graph-endpoint-pushdown").value is True
        assert native.tag("graph-endpoint-materialization").value == "identity_anchor"
        assert native.tag("graph-incoming-lookup-native").value is True
        assert hb.DEFAULT_CONTEXT.modules().tag("backend_type", "neo4j", "graph-traversal-native").value is True
        assert hb.DEFAULT_CONTEXT.modules().tag("backend_type", "surreal", "graph-traversal-native").value is False
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("backend_type", NativeGraphTagBackend.identifier)


@pytest.mark.detail
@pytest.mark.skipif(find_spec("elasticsearch") is None, reason="elasticsearch is not installed")
def test_elasticsearch_scan_consumes_every_scroll_page(monkeypatch):
    """The scan page-size tunable must never truncate an integrity enumeration."""
    import elasticsearch.helpers

    class FakeIndices:
        @staticmethod
        def exists(index):
            return True

    class FakeClient:
        indices = FakeIndices()

    calls = []

    def fake_scan(client, **kwargs):
        calls.append((client, kwargs))
        yield {"_id": "a", "_source": {"value": 1}}
        yield {"_id": "b", "_source": {"value": 2}}
        yield {"_id": "c", "_source": {"value": 3}}

    monkeypatch.setattr(elasticsearch.helpers, "scan", fake_scan)
    backend = hb.backends.ElasticsearchBackend("search", hosts=["http://127.0.0.1:1"])
    backend.client = FakeClient()

    assert [row["object_id"] for row in backend.scan("paged-doc")] == ["a", "b", "c"]
    assert calls[0][0] is backend.client
    assert calls[0][1]["query"] == {"query": {"match_all": {}}}
    assert calls[0][1]["size"] >= 1


@pytest.mark.detail
def test_milvus_preserves_rowop_order_across_upsert_and_delete_batches(monkeypatch):
    """Milvus must not move target deletion ahead of a preceding source rewrite."""

    class FakeClient:
        def __init__(self, **kwargs):
            self.calls = []

        @staticmethod
        def has_collection(name):
            return True

        def upsert(self, *, collection_name, data):
            self.calls.append(("upsert", collection_name, [row["object_id"] for row in data]))

        def delete(self, *, collection_name, filter):
            self.calls.append(("delete", collection_name, filter))

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": FakeClient}))
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")

    backend.upsert(
        [
            hb.ext.RowOp("graph-doc", "source-a", {"object_id": "source-a", "cites": []}),
            hb.ext.RowOp("graph-doc", "source-b", {"object_id": "source-b", "cites": []}),
            hb.ext.RowOp("graph-doc", "target-a", op="delete"),
            hb.ext.RowOp("graph-doc", "target-b", op="delete"),
            hb.ext.RowOp("graph-doc", "revived", {"object_id": "revived", "cites": []}),
        ]
    )

    collection = backend._collection("graph-doc")
    assert backend.client.calls == [
        ("upsert", collection, ["source-a", "source-b"]),
        ("delete", collection, 'object_id in ["target-a", "target-b"]'),
        ("upsert", collection, ["revived"]),
    ]


@pytest.mark.detail
def test_milvus_rewrite_failure_stops_before_target_delete(monkeypatch):
    """A failed source rewrite must prevent every later target delete in the ordered plan."""

    class FakeClient:
        def __init__(self, **kwargs):
            self.calls = []

        @staticmethod
        def has_collection(name):
            return True

        def upsert(self, *, collection_name, data):
            self.calls.append(("upsert", collection_name))
            raise RuntimeError("injected source rewrite failure")

        def delete(self, *, collection_name, filter):
            self.calls.append(("delete", collection_name))

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": FakeClient}))
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")

    with pytest.raises(RuntimeError, match="injected source rewrite failure"):
        backend.upsert(
            [
                hb.ext.RowOp("graph-doc", "source", {"object_id": "source", "cites": []}),
                hb.ext.RowOp("graph-doc", "target", op="delete"),
            ]
        )

    assert [kind for kind, _collection in backend.client.calls] == ["upsert"]


@pytest.mark.detail
def test_milvus_partial_success_marks_collection_dirty(monkeypatch):
    """A successful rewrite remains visible to flush logic when a later target delete fails."""

    class FakeClient:
        def __init__(self, **kwargs):
            self.calls = []

        @staticmethod
        def has_collection(name):
            return True

        def upsert(self, *, collection_name, data):
            self.calls.append(("upsert", collection_name))

        def delete(self, *, collection_name, filter):
            self.calls.append(("delete", collection_name))
            raise RuntimeError("injected target delete failure")

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": FakeClient}))
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    collection = backend._collection("graph-doc")

    with pytest.raises(RuntimeError, match="injected target delete failure"):
        backend.upsert(
            [
                hb.ext.RowOp("graph-doc", "source", {"object_id": "source", "cites": []}),
                hb.ext.RowOp("graph-doc", "target", op="delete"),
            ]
        )

    assert [kind for kind, _collection in backend.client.calls] == ["upsert", "delete"]
    assert collection in backend._dirty_collections


@pytest.mark.detail
def test_chroma_preserves_order_and_stops_after_rewrite_failure():
    """Chroma batches adjacent phases without moving target deletes before rewrites."""

    class FakeCollection:
        def __init__(self, *, fail_upsert=False):
            self.calls = []
            self.fail_upsert = fail_upsert

        def upsert(self, **payload):
            self.calls.append(("upsert", payload["ids"]))
            if self.fail_upsert:
                raise RuntimeError("injected source rewrite failure")

        def delete(self, *, ids):
            self.calls.append(("delete", ids))

        def get(self, **kwargs):
            return {"ids": [], "embeddings": None}

    class FakeClient:
        def __init__(self, collection):
            self.collection = collection

        def get_or_create_collection(self, **kwargs):
            return self.collection

    def make_backend(collection):
        backend = object.__new__(hb.backends.ChromaBackend)
        hb.backends.Backend.__init__(backend, "chroma")
        backend.client = FakeClient(collection)
        backend.client_error = ""
        backend._schemas = {}
        backend._collections = {}
        return backend

    rows = [
        hb.ext.RowOp("ordered-doc", "source-a", {"title": "A"}),
        hb.ext.RowOp("ordered-doc", "source-b", {"title": "B"}),
        hb.ext.RowOp("ordered-doc", "target-a", op="delete"),
        hb.ext.RowOp("ordered-doc", "target-b", op="delete"),
        hb.ext.RowOp("ordered-doc", "revived", {"title": "R"}),
    ]
    collection = FakeCollection()
    make_backend(collection).upsert(rows)
    assert collection.calls == [
        ("upsert", ["source-a", "source-b"]),
        ("delete", ["target-a", "target-b"]),
        ("upsert", ["revived"]),
    ]

    failing = FakeCollection(fail_upsert=True)
    with pytest.raises(RuntimeError, match="injected source rewrite failure"):
        make_backend(failing).upsert(rows[:3])
    assert failing.calls == [("upsert", ["source-a", "source-b"])]


@pytest.mark.detail
@pytest.mark.skipif(find_spec("pyarrow") is None, reason="pyarrow is not installed")
def test_lance_preserves_order_and_stops_after_rewrite_failure():
    """LanceDB applies ordered bulk phases and stops at the first failed merge."""

    class FakeMerge:
        def __init__(self, table):
            self.table = table

        def when_matched_update_all(self):
            return self

        def when_not_matched_insert_all(self):
            return self

        def execute(self, arrow_table):
            ids = arrow_table.column("object_id").to_pylist()
            self.table.calls.append(("upsert", ids))
            if self.table.fail_merge:
                raise RuntimeError("injected merge failure")

    class FakeTable:
        def __init__(self, *, fail_merge=False):
            self.calls = []
            self.fail_merge = fail_merge

        def merge_insert(self, field):
            assert field == "object_id"
            return FakeMerge(self)

        def delete(self, expression):
            self.calls.append(("delete", expression))

    def make_backend(table):
        backend = object.__new__(hb.backends.LanceBackend)
        hb.backends.Backend.__init__(backend, "lance")
        backend._schemas = {}
        backend._table = lambda entity_id: table
        return backend

    rows = [
        hb.ext.RowOp("ordered-doc", "source-a", {"title": "A"}),
        hb.ext.RowOp("ordered-doc", "source-b", {"title": "B"}),
        hb.ext.RowOp("ordered-doc", "target-a", op="delete"),
        hb.ext.RowOp("ordered-doc", "target-b", op="delete"),
        hb.ext.RowOp("ordered-doc", "revived", {"title": "R"}),
    ]
    table = FakeTable()
    make_backend(table).upsert(rows)
    assert table.calls == [
        ("upsert", ["source-a", "source-b"]),
        ("delete", "object_id IN ('target-a', 'target-b')"),
        ("upsert", ["revived"]),
    ]

    failing = FakeTable(fail_merge=True)
    with pytest.raises(RuntimeError, match="injected merge failure"):
        make_backend(failing).upsert(rows)
    assert failing.calls == [("upsert", ["source-a", "source-b"])]


@pytest.mark.detail
def test_lance_scan_suppresses_only_verified_missing_tables():
    """Integrity scans return empty for an absent table but surface open failures."""

    class MissingClient:
        @staticmethod
        def open_table(name):
            raise ValueError(f"Table {name!r} was not found")

    class FailingClient:
        @staticmethod
        def open_table(_name):
            raise ConnectionError("injected LanceDB connection failure")

    def make_backend(client):
        backend = object.__new__(hb.backends.LanceBackend)
        hb.backends.Backend.__init__(backend, "lance")
        backend.client = client
        return backend

    assert make_backend(MissingClient()).scan("scan-doc") == []
    with pytest.raises(RuntimeError, match="injected LanceDB connection failure"):
        make_backend(FailingClient()).scan("scan-doc")


@pytest.mark.detail
def test_pinecone_preserves_order_and_stops_after_rewrite_failure():
    """Pinecone never attempts a target delete after an earlier rewrite failure."""

    class PineDoc(hb.Entity):
        identifier = "ordered-pine-doc"

        title = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2])

    class FakeIndex:
        def __init__(self, *, fail_upsert=False):
            self.calls = []
            self.fail_upsert = fail_upsert

        def upsert(self, *, vectors):
            self.calls.append(("upsert", [item["id"] for item in vectors]))
            if self.fail_upsert:
                raise RuntimeError("injected source rewrite failure")

        def delete(self, *, ids):
            self.calls.append(("delete", ids))

    class FakeClient:
        def __init__(self, index):
            self.index = index

        def has_index(self, name):
            return True

        def describe_index(self, name):
            return type("Description", (), {"host": "http://localhost:5080"})()

        def Index(self, **kwargs):
            return self.index

    def make_backend(index):
        backend = object.__new__(hb.backends.PineconeBackend)
        hb.backends.Backend.__init__(backend, "pinecone")
        backend.host = "http://localhost:5080"
        backend.client = FakeClient(index)
        backend.client_error = ""
        backend._indexes = {}
        backend._schemas = {PineDoc.identifier: PineDoc.schema()}
        backend._owned_fields = {}
        backend._shared_entities = set()
        return backend

    rows = [
        hb.ext.RowOp(PineDoc.identifier, "source-a", {"title": "A", "embedding": [1.0, 0.0]}),
        hb.ext.RowOp(PineDoc.identifier, "source-b", {"title": "B", "embedding": [0.0, 1.0]}),
        hb.ext.RowOp(PineDoc.identifier, "target-a", op="delete"),
        hb.ext.RowOp(PineDoc.identifier, "target-b", op="delete"),
        hb.ext.RowOp(PineDoc.identifier, "revived", {"title": "R", "embedding": [0.5, 0.5]}),
    ]
    index = FakeIndex()
    make_backend(index).upsert(rows)
    assert index.calls == [
        ("upsert", ["source-a", "source-b"]),
        ("delete", ["target-a", "target-b"]),
        ("upsert", ["revived"]),
    ]

    failing = FakeIndex(fail_upsert=True)
    with pytest.raises(RuntimeError, match="Pinecone operation failed"):
        make_backend(failing).upsert(rows[:3])
    assert failing.calls == [("upsert", ["source-a", "source-b"])]


@pytest.mark.detail
def test_milvus_rejects_routed_graph_fields_without_a_vector_collection(monkeypatch):
    """Milvus must fail schema placement instead of silently dropping a scalar-only GraphEdge route."""

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @staticmethod
        def close():
            return None

        @staticmethod
        def list_collections():
            return []

        @staticmethod
        def drop_database(db_name):
            del db_name

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": FakeClient}))
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    ws = hb.HeavenBase(
        "milvus-graph-without-vector",
        backends={"main": hb.backends.InMemBackend("main"), "milvus": backend},
    )

    class GraphOnlyDoc(hb.Entity):
        identifier = "milvus-graph-only-doc"

        title = hb.field(hb.ShortText).store(to="main")
        embedding = hb.field(hb.Vector[2]).store(to="main")
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["milvus-graph-only-doc"]]).store(to="milvus", strategy=hb.GraphEdge)

    with pytest.raises(ValueError, match="cannot store.*without a routed Vector field"):
        ws.register(GraphOnlyDoc)
    assert GraphOnlyDoc.identifier not in ws.entities
    assert GraphOnlyDoc.identifier not in ws.storage_plans
    assert GraphOnlyDoc.identifier not in backend._schemas
    ws.drop()


def test_backend_behavior_tags_are_explicit_static_record_claims():
    sqlite = hb.backends.SQLiteBackend("sql", ":memory:")
    modules = hb.DEFAULT_CONTEXT.modules()

    assert sqlite.tag("workspace-database-namespace").value is False
    assert modules.tag("backend_type", "pgvector", "workspace-database-namespace").value is True
    assert modules.tag("backend_type", "pgvector", "near-filter-modes").value == ("combined_native", "metadata_prefilter")
    assert modules.tag("backend_type", "milvus", "unsupported-storage-types").value == ()
    assert modules.tag("backend_type", "lance", "unsupported-storage-types").value == ("hyper-g",)
    assert sqlite.tag("row-lookup").value == {"get_many": "native", "exists_many": "native", "count": "native"}
    assert all(result.source in {"record", "default", "live"} for result in sqlite.tags().values())


def test_backend_register_publishes_open_world_static_tag_claims():
    BatchedTagBackend.register(tags={"publisher.batch-size": 250})
    try:
        backend = BatchedTagBackend("batched")
        result = backend.tag("publisher.batch-size")

        assert result.value == 250
        assert result.raw == 250
        assert result.status == "unresolved"
        assert result.source == "record"
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("backend_type", BatchedTagBackend.identifier)


def test_backend_binary_storage_uses_record_and_construction_layers(tmp_path):
    generic_json = hb.backends.FileBackend("generic-json", _db(tmp_path, "generic-json"), "json")
    generic_pickle = hb.backends.FileBackend("generic-pickle", _db(tmp_path, "generic-pickle"), "pickle")
    fixed_json = hb.backends.JsonFileBackend("fixed-json", _db(tmp_path, "fixed-json"))

    assert generic_json.tag("binary-storage").value == "base64"
    assert generic_json.tag("binary-storage").source == "construction"
    assert generic_pickle.tag("binary-storage").value == "bytes"
    assert generic_pickle.tag("binary-storage").source == "construction"
    assert fixed_json.tag("binary-storage").value == "base64"
    assert fixed_json.tag("binary-storage").source == "record"


def test_backend_ws_id_is_empty_when_unbound_and_validated_when_set():
    backend = hb.backends.Backend("plain")

    assert backend.ws_id == ""

    backend.ws_id = "workspace-id"
    assert backend.ws_id == "workspace-id"

    backend.ws_id = None
    assert backend.ws_id == ""


class _ScanCountingSQLiteBackend(hb.backends.SQLiteBackend):
    def __init__(self, name, database):
        super().__init__(name, database)
        self.scan_count = 0

    def scan(self, entity_id):
        self.scan_count += 1
        return super().scan(entity_id)


def test_json_file_backend_persists_rows(tmp_path):
    """JSON file backend should persist rows and reload them by object id."""
    path = hb.utils.path_to_file_uri(tmp_path / "json-store")
    backend = hb.backends.JsonFileBackend("store", path, ws_id="json-file-test")
    object_id = "persisted-id"
    backend.upsert([hb.ext.RowOp("json-thing", object_id, {"object_id": object_id, "name": "persisted", "attrs": hb.Json().encode({"tier": 1})})])
    reopened = hb.backends.JsonFileBackend("store", path, ws_id="json-file-test")

    assert (tmp_path / "json-store" / "json-file-test" / hb.backends.Backend._physical_name("json-thing") / f"{quote(object_id, safe='')}.json").exists()
    scanned = reopened.scan("json-thing")[0]
    assert scanned["object_id"] == object_id
    assert scanned["attrs"] == hb.Json().encode({"tier": 1})


@pytest.mark.full
def test_json_file_backend_id_lookup_does_not_load_full_entity(tmp_path):
    """File backends should read requested record files for object-id lookups."""
    path = hb.utils.path_to_file_uri(tmp_path / "json-lookup-store")
    backend = hb.backends.JsonFileBackend("store", path, ws_id="json-file-lookup-test")
    backend.upsert(
        [
            hb.ext.RowOp("json-lookup-thing", "a", {"object_id": "a", "name": "Alpha"}),
            hb.ext.RowOp("json-lookup-thing", "b", {"object_id": "b", "name": "Beta"}),
        ]
    )
    reopened = hb.backends.JsonFileBackend("store", path, ws_id="json-file-lookup-test")

    rows = reopened.get_many("json-lookup-thing", ["b", "missing", "a"])

    assert [row["object_id"] if row else None for row in rows] == ["b", None, "a"]
    assert reopened.exists_many("json-lookup-thing", ["a", "missing"]) == [True, False]
    assert reopened.count("json-lookup-thing") == 2
    assert "json-lookup-thing" not in reopened._loaded_entities


def test_pickle_file_backend_persists_rows(tmp_path):
    """Pickle file backend should persist rows and reload them by object id."""
    path = hb.utils.path_to_file_uri(tmp_path / "pickle-store")
    backend = hb.backends.PickleFileBackend("store", path, ws_id="pickle-file-test")
    entity_id = "persisted-id"
    backend.upsert([hb.ext.RowOp("pickle-thing", entity_id, {"object_id": entity_id, "name": "persisted"})])
    reopened = hb.backends.PickleFileBackend("store", path, ws_id="pickle-file-test")

    assert (tmp_path / "pickle-store" / "pickle-file-test" / hb.backends.Backend._physical_name("pickle-thing") / f"{quote(entity_id, safe='')}.pkl").exists()
    assert reopened.scan("pickle-thing")[0]["object_id"] == entity_id


def test_local_backend_locations_share_strict_file_uri_grammar(tmp_path, monkeypatch):
    class FakeChroma:
        @staticmethod
        def PersistentClient(**kwargs):
            return kwargs

        @staticmethod
        def EphemeralClient(**kwargs):
            return kwargs

    monkeypatch.setitem(sys.modules, "chromadb", FakeChroma)
    uri = hb.utils.path_to_file_uri(tmp_path / "local-store")

    file_backend = hb.backends.JsonFileBackend("json", uri)
    chroma = hb.backends.ChromaBackend("chroma", uri)

    assert file_backend._base_path == str(tmp_path / "local-store")
    assert chroma.path == str(tmp_path / "local-store")
    for factory in (hb.backends.JsonFileBackend, hb.backends.PickleFileBackend, hb.backends.ChromaBackend):
        with pytest.raises(ValueError, match="file://"):
            factory("legacy", str(tmp_path / "legacy"))
    with pytest.raises(ValueError, match="resource root"):
        hb.backends.JsonFileBackend("bad-resource", "file://&/json")


def test_lance_local_namespace_applies_once_and_remote_workspace_is_rejected(tmp_path, monkeypatch):
    connections = []

    class FakeLance:
        @staticmethod
        def connect(uri, **kwargs):
            connections.append((uri, kwargs))
            return {"uri": uri, **kwargs}

    monkeypatch.setitem(sys.modules, "lancedb", FakeLance)
    base = hb.utils.path_to_file_uri(tmp_path / "workspaces" / "lance")
    local = hb.backends.LanceBackend("vec", base, ws_id="agent-shop")

    assert local.uri == str(tmp_path / "workspaces" / "lance" / "agent-shop")
    assert local.uri.count("agent-shop") == 1
    remote = hb.backends.LanceBackend("remote", "s3://bucket/lance")
    assert remote.uri == "s3://bucket/lance"
    assert connections[-1][0] == "s3://bucket/lance"
    with pytest.raises(ValueError, match="Remote Lance URI.*ws_id"):
        hb.backends.LanceBackend("bad-remote", "s3://bucket/lance", ws_id="agent-shop")


def test_physical_names_are_transparent_normalized_names():
    """_physical_name is a simple provider-separator normalizer, not a conflict-resolution layer."""
    pn = hb.backends.Backend._physical_name

    assert pn("bench-item") == "bench_item"
    assert pn("bench_item") == "bench_item"
    assert pn("a-b", "c") == "a_b_c"
    assert pn("a", "b-c") == "a_b_c"
    assert pn("ws-1", "item_2", sep="-") == "ws-1-item-2"

    assert not hasattr(hb.backends, "PhysicalNamePolicy")
    assert not hasattr(hb.utils, "entity_table_name")
    assert not hasattr(hb.utils, "workspace_table_name")


def test_sql_physical_table_name_is_not_hidden_or_truncated(tmp_path):
    """HeavenBase does not hide long user ids behind hashes; provider limits remain the user's portability concern."""
    sql = hb.backends.SQLiteBackend("b", url=f"sqlite:///{tmp_path / 'long.db'}")
    ws = hb.HeavenBase("long-id-bound", backends={"b": sql})

    class LongId(hb.Entity):
        identifier = "e" + "a" * 70

        v = hb.field(hb.Integer)

    ws.register(LongId)
    ws.upsert(LongId, {"object_id": "o1", "v": 7})
    assert sql.tables[LongId.identifier].name == LongId.identifier
    assert len(sql.tables[LongId.identifier].name) > 63
    assert ws.get("o1", entity=LongId)["v"] == 7


def test_dash_underscore_entity_ids_are_allowed_to_conflict():
    """Backends may normalize '-' and '_' to the same physical name; users own portable naming."""

    assert hb.backends.Backend._physical_name("coexist-doc") == hb.backends.Backend._physical_name("coexist_doc")


def test_backend_schema_warnings_emit_during_register(default_extensions_without_agent):
    """Backends can opt into schema-registration warnings without changing the user-facing register API."""

    class WarnBackend(hb.backends.InMemBackend):
        def schema_warnings(self, schema, bindings):
            return [f"{self.name} saw risky schema {schema.entity_id!r}"]

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ws = hb.HeavenBase("warn-hook-ws", backends={"warn": WarnBackend("warn")})

    class WarnDoc(hb.Entity):
        value = hb.field(hb.ShortText)

    with pytest.warns(UserWarning, match="risky schema 'warn-doc'"):
        ws.register(WarnDoc)


def test_sql_backend_schema_warnings_report_provider_name_risks(tmp_path, default_extensions_without_agent):
    """SQL backends can report provider identifier limits and reserved-word risks without rewriting names."""

    class WarningSQLiteBackend(hb.backends.SQLiteBackend):
        def _sql_name_warning_rules(self):
            return 12, {"select"}, False

    backend = WarningSQLiteBackend("sql", url=f"sqlite:///{tmp_path / 'warnings.db'}")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ws = hb.HeavenBase("sql-warning-ws", backends={"sql": backend})

    class SQLRiskDoc(hb.Entity):
        identifier = "very-long-sql-risk"

        select = hb.field(hb.ShortText)

    with pytest.warns(UserWarning) as caught:
        ws.register(SQLRiskDoc)

    messages = [str(item.message) for item in caught]
    assert any("SQL table" in message and "above limit 12" in message and "very_long_sql_risk" in message for message in messages)
    assert any("SQL column" in message and "reserved provider name 'select'" in message for message in messages)
    assert backend.tables[SQLRiskDoc.identifier].name == "very_long_sql_risk"


def test_sql_name_warning_helper_covers_existing_sql_backends():
    """SQL name warnings are read from registered database dialect metadata."""
    for provider in ("sqlite", "postgres", "mysql", "duckdb", "trino", "oracle", "mssql", "starrocks", "oceanbase", "pgvector"):
        _max_length, reserved = database_dialects.warning_rules(provider)
        assert "select" in reserved

    assert database_dialects.warning_rules("postgres")[0] == 63
    assert database_dialects.warning_rules("mysql")[0] == 255
    assert database_dialects.warning_rules("oracle")[0] == 128
    assert database_dialects.warning_rules("mssql")[0] == 128
    postgres = database_dialects.get("pgvector")
    assert postgres.identifier == "postgresql"
    assert postgres.name_warning_dialect_factory == ("sqlalchemy.dialects.postgresql", "dialect")


def test_provider_name_warning_metadata_lives_with_backend_classes():
    """Provider-specific name limits and keywords live with the owning backend adapters."""
    assert hb.backends.ChromaBackend.provider_max_name_length == 512
    assert hb.backends.ElasticsearchBackend.provider_max_name_length == 255
    assert hb.backends.PineconeBackend.provider_max_name_length == 45
    assert hb.backends.MilvusBackend.provider_max_name_length == 255

    assert "_score" in hb.backends.ElasticsearchBackend.provider_reserved_words
    assert "vector" in hb.backends.MilvusBackend.provider_reserved_words
    assert "select" in hb.backends.LanceBackend.provider_reserved_words
    assert "match" in hb.backends.Neo4jBackend.provider_reserved_words
    assert "__score" in hb.backends.RediSearchBackend.provider_reserved_words
    assert "relate" in hb.backends.SurrealBackend.provider_reserved_words


def test_sql_backend_default_provider_keywords_warn(tmp_path):
    """Concrete SQL backends warn for dialect keywords without a test-only override."""
    backend = hb.backends.SQLiteBackend("sql", url=f"sqlite:///{tmp_path / 'keywords.db'}")
    ws = hb.HeavenBase("sql-keyword-ws", backends={"sql": backend})

    class SQLKeywordDoc(hb.Entity):
        select = hb.field(hb.ShortText)

    with pytest.warns(UserWarning, match="reserved provider name 'select'"):
        ws.register(SQLKeywordDoc)


def test_sql_backend_schema_warnings_report_normalized_table_collisions(tmp_path):
    """SQL backends warn when transparent separator normalization maps two logical entities to one table name."""
    backend = hb.backends.SQLiteBackend("sql", url=f"sqlite:///{tmp_path / 'collision.db'}")
    ws = hb.HeavenBase("sql-collision-ws", backends={"sql": backend})

    class DashDoc(hb.Entity):
        identifier = "collision-doc"

        value = hb.field(hb.ShortText)

    class UnderDoc(hb.Entity):
        identifier = "collision_doc"

        value = hb.field(hb.ShortText)

    ws.register(DashDoc)
    with pytest.warns(UserWarning, match="multiple SQL table names"):
        ws.register(UnderDoc)


def test_non_sql_backend_provider_name_warnings_are_defined():
    """Existing non-SQL providers expose their relevant keyword or length warning rules."""

    class KeywordDoc(hb.Entity):
        identifier = "select"

        match = hb.field(hb.ShortText)
        select = hb.field(hb.ShortText)
        where = hb.field(hb.ShortText)

    schema = KeywordDoc.schema()
    surreal = _bare_backend(hb.backends.SurrealBackend, "surreal")
    neo4j = _bare_backend(hb.backends.Neo4jBackend, "neo4j")
    milvus = _bare_backend(hb.backends.MilvusBackend, "milvus")
    lance = _bare_backend(hb.backends.LanceBackend, "lance")

    assert any("SurrealDB table" in message and "reserved provider name 'select'" in message for message in surreal.schema_warnings(schema, []))
    assert any("Neo4j property" in message and "reserved provider name 'match'" in message for message in neo4j.schema_warnings(schema, []))
    assert any("Milvus collection" in message and "reserved provider name 'select'" in message for message in milvus.schema_warnings(schema, []))
    assert any("LanceDB field" in message and "reserved provider name 'select'" in message for message in lance.schema_warnings(schema, []))

    es_schema = EntitySchema(
        "search-doc",
        "SearchDoc",
        fields={"_score": FieldSchema("_score", hb.ShortText())},
    )
    elastic = _bare_backend(hb.backends.ElasticsearchBackend, "elastic")
    assert any("Elasticsearch field" in message and "reserved provider name '_score'" in message for message in elastic.schema_warnings(es_schema, []))

    rs_schema = EntitySchema(
        "redis-doc",
        "RedisDoc",
        fields={"text": FieldSchema("text", hb.ShortText()), "__score": FieldSchema("__score", hb.Float())},
    )
    redisearch = _bare_backend(hb.backends.RediSearchBackend, "redisearch")
    assert any("RediSearch field" in message and "reserved provider name 'text'" in message for message in redisearch.schema_warnings(rs_schema, []))
    assert any("RediSearch field" in message and "reserved provider name '__score'" in message for message in redisearch.schema_warnings(rs_schema, []))

    redis = _bare_backend(hb.backends.RedisBackend, "redis")
    assert redis.schema_warnings(schema, []) == []

    long_schema = EntitySchema("e" + "a" * 60, "LongNameDoc")
    pinecone = _bare_backend(hb.backends.PineconeBackend, "pinecone")
    assert any("Pinecone index" in message and "above limit 45" in message for message in pinecone.schema_warnings(long_schema, []))

    chroma_schema = EntitySchema("c" + "a" * 520, "ChromaLongNameDoc")
    chroma = _bare_backend(hb.backends.ChromaBackend, "chroma")
    assert any("Chroma collection" in message and "above limit 512" in message for message in chroma.schema_warnings(chroma_schema, []))


def test_pinecone_index_name_uses_workspace_prefix_not_user_project_name(monkeypatch):
    """A user-supplied project_name scopes the client, but index ownership still uses the workspace prefix."""

    class FakePinecone:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def list_indexes(self):
            return []

    monkeypatch.setitem(sys.modules, "pinecone", type("FakePineconeModule", (), {"Pinecone": FakePinecone}))
    long_project = "a" * 40  # a valid account-level project name, but long enough to break the old index budget
    pinecone = hb.backends.PineconeBackend("pine", host="localhost:5080", ws_id="pinecone-budget-ws", project_name=long_project)
    name = pinecone._index_name("some-entity")
    assert name.startswith(pinecone._ws_prefix() + "-")  # prefixed by the workspace namespace, not the long project
    assert pinecone.client.kwargs["project_name"] == long_project  # the client still scopes to the user's project verbatim


@pytest.mark.detail
def test_backend_workspace_entity_physical_name_matrix(tmp_path, monkeypatch):
    """Supported backend families should keep workspace namespaces separate from entity objects."""
    workspace_id = "backend-naming-ws"
    workspace_db = hb.backends.Backend._physical_name("backend-naming-ws")
    entity_id = "sample-item"
    entity_table = hb.backends.Backend._physical_name("sample-item")

    monkeypatch.chdir(tmp_path)
    sql = hb.backends.SQLiteBackend("sql")
    ws = hb.HeavenBase(workspace_id, backends={"sql": sql})

    class SQLNamingThing(hb.Entity):
        identifier = entity_id

        name = hb.field(hb.ShortText)

    ws.register(SQLNamingThing)
    assert get_file_basename(sql.db.spec.database) == workspace_db
    assert sql.tables[entity_id].name == entity_table

    class FakeLanceDB:
        @staticmethod
        def connect(uri, **kwargs):
            return type("FakeLanceClient", (), {"uri": uri, "kwargs": kwargs})()

    monkeypatch.setitem(sys.modules, "lancedb", FakeLanceDB)
    lance = hb.backends.LanceBackend("lance", hb.utils.path_to_file_uri(tmp_path / "lance"))
    lance.bind_workspace(workspace_id)
    assert get_file_basename(lance.uri) == workspace_id
    assert lance._table_name(entity_id) == entity_table

    class FakeChromaAdmin:
        def get_database(self, database):
            raise KeyError(database)

        def create_database(self, name):
            self.name = name

    class FakeChromaClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_chroma = type("FakeChroma", (), {"AdminClient": FakeChromaAdmin, "EphemeralClient": FakeChromaClient, "PersistentClient": FakeChromaClient})
    monkeypatch.setitem(sys.modules, "chromadb", fake_chroma)
    chroma = hb.backends.ChromaBackend("chroma")
    chroma.bind_workspace(workspace_id)
    assert chroma.database == workspace_db
    assert chroma.client.kwargs["database"] == workspace_db
    assert chroma._collection_name(entity_id) == entity_table

    class FakeMilvusClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def list_databases(self):
            return []

        def create_database(self, database):
            self.database = database

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": FakeMilvusClient}))
    milvus = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    milvus.bind_workspace(workspace_id)
    assert milvus.database == milvus._owner_scope()
    assert milvus.client.kwargs["db_name"] == milvus.database
    assert milvus._collection(entity_id) == entity_table

    class FakePinecone:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def list_indexes(self):
            return []

    monkeypatch.setitem(sys.modules, "pinecone", type("FakePineconeModule", (), {"Pinecone": FakePinecone}))
    pinecone = hb.backends.PineconeBackend("pine", host="localhost:5080")
    pinecone.bind_workspace(workspace_id)
    assert pinecone.project_name == hb.backends.Backend._physical_name("backend-naming-ws", sep="-")
    assert pinecone.client.kwargs["project_name"] == pinecone.project_name
    assert pinecone._index_name(entity_id).startswith(pinecone.project_name + "-")

    elastic = hb.backends.ElasticsearchBackend("search", hosts=["http://localhost:9200"], ws_id=workspace_id)
    assert elastic.to_dict()["cluster"] == workspace_db
    assert elastic._index(entity_id) == hb.backends.Backend._physical_name("backend-naming-ws", "sample-item", sep="-")

    file_backend = hb.backends.JsonFileBackend("json", hb.utils.path_to_file_uri(tmp_path / "json"))
    file_backend.bind_workspace(workspace_id)
    assert get_file_basename(file_backend.path) == workspace_id
    assert get_file_basename(file_backend._entity_dir(entity_id)) == entity_table

    mem = hb.backends.InMemBackend("mem")
    mem.bind_workspace(workspace_id)
    assert mem.ws_id == workspace_id


def test_server_sql_workspace_database_overrides_single_database_names():
    """Postgres/PgVector/MySQL should target workspace databases when ws_id is bound."""
    from heavenbase.database import DB_ENGINE

    def superuser_database(backend):
        su_kw = DB_ENGINE.apply(backend.db.spec, profile="superuser")
        return su_kw["url"].database

    pg = hb.backends.PostgresBackend("pg", database="rubik", host="localhost", port=5432, username="rubik", password="password")
    assert pg.database == "rubik"
    assert pg.tag("workspace-database-namespace").value is True
    pg.bind_workspace("server-pg-ws")
    assert pg.database == "server_pg_ws"
    assert pg.database == hb.backends.Backend._physical_name("server-pg-ws")
    assert pg.db.spec.database == hb.backends.Backend._physical_name("server-pg-ws")
    assert superuser_database(pg) == "postgres"

    pgvector = hb.backends.PgVectorBackend(
        "pgvector", database="rubik", host="localhost", port=5432, username="rubik", password="password", ws_id="server-pgv-ws"
    )
    assert pgvector.tag("workspace-database-namespace").value is True
    assert pgvector.database == hb.backends.Backend._physical_name("server-pgv-ws")
    assert pgvector.db.spec.database == hb.backends.Backend._physical_name("server-pgv-ws")
    assert superuser_database(pgvector) == "postgres"

    mysql = hb.backends.MySQLBackend("mysql", database="heavenbase", host="localhost", port=3306, username="root", password="password", ws_id="server-mysql-ws")
    assert mysql.tag("workspace-database-namespace").value is True
    assert mysql.database == hb.backends.Backend._physical_name("server-mysql-ws")
    assert mysql.db.spec.database == hb.backends.Backend._physical_name("server-mysql-ws")
    assert superuser_database(mysql) == "mysql"

    direct_mysql = hb.backends.MySQLBackend("mysql", database="heavenbase", host="localhost", port=3306, username="root", password="password")
    assert direct_mysql.database == "heavenbase"
    assert direct_mysql.db.spec.database == "heavenbase"


def test_workspace_drop_removes_sql_file_and_inmem_namespaces(tmp_path, monkeypatch):
    """Workspace drop should remove each backend's workspace-level physical namespace."""
    workspace_id = "drop-workspace-ws"
    workspace_db = hb.backends.Backend._physical_name("drop-workspace-ws")
    monkeypatch.chdir(tmp_path)
    sql = hb.backends.SQLiteBackend("main")
    store = hb.backends.JsonFileBackend("store", hb.utils.path_to_file_uri(tmp_path / "json-store"))
    mem = hb.backends.InMemBackend("mem")
    ws = hb.HeavenBase(workspace_id, backends={"main": sql, "store": store, "mem": mem})

    class DropThing(hb.Entity):
        identifier = "drop-thing"

        name = hb.field(hb.ShortText)

    ws.register(DropThing)
    ws.upsert(DropThing, {"object_id": "d1", "name": "gone"})
    mem.upsert([hb.ext.RowOp("manual", "m1", {"name": "memory"})])

    sql_path = hb.utils.file_uri_to_path(sql.db.spec.database)
    assert exists_path(sql_path)
    assert get_file_basename(store.path) == workspace_id
    assert exists_path(store.path)
    assert mem.scan("manual")

    ws.drop()

    assert not exists_path(sql_path)
    assert not exists_path(store.path)
    assert mem.scan("manual") == []
    assert workspace_id not in WORKSPACES
    assert sql.to_dict()["database"] == workspace_db


@pytest.mark.detail
def test_backend_drop_provider_namespace_matrix(tmp_path, monkeypatch):
    """Provider backends should drop the workspace database/project/index namespace when possible."""
    workspace_id = "drop-provider-ws"
    workspace_db = hb.backends.Backend._physical_name("drop-provider-ws")
    entity_id = "drop-item"

    class FakeLanceDB:
        @staticmethod
        def connect(uri, **kwargs):
            return type("FakeLanceClient", (), {"uri": uri, "kwargs": kwargs})()

    monkeypatch.setitem(sys.modules, "lancedb", FakeLanceDB)
    lance = hb.backends.LanceBackend("lance", hb.utils.path_to_file_uri(tmp_path / "lance"), ws_id=workspace_id)
    assert exists_path(lance.uri)
    lance.drop()
    assert not exists_path(lance.uri)

    class FakeChromaAdmin:
        deleted = []

        def get_database(self, database):
            return {"name": database}

        def create_database(self, name):
            self.name = name

        def delete_database(self, name):
            self.deleted.append(name)

    class FakeChromaClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_chroma = type("FakeChroma", (), {"AdminClient": FakeChromaAdmin, "EphemeralClient": FakeChromaClient, "PersistentClient": FakeChromaClient})
    monkeypatch.setitem(sys.modules, "chromadb", fake_chroma)
    chroma = hb.backends.ChromaBackend("chroma", ws_id=workspace_id)
    chroma.drop()
    assert FakeChromaAdmin.deleted == [workspace_db]

    class FakeMilvusClient:
        dropped = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def list_databases(self):
            return [workspace_db]

        def create_database(self, database):
            self.database = database

        def list_collections(self):
            return []

        def drop_database(self, db_name):
            self.dropped.append(db_name)

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": FakeMilvusClient}))
    milvus = hb.backends.MilvusBackend("milvus", uri="mock://milvus", ws_id=workspace_id)
    milvus_database = milvus.database
    milvus.drop()
    assert FakeMilvusClient.dropped == [milvus_database]

    class FakePinecone:
        deleted = []
        listed = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def list_indexes(self):
            return [{"name": name} for name in FakePinecone.listed]

        def delete_index(self, name):
            self.deleted.append(name)

    monkeypatch.setitem(sys.modules, "pinecone", type("FakePineconeModule", (), {"Pinecone": FakePinecone}))
    pinecone = hb.backends.PineconeBackend("pine", host="localhost:5080", ws_id=workspace_id)
    pine_index = pinecone._index_name(entity_id)
    ws_prefix = pinecone._ws_prefix()
    stale_index = f"{ws_prefix}-stale-extra"  # uncached but shares this workspace's prefix -> must be reaped
    foreign_index = "foreign-ws-other-item"  # different prefix -> must be left alone
    FakePinecone.listed = [stale_index, foreign_index]
    assert pine_index.startswith(ws_prefix + "-")
    pinecone.drop()
    assert set(FakePinecone.deleted) == {pine_index, stale_index} and foreign_index not in FakePinecone.deleted

    class FakeESIndices:
        def __init__(self):
            self.deleted = []

        def delete(self, index, ignore=None):
            self.deleted.append((index, ignore))

    class FakeESClient:
        def __init__(self, hosts, **kwargs):
            self.hosts = hosts
            self.kwargs = kwargs
            self.indices = FakeESIndices()

    monkeypatch.setitem(sys.modules, "elasticsearch", type("FakeElasticsearchModule", (), {"Elasticsearch": FakeESClient}))
    elastic = hb.backends.ElasticsearchBackend("search", hosts=["http://localhost:9200"], ws_id=workspace_id)
    es_index = elastic._index(entity_id)
    elastic.drop()
    assert (es_index, [404]) in elastic.client.indices.deleted


@pytest.mark.detail
def test_elasticsearch_backend_tolerates_concurrent_index_creation(monkeypatch):
    """Elasticsearch ensure should be idempotent when another process wins the create race."""

    class RaceThing(hb.Entity):
        identifier = "race-thing"

        title = hb.field(hb.ShortText)

    class FakeESIndices:
        def __init__(self):
            self.created = []
            self.mapping = {}

        def exists(self, index):
            return False

        def create(self, index, **kwargs):
            self.created.append(index)
            self.mapping = {index: {"mappings": kwargs["mappings"]}}
            raise RuntimeError("resource_already_exists_exception")

        def get_mapping(self, index):
            return self.mapping

    class FakeESClient:
        def __init__(self, hosts, **kwargs):
            self.indices = FakeESIndices()

    monkeypatch.setitem(sys.modules, "elasticsearch", type("FakeElasticsearchModule", (), {"Elasticsearch": FakeESClient}))
    elastic = hb.backends.ElasticsearchBackend("search", hosts=["http://localhost:9200"], ws_id="race-workspace")

    elastic.ensure(RaceThing.schema(), [])

    assert elastic.client.indices.created == [hb.backends.Backend._physical_name("race-workspace", "race-thing", sep="-")]


@pytest.mark.detail
def test_elasticsearch_rejects_immutable_index_policy_without_publishing_schema(monkeypatch):
    """A reindex-required mapping mismatch should not leak backend-local schema state."""

    class PolicyThing(hb.Entity):
        identifier = "policy-thing"

        title = hb.field(hb.ShortText).index(enabled=False)

    class FakeESIndices:
        def exists(self, index):
            return True

        def get_mapping(self, index):
            return {index: {"mappings": {"properties": {"title": {"type": "wildcard"}}}}}

        def put_mapping(self, **kwargs):
            raise AssertionError("immutable policy mismatch must fail before additive mapping")

    class FakeESClient:
        def __init__(self, hosts, **kwargs):
            self.indices = FakeESIndices()

    class EmptyPlan:
        def backend_indexes(self, backend):
            return []

        def backend_constraints(self, backend):
            return []

    monkeypatch.setitem(sys.modules, "elasticsearch", type("FakeElasticsearchModule", (), {"Elasticsearch": FakeESClient}))
    elastic = hb.backends.ElasticsearchBackend("search", hosts=["http://localhost:9200"], ws_id="policy-workspace")

    with pytest.raises(ValueError, match="cannot change index policy in place"):
        elastic.ensure_plan(PolicyThing.schema(), EmptyPlan())

    assert PolicyThing.identifier not in elastic._schemas


def test_file_backend_workspace_rebind_clears_loaded_state(tmp_path):
    """Direct FileBackend instances should refresh path-derived state when bound to a workspace."""
    path = hb.utils.path_to_file_uri(tmp_path / "file-rebind-store")
    backend = hb.backends.JsonFileBackend("store", path, ws_id="file-rebind-a")
    backend.upsert([hb.ext.RowOp("file-rebind-thing", "r1", {"object_id": "r1", "name": "alpha"})])
    assert backend.scan("file-rebind-thing")

    backend.bind_workspace("file-rebind-b")

    assert get_file_basename(backend.path) == "file-rebind-b"
    assert backend.data == {}
    assert backend._loaded_entities == {}
    assert backend.scan("file-rebind-thing") == []


def test_registry_file_default_path_uses_single_workspace_segment(tmp_path, monkeypatch):
    """Config-built local file backends should not turn ws_id into a base path and append it again."""
    monkeypatch.setattr(hb.CM_HVNB, "root", str(tmp_path))
    backend = hb.ext.BackendRegistry.from_config({"store": {"type": "json"}}, ws_id="registry-file-ws").backends["store"]

    assert (get_file_basename(get_file_dir(backend.path)), get_file_basename(backend.path)) == ("json", "registry-file-ws")
    assert "%" not in backend.path


@pytest.mark.parametrize(
    "backend_factory, workspace_id, backend_name",
    [
        (lambda path: hb.backends.SQLiteBackend("sql", _db(path, "sqlite.db")), "sql-test", "sql"),
        pytest.param(
            lambda path: hb.backends.DuckDBBackend("duck", _db(path, "duck.duckdb")),
            "duckdb-test",
            "duck",
            marks=[
                pytest.mark.skipif(find_spec("duckdb_engine") is None, reason="duckdb_engine is not installed"),
                pytest.mark.slow,
            ],
        ),
    ],
)
def test_sql_backends_roundtrip_and_complex_filters(tmp_path, backend_factory, workspace_id, backend_name):
    """SQL-family backends should round-trip rows and group complex filters."""
    ws = hb.HeavenBase(workspace_id, backends={"sql": backend_factory(tmp_path)})

    class SQLProduct(hb.Entity):
        identifier = f"{workspace_id}-product"

        name = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        price = hb.field(hb.Float)
        stock = hb.field(hb.Integer)

    ws.register(SQLProduct)
    ws.upsert_many(
        SQLProduct,
        [
            SQLProduct.from_dict({"object_id": "p1", "name": "ANC headphones", "body": "quiet audio", "price": 129, "stock": 5}),
            SQLProduct.from_dict({"object_id": "p2", "name": "Desk", "body": "oak office", "price": 89, "stock": 0}),
            SQLProduct.from_dict({"object_id": "p3", "name": "Earbuds", "body": "pocket audio", "price": 99, "stock": 7}),
        ],
    )

    query = (
        ws.query(SQLProduct)
        .where((SQLProduct.price <= 130) & (SQLProduct.body.match("audio") | (SQLProduct.name == "Desk")) & ~(SQLProduct.stock <= 0))
        .select("name", "price")
    )
    frame = query.execute()
    explain = query.explain()

    assert frame.ids == ["p1", "p3"]
    assert explain["steps"][0]["node"] == "and"
    assert {step["backend"] for step in explain["steps"] if step.get("backend")} == {backend_name}


@pytest.mark.full
def test_sql_get_many_and_count_use_backend_primitives(tmp_path):
    """SQL identity lookups should avoid whole-table scans."""
    backend = _ScanCountingSQLiteBackend("sql", _db(tmp_path, "lookup.db"))
    ws = hb.HeavenBase("sql-id-lookup-test", backends={"sql": backend})

    class LookupThing(hb.Entity):
        identifier = "sql-id-lookup-thing"

        name = hb.field(hb.ShortText)
        price = hb.field(hb.Float)

    ws.register(LookupThing)
    ws.upsert_many(
        LookupThing,
        [
            {"object_id": "p1", "name": "Alpha", "price": 1.0},
            {"object_id": "p2", "name": "Beta", "price": 2.0},
            {"object_id": "p3", "name": "Gamma", "price": 3.0},
        ],
    )

    rows = ws.get_many(["p3", "missing", "p1"], entity=LookupThing)

    assert [row["object_id"] if row else None for row in rows] == ["p3", None, "p1"]
    assert ws.exists_many(["p2", "missing"], entity=LookupThing) == [True, False]
    assert ws.count(LookupThing) == 3
    assert backend.scan_count == 0


@pytest.mark.parametrize(
    "backend_factory, workspace_ids",
    [
        (lambda name: hb.backends.SQLiteBackend(name, ":memory:"), ("sql-memory-a", "sql-memory-b")),
        pytest.param(
            lambda name: hb.backends.DuckDBBackend(name, ":memory:"),
            ("duck-memory-a", "duck-memory-b"),
            marks=[
                pytest.mark.skipif(find_spec("duckdb_engine") is None, reason="duckdb_engine is not installed"),
                pytest.mark.slow,
            ],
        ),
    ],
)
def test_memory_sql_backends_are_isolated(backend_factory, workspace_ids):
    """Independent in-memory SQL backends should not share row state."""
    ws_a = hb.HeavenBase(workspace_ids[0], backends={"sql": backend_factory("sql-a")})
    ws_b = hb.HeavenBase(workspace_ids[1], backends={"sql": backend_factory("sql-b")})

    class MemoryThing(hb.Entity):
        identifier = f"{workspace_ids[0]}-thing"

        name = hb.field(hb.ShortText)

    ws_a.register(MemoryThing)
    ws_b.register(MemoryThing)
    ws_a.upsert(MemoryThing.from_dict({"object_id": "one", "name": "Alpha"}))
    ws_b.upsert(MemoryThing.from_dict({"object_id": "one", "name": "Beta"}))

    assert ws_a.get("one")["name"] == "Alpha"
    assert ws_b.get("one")["name"] == "Beta"


@pytest.mark.detail
@pytest.mark.external
def test_default_routing_prefers_search_and_vector_backends(tmp_path):
    """Default routing should prefer search and vector-capable backends."""
    if find_spec("elasticsearch") is None:
        pytest.skip("elasticsearch is not installed")
    elastic_url = resolve_interpolations({"value": "${env:HVNB_ELASTICSEARCH_URL,http://localhost:9200}"})["value"]
    _require_elasticsearch(elastic_url)

    ws = hb.HeavenBase(
        f"route-test-{uuid4().hex[:8]}",
        backends={
            "sql": hb.backends.SQLiteBackend("sql", _db(tmp_path, "main.db")),
            "search": hb.backends.ElasticsearchBackend("search", hosts=[elastic_url]),
            "vec": hb.backends.InMemBackend("vec"),
        },
    )
    try:

        class RoutedDoc(hb.Entity):
            identifier = "routed-doc"

            title = hb.field(hb.ShortText)
            body = hb.field(hb.LongText)
            emb = hb.field(hb.Vector[2])

        ws.upsert(RoutedDoc.from_dict({"object_id": "d1", "title": "Reset", "body": "bluetooth audio reset", "emb": [1, 0]}))
        query = ws.query(RoutedDoc).where(RoutedDoc.body.match("audio")).near(RoutedDoc.emb, [1, 0], top_k=3).select("title", "score")
        explain = query.explain()

        assert query.execute().rows()[0]["title"] == "Reset"
        assert any(step.get("backend") == "search" and step.get("strategy") == hb.InvertedIndex.identifier for step in explain["steps"])
        assert any(step.get("backend") == "vec" and step.get("strategy") == hb.VectorIndex.identifier for step in explain["steps"])
    finally:
        ws.drop()


@pytest.mark.detail
@pytest.mark.external
def test_elasticsearch_index_policy_migration_boundary_live():
    """Live Elasticsearch should reject in-place policy changes and accept a migrated entity."""
    if find_spec("elasticsearch") is None:
        pytest.skip("elasticsearch is not installed")
    elastic_url = resolve_interpolations({"value": "${env:HVNB_ELASTICSEARCH_URL,http://localhost:9200}"})["value"]
    _require_elasticsearch(elastic_url)

    entity_id = f"policy-live-{uuid4().hex[:8]}"
    search = hb.backends.ElasticsearchBackend("search", hosts=[elastic_url])
    ws = hb.HeavenBase(f"policy-live-ws-{uuid4().hex[:8]}", backends={"main": hb.backends.InMemBackend("main"), "search": search})
    try:

        class PolicyV1(hb.Entity):
            identifier = entity_id

            title = hb.field(hb.ShortText).store(to="search")

        ws.register(PolicyV1)
        ws.upsert(PolicyV1, {"object_id": "p1", "title": "indexed"})
        old_plan = ws.storage_plans[entity_id]

        class PolicyV2(hb.Entity):
            identifier = entity_id

            title = hb.field(hb.ShortText).store(to="search").index(enabled=False, to="search")

        with pytest.raises(ValueError, match="cannot change index policy in place"):
            ws.register(PolicyV2, allow_replace=True)
        assert ws.entities[entity_id] is PolicyV1
        assert ws.storage_plans[entity_id] is old_plan
        assert search._schemas[entity_id] == PolicyV1.schema()

        class PolicyMigrated(hb.Entity):
            identifier = f"{entity_id}-v2"

            title = hb.field(hb.ShortText).store(to="search").index(enabled=False, to="search")

        ws.register(PolicyMigrated)
        ws.upsert(PolicyMigrated, {"object_id": "p1", "title": "reindexed"})
        assert not ws.storage_plans[PolicyMigrated.identifier].has_index("title", backend="search")
        assert ws.get("p1", entity=PolicyMigrated)["title"] == "reindexed"
    finally:
        ws.drop()


@pytest.mark.detail
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_native_near_and_metadata_filter(tmp_path):
    """Chroma should execute native near search and metadata filters."""
    ws = hb.HeavenBase(
        "chroma-native-test",
        backends={"main": hb.backends.InMemBackend("main"), "chroma": hb.backends.ChromaBackend("chroma", hb.utils.path_to_file_uri(tmp_path / "chroma"))},
    )

    class ChromaThing(hb.Entity):
        identifier = "chroma-native-thing"

        title = hb.field(hb.ShortText).store(to="chroma")
        price = hb.field(hb.Float).store(to="chroma")
        embedding = hb.field(hb.Vector[2]).store(to="chroma", strategy=hb.VectorIndex.identifier)

    ws.register(ChromaThing)
    ws.upsert_many(
        ChromaThing,
        [
            {"object_id": "v1", "title": "near", "price": 1.0, "embedding": [1, 0]},
            {"object_id": "v2", "title": "far", "price": 2.0, "embedding": [0, 1]},
        ],
    )

    near = ws.query(ChromaThing).near(ChromaThing.embedding, [1, 0], top_k=1).select("title", "score")
    filt = ws.query(ChromaThing).where(ChromaThing.price > 1).select("title")

    assert near.execute().rows()[0]["object_id"] == "v1"
    assert near.explain()["steps"][0]["type"] == "chroma"
    assert filt.execute().rows() == [{"object_id": "v2", "title": "far"}]
    assert filt.explain()["steps"][0]["type"] == "chroma"


@pytest.mark.detail
def test_vector_backend_array_contains_compiler_boundaries():
    """Vector-family array_contains handlers should honor backend type limits."""

    class VectorArrayThing(hb.Entity):
        identifier = "vector-array-thing"

        tags = hb.field(hb.Array[hb.ShortText]).required()
        scores = hb.field(hb.Array[hb.Integer]).required()
        raw = hb.field(hb.Array).required()

    schema = VectorArrayThing.schema()
    ctx = hb.ext.HandlerContext("vec", "milvus", hb.InlineColumn.identifier)
    milvus = seed_handlers().get(hb.Array, "array_contains", "milvus", hb.InlineColumn.identifier)(schema.get("tags"), "sale", ctx)
    milvus_numeric = seed_handlers().get(hb.Array, "array_contains", "milvus", hb.InlineColumn.identifier)(schema.get("scores"), 3, ctx)
    milvus_raw = seed_handlers().get(hb.Array, "array_contains", "milvus", hb.InlineColumn.identifier)(schema.get("raw"), "sale", ctx)

    assert milvus.notes["kind"] == "milvus_filter"
    assert milvus.payload == 'ARRAY_CONTAINS(tags, "sale")'
    assert milvus_numeric.payload == "ARRAY_CONTAINS(scores, 3)"
    assert milvus_raw.notes["mode"] == "py_scan"

    chroma = seed_handlers().get(hb.Array, "array_contains", "chroma", hb.InlineColumn.identifier)(schema.get("scores"), 3, ctx)
    assert chroma.notes["kind"] == "chroma_where"
    assert chroma.payload == {"scores": {"$contains": 3}}

    pinecone = seed_handlers().get(hb.Array, "array_contains", "pinecone", hb.InlineColumn.identifier)(schema.get("tags"), "sale", ctx)
    pinecone_numeric = seed_handlers().get(hb.Array, "array_contains", "pinecone", hb.InlineColumn.identifier)(schema.get("scores"), 3, ctx)
    assert pinecone.notes["kind"] == "pinecone_filter"
    assert pinecone.payload == {"tags": {"$eq": "sale"}}
    assert pinecone_numeric.notes["mode"] == "py_scan"


@pytest.mark.detail
def test_elasticsearch_affix_pushdown_is_literal_safe():
    """Elasticsearch text affix handlers push down only literal-safe wildcard clauses."""

    class ElasticPatternThing(hb.Entity):
        identifier = "elastic-pattern-thing"

        title = hb.field(hb.ShortText)
        day = hb.field(hb.Date)

    schema = ElasticPatternThing.schema()
    handlers = seed_handlers()
    ctx = hb.ext.HandlerContext("search", "elasticsearch", hb.InlineColumn.identifier, scalar_indexed=True)

    starts = handlers.get(hb.ShortText, "starts_with", "elasticsearch", hb.InlineColumn.identifier)(schema.get("title"), "Al", ctx)
    ends = handlers.get(hb.ShortText, "ends_with", "elasticsearch", hb.InlineColumn.identifier)(schema.get("title"), "pha", ctx)
    assert starts.notes["mode"] == "native"
    assert starts.payload == {"wildcard": {"title": {"value": "Al*", "case_insensitive": False}}}
    assert ends.notes["mode"] == "native"
    assert ends.payload == {"wildcard": {"title": {"value": "*pha", "case_insensitive": False}}}

    unsafe_contains = handlers.get(hb.ShortText, "contains", "elasticsearch", hb.InlineColumn.identifier)(schema.get("title"), "a*", ctx)
    unsafe_like = handlers.get(hb.ShortText, "like", "elasticsearch", hb.InlineColumn.identifier)(schema.get("title"), "%a*%", ctx)
    date_pattern = handlers.get(hb.Date, "contains", "elasticsearch", hb.InlineColumn.identifier)(schema.get("day"), "2026", ctx)
    regex = handlers.get(hb.ShortText, "regex", "elasticsearch", hb.InlineColumn.identifier)(schema.get("title"), "^Al", ctx)
    assert unsafe_contains.notes["mode"] == "py_scan"
    assert unsafe_like.notes["mode"] == "py_scan"
    assert date_pattern.notes["mode"] == "py_scan"
    assert regex.notes["mode"] == "py_scan"
    assert "fallback_reason" in regex.notes


@pytest.mark.detail
def test_milvus_near_handler_rejects_dimension_mismatch():
    """Milvus near handler should raise a clear error on dimension mismatch."""
    from heavenbase.execution.handlers.seed import seed_handlers
    from heavenbase.schema import FieldSchema

    handlers = seed_handlers()
    field_schema = FieldSchema("emb", hb.Vector[3])
    ctx = hb.ext.HandlerContext("vec", "milvus", hb.VectorIndex.identifier)
    with pytest.raises(ValueError, match="received dimension 2"):
        handlers.get(hb.Vector, "near", "milvus", hb.VectorIndex.identifier)(field_schema, [1, 0], ctx)


@pytest.mark.detail
def test_milvus_json_filter_falls_back_with_reason():
    """Milvus Json path filters should compile to py_scan with a fallback reason."""
    from heavenbase.execution.handlers.seed import seed_handlers
    from heavenbase.schema import FieldSchema

    handlers = seed_handlers()
    field_schema = FieldSchema("attrs", hb.Json())
    ctx = hb.ext.HandlerContext("vec", "milvus", hb.InlineColumn.identifier)
    value = {"path": "tier", "eq": "gold"}
    fragment = handlers.get(hb.Json, "get", "milvus", hb.InlineColumn.identifier)(field_schema, value, ctx)

    assert fragment.notes["mode"] == "py_scan"
    assert "fallback_reason" in fragment.notes
    assert "Json" in fragment.notes["fallback_reason"]


@pytest.mark.detail
def test_milvus_schema_declares_logical_fields_as_physical_columns(monkeypatch):
    """Milvus should declare only routed logical fields as physical columns."""

    class FakeDataType:
        ARRAY = "ARRAY"
        BOOL = "BOOL"
        DOUBLE = "DOUBLE"
        FLOAT_VECTOR = "FLOAT_VECTOR"
        INT64 = "INT64"
        JSON = "JSON"
        VARCHAR = "VARCHAR"

    class FakeSchemaDef:
        def __init__(self):
            self.fields = []

        def add_field(self, name, dtype, **kwargs):
            self.fields.append({"name": name, "dtype": dtype, "kwargs": kwargs})

    class FakeIndexParams:
        def __init__(self):
            self.indexes = []

        def add_index(self, **kwargs):
            self.indexes.append(kwargs)

    class FakeMilvusClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.schema = None
            self.schema_args = {}
            self.index_params = None
            self.collection = ""

        def has_collection(self, name):
            return False

        def create_schema(self, **kwargs):
            self.schema_args = kwargs
            self.schema = FakeSchemaDef()
            return self.schema

        def prepare_index_params(self):
            self.index_params = FakeIndexParams()
            return self.index_params

        def create_collection(self, collection_name, schema, index_params, consistency_level):
            self.collection = collection_name
            self.consistency_level = consistency_level

    fake_pymilvus = type("FakePymilvus", (), {"DataType": FakeDataType, "MilvusClient": FakeMilvusClient})
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    ws = hb.HeavenBase("milvus-schema-column-test", backends={"main": hb.backends.InMemBackend("main"), "milvus": backend})

    class MilvusColumnThing(hb.Entity):
        identifier = "milvus-column-thing"

        name = hb.field(hb.ShortText)
        subtitle = hb.field(hb.MediumText)
        body = hb.field(hb.LongText)
        price = hb.field(hb.Float)
        stock = hb.field(hb.Integer)
        active = hb.field(hb.Boolean)
        tags = hb.field(hb.Array[hb.ShortText])
        scores = hb.field(hb.Array[hb.Integer])
        attrs = hb.field(hb.Json)
        topics = hb.field(hb.HyperG["slot", "value", "topic"])
        artifact = hb.field(hb.Artifact)
        emb = hb.field(hb.Vector[2]).required().store(to="milvus", strategy=hb.VectorIndex.identifier)

    ws.register(MilvusColumnThing)
    fields = {field["name"]: field for field in backend.client.schema.fields}
    indexed = {index["field_name"] for index in backend.client.index_params.indexes}

    assert backend.client.kwargs["db_name"] == backend.database
    assert backend.client.collection == hb.backends.Backend._physical_name("milvus-column-thing")
    assert backend.client.consistency_level == "Strong"
    assert backend.client.schema_args == {"auto_id": False, "enable_dynamic_field": False}
    assert "$meta" not in fields
    assert set(fields) == {"object_id", "emb"}
    assert fields["object_id"]["kwargs"] == {"is_primary": True, "max_length": hb.Identifier().max_chars}
    assert fields["object_id"]["dtype"] == FakeDataType.VARCHAR
    assert fields["emb"]["kwargs"] == {"dim": 2}
    assert indexed == {"emb"}

    WORKSPACES.pop("milvus-schema-column-test", None)


@pytest.mark.detail
def test_milvus_existing_collection_rejects_missing_physical_fields(monkeypatch):
    """Milvus should not silently reuse stale dynamic collections."""

    class FakeDataType:
        FLOAT_VECTOR = "FLOAT_VECTOR"
        VARCHAR = "VARCHAR"

    class FakeMilvusClient:
        def __init__(self, **kwargs):
            self.description = {
                "enable_dynamic_field": True,
                "fields": [{"name": "entity_id"}, {"name": "emb"}],
            }

        def has_collection(self, name):
            return True

        def describe_collection(self, collection_name):
            return self.description

    fake_pymilvus = type("FakePymilvus", (), {"DataType": FakeDataType, "MilvusClient": FakeMilvusClient})
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    ws = hb.HeavenBase("milvus-stale-column-test", backends={"main": hb.backends.InMemBackend("main"), "milvus": backend})

    class MilvusStaleThing(hb.Entity):
        identifier = "milvus-stale-thing"

        name = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2]).required().store(to="milvus", strategy=hb.VectorIndex.identifier)

    try:
        with pytest.raises(RuntimeError, match="missing_fields"):
            ws.register(MilvusStaleThing)
    finally:
        WORKSPACES.pop("milvus-stale-column-test", None)


@pytest.mark.detail
def test_milvus_existing_collection_rejects_vector_dimension_mismatch(monkeypatch):
    """Milvus should reject an existing physical column with the wrong vector shape."""

    class FakeDataType:
        FLOAT_VECTOR = "FLOAT_VECTOR"
        VARCHAR = "VARCHAR"

    class FakeMilvusClient:
        def __init__(self, **kwargs):
            self.description = {
                "enable_dynamic_field": False,
                "fields": [
                    {"name": "object_id", "type": FakeDataType.VARCHAR},
                    {"name": "name", "type": FakeDataType.VARCHAR},
                    {"name": "emb", "type": FakeDataType.FLOAT_VECTOR, "params": {"dim": "3"}},
                ],
            }

        def has_collection(self, name):
            return True

        def describe_collection(self, collection_name):
            return self.description

    fake_pymilvus = type("FakePymilvus", (), {"DataType": FakeDataType, "MilvusClient": FakeMilvusClient})
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    ws = hb.HeavenBase("milvus-wrong-dim-test", backends={"main": hb.backends.InMemBackend("main"), "milvus": backend})

    class MilvusWrongDimThing(hb.Entity):
        identifier = "milvus-wrong-dim-thing"

        name = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2]).required().store(to="milvus", strategy=hb.VectorIndex.identifier)

    try:
        with pytest.raises(RuntimeError, match="mismatched_fields"):
            ws.register(MilvusWrongDimThing)
    finally:
        WORKSPACES.pop("milvus-wrong-dim-test", None)


@pytest.mark.detail
def test_milvus_native_filter_flushes_dirty_collection(monkeypatch):
    """Milvus native filters should see rows written just before candidate filtering."""
    from heavenbase.execution.contracts import QueryFragment

    class FakeMilvusClient:
        def __init__(self, **kwargs):
            self.flushes = []
            self.queries = []

        def has_collection(self, name):
            return True

        def flush(self, collection_name):
            self.flushes.append(collection_name)

        def query_iterator(self, collection_name, batch_size, filter, output_fields, consistency_level):
            self.queries.append((collection_name, filter, output_fields, batch_size, consistency_level))
            batches = [[{"object_id": "file-01", "tags": ["finance"]}], []]

            class Iterator:
                def next(self):
                    return batches.pop(0)

                @staticmethod
                def close():
                    return None

            return Iterator()

    fake_pymilvus = type("FakePymilvus", (), {"MilvusClient": FakeMilvusClient})
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    collection = backend._collection("milvus-filter-flush-thing")
    backend._dirty_collections.add(collection)

    fragment = QueryFragment(
        "milvus",
        "array_contains",
        "tags",
        'ARRAY_CONTAINS(tags, "finance")',
        notes={"kind": "milvus_filter", "entity_id": "milvus-filter-flush-thing"},
    )

    frame = backend.execute(fragment)

    assert frame.ids == ["file-01"]
    assert backend.client.flushes == [collection]
    assert collection not in backend._dirty_collections
    assert backend.client.queries[0][1] == 'ARRAY_CONTAINS(tags, "finance")'
    assert backend.client.queries[0][3] == int(hb.CM_HVNB.get("heavenbase.backend_defaults.milvus.filter_limit", default=10000))
    assert backend.client.queries[0][4] == "Strong"


@pytest.mark.detail
def test_milvus_mixed_logical_fallback_warns_and_scans(monkeypatch):
    """Milvus mixed native/scan logical filters should delegate to the shared fallback executor."""
    from heavenbase.execution.contracts import QueryFragment

    class FakeMilvusClient:
        def __init__(self, **kwargs):
            self.queries = []

        def has_collection(self, name):
            return True

        def query(self, collection_name, filter, output_fields, limit):
            self.queries.append((collection_name, filter, output_fields, limit))
            return [
                {"object_id": "file-01", "tier": "gold", "body": "native and scanned"},
                {"object_id": "file-02", "tier": "gold", "body": "native only"},
                {"object_id": "file-03", "tier": "silver", "body": "native and scanned"},
            ]

        def query_iterator(self, collection_name, batch_size, filter, output_fields, consistency_level):
            self.queries.append((collection_name, filter, output_fields, batch_size, consistency_level))
            batches = [
                [
                    {"object_id": "file-01", "tier": "gold", "body": "native and scanned"},
                    {"object_id": "file-02", "tier": "gold", "body": "native only"},
                    {"object_id": "file-03", "tier": "silver", "body": "native and scanned"},
                ],
                [],
            ]

            class Iterator:
                def next(self):
                    return batches.pop(0)

                @staticmethod
                def close():
                    return None

            return Iterator()

    fake_pymilvus = type("FakePymilvus", (), {"MilvusClient": FakeMilvusClient})
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)
    manager = hb.DEFAULT_CONTEXT.config
    real_get = manager.get
    monkeypatch.setitem(
        vars(manager),
        "get",
        lambda key, default=None: True if key == "heavenbase.query.warn_on_python_fallback" else real_get(key, default=default),
    )
    backend = hb.backends.MilvusBackend(
        "milvus",
        uri="mock://milvus",
        resolver=hb.DEFAULT_CONTEXT.modules(),
        config=manager,
    )
    fragment = QueryFragment(
        "milvus",
        "and",
        "*",
        [
            QueryFragment(
                "milvus",
                "eq",
                "tier",
                'tier == "gold"',
                notes={"kind": "milvus_filter", "mode": "native", "row_payload": lambda row: row.get("tier") == "gold"},
            ),
            QueryFragment(
                "milvus",
                "match",
                "body",
                lambda row: "scanned" in str(row.get("body", "")),
                notes={"mode": "py_scan", "fallback_reason": "Milvus match uses provider scan fallback for text semantics."},
            ),
        ],
        notes={"entity_id": "milvus-mixed-fallback-thing"},
    )

    with pytest.warns(UserWarning, match="Python scan fallback"):
        frame = backend.execute(fragment)

    assert frame.ids == ["file-01"]
    assert backend.client.queries[0][1] == ""


def test_filter_registry_keeps_ops_type_scoped():
    """The handler registry should expose only type-appropriate filter ops."""
    handlers = seed_handlers()
    assert handlers.get(hb.ShortText, "regex", "inmem", hb.InlineColumn.identifier)
    assert handlers.get(hb.Json, "get", "inmem", hb.InlineColumn.identifier)
    assert handlers.get(hb.Array, "array_contains", "inmem", hb.InlineColumn.identifier)
    with pytest.raises(LookupError):
        handlers.get(hb.Float, "regex", "inmem", hb.InlineColumn.identifier)
    with pytest.raises(LookupError):
        handlers.get(hb.Boolean, "wildcard", "inmem", hb.InlineColumn.identifier)
    with pytest.raises(LookupError):
        handlers.get(hb.Array, "like", "inmem", hb.InlineColumn.identifier)
    with pytest.raises(LookupError):
        handlers.get(hb.Json, "match", "inmem", hb.InlineColumn.identifier)


@pytest.mark.detail
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_native_array_contains_filter(tmp_path):
    """Chroma should compile scalar Array membership to a native metadata filter."""
    ws = hb.HeavenBase(
        "chroma-array-filter-test",
        backends={
            "main": hb.backends.InMemBackend("main"),
            "chroma": hb.backends.ChromaBackend("chroma", hb.utils.path_to_file_uri(tmp_path / "chroma-array")),
        },
    )

    class ChromaArrayThing(hb.Entity):
        identifier = "chroma-array-thing"

        title = hb.field(hb.ShortText).store(to="chroma")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="chroma")
        embedding = hb.field(hb.Vector[2]).store(to="chroma", strategy=hb.VectorIndex.identifier)

    ws.register(ChromaArrayThing)
    ws.upsert_many(
        ChromaArrayThing,
        [
            {"object_id": "a", "title": "audio", "tags": ["audio", "guide"], "embedding": [1, 0]},
            {"object_id": "b", "title": "office", "tags": ["desk"], "embedding": [0, 1]},
        ],
    )

    query = ws.query(ChromaArrayThing).where(ChromaArrayThing.tags.array_contains("audio")).select("title")

    assert query.execute().rows() == [{"object_id": "a", "title": "audio"}]
    assert query.explain()["steps"][0]["type"] == "chroma"


def test_workspace_id_class_access_without_workspace_instance():
    """Entity.register(workspace_id) should bind class access to an existing workspace."""
    ws = hb.HeavenBase("class-api-test")

    class ClassAPIThing(hb.Entity):
        identifier = "class-api-thing"

        name = hb.field(hb.ShortText)

    ClassAPIThing.register("class-api-test")
    entity_id = ws.upsert(ClassAPIThing.from_dict({"object_id": "direct", "name": "direct"}))

    assert ws.get(entity_id)["name"] == "direct"
    assert ws.query(ClassAPIThing).where(ClassAPIThing.name == "direct").count().execute().scalar() == 1


@pytest.mark.detail
def test_provider_classes_construct(tmp_path):
    """Provider constructors should stay importable; CI requires only local durable backends."""
    providers = [
        hb.backends.InMemBackend("mem"),
        hb.backends.JsonFileBackend("json", hb.utils.path_to_file_uri(tmp_path / "x.json")),
        hb.backends.PickleFileBackend("pickle", hb.utils.path_to_file_uri(tmp_path / "x.pkl")),
        hb.backends.SQLiteBackend("sqlite", _db(tmp_path, "x.db")),
    ]
    expected = {"inmem", "json", "pickle", "sqlite"}

    if _github_actions():
        if find_spec("lancedb") is None:
            pytest.fail("lancedb is required on GitHub Actions")
        providers.append(hb.backends.LanceBackend("lance", hb.utils.path_to_file_uri(tmp_path / "lance")))
        expected.add("lance")
        assert {backend.type for backend in providers} >= expected
        return

    providers += [
        hb.backends.DuckDBBackend("duck", _db(tmp_path, "x.duckdb")),
        hb.backends.PostgresBackend("postgres"),
        hb.backends.TrinoBackend("trino"),
        hb.backends.MySQLBackend("mysql"),
        hb.backends.StarRocksBackend("starrocks"),
        hb.backends.OracleBackend("oracle"),
        hb.backends.PgVectorBackend("pgvector"),
        hb.backends.MilvusBackend("milvus"),
        hb.backends.ElasticsearchBackend("elastic"),
    ]
    expected |= {"duckdb", "postgres", "trino", "mysql", "starrocks", "oracle", "pgvector", "milvus", "elasticsearch"}

    if find_spec("lancedb") is not None:
        providers.append(hb.backends.LanceBackend("lance", hb.utils.path_to_file_uri(tmp_path / "lance")))
    if find_spec("chromadb") is not None:
        providers.append(hb.backends.ChromaBackend("chroma"))
    if find_spec("pinecone") is not None:
        providers.append(hb.backends.PineconeBackend("pinecone"))

    if find_spec("lancedb") is not None:
        expected.add("lance")
    if find_spec("chromadb") is not None:
        expected.add("chroma")
    if find_spec("pinecone") is not None:
        expected.add("pinecone")

    assert {backend.type for backend in providers} >= expected


def test_backend_type_records_use_type_config(tmp_path):
    """Backend config loading should resolve type aliases through one record."""
    backends = hb.ext.BackendRegistry.from_config(
        {
            "main": {"type": "sqlite", "database": _db(tmp_path, "main.db")},
            "vec": {"type": "inmem"},
            "search": {"type": "elastic"},
            "pine": {"type": "pinecone", "host": "localhost:5080"},
        }
    ).backends

    assert backends["main"].type == "sqlite"
    assert backends["vec"].type == "inmem"
    assert backends["search"].type == "elasticsearch"
    assert backends["pine"].type == "pinecone"
    assert backends["pine"].host == "localhost:5080"
    assert hb.BackendType.identifier_of("search") == "elasticsearch"
    assert hb.BackendType.factory("search") is hb.BackendType.factory("elasticsearch")
    assert not hasattr(hb.ext, "get_backend_builder")
    assert not hasattr(hb.ext, "register_backend_builder")
    assert {"sqlite", "inmem", "elasticsearch", "pinecone"} <= set(hb.BackendType.identifiers())
    assert "search" not in hb.BackendType.identifiers()


def test_late_external_backend_receives_generic_handlers():
    """Backends registered after import should still receive generic scan handlers."""
    from heavenbase.schema import FieldSchema

    LateExternalBackend.register(tags={"portable": True, "storage-roles": ["row"]})
    try:
        built = hb.BackendType.build(LateExternalBackend.identifier, {"type": LateExternalBackend.identifier}, ws_id="late-ws")
        assert isinstance(built, LateExternalBackend)
        assert built.ws_id == "late-ws"

        handlers = seed_handlers()
        field_schema = FieldSchema("name", hb.ShortText())
        ctx = hb.ext.HandlerContext("late", LateExternalBackend.identifier, hb.InlineColumn.identifier)
        fragment = handlers.get(hb.ShortText, "eq", LateExternalBackend.identifier, hb.InlineColumn.identifier)(field_schema, "alpha", ctx)

        assert fragment.notes["mode"] == "py_scan"
        assert fragment.payload({"name": "alpha"})
        assert not fragment.payload({"name": "beta"})
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("backend_type", LateExternalBackend.identifier)


def test_backend_record_can_name_one_custom_factory():
    """A custom factory should live on the same record as its Backend class."""
    CustomFactoryBackend.register(factory=build_custom_factory_backend)
    try:
        backend = hb.BackendType.build(
            "custom",
            {"type": "custom-factory-backend", "marker": "record-factory"},
            ws_id="custom-factory-ws",
        )

        assert type(backend) is CustomFactoryBackend
        assert backend.marker == "record-factory"
        assert backend.ws_id == "custom-factory-ws"
        assert hb.BackendType.factory("custom-factory-backend") is build_custom_factory_backend
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("backend_type", "custom-factory-backend")


def test_backend_locked_decorator_is_available_on_extension_contract():
    """Extension authors should not need to import backend internals for locking."""

    class LockedBackend(hb.ext.Backend):
        @hb.ext.Backend.locked
        def upsert(self, rows):
            self.rows = getattr(self, "rows", {})
            for row in rows:
                bucket = self.rows.setdefault(row.entity_id, {})
                if row.op == "delete":
                    bucket.pop(row.object_id, None)
                    continue
                bucket[row.object_id] = {**row.values, "object_id": row.object_id}

        @hb.ext.Backend.locked
        def scan(self, entity_id):
            return [dict(row) for row in getattr(self, "rows", {}).get(entity_id, {}).values()]

    backend = LockedBackend("locked-contract")
    backend.upsert([hb.ext.RowOp("locked-doc", "a", {"name": "Alpha"})])

    assert backend.scan("locked-doc") == [{"name": "Alpha", "object_id": "a"}]

    backend.delete("locked-doc", ["a"])

    assert backend.scan("locked-doc") == []


def test_backend_register_does_not_inherit_parent_identifier_or_aliases():
    """Class-owned registration should use metadata declared directly on the backend subclass."""

    ShadowInMemBackend.register()
    try:
        assert hb.BackendType.identifier_of(ShadowInMemBackend) == "shadow-in-mem-backend"
        assert hb.BackendType.load("shadow-in-mem-backend") is ShadowInMemBackend
        assert "inmem" not in hb.ext.backend_aliases("shadow-in-mem-backend")
        assert "memory" not in hb.ext.backend_aliases("shadow-in-mem-backend")
        built = hb.BackendType.build("shadow", {"type": "shadow-in-mem-backend"}, ws_id="shadow-ws")
        assert isinstance(built, ShadowInMemBackend)
        assert built.type == "shadow-in-mem-backend"
        assert built.ws_id == "shadow-ws"
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("backend_type", "shadow-in-mem-backend")


def test_backend_tags_drive_routing_and_handlers(tmp_path):
    """Canonical tags should nominate routing candidates without family selectors."""
    modules = hb.DEFAULT_CONTEXT.modules()
    assert modules.tag("backend_type", "sqlite", "sql").value is True
    assert modules.tag("backend_type", "pg", "sql").value is True
    assert "pg" in hb.ext.backend_aliases("postgres")
    assert modules.tag("logical_type", "long-text", "filter").value is True
    assert "interval" in hb.ext.logical_type_aliases("duration")
    assert modules.tag("logical_type", "array", "side").value is True
    assert modules.tag("strategy", "inline", "portable").value is True
    assert hb.StorageStrategy.identifier_of(LocalStrategy) == "local-strategy"
    assert hb.StorageStrategy.identifier_of(LocalStrategy()) == "local-strategy"
    hb.ext.register_strategy(LocalStrategy)
    assert hb.StorageStrategy.load("local-strategy") is LocalStrategy
    assert hb.StorageStrategy.load("LocalStrategy") is LocalStrategy
    assert hb.StorageStrategy.identifier_of(hb.VectorIndex()) == hb.VectorIndex.identifier
    assert hb.StorageStrategy.load("VectorIndex") is hb.VectorIndex
    assert hb.StorageStrategy.load("vector") is hb.VectorIndex
    assert hb.StorageStrategy.load(hb.VectorIndex()) is hb.VectorIndex
    assert "VectorIndex" in hb.ext.strategy_aliases("vector")
    assert hb.ext.normalize_op("$array_contains") == "array_contains"
    assert "text" in modules.tag("operation", "contains", "filter-domains").value
    assert "array" in modules.tag("operation", "$contained_in", "filter-domains").value
    assert "$contained_in" in hb.ext.op_aliases("contained_in")
    assert hb.ext.registered_storage_profiles()[hb.LongText.identifier].default_strategy == hb.InlineColumn.identifier

    ws = hb.HeavenBase("family-route-test", backends={"warehouse": hb.backends.SQLiteBackend("warehouse", _db(tmp_path, "warehouse.db"))})

    class FamilyRouteThing(hb.Entity):
        identifier = "family-route-thing"

        tags = hb.field(hb.Array[hb.ShortText])

    ws.register(FamilyRouteThing)
    binding = ws.storage_plans[FamilyRouteThing.schema().entity_id].first("tags")

    assert binding.backend == "warehouse"
    assert binding.strategy == hb.SparseGramIndex.identifier

    class FamilySearchThing(hb.Entity):
        identifier = "family-search-thing"

        body = hb.field(hb.LongText)

    search = hb.backends.ElasticsearchBackend("search", hosts=["http://127.0.0.1:1"])
    search.client = None
    search_ws = hb.HeavenBase(
        "family-search-route-test",
        backends={"main": hb.backends.InMemBackend("main"), "search": search},
    )
    search_ws.register(FamilySearchThing)
    search_binding = search_ws.storage_plans[FamilySearchThing.schema().entity_id].first("body")

    assert search_binding.backend == "search"
    assert search_binding.strategy == hb.InvertedIndex.identifier

    with pytest.raises(ValueError, match="backend instance name"):
        hb.backends.InMemBackend("Bad Backend")
    with pytest.raises(ValueError, match="strategy identifier"):
        hb.StorageStrategy.load("Bad Strategy")


def test_database_local_path_tag_drives_backend_config():
    """Backend config should ask physical dialect metadata for database lifecycle behavior."""
    from heavenbase.backends.sql._utils import database_uses_local_path

    assert database_uses_local_path("sqlite") is True
    assert database_uses_local_path("duckdb") is True
    assert database_uses_local_path("postgresql") is False
    assert hb.ext.database_dialects.get("sqlite").supports("database-drop") is True
    assert hb.ext.database_dialects.get("postgresql").supports("database-drop") is True
    assert hb.ext.database_dialects.get("trino").supports("database-drop") is False

    hb.ext.database_dialects.register(
        hb.ext.DatabaseDialectSpec(
            "local-path-test",
            tags={"database-local-path": True, "database-create": True, "database-drop": True},
        ),
        replace=True,
    )
    assert database_uses_local_path("local-path-test") is True
    assert hb.ext.database_dialects.get("local-path-test").supports("database-create") is True
    assert hb.ext.database_dialects.get("local-path-test").supports("database-drop") is True


def test_sql_backend_drop_uses_database_lifecycle_tag():
    """SQLBackend.drop should ask database dialect metadata before dropping a namespace."""
    from heavenbase.backends.sql.base import SQLBackend
    from heavenbase.database import DBSpec

    hb.ext.database_dialects.register(hb.ext.DatabaseDialectSpec("drop-tag-test", tags={"database-drop": True}), replace=True)
    calls = []

    class FakeDB:
        def __init__(self, dialect: str):
            self.spec = DBSpec(
                "db",
                {"provider": dialect, "dialect": dialect, "database": f"{dialect}_db"},
                bindings={
                    "contract": "db-route/v1",
                    "provider_dialect": dialect,
                    "dialect": hb.ext.database_dialects.get(dialect).to_dict(),
                    "superuser": {},
                },
            )

        def drop(self):
            calls.append(("drop_tables", self.spec.dialect))

        def drop_database(self):
            calls.append(("drop_database", self.spec.dialect))

        def dispose(self):
            calls.append(("dispose", self.spec.dialect))

    backend = object.__new__(SQLBackend)
    backend._lock = threading.RLock()
    backend.name = "sql"
    backend.tables = {}
    backend._side_specs = {}
    backend._strategy_adapters = {}

    backend.db = FakeDB("drop-tag-test")
    SQLBackend.drop(backend)

    backend.db = FakeDB("postgres")
    SQLBackend.drop(backend)

    backend.db = FakeDB("trino")
    SQLBackend.drop(backend)

    assert ("drop_database", "drop-tag-test") in calls
    assert ("drop_tables", "drop-tag-test") not in calls
    assert ("drop_database", "postgres") in calls
    assert ("drop_tables", "postgres") not in calls
    assert ("drop_tables", "trino") in calls


@pytest.mark.parametrize("pool_class", [None, "static", "singleton", "assertion"])
def test_sql_backend_drop_falls_back_to_dropping_tables_when_the_database_drop_fails(tmp_path, monkeypatch, pool_class):
    """A failed database drop falls back to the reflected tables, also for an engine private to its facade."""
    import sqlite3
    from contextlib import closing

    import heavenbase.database as db_api

    path = tmp_path / "drop-fallback.db"
    config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(path)}
    if pool_class is not None:
        config["pool"] = {"pool_class": pool_class}
    ws = hb.HeavenBase("drop-fallback-ws", detached=True, backends={"main": config})

    class DropFallbackDoc(hb.Entity):
        identifier = "drop-fallback-doc"

        name = hb.field(hb.ShortText)

    ws.register(DropFallbackDoc)
    ws.upsert(DropFallbackDoc, {"object_id": "d1", "name": "kept until drop"})
    backend = ws.backends.get("main")
    assert backend.db.spec.serialized is (pool_class is not None)

    def refuse(spec):
        raise PermissionError(f"no privilege to drop {spec.database}")

    monkeypatch.setattr(db_api, "drop_database", refuse)
    backend.drop()

    assert backend.db is None
    if exists_path(str(path)):
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []


def test_storage_strategy_arguments_accept_classes_and_identifiers(tmp_path):
    """Storage placement inputs should accept strategy classes, instances, aliases, and identifiers."""
    class_rule = hb.ext.StorageRule(backend="vec", strategy=hb.VectorIndex)
    instance_rule = hb.ext.StorageRule(backend="vec", strategy=hb.VectorIndex())
    alias_rule = hb.ext.StorageRule(backend="vec", strategy="VectorIndex")
    profile = hb.ext.StorageProfile(
        hb.ShortText,
        ({"tag": "storage-roles", "contains": "row"},),
        hb.InlineColumn,
        "test-profile",
        ({"when": {"tag": "sql", "equals": True}, "strategy": hb.SideTable},),
    )

    assert [class_rule.strategy, instance_rule.strategy] == [hb.VectorIndex.identifier] * 2
    assert alias_rule.strategy == "VectorIndex"
    assert profile.default_strategy == hb.InlineColumn.identifier
    assert profile.strategy_for({"sql": True}) == hb.SideTable.identifier
    assert profile.strategy_for({"sql": False}) == hb.InlineColumn.identifier
    with pytest.raises(TypeError, match="strategy identifier"):
        hb.ext.StorageRule(backend="vec", strategy=object())

    ws = hb.HeavenBase("strategy-class-route-test", backends={"main": hb.backends.InMemBackend("main"), "vec": hb.backends.InMemBackend("vec")})

    class StrategyClassThing(hb.Entity):
        identifier = "strategy-class-thing"

        title = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex)

    ws.register(StrategyClassThing)
    binding = ws.storage_plans[StrategyClassThing.schema().entity_id].first("emb")

    assert StrategyClassThing._hb_specs["emb"].stores[0].strategy == hb.VectorIndex.identifier
    assert binding.backend == "vec"
    assert binding.strategy == hb.VectorIndex.identifier


def test_public_catalog_discovers_selectable_components(tmp_path):
    """Users should discover types, strategies, backends, ops, and support rows without reading registries."""
    type_identifiers = {option.identifier for option in hb.capabilities.logical_types()}
    assert {"short-text", "array", "vector"} <= type_identifiers
    duration = next(option for option in hb.capabilities.logical_types() if option.identifier == "duration")
    assert "interval" in duration.aliases
    assert "interval" in duration.to_dict()["aliases"]
    assert hb.ext.get_logical_type("vector") is hb.Vector

    vector_strategies = hb.capabilities.strategies(hb.Vector)
    assert [option.identifier for option in vector_strategies] == [hb.VectorIndex.identifier]
    assert vector_strategies[0].strategy is hb.VectorIndex
    assert "VectorIndex" in vector_strategies[0].to_dict()["aliases"]

    assert hb.capabilities.supports(hb.Array, "contained_in", "sqlite", hb.SparseGramIndex)
    array_side_backends = hb.capabilities.backends(hb.Array, hb.SideTable, op="$array_contains")
    assert "sqlite" in {option.type for option in array_side_backends}
    assert "inmem" not in {option.type for option in array_side_backends}
    inmem = next(option for option in hb.capabilities.backends() if option.type == "inmem")
    assert "memory" in inmem.to_dict()["aliases"]
    assert hb.capabilities.supports(hb.Array, "array_contains", "sqlite", hb.SideTable)
    assert not hb.capabilities.supports(hb.Array, "array_contains", "inmem", hb.SideTable)

    sqlite_array_ops = hb.capabilities.ops(hb.Array, hb.SideTable, backend="sqlite")
    assert "array_contains" in sqlite_array_ops
    vector_capability = hb.capabilities.capabilities(hb.Vector, hb.VectorIndex, backend="inmem", op="near")[0]
    assert vector_capability.strategy == hb.VectorIndex.identifier
    assert vector_capability.backend_identifier == "inmem"
    assert vector_capability.to_dict()["backend_identifier"] == "inmem"
    assert "backend" not in vector_capability.to_dict()
    contained_capability = hb.capabilities.capabilities(hb.Array, hb.SparseGramIndex, backend="sqlite", op="contained_in")[0].to_dict()
    assert "$contained_in" in contained_capability["aliases"]["op"]
    assert "array" in hb.DEFAULT_CONTEXT.modules().tag("operation", "contained_in", "filter-domains").value
    assert "SparseGramIndex" in contained_capability["aliases"]["strategy"]
    assert hb.capabilities.supports(hb.Vector, "$near", hb.backends.InMemBackend, hb.VectorIndex)
    assert not hb.capabilities.supports(hb.Array, "contained_in", "gram", hb.SparseGramIndex)
    sparse_gram_backend_types = {option.type for option in hb.capabilities.backends(hb.Array, hb.SparseGramIndex, op="contained_in")}
    assert "sqlite" in sparse_gram_backend_types
    assert "gram" not in sparse_gram_backend_types

    ws = hb.HeavenBase(
        "catalog-discovery-test",
        backends={
            "main": hb.backends.InMemBackend("main"),
            "warehouse": hb.backends.SQLiteBackend("warehouse", _db(tmp_path, "warehouse.db")),
            "vec": hb.backends.InMemBackend("vec"),
        },
    )
    workspace_backends = ws.capabilities.backends(hb.Array, hb.SideTable, op="array_contains")
    assert [(option.name, option.type) for option in workspace_backends] == [("warehouse", "sqlite")]

    vector_backend_names = {option.name for option in ws.capabilities.backends(hb.Vector, hb.VectorIndex, op="near")}
    assert {"main", "vec"} <= vector_backend_names
    assert ws.capabilities.supports(hb.Vector, "$near", "vec", "VectorIndex")


def test_metaschema_publishes_registry_vocabulary_rows(tmp_path):
    """Agents should inspect registered Lego pieces through system rows."""
    ws = hb.HeavenBase(
        "registry-vocabulary-test",
        detached=True,
        backends={
            "main": hb.backends.InMemBackend("main"),
            "warehouse": hb.backends.SQLiteBackend("warehouse", _db(tmp_path, "warehouse.db")),
        },
    )

    type_rows = {row["subject_id"]: row for row in ws.query(hb.MetaSchema).where({"kind": "logical_type"}).execute().rows()}
    strategy_rows = {row["subject_id"]: row for row in ws.query(hb.MetaSchema).where({"kind": "storage_strategy"}).execute().rows()}
    operation_rows = {row["subject_id"]: row for row in ws.query(hb.MetaSchema).where({"kind": "operation"}).execute().rows()}
    backend_type_rows = {row["subject_id"]: row for row in ws.query(hb.MetaSchema).where({"kind": "backend_type"}).execute().rows()}
    tag_rows = {row["subject_id"]: row for row in ws.query(hb.MetaSchema).where({"kind": "tag"}).execute().rows()}
    capability_rows = {row["subject_id"]: row for row in ws.query(hb.MetaSchema).where({"kind": "capability"}).execute().rows()}
    aggregate_compiler_rows = {row["subject_id"]: row for row in ws.query(hb.MetaSchema).where({"kind": "aggregate_compiler"}).execute().rows()}

    assert {"short-text", "array", "vector"} <= set(type_rows)
    assert type_rows["short-text"]["dtype"] == "short-text"
    assert "tags" not in type_rows["short-text"]["meta"]
    assert "families" not in type_rows["short-text"]["meta"]
    assert type_rows["short-text"]["source"] == "python-import"
    assert hb.ShortText.__name__ in type_rows["short-text"]["meta"]["class"]
    assert "interval" in type_rows["duration"]["meta"]["aliases"]

    assert hb.VectorIndex.identifier in strategy_rows
    assert strategy_rows[hb.VectorIndex.identifier]["strategy"] == hb.VectorIndex.identifier
    assert "tags" not in strategy_rows[hb.VectorIndex.identifier]["meta"]
    assert "families" not in strategy_rows[hb.VectorIndex.identifier]["meta"]
    assert "VectorIndex" in strategy_rows[hb.VectorIndex.identifier]["meta"]["aliases"]

    assert "contained_in" in operation_rows
    assert "tags" not in operation_rows["contained_in"]["meta"]
    assert "families" not in operation_rows["contained_in"]["meta"]
    assert "$contained_in" in operation_rows["contained_in"]["meta"]["aliases"]
    assert "near" in operation_rows

    assert "inmem" in backend_type_rows
    assert backend_type_rows["inmem"]["type"] == "inmem"
    assert "tags" not in backend_type_rows["inmem"]["meta"]
    assert "families" not in backend_type_rows["inmem"]["meta"]
    assert "memory" in backend_type_rows["inmem"]["meta"]["aliases"]

    assert tag_rows["binary-storage"]["meta"]["default"] == "bytes"
    assert tag_rows["binary-storage"]["source"] == "inline"
    assert tag_rows["scalar-index"]["meta"]["default"]["value"] is False

    assert {row["type"] for row in capability_rows.values()} == {"inmem", "sqlite"}
    assert set(aggregate_compiler_rows) == {"sqlite"}

    contained_key = f"sqlite:array:contained_in:{hb.SparseGramIndex.identifier}"
    assert contained_key in capability_rows
    contained_capability = capability_rows[contained_key]["meta"]
    assert contained_capability["handler_key"] == ["array", "contained_in", "sqlite", hb.SparseGramIndex.identifier]
    assert "$contained_in" in contained_capability["aliases"]["op"]
    assert "families" not in contained_capability
    assert "SparseGramIndex" in contained_capability["aliases"]["strategy"]


def test_default_backend_config_uses_bound_config_manager():
    """Omitted backend config should load only the bound ConfigManager defaults."""
    backends = hb.ext.BackendRegistry.from_config().backends

    assert backends["main"].type == "sqlite"
    assert backends["vec"].type == "inmem"
    assert "gram" not in backends


def test_empty_explicit_backend_config_is_rejected():
    """An explicit empty mapping must not activate a hidden backend list."""
    with pytest.raises(ValueError, match="does not define any enabled backends"):
        hb.ext.BackendRegistry.from_config({})


def test_modular_import_paths_and_builtin_registry_access(tmp_path):
    """Public package imports should expose modular backend and handler APIs."""
    from heavenbase.backends import InMemBackend, SQLiteBackend
    from heavenbase.ext import BackendRegistry
    from heavenbase.backends.elasticsearch import ElasticsearchBackend
    from heavenbase.backends.file import JsonFileBackend, PickleFileBackend
    from heavenbase.backends.inmem import InMemBackend as InMemBackendFromPackage
    from heavenbase.backends.sql import SQLBackend
    from heavenbase.backends.sql.starrocks import StarRocksBackend
    from heavenbase.backends.sql.trino import TrinoBackend
    from heavenbase.backends.vector.pgvector import PgVectorBackend
    from heavenbase.backends.vector.pinecone import PineconeBackend as PineconeBackendFromPackage
    from heavenbase.execution.handlers import HandlerRegistry
    from heavenbase.execution.handlers.seed import seed_handlers
    from heavenbase.execution.handlers.filters import filter_handler
    from heavenbase.strategies import InlineColumn, StorageStrategy, VectorIndex

    assert hb.backends.SQLiteBackend is SQLiteBackend
    assert hb.ext.BackendRegistry is BackendRegistry
    assert hb.backends.ElasticsearchBackend is ElasticsearchBackend
    assert hb.ext.HandlerRegistry is HandlerRegistry
    assert hb.backends.InMemBackend is InMemBackendFromPackage
    assert hb.backends.JsonFileBackend is JsonFileBackend
    assert hb.backends.PickleFileBackend is PickleFileBackend
    assert hb.backends.PgVectorBackend is PgVectorBackend
    assert hb.backends.PineconeBackend is PineconeBackendFromPackage
    assert hb.backends.StarRocksBackend is StarRocksBackend
    assert hb.backends.TrinoBackend is TrinoBackend
    assert callable(hb.BackendType.build)
    assert hb.InlineColumn is InlineColumn
    assert issubclass(VectorIndex, StorageStrategy)
    backend_identifiers = hb.BackendType.identifiers()
    assert "sqlite" in backend_identifiers
    assert "starrocks" in backend_identifiers
    assert "trino" in backend_identifiers
    assert "pinecone" in backend_identifiers
    assert callable(filter_handler("eq"))
    assert seed_handlers().get(hb.ShortText, "eq", "inmem", InlineColumn.identifier)

    ws = hb.HeavenBase(
        "modular-access-test",
        backends={
            "sql": {"type": "sqlite", "database": _db(tmp_path, "main.db")},
            "vec": {"type": "inmem"},
        },
    )
    assert isinstance(ws.backends, BackendRegistry)
    assert isinstance(ws.backends.get("sql"), SQLBackend)

    backends = ws.query(hb.MetaSchema).where(hb.MetaSchema.kind == "backend").execute()
    capabilities = ws.query(hb.MetaSchema).where(hb.MetaSchema.kind == "capability").execute()

    assert {row["backend"] for row in backends.rows()} >= {"sql", "vec"}
    capability_rows = capabilities.rows()
    assert any(row["name"] == "near" and row["strategy"] == VectorIndex.identifier for row in capability_rows)
    assert all(row["backend"] == "" and row["type"] for row in capability_rows)


def test_metaschema_updates_when_registering_entity():
    ws = hb.HeavenBase._construct(
        "metaschema-test",
        context=hb.DEFAULT_CONTEXT,
        detached=True,
        tracked=False,
        register_workspace=False,
        register_pending=False,
        track_live=False,
        publish=False,
    )
    before = ws.query(hb.MetaSchema).where(hb.MetaSchema.subject_id == "meta-note").count().execute().scalar()

    class MetaNote(hb.Entity):
        identifier = "meta-note"

        title = hb.field(hb.ShortText, desc="Note title")
        tags = hb.field(hb.Json, desc="Searchable note tags")

    assert not hasattr(hb, "Schema")
    assert before == 0

    ws.register(MetaNote)
    rows = ws.query(hb.MetaSchema).where(hb.MetaSchema.subject_id == "meta-note").select("kind", "field", "dtype").execute().rows()

    assert any(row["kind"] == "entity" for row in rows)
    assert {row["field"] for row in rows if row["kind"] == "field"} >= {"object_id", "title", "tags"}


@pytest.mark.detail
def test_starrocks_type_mapping_and_quoting():
    """StarRocks helper functions should map types and quote identifiers correctly."""
    from heavenbase.backends.sql.starrocks import _q, _sr_type, _sr_varchar_len

    class OversizedIdentifier:
        identifier = "identifier"
        max_chars = 70000

    config = hb.DEFAULT_CONTEXT.config
    resolver = hb.DEFAULT_CONTEXT.modules()

    def sr_type(dtype):
        return _sr_type(dtype, config=config, resolver=resolver)

    assert _q("my-table") == "`my-table`"
    assert _q("a`b") == "`a``b`"
    assert sr_type(hb.Identifier()) == "VARCHAR(63)"
    assert sr_type(OversizedIdentifier()) == "VARCHAR(65533)"
    assert sr_type(hb.ShortText()) == "VARCHAR(255)"
    assert sr_type(hb.MediumText()) == "VARCHAR(4095)"
    assert sr_type(hb.LongText()) == "VARCHAR(65533)"
    assert _sr_varchar_len(70000, config) == 65533
    assert sr_type(hb.Integer()) == "BIGINT"
    assert sr_type(hb.Duration()) == "BIGINT"
    assert sr_type(hb.Float()) == "DOUBLE"
    assert sr_type(hb.Boolean()) == "BOOLEAN"


def test_duration_order_filter_sqlite_native():
    """Duration fields should use integer storage and honor numeric order filters on SQLite."""
    ws = hb.HeavenBase("duration-order-filter", backends={"main": {"type": "sqlite", "database": ":memory:"}})

    class TTLThing(hb.Entity):
        identifier = "ttl-thing"
        ttl_ms = hb.field(hb.Duration).default(0)

    ws.register(TTLThing)
    ws.upsert(TTLThing, {"object_id": "short", "ttl_ms": 50})
    ws.upsert(TTLThing, {"object_id": "long", "ttl_ms": 500})

    assert ws.query(TTLThing).where(TTLThing.ttl_ms >= 100).execute().ids == ["long"]
    assert ws.query(TTLThing).where(TTLThing.ttl_ms < 100).execute().ids == ["short"]


def test_duration_large_value_sqlite_roundtrip():
    """Duration fields should persist millisecond values beyond signed 32-bit range."""
    large_ttl = 2_592_000_000
    assert large_ttl > 2_147_483_647
    ws = hb.HeavenBase("duration-large-roundtrip", backends={"main": {"type": "sqlite", "database": ":memory:"}})

    class LargeTTLThing(hb.Entity):
        identifier = "large-ttl-thing"
        ttl_ms = hb.field(hb.Duration).default(0)

    ws.register(LargeTTLThing)
    ws.upsert(LargeTTLThing, {"object_id": "big", "ttl_ms": large_ttl})
    assert ws.get("big", LargeTTLThing)["ttl_ms"] == large_ttl


@pytest.mark.fast
def test_starrocks_object_id_length_takes_the_entity_domain_and_the_optional_config_cap():
    """A new StarRocks table's object_id length follows the entity's domain; the config key only lowers it."""
    from heavenbase.schema import EntitySchema, FieldSchema

    class Config:
        def __init__(self, values):
            self.values = values

        def get(self, path=None, default=None):
            return self.values.get(path, default)

    def schema(max_chars):
        return EntitySchema(
            "starrocks-long-id",
            "StarRocksLongId",
            fields={"object_id": FieldSchema("object_id", hb.Identifier(max_chars=max_chars), pk=True, required=True)},
        )

    backend = hb.backends.StarRocksBackend("starrocks")
    assert backend._object_id_length(schema(200)) == 200
    assert backend._object_id_length(schema(None)) == hb.Identifier().max_chars
    backend._config = Config({"heavenbase.backend_defaults.starrocks.object_id.max_length": 120})
    assert backend._object_id_length(schema(200)) == 120
    backend._config = Config({"heavenbase.backend_defaults.starrocks.varchar.max_length": 150})
    assert backend._object_id_length(schema(200)) == 150
