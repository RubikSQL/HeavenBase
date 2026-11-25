"""Tests for backend-native execution paths.

Objective: verify that SQLBackend and LanceBackend execute fragments natively
rather than falling back to full table scans.
"""

import json
from importlib.util import find_spec

import pytest

import heavenbase as hb

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry

_LANCE_UNAVAILABLE = find_spec("lancedb") is None


def _db(path, name: str) -> str:
    return hb.utils.path_to_file_uri(path / name)


@pytest.fixture(autouse=True)
def _isolate_workspaces():
    before = set(WORKSPACES.keys())
    yield
    for wid in set(WORKSPACES.keys()) - before:
        WORKSPACES.pop(wid, None)


@pytest.fixture
def tmp_sql_ws(tmp_path):
    ws = hb.HeavenBase("native-sql-test", backends={"sql": hb.backends.SQLiteBackend("sql", _db(tmp_path, "native_sql.db"))})
    ws.register(_Product)
    yield ws
    WORKSPACES.pop("native-sql-test", None)


class _Product(hb.Entity):
    identifier = "native-product"

    name = hb.field(hb.ShortText)
    price = hb.field(hb.Float)
    stock = hb.field(hb.Integer)


class _SpySQLiteBackend(hb.backends.SQLiteBackend):
    def __init__(self, name, database):
        super().__init__(name, database)
        self.ops = []

    def execute(self, fragment):
        self.ops.append(fragment.op)
        return super().execute(fragment)


class _ColumnBackend(hb.backends.InMemBackend):
    def __init__(self, name, cols):
        super().__init__(name)
        self.cols = set(cols)
        self.ops = []

    def upsert(self, rows):
        ns = self.data.setdefault(self.ws_id, {})
        for row in rows:
            bucket = ns.setdefault(row.entity_id, {})
            if row.op == "delete":
                bucket.pop(row.object_id, None)
            else:
                item = {k: v for k, v in row.values.items() if k in self.cols}
                item["object_id"] = row.object_id
                bucket[row.object_id] = item

    def execute(self, fragment):
        self.ops.append(fragment.op)
        return super().execute(fragment)


class _NativeOnlyLanceBackend(hb.backends.LanceBackend):
    def scan(self, entity_id):
        raise AssertionError("Lance scalar filters should execute through native where")


class _NativeOnlyChromaBackend(hb.backends.ChromaBackend):
    def scan(self, entity_id):
        raise AssertionError("Chroma id lookups should use native collection get/count")


class _NativeOnlyMilvusBackend(hb.backends.MilvusBackend):
    def scan(self, entity_id):
        raise AssertionError("Milvus id lookups should use native get/stats")


class _NativeOnlyPineconeBackend(hb.backends.PineconeBackend):
    def scan(self, entity_id):
        raise AssertionError("Pinecone id lookups should use native fetch/stats")


class _NativeOnlyElasticsearchBackend(hb.backends.ElasticsearchBackend):
    def scan(self, entity_id):
        raise AssertionError("Elasticsearch id lookups should use native mget/count")


def test_sql_scalar_filters_preserve_results(tmp_sql_ws):
    """SQL safe routes should preserve equality, range, set, and text results."""
    ws = tmp_sql_ws
    ws.upsert_many(
        _Product,
        [
            {"object_id": "p1", "name": "Bluetooth", "price": 10.0, "stock": 1},
            {"object_id": "p2", "name": "Wired", "price": 20.0, "stock": 2},
            {"object_id": "p3", "name": "Monitor", "price": 30.0, "stock": 3},
        ],
    )

    assert ws.query(_Product).where(_Product.name == "Bluetooth").execute().ids == ["p1"]
    assert ws.query(_Product).where(_Product.name != "Bluetooth").execute().ids == ["p2", "p3"]
    assert ws.query(_Product).where(_Product.name.in_(["Bluetooth", "Monitor"])).execute().ids == ["p1", "p3"]
    assert ws.query(_Product).where(_Product.price > 15).count().execute().scalar() == 2
    assert ws.query(_Product).where(_Product.price >= 20).count().execute().scalar() == 2
    assert ws.query(_Product).where(_Product.price < 25).count().execute().scalar() == 2
    assert ws.query(_Product).where(_Product.price <= 20).count().execute().scalar() == 2
    assert ws.query(_Product).where(_Product.name.match("blue")).execute().ids == ["p1"]
    assert ws.query(_Product).where(_Product.name.ilike("%blue%")).execute().ids == ["p1"]


@pytest.mark.detail
def test_nontrivial_provider_backends_do_not_inherit_inmem():
    """Provider/search/vector adapters should not use InMemBackend as persistence."""
    provider_classes = [
        hb.backends.ChromaBackend,
        hb.backends.MilvusBackend,
        hb.backends.PineconeBackend,
        hb.backends.ElasticsearchBackend,
        hb.backends.LanceBackend,
        hb.backends.PgVectorBackend,
    ]

    for cls in provider_classes:
        assert not issubclass(cls, hb.backends.InMemBackend), cls.__name__


@pytest.mark.detail
def test_unavailable_provider_backends_fail_without_scan_or_mirror():
    """Unavailable providers should fail clearly instead of returning stale in-process rows."""
    providers = [
        hb.backends.ChromaBackend("chroma"),
        hb.backends.MilvusBackend("milvus"),
        hb.backends.PineconeBackend("pinecone"),
        hb.backends.ElasticsearchBackend("search"),
    ]

    for backend in providers:
        backend.client = None
        backend.client_error = "offline"
        assert backend.tag("row-lookup").value == {"get_many": "native", "exists_many": "native", "count": "native"}
        availability = backend.tag("backend-availability")
        assert availability.value == "unavailable"
        assert availability.source == "live"

        with pytest.raises(RuntimeError, match="unavailable.*offline"):
            backend.upsert([hb.ext.RowOp("provider-unavailable-doc", "u1", {"title": "stored"})])
        with pytest.raises(RuntimeError, match="unavailable.*offline"):
            backend.delete("provider-unavailable-doc", ["u1"])
        with pytest.raises(RuntimeError, match="unavailable.*offline"):
            backend.scan("provider-unavailable-doc")
        with pytest.raises(RuntimeError, match="unavailable.*offline"):
            backend.get_many("provider-unavailable-doc", ["u1"])
        with pytest.raises(RuntimeError, match="unavailable.*offline"):
            backend.exists_many("provider-unavailable-doc", ["u1"])
        with pytest.raises(RuntimeError, match="unavailable.*offline"):
            backend.count("provider-unavailable-doc")
        with pytest.raises(RuntimeError, match="unavailable.*offline"):
            backend.execute(
                hb.ext.QueryFragment(backend.name, "near", "emb", {"vector": [1.0, 0.0], "top_k": 1}, notes={"object_id": "provider-unavailable-doc"})
            )


@pytest.mark.detail
def test_external_provider_only_route_rejects_when_provider_unavailable():
    """A provider-only route should not be silently persisted to the workspace inmem backend."""
    ws_id = "provider-unavailable-route-test"
    backend = hb.backends.ChromaBackend("chroma", ws_id=ws_id)
    backend.client = None
    backend.client_error = "offline"
    ws = hb.HeavenBase(ws_id, detached=True, backends={"main": hb.backends.InMemBackend("main"), "chroma": backend})

    class ProviderUnavailableDoc(hb.Entity):
        identifier = "provider-unavailable-doc"

        title = hb.field(hb.ShortText).store(to="chroma")
        emb = hb.field(hb.Vector[2]).store(to="chroma", strategy=hb.VectorIndex.identifier)

    ws.register(ProviderUnavailableDoc)
    with pytest.raises(RuntimeError, match="Chroma backend 'chroma' is unavailable.*offline"):
        ws.upsert(ProviderUnavailableDoc, {"object_id": "u1", "title": "No mirror", "emb": [1.0, 0.0]})

    WORKSPACES.pop(ws_id, None)


@pytest.mark.detail
def test_unavailable_provider_backend_metadata_is_honest():
    """Backend rows stay structural while effective availability is inspected directly."""
    ws_id = "provider-unavailable-metadata-test"
    backend = hb.backends.ChromaBackend("chroma", ws_id=ws_id)
    backend.client = None
    backend.client_error = "offline"
    ws = hb.HeavenBase(ws_id, detached=True, backends={"main": hb.backends.InMemBackend("main"), "chroma": backend})

    rows = ws.query(hb.MetaSchema).where(hb.MetaSchema.kind == "backend").where(hb.MetaSchema.backend == "chroma").execute().rows()
    assert len(rows) == 1
    meta = rows[0]["meta"]
    assert meta["connected"] is False
    assert "capabilities" not in meta
    assert "tags" not in meta
    assert backend.tag("backend-availability").value == "unavailable"
    assert backend.tag("row-lookup").value == {"get_many": "native", "exists_many": "native", "count": "native"}

    WORKSPACES.pop(ws_id, None)


def test_sql_inline_text_array_exact_filters_preserve_scan_semantics(tmp_sql_ws):
    """SQL main-table text arrays should scan until nested text parity is proved."""
    ws = tmp_sql_ws

    class InlineArrayDoc(hb.Entity):
        identifier = "native-sql-inline-array-exact-doc"

        tags = hb.field(hb.Array[hb.ShortText]).store(to="sql", strategy=hb.InlineColumn)

    ws.register(InlineArrayDoc)
    ws.upsert_many(
        InlineArrayDoc,
        [
            {"object_id": "a", "tags": ["red", "blue"]},
            {"object_id": "b", "tags": ["blue", "red"]},
            {"object_id": "c", "tags": []},
            {"object_id": "d", "tags": ["red"]},
        ],
    )

    eq_query = ws.query(InlineArrayDoc).where(InlineArrayDoc.tags == ["red", "blue"])
    assert eq_query.execute().ids == ["a"]
    eq_step = eq_query.explain()["steps"][0]
    assert eq_step["handler_mode"] == "py_scan"
    assert eq_step["fallback_reason"] == "text_semantics_not_proven"

    assert ws.query(InlineArrayDoc).where(InlineArrayDoc.tags != ["red", "blue"]).execute().ids == ["b", "c", "d"]
    assert ws.query(InlineArrayDoc).where(InlineArrayDoc.tags.in_([["blue", "red"], ["missing"]])).execute().ids == ["b"]

    tuple_query = ws.query(InlineArrayDoc).where(InlineArrayDoc.tags == ("red", "blue"))
    assert tuple_query.execute().ids == ["a"]
    assert tuple_query.explain()["steps"][0]["handler_mode"] == "py_scan"


def test_sql_side_table_array_exact_filters_preserve_scan_semantics(tmp_sql_ws):
    """SQL SideTable arrays should scan until owner-id equality is proved."""
    ws = tmp_sql_ws

    class ArrayDoc(hb.Entity):
        identifier = "native-sql-array-exact-doc"

        tags = hb.field(hb.Array[hb.ShortText]).store(to="sql", strategy=hb.SideTable)

    ws.register(ArrayDoc)
    ws.upsert_many(
        ArrayDoc,
        [
            {"object_id": "a", "tags": ["red", "blue"]},
            {"object_id": "b", "tags": ["blue", "red"]},
            {"object_id": "c", "tags": []},
            {"object_id": "d", "tags": ["red"]},
        ],
    )

    eq_query = ws.query(ArrayDoc).where(ArrayDoc.tags == ["red", "blue"])
    assert eq_query.execute().ids == ["a"]
    eq_step = eq_query.explain()["steps"][0]
    assert eq_step["handler_mode"] == "py_scan"
    assert eq_step["fallback_reason"] == "text_semantics_not_proven"

    assert ws.query(ArrayDoc).where(ArrayDoc.tags != ["red", "blue"]).execute().ids == ["b", "c", "d"]
    assert ws.query(ArrayDoc).where(ArrayDoc.tags.in_([["blue", "red"], ["missing"]])).execute().ids == ["b"]
    assert ws.query(ArrayDoc).where(ArrayDoc.tags.in_([("red", "blue")])).execute().ids == ["a"]


def test_sql_explain_shows_native_handler(tmp_sql_ws):
    """SQL explain output should mark native handler execution."""
    ws = tmp_sql_ws
    steps = ws.query(_Product).where(_Product.price > 10).explain()["steps"]
    filter_steps = [s for s in steps if s.get("node") == "filter"]
    assert filter_steps
    assert filter_steps[0]["type"] == "sqlite"


def test_sql_groups_same_backend_boolean_tree(tmp_path):
    """Same-backend boolean trees should compile to one grouped SQL execution."""
    backend = _SpySQLiteBackend("sql", _db(tmp_path, "grouped.db"))
    ws = hb.HeavenBase("native-sql-group-test", backends={"sql": backend})
    ws.register(_Product)
    ws.upsert_many(
        _Product,
        [
            {"object_id": "p1", "name": "Desk", "price": 100.0, "stock": 5},
            {"object_id": "p2", "name": "Chair", "price": 50.0, "stock": 4},
            {"object_id": "p3", "name": "Monitor", "price": 200.0, "stock": 0},
        ],
    )

    query = ws.query(_Product).where((_Product.price >= 50) & ((_Product.name == "Desk") | (_Product.name == "Chair")) & ~(_Product.stock <= 0))
    backend.ops.clear()
    frame = query.execute()

    assert frame.ids == ["p1", "p2"]
    assert backend.ops == ["and"]
    assert any(step.get("node") == "backend_group" and step.get("n") == 3 for step in query.explain()["steps"])


def test_multi_backend_and_groups_and_merges_columns():
    """Multi-backend AND queries should group per backend and merge columns."""
    text = _ColumnBackend("text", ["id", "name"])
    nums = _ColumnBackend("nums", ["price", "stock"])
    ws = hb.HeavenBase("native-split-group-test", backends={"text": text, "nums": nums})

    class SplitProduct(hb.Entity):
        identifier = "native-split-product"

        name = hb.field(hb.ShortText).store(to="text")
        price = hb.field(hb.Float).store(to="nums")
        stock = hb.field(hb.Integer).store(to="nums")

    ws.register(SplitProduct)
    ws.upsert_many(
        SplitProduct,
        [
            {"object_id": "p1", "name": "Desk", "price": 120.0, "stock": 5},
            {"object_id": "p2", "name": "Desk", "price": 80.0, "stock": 5},
            {"object_id": "p3", "name": "Monitor", "price": 200.0, "stock": 0},
        ],
    )

    frame = (
        ws.query(SplitProduct).where((SplitProduct.name == "Desk") & (SplitProduct.price > 100) & (SplitProduct.stock > 0)).select("name", "price").execute()
    )

    assert frame.rows() == [{"object_id": "p1", "name": "Desk", "price": 120.0}]
    assert text.ops == ["eq"]
    assert nums.ops == ["and"]


def test_complex_nested_cross_backend_grouping():
    """Nested cross-backend filters should group same-backend clauses correctly."""
    text = _ColumnBackend("text", ["title", "tags"])
    nums = _ColumnBackend("nums", ["price", "stock", "rating"])
    flags = _ColumnBackend("flags", ["active"])
    ws = hb.HeavenBase("native-complex-split-group-test", backends={"text": text, "nums": nums, "flags": flags})

    class ComplexProduct(hb.Entity):
        identifier = "native-complex-product"

        title = hb.field(hb.ShortText).store(to="text")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="text")
        price = hb.field(hb.Float).store(to="nums")
        stock = hb.field(hb.Integer).store(to="nums")
        rating = hb.field(hb.Float).store(to="nums")
        active = hb.field(hb.Boolean).store(to="flags")

    ws.register(ComplexProduct)
    ws.upsert_many(
        ComplexProduct,
        [
            {"object_id": "p1", "title": "Desk", "tags": ["office", "sale"], "price": 120.0, "stock": 5, "rating": 4.5, "active": True},
            {"object_id": "p2", "title": "Lamp", "tags": ["home"], "price": 80.0, "stock": 10, "rating": 3.5, "active": False},
            {"object_id": "p3", "title": "Chair", "tags": ["office"], "price": 140.0, "stock": 3, "rating": 3.0, "active": False},
            {"object_id": "p4", "title": "Desk", "tags": ["office"], "price": 30.0, "stock": 5, "rating": 5.0, "active": True},
        ],
    )

    query = ws.query(ComplexProduct).where(
        ((ComplexProduct.title == "Desk") | (ComplexProduct.title == "Lamp"))
        & ComplexProduct.tags.array_contains("office")
        & (ComplexProduct.price >= 50)
        & (ComplexProduct.stock > 0)
        & ((ComplexProduct.rating < 4) | ComplexProduct.active.op("eq", True))
    )
    frame = query.select("title", "price", "active").execute()
    explain = query.explain()

    assert frame.rows() == [{"object_id": "p1", "title": "Desk", "price": 120.0, "active": True}]
    assert text.ops == ["and"]
    assert sorted(nums.ops) == ["and", "lt"]
    assert flags.ops == ["eq"]
    assert any(step.get("node") == "backend_group" and step.get("backend") == "text" and step.get("n") == 2 for step in explain["steps"])
    assert any(step.get("node") == "backend_group" and step.get("backend") == "nums" and step.get("n") == 2 for step in explain["steps"])


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_native_near_search(tmp_path):
    """LanceDB should execute native near search and persist inserted rows."""
    ws = hb.HeavenBase(
        "native-lance-test",
        backends={"main": hb.backends.InMemBackend("main"), "vec": hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance"))},
    )

    class LanceDoc(hb.Entity):
        identifier = "lance-doc"

        title = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(LanceDoc)
    ws.upsert_many(
        LanceDoc,
        [
            {"object_id": "d1", "title": "Close", "emb": [1.0, 0.0]},
            {"object_id": "d2", "title": "Far", "emb": [0.0, 1.0]},
            {"object_id": "d3", "title": "Closer", "emb": [0.9, 0.1]},
        ],
    )

    frame = ws.query(LanceDoc).near(LanceDoc.emb, [1, 0], top_k=2).select("title", "score").execute()
    assert len(frame) == 2
    assert frame.rows()[0]["title"] == "Close"
    assert "score" in frame.col_names()

    # Verify data is actually persisted in LanceDB by scanning.
    backend = ws.backends.get("vec")
    rows = backend.scan("lance-doc")
    assert len(rows) == 3
    assert {r["object_id"] for r in rows} == {"d1", "d2", "d3"}

    WORKSPACES.pop("native-lance-test", None)


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_native_scalar_filter_group(tmp_path):
    """LanceDB scalar filters should execute through native where clauses."""
    ws = hb.HeavenBase(
        "native-lance-filter-test",
        backends={"main": hb.backends.InMemBackend("main"), "vec": _NativeOnlyLanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-filter"))},
    )

    class LanceScalarDoc(hb.Entity):
        identifier = "lance-scalar-doc"

        title = hb.field(hb.ShortText).store(to="vec")
        score = hb.field(hb.Float).store(to="vec")

    ws.register(LanceScalarDoc)
    ws.upsert_many(
        LanceScalarDoc,
        [
            {"object_id": "d1", "title": "Close", "score": 5.0},
            {"object_id": "d2", "title": "Far", "score": 10.0},
            {"object_id": "d3", "title": "Closer", "score": 7.0},
        ],
    )

    frame = ws.query(LanceScalarDoc).where((LanceScalarDoc.score > 6) & (LanceScalarDoc.title != "Far")).execute()
    assert frame.ids == ["d3"]

    WORKSPACES.pop("native-lance-filter-test", None)


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_rows_read_a_missing_value_as_none_and_an_integer_as_int(tmp_path):
    """LanceDB reads keep a null as None and an integer column as int on every read path."""
    backend = hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-rows"))
    ws = hb.HeavenBase("native-lance-row-types-test", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})

    class LanceRowDoc(hb.Entity):
        identifier = "lance-row-doc"

        title = hb.field(hb.ShortText).store(to="vec").optional()
        count = hb.field(hb.Integer).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(LanceRowDoc)
        ws.upsert_many(LanceRowDoc, [{"object_id": "c1", "title": "One", "count": 3, "emb": [1.0, 0.0]}, {"object_id": "c2", "emb": [0.0, 1.0]}])

        def scalars(rows):
            return [(row["object_id"], row["title"], row["count"], type(row["count"]).__name__) for row in rows]

        expected = [("c1", "One", 3, "int"), ("c2", None, None, "NoneType")]
        assert scalars(ws.get_many(["c1", "c2"], entity=LanceRowDoc)) == expected
        assert scalars(backend.get_many(LanceRowDoc.identifier, ["c1", "c2"])) == expected
        assert scalars(sorted(backend.scan(LanceRowDoc.identifier), key=lambda row: row["object_id"])) == expected
        near = ws.query(LanceRowDoc).near(LanceRowDoc.emb, [1.0, 0.0], top_k=2).select("title", "count").execute().rows()
        assert scalars(near) == expected
        # starts_with has no LanceDB compiler, so the portable predicate reads the scanned rows
        assert ws.query(LanceRowDoc).where(LanceRowDoc.title.op("starts_with", "O")).execute().ids == ["c1"]
        assert ws.query(LanceRowDoc).where(LanceRowDoc.count >= 3).select("count").execute().rows() == [{"object_id": "c1", "count": 3}]
    finally:
        ws.release()


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_vector_columns_read_as_one_float32_array_per_cell(tmp_path):
    """A Vector reads as a float32 NumPy array, as through pandas in 0.1.2.2, and a page shares one copied matrix."""
    import numpy as np

    backend = hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-vectors"))
    ws = hb.HeavenBase("native-lance-vector-cells-test", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})

    class LanceVectorDoc(hb.Entity):
        identifier = "lance-vector-doc"

        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
        extra = hb.field(hb.Vector[2]).store(to="vec").optional()

    try:
        ws.register(LanceVectorDoc)
        ws.upsert_many(LanceVectorDoc, [{"object_id": "v1", "emb": [1.0, 0.0], "extra": [0.5, 0.25]}, {"object_id": "v2", "emb": [0.0, 1.0]}])
        rows = sorted(backend.scan(LanceVectorDoc.identifier), key=lambda row: row["object_id"])
        assert [(type(row["emb"]).__name__, row["emb"].dtype.name, row["emb"].tolist()) for row in rows] == [
            ("ndarray", "float32", [1.0, 0.0]),
            ("ndarray", "float32", [0.0, 1.0]),
        ]
        # an optional Vector with a null cell still reads its present cells as float32 arrays
        assert (rows[0]["extra"].dtype.name, rows[0]["extra"].tolist(), rows[1]["extra"]) == ("float32", [0.5, 0.25], None)
        # one copy per page: each cell is a writable view of a single matrix, not a list of Python floats
        assert rows[0]["emb"].base is not None and rows[0]["emb"].base is rows[1]["emb"].base
        assert rows[0]["emb"].flags.writeable
        got = ws.get_many(["v1", "v2"], entity=LanceVectorDoc)
        assert [(row["emb"].tolist(), None if row["extra"] is None else row["extra"].tolist()) for row in got] == [
            ([1.0, 0.0], [0.5, 0.25]),
            ([0.0, 1.0], None),
        ]
        assert isinstance(got[0]["emb"], np.ndarray)
    finally:
        ws.release()


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_column_values_match_arrow_cells_for_every_list_layout():
    """The vectorised list conversion returns exactly Arrow's cells for sliced, chunked, null and variable-length columns."""
    import pyarrow as pa

    from heavenbase.backends.vector.lance import _column_values

    def same(column):
        expected = column.to_pylist()
        got = [None if cell is None else cell.tolist() for cell in _column_values(column)]
        assert got == expected

    fixed = pa.array([[1.0, 2.0], None, [3.0, 4.5], [0.0, -1.0]], type=pa.list_(pa.float32(), 2))
    varied = pa.array([[1.0], None, [], [2.0, 3.0, 4.0]], type=pa.list_(pa.float32()))
    for column in (fixed, varied, pa.array([[7, 8]], type=pa.list_(pa.int64(), 2))):
        same(column)
        same(column.slice(1))
        same(column.slice(1, 2))
        same(column.slice(0, 0))
        same(pa.chunked_array([column.slice(0, 2), column.slice(2)]))
        same(pa.chunked_array([], type=column.type))
    # an item null cannot become a NumPy float, so such a column keeps Arrow's own cells
    assert _column_values(pa.array([[1.0, None]], type=pa.list_(pa.float32(), 2))) == [[1.0, None]]
    assert _column_values(pa.array([3, None])) == [3, None]


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_array_fields_round_trip_as_json_text(tmp_path):
    """An Array field on LanceDB is written as JSON text and reads back as its list, absent as None."""
    backend = hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-arrays"))
    ws = hb.HeavenBase("native-lance-array-test", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})

    class LanceArrayDoc(hb.Entity):
        identifier = "lance-array-doc"

        items = hb.field(hb.Array[hb.Integer]).store(to="vec").optional()
        tags = hb.field(hb.Array[hb.ShortText]).store(to="vec")
        loose = hb.field(hb.Array).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(LanceArrayDoc)
        ws.upsert_many(
            LanceArrayDoc,
            [
                {"object_id": "a1", "items": [1, 2], "tags": ["x", "y"], "loose": [1, "two", {"k": [3]}], "emb": [1.0, 0.0]},
                {"object_id": "a2", "tags": [], "emb": [0.0, 1.0]},
            ],
        )
        expected = [("a1", [1, 2], ["x", "y"], [1, "two", {"k": [3]}]), ("a2", None, [], None)]

        def arrays(rows):
            return [(row["object_id"], row["items"], row["tags"], row["loose"]) for row in rows]

        assert arrays(ws.get_many(["a1", "a2"], entity=LanceArrayDoc)) == expected
        assert arrays(sorted(backend.scan(LanceArrayDoc.identifier), key=lambda row: row["object_id"])) == expected
        assert arrays(ws.query(LanceArrayDoc).near(LanceArrayDoc.emb, [1.0, 0.0], top_k=2).execute().rows()) == expected
        assert ws.query(LanceArrayDoc).where(LanceArrayDoc.tags.array_contains("y")).execute().ids == ["a1"]
        ws.upsert(LanceArrayDoc, {"object_id": "a2", "items": [5], "tags": ["z"], "emb": [0.0, 1.0]})
        assert ws.get("a2", entity=LanceArrayDoc)["items"] == [5]
    finally:
        ws.release()


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_native_near_searches_the_named_vector_column(tmp_path):
    """With two Vector fields on one table, a native near ranks by the field it names."""
    backend = hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-two-vectors"))
    ws = hb.HeavenBase("native-lance-two-vectors-test", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})

    class LanceTwoVectorDoc(hb.Entity):
        identifier = "lance-two-vector-doc"

        first = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
        second = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(LanceTwoVectorDoc)
        ws.upsert_many(
            LanceTwoVectorDoc,
            [{"object_id": "p", "first": [1.0, 0.0], "second": [0.0, 1.0]}, {"object_id": "q", "first": [0.0, 1.0], "second": [1.0, 0.0]}],
        )
        assert ws.query(LanceTwoVectorDoc).near(LanceTwoVectorDoc.first, [1.0, 0.0], top_k=2).execute().ids == ["p", "q"]
        assert ws.query(LanceTwoVectorDoc).near(LanceTwoVectorDoc.second, [1.0, 0.0], top_k=2).execute().ids == ["q", "p"]
    finally:
        ws.release()


class LanceGrowDoc(hb.Entity):
    identifier = "lance-grow-doc"

    title = hb.field(hb.ShortText).store(to="vec").optional()
    emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)


class LanceGrowDocV2(hb.Entity):
    identifier = "lance-grow-doc"

    title = hb.field(hb.ShortText).store(to="vec").optional()
    emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
    tags = hb.field(hb.Array[hb.ShortText]).store(to="vec").optional()
    n = hb.field(hb.Integer).store(to="vec").optional()
    extra = hb.field(hb.Vector[2]).store(to="vec").optional()


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
@pytest.mark.parametrize("how", ["alter", "reopen"])
def test_lance_field_added_later_stores_its_values(tmp_path, how):
    """A field added by alter_entity, or by a wider definition on reopen, gains a LanceDB column and keeps every row."""
    uri = hb.utils.path_to_file_uri(tmp_path / "lance-grow")

    def workspace():
        return hb.HeavenBase(
            "native-lance-grow-test",
            detached=True,
            backends={"main": hb.backends.InMemBackend("main"), "vec": hb.backends.LanceBackend("vec", uri)},
        )

    ws = workspace()
    try:
        ws.register(LanceGrowDoc)
        ws.upsert_many(LanceGrowDoc, [{"object_id": "a", "title": "old", "emb": [1.0, 0.0]}, {"object_id": "z", "title": "keep", "emb": [0.0, 1.0]}])
        if how == "alter":
            ws.alter_entity(LanceGrowDocV2, confirm=LanceGrowDoc.identifier)
        else:
            ws.release()
            ws = workspace()
            ws.register(LanceGrowDocV2)
        ws.upsert(LanceGrowDocV2, {"object_id": "a", "title": "new", "emb": [1.0, 0.0], "tags": ["y"], "n": 5, "extra": [0.5, 0.5]})
        ws.upsert(LanceGrowDocV2, {"object_id": "b", "title": "u", "emb": [0.5, 0.5], "tags": ["x"], "n": 4})
        rows = ws.get_many(["a", "b", "z"], entity=LanceGrowDocV2)
        assert [(row["object_id"], row["title"], row["tags"], row["n"]) for row in rows] == [
            ("a", "new", ["y"], 5),
            ("b", "u", ["x"], 4),
            ("z", "keep", None, None),
        ]
        assert [None if row["extra"] is None else row["extra"].tolist() for row in rows] == [[0.5, 0.5], None, None]
        assert ws.count(LanceGrowDocV2) == 3
        assert ws.query(LanceGrowDocV2).where(LanceGrowDocV2.n >= 5).execute().ids == ["a"]
    finally:
        ws.release()


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_failed_native_upsert_preserves_other_clients_commits(tmp_path, monkeypatch):
    """A separate LanceDB client can commit while the first client's merge fails."""
    import lancedb

    backend = hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path))
    entity_id = "concurrent-lance-upsert"
    table_name = backend._table_name(entity_id)
    table = backend.client.create_table(table_name, [{"object_id": "target", "title": "old"}])
    peer = lancedb.connect(str(tmp_path))
    committed = {"target": "old"}

    def peer_add(object_id):
        other = peer.open_table(table_name)
        other.add([{"object_id": object_id, "title": "peer"}])
        assert object_id in {row["object_id"] for row in other.to_arrow().to_pylist()}
        committed[object_id] = "peer"

    class FailingTable:
        def __getattr__(self, name):
            return getattr(table, name)

        def merge_insert(self, *args):
            peer_add("during-merge")
            raise OSError("merge refused")

        def add(self, data):
            peer_add("during-replacement")
            raise OSError("add refused")

    monkeypatch.setattr(backend, "_table", lambda entity_id: FailingTable())
    with pytest.raises(OSError, match="(merge|add) refused"):
        backend.upsert([hb.ext.RowOp(entity_id, "target", {"title": "new"})])
    assert {row["object_id"]: row["title"] for row in peer.open_table(table_name).to_arrow().to_pylist()} == committed


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_id_lookup_primitives_do_not_scan(tmp_path):
    """LanceDB row identity primitives should use native id filters/count."""
    backend = _NativeOnlyLanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-id-lookup"))
    ws = hb.HeavenBase("native-lance-id-lookup-test", backends={"main": hb.backends.InMemBackend("main"), "vec": backend})

    class LanceLookupDoc(hb.Entity):
        identifier = "lance-lookup-doc"

        title = hb.field(hb.ShortText).store(to="vec")
        score = hb.field(hb.Float).store(to="vec")

    ws.register(LanceLookupDoc)
    ws.upsert_many(
        LanceLookupDoc,
        [
            {"object_id": "d1", "title": "One", "score": 1.0},
            {"object_id": "d2", "title": "Two", "score": 2.0},
            {"object_id": "d3", "title": "Three", "score": 3.0},
        ],
    )

    rows = ws.get_many(["d3", "missing", "d1"], entity=LanceLookupDoc)

    assert [row["object_id"] if row else None for row in rows] == ["d3", None, "d1"]
    assert ws.exists_many(["d2", "missing"], entity=LanceLookupDoc) == [True, False]
    assert ws.count(LanceLookupDoc) == 3

    WORKSPACES.pop("native-lance-id-lookup-test", None)


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_id_lookup_primitives_survive_reopen(tmp_path):
    """LanceDB row identity primitives should read provider rows after process-local state is gone."""
    path = hb.utils.path_to_file_uri(tmp_path / "lance-id-lookup-reopen")
    ws_id = "native-lance-id-lookup-reopen"
    ws = hb.HeavenBase(ws_id, backends={"main": hb.backends.InMemBackend("main"), "vec": hb.backends.LanceBackend("vec", path)})

    class LanceReopenDoc(hb.Entity):
        identifier = "lance-reopen-doc"

        title = hb.field(hb.ShortText).store(to="vec")
        score = hb.field(hb.Float).store(to="vec")

    ws.register(LanceReopenDoc)
    ws.upsert_many(
        LanceReopenDoc,
        [
            {"object_id": "l1", "title": "One", "score": 1.0},
            {"object_id": "l2", "title": "Two", "score": 2.0},
            {"object_id": "l3", "title": "Three", "score": 3.0},
        ],
    )

    reopened = hb.HeavenBase(ws_id, detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": _NativeOnlyLanceBackend("vec", path)})
    reopened.register(LanceReopenDoc)
    rows = reopened.get_many(["l3", "missing", "l1"], entity=LanceReopenDoc)

    assert [row["object_id"] if row else None for row in rows] == ["l3", None, "l1"]
    assert reopened.exists_many(["l2", "missing"], entity=LanceReopenDoc) == [True, False]
    assert reopened.count(LanceReopenDoc) == 3

    WORKSPACES.pop(ws_id, None)


@pytest.mark.detail
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_id_lookup_primitives_survive_reopen(tmp_path):
    """Chroma row identity primitives should read persisted provider rows, not a mirror scan."""
    path = hb.utils.path_to_file_uri(tmp_path / "chroma-id-lookup")
    ws_id = "native-chroma-id-lookup"
    ws = hb.HeavenBase(ws_id, backends={"main": hb.backends.InMemBackend("main"), "chroma": hb.backends.ChromaBackend("chroma", path)})

    class ChromaLookupDoc(hb.Entity):
        identifier = "chroma-lookup-doc"

        title = hb.field(hb.ShortText).store(to="chroma")
        score = hb.field(hb.Float).store(to="chroma")
        emb = hb.field(hb.Vector[2]).store(to="chroma", strategy=hb.VectorIndex.identifier)

    ws.register(ChromaLookupDoc)
    ws.upsert_many(
        ChromaLookupDoc,
        [
            {"object_id": "c1", "title": "One", "score": 1.0, "emb": [1.0, 0.0]},
            {"object_id": "c2", "title": "Two", "score": 2.0, "emb": [0.0, 1.0]},
            {"object_id": "c3", "title": "Three", "score": 3.0, "emb": [0.5, 0.5]},
        ],
    )

    reopened = hb.HeavenBase(ws_id, detached=True, backends={"main": hb.backends.InMemBackend("main"), "chroma": _NativeOnlyChromaBackend("chroma", path)})
    reopened.register(ChromaLookupDoc)
    rows = reopened.get_many(["c3", "missing", "c1"], entity=ChromaLookupDoc)

    assert [row["object_id"] if row else None for row in rows] == ["c3", None, "c1"]
    assert reopened.exists_many(["c2", "missing"], entity=ChromaLookupDoc) == [True, False]
    assert reopened.count(ChromaLookupDoc) == 3

    WORKSPACES.pop(ws_id, None)


@pytest.mark.slow
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
@pytest.mark.parametrize("persistent", [True, False], ids=["persistent", "ephemeral"])
def test_chroma_writes_more_rows_than_one_chroma_call_accepts(tmp_path, persistent):
    """``upsert_many`` and ``delete_many`` above the client's ``get_max_batch_size()`` (5,461 on chromadb 1.5.9) succeed in full."""
    ws_id = f"native-chroma-large-write-{'persistent' if persistent else 'ephemeral'}"
    backend = hb.backends.ChromaBackend("chroma", hb.utils.path_to_file_uri(tmp_path / "chroma") if persistent else None)
    ws = hb.HeavenBase(ws_id, detached=True, backends={"main": hb.backends.InMemBackend("main"), "chroma": backend})

    class ChromaBulkDoc(hb.Entity):
        identifier = "chroma-bulk-doc"

        title = hb.field(hb.ShortText).store(to="chroma")
        emb = hb.field(hb.Vector[2]).store(to="chroma", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(ChromaBulkDoc)
        size = backend.client.get_max_batch_size()
        written: list[str] = []
        for total in (size + 1, 3 * size):
            ids = [f"b{total}-{position:05d}" for position in range(total)]
            rows = [{"object_id": object_id, "title": object_id, "emb": [1.0, position / total]} for position, object_id in enumerate(ids)]
            ws.upsert_many(ChromaBulkDoc, rows)
            written.extend(ids)
            assert ws.count(ChromaBulkDoc) == len(written)
            assert [row["title"] for row in ws.get_many(ids, entity=ChromaBulkDoc)] == ids
        assert ws.delete_many([(ChromaBulkDoc, object_id) for object_id in written]) == [True] * len(written)
        assert ws.count(ChromaBulkDoc) == 0
    finally:
        ws.drop()


@pytest.fixture
def _chroma_never_embeds(monkeypatch):
    """Fail any Chroma default-model embedding and any non-loopback connection, as on an offline host."""
    import socket

    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    model_dir = ONNXMiniLM_L6_V2.DOWNLOAD_PATH
    had_model_dir = model_dir.exists()
    connect = socket.socket.connect

    def local_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
            raise OSError(f"outbound connection to {host!r} is blocked in this test")
        return connect(sock, address)

    def refuse(*args, **kwargs):
        raise AssertionError("Chroma computed an embedding with its default model")

    def refuse_call(self, input):
        # chromadb 0.5/0.6 check that an embedding function's __call__ takes exactly (self, input).
        raise AssertionError("Chroma computed an embedding with its default model")

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(ONNXMiniLM_L6_V2, "__call__", refuse_call)
    monkeypatch.setattr(ONNXMiniLM_L6_V2, "_download_model_if_not_exists", refuse)
    yield
    assert had_model_dir or not model_dir.exists(), "Chroma downloaded its default embedding model"


@pytest.mark.detail
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_never_embeds_rows_without_vectors(tmp_path, _chroma_never_embeds):
    """Chroma rows always carry HeavenBase vectors or placeholders, never Chroma's own embeddings."""
    ws_id = "native-chroma-no-embed"
    ws = hb.HeavenBase(
        ws_id,
        detached=True,
        backends={"main": hb.backends.InMemBackend("main"), "chroma": hb.backends.ChromaBackend("chroma", hb.utils.path_to_file_uri(tmp_path / "chroma"))},
    )

    class ChromaPlainDoc(hb.Entity):
        identifier = "chroma-plain-doc"

        title = hb.field(hb.ShortText).store(to="chroma")
        price = hb.field(hb.Float).store(to="chroma")

    class ChromaVectorDoc(hb.Entity):
        identifier = "chroma-vector-doc"

        title = hb.field(hb.ShortText).store(to="chroma")
        emb = hb.field(hb.Vector[2]).store(to="chroma", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(ChromaPlainDoc)
        ws.register(ChromaVectorDoc)
        ws.upsert_many(ChromaPlainDoc, [{"object_id": "p1", "title": "alpha", "price": 10.0}, {"object_id": "p2", "title": "beta", "price": 20.0}])
        assert ws.query(ChromaPlainDoc).where(ChromaPlainDoc.price == 10.0).execute().rows()[0]["title"] == "alpha"

        ws.upsert_many(ChromaVectorDoc, [{"object_id": "a", "title": "one", "emb": [1.0, 0.0]}, {"object_id": "b", "title": "two", "emb": [0.0, 1.0]}])
        ws.upsert(ChromaVectorDoc, {"object_id": "a", "title": "renamed"})  # a row without its vector keeps the stored one
        ws.upsert_many(ChromaVectorDoc, [{"object_id": "b", "title": "two again"}, {"object_id": "c", "title": "three", "emb": [0.6, 0.8]}])
        assert ws.get("a", entity=ChromaVectorDoc)["emb"] == pytest.approx([1.0, 0.0])
        assert ws.get("b", entity=ChromaVectorDoc)["title"] == "two again"
        nearest = ws.query(ChromaVectorDoc).near(ChromaVectorDoc.emb, [1.0, 0.0], top_k=3).select("title").execute().rows()
        assert [row["title"] for row in nearest] == ["renamed", "three", "two again"]
        with pytest.raises(ValueError, match="needs a 'emb' vector for new rows \\['d'\\]"):
            ws.upsert(ChromaVectorDoc, {"object_id": "d", "title": "no vector yet"})
        with pytest.raises(ValueError, match="cannot clear its 'emb' vector; rows \\['b'\\]"):
            ws.upsert(ChromaVectorDoc, {"object_id": "b", "emb": None})
        assert ws.get("b", entity=ChromaVectorDoc)["emb"] == pytest.approx([0.0, 1.0])
    finally:
        ws.drop()


@pytest.mark.detail
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_stores_entities_whose_vector_lives_on_another_backend(tmp_path, _chroma_never_embeds):
    """An entity with its Vector field on another backend writes placeholder embeddings to Chroma."""
    ws_id = "native-chroma-split-vector"
    backend = hb.backends.ChromaBackend("chroma", hb.utils.path_to_file_uri(tmp_path / "chroma"))
    ws = hb.HeavenBase(ws_id, detached=True, backends={"main": hb.backends.InMemBackend("main"), "chroma": backend})

    class ChromaSplitDoc(hb.Entity):
        identifier = "chroma-split-doc"

        title = hb.field(hb.ShortText).store(to="chroma")
        emb = hb.field(hb.Vector[2]).store(to="main", strategy=hb.VectorIndex)

    try:
        ws.register(ChromaSplitDoc)
        ws.upsert_many(ChromaSplitDoc, [{"object_id": "a", "title": "one", "emb": [1.0, 0.0]}, {"object_id": "b", "title": "two", "emb": [0.0, 1.0]}])
        assert ws.get("a", entity=ChromaSplitDoc) == {"object_id": "a", "title": "one", "emb": pytest.approx([1.0, 0.0])}
        assert all("emb" not in row for row in backend.scan(ChromaSplitDoc.schema().entity_id))
        nearest = ws.query(ChromaSplitDoc).near(ChromaSplitDoc.emb, [0.0, 1.0], top_k=2).select("title").execute().rows()
        assert [row["title"] for row in nearest] == ["two", "one"]
    finally:
        ws.drop()


@pytest.mark.detail
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_writes_into_collections_holding_default_model_embeddings(tmp_path, _chroma_never_embeds):
    """Collections written by 0.1.2.2 hold 384-wide default-model embeddings and must stay writable."""
    ws_id = "native-chroma-legacy-width"
    backend = hb.backends.ChromaBackend("chroma", hb.utils.path_to_file_uri(tmp_path / "chroma"))
    ws = hb.HeavenBase(ws_id, detached=True, backends={"main": hb.backends.InMemBackend("main"), "chroma": backend})

    class ChromaLegacyDoc(hb.Entity):
        identifier = "chroma-legacy-doc"

        title = hb.field(hb.ShortText).store(to="chroma")

    try:
        ws.register(ChromaLegacyDoc)
        legacy = backend._collection(ChromaLegacyDoc.schema().entity_id)
        legacy.upsert(ids=["old"], embeddings=[[0.05] * 384], documents=["legacy"], metadatas=[{"title": "legacy"}])
        ws.upsert(ChromaLegacyDoc, {"object_id": "new", "title": "fresh"})
        assert [row["object_id"] if row else None for row in ws.get_many(["old", "new"], entity=ChromaLegacyDoc)] == ["old", "new"]
        assert len(legacy.get(ids=["new"], include=["embeddings"])["embeddings"][0]) == 384
    finally:
        ws.drop()


@pytest.mark.detail
def test_provider_backends_id_primitives_use_native_clients():
    """Provider adapters should use native point-lookup APIs when clients are connected."""

    class FakeMilvusClient:
        def __init__(self):
            self.rows = {"m1": {"object_id": "m1", "title": "One"}, "m2": {"object_id": "m2", "title": "Two"}}
            self.consistency_levels = []

        def has_collection(self, name):
            return True

        def query(self, collection_name, filter, output_fields, consistency_level):
            self.consistency_levels.append(consistency_level)
            if output_fields == ["count(*)"]:
                assert filter == ""
                return [{"count(*)": len(self.rows)}]
            # point lookups are an escaped primary-key ``in`` filter (``MilvusClient.get`` does not escape keys)
            assert filter.startswith("object_id in [") and output_fields == ["*"], (filter, output_fields)
            ids = json.loads(filter[len("object_id in ") :])
            return [self.rows[row_id] for row_id in ids if row_id in self.rows]

    class FakePineconeIndex:
        def __init__(self):
            self.rows = {
                "p1": {"metadata": {"object_id": "p1", "title": "One"}, "values": [1.0, 0.0]},
                "p2": {"metadata": {"object_id": "p2", "title": "Two"}, "values": [0.0, 1.0]},
            }
            self.fetch_sizes = []

        def fetch(self, ids):
            self.fetch_sizes.append(len(ids))
            return {"vectors": {row_id: self.rows[row_id] for row_id in ids if row_id in self.rows}}

        def describe_index_stats(self):
            return {"total_vector_count": len(self.rows)}

    class FakePineconeDescription:
        host = "http://localhost:5080"

    class FakePineconeClient:
        def __init__(self):
            self.index = FakePineconeIndex()

        def has_index(self, name):
            return True

        def describe_index(self, name):
            return FakePineconeDescription()

        def Index(self, **kwargs):
            return self.index

    class FakeESIndices:
        def exists(self, index):
            return True

    class FakeESClient:
        def __init__(self):
            self.indices = FakeESIndices()
            self.rows = {"e1": {"object_id": "e1", "title": "One"}, "e2": {"object_id": "e2", "title": "Two"}}

        def mget(self, index, ids):
            return {"docs": [{"_id": row_id, "found": row_id in self.rows, "_source": self.rows.get(row_id, {})} for row_id in ids]}

        def count(self, index):
            return {"count": len(self.rows)}

    milvus = _NativeOnlyMilvusBackend("milvus")
    milvus.client = FakeMilvusClient()
    assert milvus.tag("row-lookup").value == {"get_many": "native", "exists_many": "native", "count": "native"}
    assert [row["object_id"] if row else None for row in milvus.get_many("milvus-lookup-doc", ["m2", "missing", "m1"])] == ["m2", None, "m1"]
    assert milvus.exists_many("milvus-lookup-doc", ["m1", "missing"]) == [True, False]
    assert milvus.count("milvus-lookup-doc") == 2
    assert milvus.exact_count("milvus-lookup-doc") == 2
    assert milvus.client.consistency_levels == ["Strong", "Strong", "Strong", "Strong"]
    milvus.client.query = lambda **kwargs: [{}]
    with pytest.raises(RuntimeError, match=r"missing count\(\*\) value"):
        milvus.count("milvus-lookup-doc")

    pinecone = _NativeOnlyPineconeBackend("pinecone")
    pinecone.client = FakePineconeClient()
    assert pinecone.tag("row-lookup").value == {"get_many": "native", "exists_many": "native", "count": "native"}
    assert [row["object_id"] if row else None for row in pinecone.get_many("pinecone-lookup-doc", ["p2", "missing", "p1"])] == ["p2", None, "p1"]
    assert pinecone.exists_many("pinecone-lookup-doc", ["p1", "missing"]) == [True, False]
    assert pinecone.count("pinecone-lookup-doc") == 2
    with pytest.raises(NotImplementedError, match="cannot prove an exact fragment count"):
        pinecone.exact_count("pinecone-lookup-doc")
    pinecone.client.index.rows = {f"p{index}": {"metadata": {"object_id": f"p{index}", "title": str(index)}, "values": [1.0, 0.0]} for index in range(1005)}
    large_ids = [*[f"p{index}" for index in range(1004)], "missing", "p0"]
    pinecone.client.index.fetch_sizes = []
    large_rows = pinecone.get_many("pinecone-lookup-doc", large_ids)
    assert pinecone.client.index.fetch_sizes == [1000, 5]
    assert [row["object_id"] if row else None for row in large_rows[-2:]] == [None, "p0"]
    pinecone.client.index.fetch_sizes = []
    assert pinecone.exists_many("pinecone-lookup-doc", large_ids)[-2:] == [False, True]
    assert pinecone.client.index.fetch_sizes == [1000, 5]

    elastic = _NativeOnlyElasticsearchBackend("search")
    elastic.client = FakeESClient()
    assert elastic.tag("row-lookup").value == {"get_many": "native", "exists_many": "native", "count": "native"}
    assert [row["object_id"] if row else None for row in elastic.get_many("elastic-lookup-doc", ["e2", "missing", "e1"])] == ["e2", None, "e1"]
    assert elastic.exists_many("elastic-lookup-doc", ["e1", "missing"]) == [True, False]
    assert elastic.count("elastic-lookup-doc") == 2


@pytest.mark.detail
def test_provider_get_many_rejects_non_string_columns_without_disabling_the_client():
    """A caller's bad ``columns=`` is the documented ``TypeError``, raised before the provider read.

    It must not be reported as a provider failure: Pinecone used to drop its client and every
    provider overwrote ``client_error``, so later valid reads failed or reported a stale error.
    """

    class FakePineconeIndex:
        def fetch(self, ids):
            return {"vectors": {"p1": {"metadata": {"object_id": "p1", "title": "One"}, "values": [1.0, 0.0]}}}

    class FakePineconeDescription:
        host = "http://localhost:5080"

    class FakePineconeClient:
        index = FakePineconeIndex()

        def has_index(self, name):
            return True

        def describe_index(self, name):
            return FakePineconeDescription()

        def Index(self, **kwargs):
            return self.index

    class FakeESIndices:
        def exists(self, index):
            return True

    class FakeESClient:
        indices = FakeESIndices()

        def mget(self, index, ids):
            return {"docs": [{"_id": "e1", "found": True, "_source": {"object_id": "e1", "title": "One"}}]}

    pinecone = _NativeOnlyPineconeBackend("pinecone")
    client = pinecone.client = FakePineconeClient()
    pinecone.client_error = None
    elastic = _NativeOnlyElasticsearchBackend("search")
    elastic.client = FakeESClient()
    elastic.client_error = None
    for backend, entity_id, object_id in ((pinecone, "pinecone-columns-doc", "p1"), (elastic, "elastic-columns-doc", "e1")):
        with pytest.raises(TypeError, match="columns must be field names"):
            backend.get_many(entity_id, [object_id], columns=[1])
        assert backend.client_error is None
        assert backend.get_many(entity_id, [object_id], columns=iter(["title"])) == [{"object_id": object_id, "title": "One"}]
    assert pinecone.client is client


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_fallback_filter_after_native_near(tmp_path):
    """LanceDB near search should accept bounded candidate-ID prefilters."""
    ws = hb.HeavenBase(
        "native-lance-fallback-test",
        backends={"main": hb.backends.InMemBackend("main"), "vec": hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-fb"))},
    )

    class LanceFallbackDoc(hb.Entity):
        identifier = "lance-fallback-doc"

        title = hb.field(hb.ShortText)
        score = hb.field(hb.Float)
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(LanceFallbackDoc)
    ws.upsert_many(
        LanceFallbackDoc,
        [
            {"object_id": "d1", "title": "Close", "score": 5.0, "emb": [1.0, 0.0]},
            {"object_id": "d2", "title": "Far", "score": 10.0, "emb": [0.0, 1.0]},
            {"object_id": "d3", "title": "Closer", "score": 7.0, "emb": [0.9, 0.1]},
        ],
    )

    query = ws.query(LanceFallbackDoc).near(LanceFallbackDoc.emb, [1, 0], top_k=2).where(LanceFallbackDoc.score > 6)
    near_step = [step for step in query.explain()["steps"] if step.get("node") == "near"][0]
    frame = query.execute()

    assert near_step["near_filter_mode"] == "candidate_id_prefilter"
    assert frame.ids == ["d3", "d2"]

    WORKSPACES.pop("native-lance-fallback-test", None)
