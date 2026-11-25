from concurrent.futures import ThreadPoolExecutor

import heavenbase as hb
import pytest
from heavenbase.execution import QueryFragment, RowOp
from heavenbase.backends.sql.strategies.gram import GramConfig, keyword_grams, normalize_text, query_grams, resolve_gram_normalizer, resolve_gram_tokenizer
from heavenbase.backends.sql.strategies.gram.sparse import resolve_hash
from heavenbase.execution.frame import ResultFrame, union
from heavenbase.placement import StorageBinding


def _db(path, name: str) -> str:
    return hb.utils.path_to_file_uri(path / name)


def _compact_dash(text, _config):
    """Normalize text by removing dashes and case folding."""
    return str(text).replace("-", "").casefold()


def _dash_tokens(text, _config):
    """Split text into non-empty dash-delimited tokens."""
    return [item for item in str(text).split("-") if item]


def _thread_norm(text, _config):
    """Return text unchanged for concurrent registration coverage."""
    return str(text)


def _thread_tokens(text, _config):
    """Return one token for concurrent registration coverage."""
    return [str(text)]


class GramRouteDoc(hb.Entity):
    identifier = "gram-route-doc"

    title: hb.ShortText
    keywords: hb.Array[hb.ShortText]
    numbers: hb.Array[hb.Integer]


class GramFallbackDoc(hb.Entity):
    identifier = "gram-fallback-doc"

    name: hb.ShortText
    keywords: hb.Array[hb.ShortText]


class GramNativeDoc(hb.Entity):
    identifier = "gram-native-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))


class GramScanParityDoc(hb.Entity):
    identifier = "gram-scan-parity-doc"

    title: hb.ShortText
    keywords: hb.Array[hb.ShortText]


class GramCatalogSource(hb.Entity):
    identifier = "gram-catalog-source"

    name: hb.ShortText
    keywords: hb.Array[hb.ShortText]


class GramStrategyOptionDoc(hb.Entity):
    identifier = "gram-strategy-option-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText]).store(
        to="main", strategy=hb.SparseGramIndex(normalizer="simple", gram_unit="token", tokenizer="space", gram_width=2)
    )


class SQLSparseGramDoc(hb.Entity):
    identifier = "sql-sparse-gram-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))


def _workspace(name: str):
    return hb.HeavenBase(
        name,
        detached=True,
        backends={
            "main": {"type": "sqlite", "name": "main", "database": ":memory:"},
        },
    )


def _sql_sparse_gram_backend(database: str, ws_id: str, options: dict[str, object] | None = None):
    schema = SQLSparseGramDoc.schema()
    backend = hb.backends.SQLiteBackend("main", database=database, ws_id=ws_id)
    bindings = [
        StorageBinding(schema.entity_id, "object_id", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "title", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "keywords", "main", hb.SparseGramIndex.identifier, "test", options or {"normalizer": "simple"}),
    ]
    backend.ensure(schema, bindings)
    return backend


def _sql_sparse_query(backend, query: str) -> ResultFrame:
    schema = SQLSparseGramDoc.schema()
    return backend.execute(
        QueryFragment(
            "main",
            "contained_in",
            "keywords",
            {"query": query},
            notes={"kind": "sql_sparse_gram_contained_in", "entity_id": schema.entity_id},
        )
    )


def test_array_short_text_routes_to_sparse_gram_without_affecting_other_arrays():
    ws = _workspace("gram-routing-test")
    ws.register(GramRouteDoc)
    plan = ws.storage_plans[GramRouteDoc.schema().entity_id]

    assert plan.first("keywords").backend == "main"
    assert plan.first("keywords").strategy == hb.SparseGramIndex.identifier
    assert plan.first("numbers").backend == "main"
    assert plan.first("numbers").strategy == hb.SideTable.identifier
    assert hb.ext.registered_storage_profiles()["array[short-text]"].default_strategy == hb.SparseGramIndex.identifier
    assert hb.ext.registered_storage_profiles()[hb.Array.identifier].default_strategy == hb.InlineColumn.identifier


def test_contained_in_scan_fallback_returns_match_metadata():
    ws = hb.HeavenBase("gram-fallback-test", detached=True, backends={"main": {"type": "inmem", "name": "main"}})
    ws.register(GramFallbackDoc)
    ws.upsert_many(
        GramFallbackDoc,
        [
            {"object_id": "a", "name": "A", "keywords": ["Blue", "green"]},
            {"object_id": "b", "name": "B", "keywords": ["red"]},
        ],
    )

    rows = ws.query(GramFallbackDoc).where(GramFallbackDoc.keywords.contained_in("The BLUE whale")).select("name", "match").execute().rows()
    step = ws.explain(ws.query(GramFallbackDoc).where(GramFallbackDoc.keywords.contained_in("The BLUE whale")).spec)["steps"][0]

    assert [row["object_id"] for row in rows] == ["a"]
    assert rows[0]["name"] == "A"
    assert rows[0]["match"][0]["keyword"] == "Blue"
    assert rows[0]["match"][0]["query_text"] == "BLUE"
    assert step["backend"] == "main"
    assert step["handler_mode"] == "py_scan"


def test_sql_sparse_gram_native_contained_in_hydrates_rows_and_match_metadata():
    ws = _workspace("gram-native-test")
    ws.register(GramNativeDoc)
    ws.upsert_many(
        GramNativeDoc,
        [
            {"object_id": "a", "title": "Alpha", "keywords": ["Straße", "needle"]},
            {"object_id": "b", "title": "Beta", "keywords": ["other"]},
        ],
    )

    query = ws.query(GramNativeDoc).where(GramNativeDoc.keywords.contained_in("A STRASSE appears"))
    rows = query.select("title", "match").execute().rows()
    step = ws.explain(query.spec)["steps"][0]

    assert rows == [
        {
            "object_id": "a",
            "title": "Alpha",
            "match": [
                {
                    "field": "keywords",
                    "op": "contained_in",
                    "keyword": "Straße",
                    "keyword_norm": "strasse",
                    "query_span": [2, 9],
                    "query_norm_span": [2, 9],
                    "query_text": "STRASSE",
                    "ordinal": 0,
                    "normalizer": "simple:auto",
                    "precision": "exact",
                }
            ],
        }
    ]
    assert step["backend"] == "main"
    assert step["strategy"] == hb.SparseGramIndex.identifier
    assert step["handler_kind"] == "sql_sparse_gram_contained_in"
    assert step["strategy_adapter"] == "sql_sparse_gram"
    assert step["handler_mode"] == "native"


def test_sql_sparse_gram_json_query_and_logical_ops_match_scan_fallback():
    native = _workspace("gram-native-parity-test")
    scan = hb.HeavenBase("gram-scan-parity-test", detached=True, backends={"main": {"type": "inmem", "name": "main"}})
    for ws, entity in ((native, GramNativeDoc), (scan, GramScanParityDoc)):
        ws.register(entity)
        ws.upsert_many(
            entity,
            [
                {"object_id": "a", "title": "Alpha", "keywords": ["redis query", "cache"]},
                {"object_id": "b", "title": "Beta", "keywords": ["sparse gram", "search"]},
                {"object_id": "c", "title": "Gamma", "keywords": ["other"]},
            ],
        )

    spec = {"filter": {"keywords": {"$contained_in": "the REDIS QUERY path"}}, "select": ["title", "match"]}
    rows = native.query_json(GramNativeDoc, spec).execute().rows()
    assert [row["object_id"] for row in rows] == ["a"]
    assert rows[0]["match"][0]["keyword"] == "redis query"

    queries = ["redis query engine", "sparse gram index", "nothing useful"]
    for text in queries:
        native_ids = native.query(GramNativeDoc).where(GramNativeDoc.keywords.contained_in(text)).execute().ids
        scan_ids = scan.query(GramScanParityDoc).where(GramScanParityDoc.keywords.contained_in(text)).execute().ids
        assert native_ids == scan_ids

    logical = native.query(GramNativeDoc).where(GramNativeDoc.keywords.contained_in("redis query") | GramNativeDoc.keywords.contained_in("sparse gram"))
    assert logical.execute().ids == ["a", "b"]

    negated = native.query(GramNativeDoc).where(~GramNativeDoc.keywords.contained_in("redis query"))
    assert negated.select("title").execute().ids == ["b", "c"]
    assert negated.execute().metadata_columns == set()
    assert scan.query(GramScanParityDoc).where(~GramScanParityDoc.keywords.contained_in("redis query")).execute().metadata_columns == set()


def test_contained_in_without_matches_keeps_the_match_column_on_both_routes():
    """A miss answers the match metadata column a hit carries, on the native and scan routes alike."""
    native = _workspace("gram-native-miss-test")
    scan = hb.HeavenBase("gram-scan-miss-test", detached=True, backends={"main": {"type": "inmem", "name": "main"}})
    for ws, entity in ((native, GramNativeDoc), (scan, GramScanParityDoc)):
        ws.register(entity)
        ws.upsert(entity, {"object_id": "a", "title": "Alpha", "keywords": ["needle"]})
        for text, ids in (("a needle here", ["a"]), ("nothing useful", [])):
            frame = ws.query(entity).where(entity.keywords.contained_in(text)).select("title", "match").execute()
            assert frame.ids == ids and frame.col_names() == ["object_id", "title", "match"]


def test_custom_gram_normalizer_registered_through_ext():
    hb.ext.register_gram_normalizer("unit_compact_dash", _compact_dash)
    ws = _workspace("gram-custom-normalizer-test")
    entity = type(
        "CustomGramNormalizerDoc",
        (hb.Entity,),
        {
            "identifier": "custom-gram-normalizer-doc",
            "title": hb.field(hb.ShortText),
            "keywords": hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="unit_compact_dash")),
        },
    )
    ws.register(entity)
    ws.upsert_many(
        entity,
        [
            {"object_id": "a", "title": "Alpha", "keywords": ["New-York"]},
            {"object_id": "b", "title": "Beta", "keywords": ["Boston"]},
        ],
    )

    rows = ws.query(entity).where(entity.keywords.contained_in("newyork transit")).select("match").execute().rows()

    assert [row["object_id"] for row in rows] == ["a"]
    assert rows[0]["match"][0]["keyword_norm"] == "newyork"
    assert rows[0]["match"][0]["precision"] == "normalized_only"
    assert hb.ext.resolve_gram_normalizer(GramConfig.load(normalizer="unit_compact_dash")) is _compact_dash


def test_sparse_gram_strategy_accepts_direct_normalizer_callable():
    def compact_dash(text, _config):
        return str(text).replace("-", "").casefold()

    ws = _workspace("gram-direct-normalizer-test")
    entity = type(
        "DirectGramNormalizerDoc",
        (hb.Entity,),
        {
            "identifier": "direct-gram-normalizer-doc",
            "title": hb.field(hb.ShortText),
            "keywords": hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer=compact_dash)),
        },
    )
    ws.register(entity)
    ws.upsert_many(
        entity,
        [
            {"object_id": "a", "title": "Alpha", "keywords": ["New-York"]},
            {"object_id": "b", "title": "Beta", "keywords": ["Boston"]},
        ],
    )

    rows = ws.query(entity).where(entity.keywords.contained_in("newyork transit")).select("match").execute().rows()

    assert [row["object_id"] for row in rows] == ["a"]
    assert rows[0]["match"][0]["keyword_norm"] == "newyork"
    assert resolve_gram_normalizer(GramConfig.load(normalizer=compact_dash)) is compact_dash


def test_sparse_gram_rejects_unregistered_dotted_normalizer_string():
    with pytest.raises(ValueError, match="Unknown gram normalizer"):
        resolve_gram_normalizer(GramConfig.load(normalizer="tests.test_sql_sparse_gram.compact_dash"))


def test_sparse_gram_rejects_dotted_hash_function_string():
    with pytest.raises(ValueError, match="Unknown hash function"):
        resolve_hash(GramConfig.load(hash_function="tests.test_utils_basic._keyword_hash_int"))


def test_gram_registration_is_thread_safe():
    prefix = "unit_thread"

    def register(index: int) -> None:
        hb.ext.register_gram_normalizer(f"{prefix}_norm_{index}", _thread_norm, replace=True)
        hb.ext.register_gram_tokenizer(f"{prefix}_tok_{index}", _thread_tokens, replace=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(register, range(20)))

    assert resolve_gram_normalizer(GramConfig.load(normalizer=f"{prefix}_norm_7")) is _thread_norm
    assert resolve_gram_tokenizer(GramConfig.load(tokenizer=f"{prefix}_tok_7")) is _thread_tokens


def test_default_gram_normalizer_collapses_punctuation_and_whitespace():
    config = GramConfig.load(normalizer="simple")

    assert normalize_text("  Redis-Query,\tEngine!!  ", config).text == "redis query engine"


def test_builtin_gram_normalizer_alternatives():
    assert normalize_text("Redis/Query", GramConfig.load(normalizer="default")).text == "redis query"
    assert normalize_text("  Redis-Query,\tEngine!!  ", GramConfig.load(normalizer="fast")).text == "redis query engine"
    assert normalize_text("北京A", GramConfig.load(normalizer="char")).text == "北 京 A"
    assert normalize_text("  Redis-Query  ", GramConfig.load(normalizer="none")).text == "  Redis-Query  "


def test_native_contained_in_matches_across_punctuation_boundaries():
    ws = _workspace("gram-punctuation-normalizer-test")
    ws.register(GramNativeDoc)
    ws.upsert(GramNativeDoc, {"object_id": "a", "title": "Alpha", "keywords": ["Redis-Query"]})

    rows = ws.query(GramNativeDoc).where(GramNativeDoc.keywords.contained_in("redis query engine")).select("match").execute().rows()

    assert [row["object_id"] for row in rows] == ["a"]
    assert rows[0]["match"][0]["keyword_norm"] == "redis query"


def test_sparse_gram_strategy_options_round_trip_through_manifest_and_explain():
    ws = _workspace("gram-strategy-options-test")
    ws.register(GramStrategyOptionDoc)
    ws.upsert(GramStrategyOptionDoc, {"object_id": "a", "title": "Alpha", "keywords": ["Redis Query"]})

    binding = ws.storage_plans[GramStrategyOptionDoc.schema().entity_id].first("keywords")
    manifest = ws.to_manifest()
    stores = manifest["entities"][0]["fields"]["keywords"]["stores"]
    rows = ws.query(GramStrategyOptionDoc).where(GramStrategyOptionDoc.keywords.contained_in("the redis query path")).select("match").execute().rows()
    step = ws.explain(ws.query(GramStrategyOptionDoc).where(GramStrategyOptionDoc.keywords.contained_in("the redis query path")).spec)["steps"][0]
    clone = hb.WorkspaceManifest.from_dict(manifest).open(detached=True)
    clone_binding = clone.storage_plans[GramStrategyOptionDoc.schema().entity_id].first("keywords")

    assert binding.strategy == hb.SparseGramIndex.identifier
    assert binding.strategy_options == {"normalizer": "simple", "gram_unit": "token", "tokenizer": "space", "gram_width": 2}
    assert stores == [
        {
            "backend": "main",
            "strategy": {"type": hb.SparseGramIndex.identifier, "normalizer": "simple", "gram_unit": "token", "tokenizer": "space", "gram_width": 2},
            "optional": False,
            "reason": "field",
        }
    ]
    assert rows[0]["match"][0]["keyword"] == "Redis Query"
    assert step["strategy_options"] == binding.strategy_options
    assert clone_binding.strategy_options == binding.strategy_options


def test_sparse_gram_strategy_options_drive_sql_adapter_config():
    ws = _workspace("gram-strategy-normalizer-test")
    ws.register(
        type(
            "GramStrategyNormalizerDoc",
            (hb.Entity,),
            {
                "identifier": "gram-strategy-normalizer-doc",
                "title": hb.field(hb.ShortText),
                "keywords": hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple")),
            },
        )
    )

    entity = ws.entities["gram-strategy-normalizer-doc"]
    ws.upsert(entity, {"object_id": "a", "title": "Alpha", "keywords": ["Redis-Query"]})

    rows = ws.query(entity).where(entity.keywords.contained_in("redis query path")).select("match").execute().rows()

    assert [row["object_id"] for row in rows] == ["a"]
    assert rows[0]["match"][0]["keyword_norm"] == "redis query"


def test_sql_backend_hosts_sparse_gram_strategy_adapter():
    ws = hb.HeavenBase("sql-sparse-gram-adapter-test", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": ":memory:"}})
    ws.register(SQLSparseGramDoc)
    ws.upsert_many(
        SQLSparseGramDoc,
        [
            {"object_id": "a", "title": "Alpha", "keywords": ["Redis Query", "cache"]},
            {"object_id": "b", "title": "Beta", "keywords": ["sparse gram"]},
            {"object_id": "c", "title": "Gamma", "keywords": ["other"]},
        ],
    )

    schema = SQLSparseGramDoc.schema()
    backend = ws.backends.get("main")
    binding = ws.storage_plans[schema.entity_id].first("keywords")
    adapter = backend.strategy_adapter(hb.SparseGramIndex)
    query = ws.query(SQLSparseGramDoc).where(SQLSparseGramDoc.keywords.contained_in("the redis query path"))
    rows = query.select("title", "keywords", "match").execute().rows()
    step = ws.explain(query.spec)["steps"][0]

    assert binding.backend == "main"
    assert binding.strategy == hb.SparseGramIndex.identifier
    assert backend.supports_strategy(schema, binding)
    assert adapter is not None
    assert adapter.to_dict()["identifier"] == "sql_sparse_gram"
    assert adapter.posting_count(schema.entity_id, "keywords") > 0
    gram_index = adapter._need_index()
    assert gram_index.db is backend.db
    posting_index_columns = {tuple(column.name for column in item.columns) for item in gram_index.postings.indexes}
    assert ("ns", "entity_id", "field_name", "gram_hash") in posting_index_columns
    assert all("mask_bit" not in columns for columns in posting_index_columns)
    assert rows[0]["object_id"] == "a"
    assert rows[0]["title"] == "Alpha"
    assert rows[0]["keywords"] == ["Redis Query", "cache"]
    assert rows[0]["match"][0]["keyword"] == "Redis Query"
    assert step["backend"] == "main"
    assert step["type"] == "sqlite"
    assert step["strategy"] == hb.SparseGramIndex.identifier
    assert step["handler_kind"] == "sql_sparse_gram_contained_in"
    assert step["strategy_adapter"] == "sql_sparse_gram"
    assert step["handler_mode"] == "native"

    logical = ws.query(SQLSparseGramDoc).where(SQLSparseGramDoc.keywords.contained_in("redis query") | SQLSparseGramDoc.keywords.contained_in("sparse gram"))
    assert logical.execute().ids == ["a", "b"]
    assert ws.query(SQLSparseGramDoc).where(~SQLSparseGramDoc.keywords.contained_in("redis query")).execute().ids == ["b", "c"]


def test_sql_sparse_gram_adapter_loads_runtime_defaults_lazily(monkeypatch):
    calls = []
    original = GramConfig.load.__func__

    def load(cls, *, manager=hb.CM_HVNB, **overrides):
        calls.append(manager)
        return original(cls, manager=manager, gram_width=7, **overrides)

    monkeypatch.setattr(GramConfig, "load", classmethod(load))
    backend = hb.backends.SQLiteBackend("main", database=":memory:", ws_id="gram-runtime-defaults")
    adapter = backend.strategy_adapter(hb.SparseGramIndex)

    assert calls == []
    assert adapter.config.gram_width == 7
    assert calls == [hb.CM_HVNB]


def test_sql_sparse_gram_adapter_composes_with_sql_native_filters_without_scan(monkeypatch):
    ws = hb.HeavenBase("sql-sparse-gram-mixed-native-test", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": ":memory:"}})

    class MixedNativeDoc(hb.Entity):
        identifier = "sql-sparse-gram-mixed-native-doc"

        title: hb.ShortText
        score: hb.Integer
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))

    ws.register(MixedNativeDoc)
    ws.upsert_many(
        MixedNativeDoc,
        [
            {"object_id": "a", "title": "Alpha", "score": 1, "keywords": ["Redis Query", "cache"]},
            {"object_id": "b", "title": "Beta", "score": 2, "keywords": ["Redis Query"]},
            {"object_id": "c", "title": "Alpha", "score": 1, "keywords": ["other"]},
        ],
    )

    backend = ws.backends.get("main")
    query = ws.query(MixedNativeDoc).where((MixedNativeDoc.score == 1) & MixedNativeDoc.keywords.contained_in("the redis query path"))
    steps = ws.explain(query.spec)["steps"]

    def fail_scan(_entity_id):
        raise AssertionError("mixed SQL-native and strategy-adapter fragments should compose without a backend scan")

    monkeypatch.setattr(backend, "scan", fail_scan)
    rows = query.select("title", "match").execute().rows()

    assert rows == [
        {
            "object_id": "a",
            "title": "Alpha",
            "match": [
                {
                    "field": "keywords",
                    "op": "contained_in",
                    "keyword": "Redis Query",
                    "keyword_norm": "redis query",
                    "query_span": [4, 15],
                    "query_norm_span": [4, 15],
                    "query_text": "redis query",
                    "ordinal": 0,
                    "normalizer": "simple:auto",
                    "precision": "exact",
                }
            ],
        }
    ]
    assert {step["handler_mode"] for step in steps if step.get("node") == "filter"} == {"native"}
    assert any(step.get("strategy_adapter") == "sql_sparse_gram" for step in steps)


def test_sql_sparse_gram_custom_normalizer_survives_mixed_scan_tree():
    """A scan sibling must not replace adapter-owned normalization with a generic predicate."""

    hb.ext.register_gram_normalizer("unit_mixed_compact_dash", _compact_dash)
    ws = hb.HeavenBase("sql-sparse-gram-mixed-scan-test", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": ":memory:"}})

    class MixedScanDoc(hb.Entity):
        identifier = "sql-sparse-gram-mixed-scan-doc"

        title: hb.ShortText
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="unit_mixed_compact_dash"))

    ws.register(MixedScanDoc)
    ws.upsert_many(
        MixedScanDoc,
        [
            {"object_id": "a", "title": "Alpha", "keywords": ["foo-bar"]},
            {"object_id": "b", "title": "Beta", "keywords": ["foobar"]},
        ],
    )

    native = ws.query(MixedScanDoc).where(MixedScanDoc.keywords.contained_in("xx foobar yy"))
    mixed = ws.query(MixedScanDoc).where((MixedScanDoc.title == "Alpha") & MixedScanDoc.keywords.contained_in("xx foobar yy"))

    assert native.execute().ids == ["a", "b"]
    assert mixed.execute().ids == ["a"]
    assert mixed.select("match").execute().rows()[0]["match"][0]["keyword_norm"] == "foobar"


def test_sql_sparse_gram_adapter_failure_rolls_back_sql_row():
    ws = hb.HeavenBase("sql-sparse-gram-rollback-test", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": ":memory:"}})
    ws.register(SQLSparseGramDoc)
    schema = SQLSparseGramDoc.schema()
    backend = ws.backends.get("main")
    adapter = backend.strategy_adapter(hb.SparseGramIndex)
    original_upsert = adapter.upsert

    def fail_upsert(_rows):
        raise RuntimeError("forced gram index failure")

    adapter.upsert = fail_upsert
    try:
        with pytest.raises(RuntimeError, match="forced gram index failure"):
            ws.upsert(SQLSparseGramDoc, {"object_id": "a", "title": "Alpha", "keywords": ["Redis Query"]})
    finally:
        adapter.upsert = original_upsert

    assert backend.count(schema.entity_id) == 0
    assert ws.query(SQLSparseGramDoc).execute().ids == []


def test_sql_backend_sparse_gram_persists_rows_and_postings_across_instances(tmp_path):
    schema = SQLSparseGramDoc.schema()
    database = _db(tmp_path, "sql-sparse-gram-persist.db")
    backend = _sql_sparse_gram_backend(database, "sql-sparse-gram-persist-test")
    backend.upsert(
        [
            RowOp(schema.entity_id, "a", {"object_id": "a", "title": "Alpha", "keywords": ["delta", "cache"]}),
            RowOp(schema.entity_id, "b", {"object_id": "b", "title": "Beta", "keywords": ["other"]}),
        ]
    )
    adapter = backend.strategy_adapter(hb.SparseGramIndex)

    assert adapter._need_index().db is backend.db
    assert adapter.posting_count(schema.entity_id, "keywords") > 0

    reopened = _sql_sparse_gram_backend(database, "sql-sparse-gram-persist-test")
    reopened_adapter = reopened.strategy_adapter(hb.SparseGramIndex)
    frame = _sql_sparse_query(reopened, "find DELTA here")
    rows = frame.rows()
    probe = reopened_adapter.contained_in_probe(schema.entity_id, "keywords", "find DELTA here")
    stats = reopened_adapter.posting_stats(schema.entity_id, "keywords", limit=3)

    assert reopened.count(schema.entity_id) == 2
    assert reopened.get_many(schema.entity_id, ["a", "missing"]) == [
        {"object_id": "a", "title": "Alpha", "keywords": ["delta", "cache"]},
        None,
    ]
    assert frame.metadata_columns == {"match"}
    assert rows[0]["object_id"] == "a"
    assert rows[0]["title"] == "Alpha"
    assert rows[0]["keywords"] == ["delta", "cache"]
    assert rows[0]["match"][0]["keyword"] == "delta"
    assert probe["query_hashes"] > 0
    assert probe["posting_hits"] >= 1
    assert probe["candidates"] == 1
    assert probe["exact_matches"] == 1
    assert stats
    assert stats[0]["posting_count"] >= 1


def test_sql_backend_sparse_gram_updates_and_deletes_canonical_sql_rows(tmp_path):
    schema = SQLSparseGramDoc.schema()
    backend = _sql_sparse_gram_backend(_db(tmp_path, "sql-sparse-gram-update-delete.db"), "sql-sparse-gram-update-delete-test")
    backend.upsert(
        [
            RowOp(schema.entity_id, "a", {"object_id": "a", "title": "Alpha", "keywords": ["redis query"]}),
            RowOp(schema.entity_id, "b", {"object_id": "b", "title": "Beta", "keywords": ["sparse gram"]}),
        ]
    )

    assert _sql_sparse_query(backend, "the redis query path").ids == ["a"]
    assert _sql_sparse_query(backend, "sparse gram index").ids == ["b"]

    backend.upsert([RowOp(schema.entity_id, "a", {"object_id": "a", "title": "Alpha Updated", "keywords": ["vector index"]})])

    updated_rows = _sql_sparse_query(backend, "vector index").rows()
    assert _sql_sparse_query(backend, "the redis query path").ids == []
    assert updated_rows[0]["object_id"] == "a"
    assert updated_rows[0]["title"] == "Alpha Updated"
    assert updated_rows[0]["keywords"] == ["vector index"]
    assert updated_rows[0]["match"][0]["keyword"] == "vector index"

    backend.upsert([RowOp(schema.entity_id, "a", op="delete")])

    assert backend.get_many(schema.entity_id, ["a", "b"]) == [
        None,
        {"object_id": "b", "title": "Beta", "keywords": ["sparse gram"]},
    ]
    assert _sql_sparse_query(backend, "vector index").ids == []
    assert _sql_sparse_query(backend, "sparse gram index").ids == ["b"]


@pytest.mark.full
def test_sql_sparse_gram_adapter_rebuilds_on_strategy_fingerprint_mismatch(tmp_path):
    schema = SQLSparseGramDoc.schema()
    database = _db(tmp_path, "sql-sparse-gram-fingerprint.db")

    def backend_with(options: dict[str, object]):
        return _sql_sparse_gram_backend(database, "sql-sparse-gram-fingerprint-test", options)

    def query_ids(backend) -> list[str]:
        return _sql_sparse_query(backend, "the redis query path").ids

    backend = backend_with({"normalizer": "simple"})
    backend.upsert([RowOp(schema.entity_id, "a", {"object_id": "a", "title": "Alpha", "keywords": ["Redis Query"]})])
    assert query_ids(backend) == ["a"]

    reopened = backend_with({"normalizer": "simple", "gram_unit": "token", "tokenizer": "space", "gram_width": 2})
    adapter = reopened.strategy_adapter(hb.SparseGramIndex)

    assert reopened.count(schema.entity_id) == 1
    assert adapter.posting_count(schema.entity_id, "keywords") > 0
    assert query_ids(reopened) == ["a"]


@pytest.mark.fast
def test_sql_backend_skips_adapter_maintenance_for_rows_that_opt_out(tmp_path):
    """A ``RowOp`` with ``strategies=False`` writes the canonical row without touching keyword tables."""
    backend = _sql_sparse_gram_backend(_db(tmp_path, "gram-opt-out.db"), "gram-opt-out")
    schema = SQLSparseGramDoc.schema()
    adapter = backend.strategy_adapter(hb.SparseGramIndex)

    backend.upsert([RowOp(schema.entity_id, "a", {"title": "Indexed", "keywords": ["redis query engine"]})])
    backend.upsert([RowOp(schema.entity_id, "b", {"title": "Bare", "keywords": []}, strategies=False)])
    assert [row["object_id"] for row in backend.scan(schema.entity_id)] == ["a", "b"]
    assert adapter.posting_count(schema.entity_id, "keywords") > 0
    assert _sql_sparse_query(backend, "the redis query engine").ids == ["a"]

    postings = adapter.posting_count(schema.entity_id, "keywords")
    backend.upsert([RowOp(schema.entity_id, "b", op="delete", strategies=False)])
    assert adapter.posting_count(schema.entity_id, "keywords") == postings
    assert [row["object_id"] for row in backend.scan(schema.entity_id)] == ["a"]


@pytest.mark.fast
def test_sql_sparse_gram_adapter_prunes_unbound_fields_and_rebuilds_them_from_rows(tmp_path):
    """Ensuring without the keyword binding keeps its index; pruning to those bindings drops it; binding it again rebuilds from stored rows."""
    database = _db(tmp_path, "gram-prune.db")
    backend = _sql_sparse_gram_backend(database, "gram-prune")
    schema = SQLSparseGramDoc.schema()
    adapter = backend.strategy_adapter(hb.SparseGramIndex)
    backend.upsert([RowOp(schema.entity_id, "a", {"title": "Indexed", "keywords": ["redis query engine"]})])
    assert adapter.posting_count(schema.entity_id, "keywords") > 0

    inline = [
        StorageBinding(schema.entity_id, "object_id", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "title", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "keywords", "main", hb.InlineColumn.identifier, "test"),
    ]
    backend.ensure(schema, inline)
    # ensure only adds storage: the registration that binds the index stays served until the new one takes effect
    assert _sql_sparse_query(backend, "the redis query engine").ids == ["a"]
    backend.prune(schema, inline)
    assert adapter.posting_count(schema.entity_id, "keywords") == 0
    backend.upsert([RowOp(schema.entity_id, "b", {"title": "Unindexed", "keywords": ["embedding search"]})])
    assert adapter.posting_count(schema.entity_id, "keywords") == 0

    reopened = _sql_sparse_gram_backend(database, "gram-prune")
    assert reopened.strategy_adapter(hb.SparseGramIndex).posting_count(schema.entity_id, "keywords") > 0
    assert _sql_sparse_query(reopened, "the embedding search request").ids == ["b"]
    assert _sql_sparse_query(reopened, "the redis query engine").ids == ["a"]


def test_pruning_an_entity_that_never_had_an_index_creates_no_gram_tables(tmp_path):
    """Registering only inline keyword fields on a fresh backend leaves no sparse-GRAM table or statement behind."""
    from sqlalchemy import event

    schema = SQLSparseGramDoc.schema()
    backend = hb.backends.SQLiteBackend("main", database=_db(tmp_path, "gram-untouched.db"), ws_id="gram-untouched")
    statements = []
    event.listen(backend.db.engine, "before_cursor_execute", lambda conn, cursor, statement, *_: statements.append(statement))
    inline = [
        StorageBinding(schema.entity_id, "object_id", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "title", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "keywords", "main", hb.InlineColumn.identifier, "test"),
    ]

    backend.ensure(schema, inline)
    backend.prune(schema, inline)
    backend.upsert([RowOp(schema.entity_id, "a", {"title": "Inline", "keywords": ["redis query engine"]})])
    backend.ensure(schema, inline)
    backend.prune(schema, inline)

    assert [row["keywords"] for row in backend.scan(schema.entity_id)] == [["redis query engine"]]
    # a write looks up whether another registration created the index tables since, and runs nothing else on them
    assert not [statement for statement in statements if "hb_gram" in statement and not statement.startswith("PRAGMA")]
    assert not [name for name in backend.db.tables() if name.startswith("hb_gram")]
    # binding the field later builds the index exactly as before
    backend.ensure(schema, inline[:2] + [StorageBinding(schema.entity_id, "keywords", "main", hb.SparseGramIndex.identifier, "test", {"normalizer": "simple"})])
    assert _sql_sparse_query(backend, "the redis query engine").ids == ["a"]


@pytest.mark.fast
def test_catalog_keywords_route_to_sql_sparse_gram_and_return_match_metadata():
    ws = _workspace("gram-catalog-smoke-test")
    ws.register(GramCatalogSource)
    ws.upsert_many(
        GramCatalogSource,
        [
            {"object_id": "a", "name": "Redis", "keywords": ["redis query engine", "cache"]},
            {"object_id": "b", "name": "Vector", "keywords": ["embedding search"]},
        ],
    )

    plan = ws.storage_plans[hb.Catalog.schema().entity_id]
    rows = (
        ws.query(hb.Catalog)
        .where(hb.Catalog.keywords.contained_in("Mentioned Redis Query Engine in the request"))
        .select("target_id", "match")
        .execute()
        .rows()
    )
    step = ws.explain(ws.query(hb.Catalog).where(hb.Catalog.keywords.contained_in("Mentioned Redis Query Engine in the request")).spec)["steps"][0]

    assert plan.first("keywords").backend == "main"
    assert step["handler_kind"] == "sql_sparse_gram_contained_in"
    assert step["strategy_adapter"] == "sql_sparse_gram"
    assert [row["target_id"] for row in rows] == ["a"]
    assert rows[0]["match"][0]["keyword"] == "redis query engine"
    assert rows[0]["match"][0]["query_text"] == "Redis Query Engine"
    assert rows[0]["match"][0]["precision"] == "exact"


def test_sql_sparse_gram_exact_verification_filters_hash_collisions():
    ws = _workspace("gram-collision-test")
    entity = type(
        "SQLSparseGramCollisionDoc",
        (hb.Entity,),
        {
            "identifier": "sql-sparse-gram-collision-doc",
            "title": hb.field(hb.ShortText),
            "keywords": hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple", hash_bits=1)),
        },
    )
    ws.register(entity)
    ws.upsert_many(
        entity,
        [
            {"object_id": "a", "title": "Alpha", "keywords": ["ab"]},
            {"object_id": "b", "title": "Beta", "keywords": ["cd"]},
        ],
    )

    assert ws.query(entity).where(entity.keywords.contained_in("zabz")).execute().ids == ["a"]


def test_sparse_gram_candidates_cover_short_and_multi_character_keywords():
    config = GramConfig.load(normalizer="simple", sparse_grams_per_keyword=0)

    assert [part.gram for part in keyword_grams("ab", config)] == ["ab"]
    assert {part.gram_hash for part in keyword_grams("ab", config)} <= query_grams("zab", config, short_widths={2})
    assert [part.gram for part in keyword_grams("北京", config)] == ["北京"]
    assert {part.gram_hash for part in keyword_grams("北京", config)} <= query_grams("我喜欢北京烤鸭", config, short_widths={2})

    keyword_parts = keyword_grams("abcdef", config)

    assert keyword_parts
    assert all(len(part.gram) >= config.gram_width for part in keyword_parts)
    assert {part.gram_hash for part in keyword_parts} <= query_grams("zabcdefz", config, short_widths=set())


@pytest.mark.full
def test_sparse_gram_can_limit_preserved_postings_without_losing_recall():
    config = GramConfig.load(normalizer="simple", sparse_grams_per_keyword=2)
    keyword_parts = keyword_grams("redis query engine", config)

    assert len(keyword_parts) == 2
    assert all(len(part.gram) >= config.gram_width for part in keyword_parts)
    assert {part.gram_hash for part in keyword_parts} <= query_grams("the redis query engine path", config, short_widths=set())


def test_sparse_gram_tokenizer_supports_token_level_grams():
    config = GramConfig.load(normalizer="simple", gram_unit="token", tokenizer="space", gram_width=2)

    keyword_parts = keyword_grams("redis query engine", config)

    assert keyword_parts
    assert all("\x1f" in part.gram for part in keyword_parts)
    assert {part.gram_hash for part in keyword_parts} <= query_grams("the redis query engine path", config, short_widths=set())
    assert {part.gram_hash for part in keyword_grams("redis", config)} <= query_grams("redis path", config, short_widths={1})


def test_custom_gram_tokenizer_registered_through_ext():
    hb.ext.register_gram_tokenizer("unit_dash_tokens", _dash_tokens)
    config = GramConfig.load(normalizer="none", gram_unit="token", tokenizer="unit_dash_tokens", gram_width=2)
    keyword_parts = keyword_grams("redis-query", config)

    assert hb.ext.resolve_gram_tokenizer(config) is _dash_tokens
    assert {part.gram_hash for part in keyword_parts} <= query_grams("the-redis-query-path", config, short_widths={2})


def test_sparse_gram_strategy_accepts_direct_tokenizer_callable():
    def dash_tokens(text, _config):
        return [item for item in str(text).split("-") if item]

    config = GramConfig.load(normalizer="none", gram_unit="token", tokenizer=dash_tokens, gram_width=2)
    keyword_parts = keyword_grams("redis-query", config)

    assert resolve_gram_tokenizer(config) is dash_tokens
    assert {part.gram_hash for part in keyword_parts} <= query_grams("the-redis-query-path", config, short_widths={2})


def test_result_frame_metadata_columns_merge_without_hard_coded_match_column():
    left = ResultFrame.from_rows([{"object_id": "a", "tags": ["left"], "evidence": [{"source": "left"}]}], metadata_columns={"evidence"})
    right = ResultFrame.from_rows([{"object_id": "a", "tags": ["right"], "evidence": [{"source": "right"}]}], metadata_columns={"evidence"})

    frame = union([left, right])

    assert frame.metadata_columns == {"evidence"}
    assert frame.rows() == [{"object_id": "a", "evidence": [{"source": "left"}, {"source": "right"}], "tags": ["right"]}]


def test_sparse_gram_default_normalizer_is_language_neutral():
    """Implicit SparseGramIndex must not select spaCy auto normalization by default."""
    from heavenbase.backends.sql.strategies.gram.config import GramConfig

    assert GramConfig().normalizer == "default"
    assert GramConfig.load().normalizer == "default"
    assert GramConfig.load().normalizer_id() == "default:auto"


@pytest.mark.fast
def test_sql_sparse_gram_binds_backend_dialect_spec():
    """SparseGram internal SQL types must use the Backend dialect, not ambient defaults."""
    from heavenbase.backends.sql.strategies.sparse_gram import _SQLGramIndex

    backend = hb.backends.SQLiteBackend("main", database=":memory:", ws_id="gram-dialect-authority")
    index = _SQLGramIndex(backend, namespace="gram-dialect-authority")
    for column in (index.meta.c.ns, index.meta.c.value, index.keywords.c.keyword, index.postings.c.gram_hash):
        assert getattr(column.type, "dialect_spec", None) is backend.db.dialect_spec
    index.ensure()
    backend.drop()


def _open_shared(tmp_path, ws_id):
    return hb.HeavenBase(ws_id, detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": _db(tmp_path, "shared.db")}})


def _contained(ws, text):
    return ws.query(GramNativeDoc).where(GramNativeDoc.keywords.contained_in(text)).execute().ids


def _gram_namespaces(path) -> dict[str, set[str]]:
    import sqlite3

    with sqlite3.connect(path) as connection:
        return {
            table: {row[0] for row in connection.execute(f"SELECT DISTINCT ns FROM {table}")}
            for table in ("hb_gram_meta", "hb_gram_keywords", "hb_gram_postings")
        }


@pytest.mark.slow
def test_two_workspaces_on_one_sql_database_share_one_gram_index(tmp_path):
    """Workspaces share entity tables on one database, so a write through either keeps the other's contained_in right."""
    first = _open_shared(tmp_path, "gram-shared-a")
    first.register(GramNativeDoc)
    first.upsert(GramNativeDoc, {"object_id": "x", "title": "X", "keywords": ["needle"]})
    second = _open_shared(tmp_path, "gram-shared-b")
    try:
        second.register(GramNativeDoc)
        assert _contained(second, "find the needle here") == ["x"]
        second.upsert(GramNativeDoc, {"object_id": "x", "title": "X", "keywords": ["haystack"]})
        second.upsert(GramNativeDoc, {"object_id": "y", "title": "Y", "keywords": ["needle"]})
        for ws in (first, second):
            assert _contained(ws, "find the needle here") == ["y"]
            assert _contained(ws, "a haystack here") == ["x"]
    finally:
        second.release()
        first.release()
    reopened = _open_shared(tmp_path, "gram-shared-a")
    try:
        reopened.register(GramNativeDoc)
        assert _contained(reopened, "find the needle here") == ["y"] and _contained(reopened, "a haystack here") == ["x"]
    finally:
        reopened.release()
    assert _gram_namespaces(tmp_path / "shared.db") == {
        "hb_gram_meta": {"sql_sparse_gram", "gram-shared-a:main:sql_sparse_gram"},
        "hb_gram_keywords": {"sql_sparse_gram"},
        "hb_gram_postings": {"sql_sparse_gram"},
    }


class GramRemovableDoc(hb.Entity):
    identifier = "gram-removable-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText])


class GramRemovableDocWithoutKeywords(hb.Entity):
    identifier = "gram-removable-doc"

    title: hb.ShortText


class GramRemovableDocWithIntegerKeywords(hb.Entity):
    identifier = "gram-removable-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.Integer])


@pytest.mark.slow
@pytest.mark.parametrize("unbound", [GramRemovableDocWithoutKeywords, GramRemovableDocWithIntegerKeywords], ids=["removed", "retyped"])
def test_a_field_removed_or_retyped_in_another_process_is_rebuilt_when_bound_again(tmp_path, unbound):
    """A process that registers the entity without the indexed field drops its index, so binding the field later rebuilds it from rows."""

    def stage(entity, upsert=()):
        ws = _open_shared(tmp_path, "gram-removable")
        try:
            ws.register(entity)
            ws.upsert_many(entity, list(upsert))
            if entity is not GramRemovableDoc:
                return None
            hits = {text: ws.query(entity).where(entity.keywords.contained_in(text)).execute().ids for text in ("the redis query engine", "a vector store")}
            return ws.read_rows(entity, ["a", "b"]), hits
        finally:
            ws.release()

    indexed = [{"object_id": "a", "title": "A", "keywords": ["redis query engine"]}, {"object_id": "b", "title": "B", "keywords": ["vector store"]}]
    stage(GramRemovableDoc, indexed)
    stage(unbound, [{"object_id": "a", "title": "A2"}])
    rows, hits = stage(GramRemovableDoc)

    assert [(row["object_id"], row["keywords"]) for row in rows] == [("a", None), ("b", ["vector store"])]
    assert hits == {"the redis query engine": [], "a vector store": ["b"]}


def _act_as_0_1_2_2(patch):
    """Make the adapter index as HeavenBase 0.1.2.2 did, under a per-workspace namespace."""
    from heavenbase.backends.sql.strategies.sparse_gram import SQLSparseGramAdapter, _SQLGramIndex

    def ensure_fingerprint(self, entity_id, field_name, config, **_registration):
        # 0.1.2.2 rebuilt a field only when its recorded fingerprint differed, checked it once per field and
        # connection, and kept no retirement step and no per-query check.
        key = self._fingerprint_key(entity_id, field_name)
        checked = self.__dict__.setdefault("_checked_as_0_1_2_2", set())
        if key in checked:
            return
        index = self._need_index()
        fingerprint = config.fingerprint()
        existing = index.get_meta(key)
        if existing and existing != fingerprint:
            self._rebuild_field_index(entity_id, field_name)
        index.set_meta(key, fingerprint)
        checked.add(key)

    patch.setattr(SQLSparseGramAdapter, "_namespace", SQLSparseGramAdapter._workspace_namespace)
    patch.setattr(_SQLGramIndex, "_stored_keyword_id", lambda self, keyword_id: keyword_id)
    patch.setattr(SQLSparseGramAdapter, "_ensure_fingerprint", ensure_fingerprint)


def _gram_meta(path, ns: str) -> dict[str, str]:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(path)) as connection:
        return {row[0]: row[1] for row in connection.execute("SELECT key, value FROM hb_gram_meta WHERE ns = ?", (ns,))}


def _stage(tmp_path, ws_id, *, upsert=None, contained=None):
    ws = _open_shared(tmp_path, ws_id)
    try:
        ws.register(GramNativeDoc)
        if upsert is not None:
            ws.upsert(GramNativeDoc, upsert)
        return None if contained is None else _contained(ws, contained)
    finally:
        ws.release()


@pytest.mark.slow
def test_sql_sparse_gram_retires_a_per_workspace_index_written_by_0_1_2_2(tmp_path, monkeypatch):
    """The index of the only workspace 0.1.2.2 indexed is rebuilt under the shared namespace, then deleted and marked."""
    from heavenbase.backends.sql.strategies.sparse_gram import _RETIRED_FINGERPRINT

    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        _stage(tmp_path, "gram-legacy-a", upsert={"object_id": "x", "title": "X", "keywords": ["needle"]})
        assert _stage(tmp_path, "gram-legacy-a", contained="find the needle here") == ["x"]
    _stage(tmp_path, "gram-legacy-new", upsert={"object_id": "y", "title": "Y", "keywords": ["needle"]})

    assert _stage(tmp_path, "gram-legacy-a", contained="find the needle here") == ["x", "y"]
    namespaces = _gram_namespaces(tmp_path / "shared.db")
    assert namespaces["hb_gram_keywords"] == namespaces["hb_gram_postings"] == {"sql_sparse_gram"}
    # The retired namespace keeps only marker fingerprints (the Catalog entity indexes keywords too).
    assert set(_gram_meta(tmp_path / "shared.db", "gram-legacy-a:main:sql_sparse_gram").values()) == {_RETIRED_FINGERPRINT}
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        # 0.1.2.2 sees a changed fingerprint and rebuilds its index from every stored row
        assert _stage(tmp_path, "gram-legacy-a", contained="find the needle here") == ["x", "y"]


@pytest.mark.slow
def test_sql_sparse_gram_keeps_the_indexes_of_several_0_1_2_2_workspaces_so_each_reopens_there(tmp_path, monkeypatch):
    """0.1.2.2 keyword ids leave out the workspace, so two of its indexes cannot both hold every row; both are kept as they were."""
    path = tmp_path / "shared.db"
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        # Two workspaces with disjoint rows could share one database there without colliding.
        _stage(tmp_path, "gram-several-a", upsert={"object_id": "x", "title": "X", "keywords": ["needle"]})
        _stage(tmp_path, "gram-several-b", upsert={"object_id": "y", "title": "Y", "keywords": ["needle"]})
    before = _gram_namespaces(path)["hb_gram_keywords"]
    assert before == {"gram-several-a:main:sql_sparse_gram", "gram-several-b:main:sql_sparse_gram"}

    assert _stage(tmp_path, "gram-several-a", contained="find the needle here") == ["x", "y"]
    assert _stage(tmp_path, "gram-several-c", contained="find the needle here") == ["x", "y"]
    assert _gram_namespaces(path)["hb_gram_keywords"] == before | {"sql_sparse_gram"}
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        # each workspace reopens with the index it had; neither rebuilds over the other's keyword ids
        assert _stage(tmp_path, "gram-several-a", contained="find the needle here") == ["x"]
        assert _stage(tmp_path, "gram-several-b", contained="find the needle here") == ["y"]
        _stage(tmp_path, "gram-several-b", upsert={"object_id": "y", "title": "Y", "keywords": ["thread"]})
        _stage(tmp_path, "gram-several-a", upsert={"object_id": "z", "title": "Z", "keywords": ["needle"]})
    # rows 0.1.2.2 indexed or replaced since reach the shared index at the next registration
    assert _stage(tmp_path, "gram-several-c", contained="find the needle here") == ["x", "z"]
    assert _stage(tmp_path, "gram-several-c", contained="a thread here") == ["y"]


@pytest.mark.slow
def test_sql_sparse_gram_stops_retiring_once_a_second_0_1_2_2_workspace_indexes_the_field(tmp_path, monkeypatch):
    """A retired index whose database a second 0.1.2.2 workspace later indexes loses its marker, so 0.1.2.2 opens both."""
    from heavenbase.backends.sql.strategies.sparse_gram import _RETIRED_FINGERPRINT

    path = tmp_path / "shared.db"
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        _stage(tmp_path, "gram-second-a", upsert={"object_id": "x", "title": "X", "keywords": ["needle"]})
    assert _stage(tmp_path, "gram-second-a", contained="find the needle here") == ["x"]
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        _stage(tmp_path, "gram-second-b", upsert={"object_id": "y", "title": "Y", "keywords": ["needle"]})
    assert _stage(tmp_path, "gram-second-c", contained="find the needle here") == ["x", "y"]
    assert _RETIRED_FINGERPRINT not in _gram_meta(path, "gram-second-a:main:sql_sparse_gram").values()
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        # the retired index stays empty in 0.1.2.2 instead of rebuilding over the second workspace's keyword ids
        assert _stage(tmp_path, "gram-second-a", contained="find the needle here") == []
        assert _stage(tmp_path, "gram-second-b", contained="find the needle here") == ["y"]


@pytest.mark.slow
def test_sql_sparse_gram_rebuilds_for_rows_0_1_2_2_indexed_while_this_version_stays_open(tmp_path, monkeypatch):
    """Every registration looks for rows 0.1.2.2 indexed, so registering again in an open process finds them."""
    opened = _open_shared(tmp_path, "gram-open")
    try:
        opened.register(GramNativeDoc)
        opened.upsert(GramNativeDoc, {"object_id": "x", "title": "X", "keywords": ["needle"]})
        with monkeypatch.context() as patch:
            _act_as_0_1_2_2(patch)
            _stage(tmp_path, "gram-open-old", upsert={"object_id": "y", "title": "Y", "keywords": ["needle"]})
        assert _contained(opened, "find the needle here") == ["x"]  # not found before the next registration
        opened.register(GramNativeDoc, allow_replace=True)
        assert _contained(opened, "find the needle here") == ["x", "y"]
    finally:
        opened.release()


@pytest.mark.slow
def test_sql_sparse_gram_rebuilds_both_indexes_when_0_1_2_2_and_this_version_take_turns(tmp_path, monkeypatch):
    """Rows that 0.1.2.2 indexes after this version retired its index reach the shared index, and 0.1.2.2 rebuilds its own."""
    from heavenbase.backends.sql.strategies.sparse_gram import _RETIRED_FINGERPRINT

    path = tmp_path / "shared.db"
    legacy_ns = "gram-turns:main:sql_sparse_gram"
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        _stage(tmp_path, "gram-turns", upsert={"object_id": "x", "title": "X", "keywords": ["needle"]})
    assert _stage(tmp_path, "gram-turns", contained="find the needle here") == ["x"]
    assert set(_gram_meta(path, legacy_ns).values()) == {_RETIRED_FINGERPRINT}

    # A downgrade: 0.1.2.2 sees a changed fingerprint, rebuilds its deleted index from stored rows, then writes.
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        assert _stage(tmp_path, "gram-turns", contained="find the needle here") == ["x"]
        _stage(tmp_path, "gram-turns", upsert={"object_id": "z", "title": "Z", "keywords": ["needle"]})
    assert sorted(_stage(tmp_path, "gram-turns", contained="find the needle here")) == ["x", "z"]

    # A 0.1.2.2 process that stayed open writes keyword rows without restoring its fingerprint.
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        _stage(tmp_path, "gram-turns", upsert={"object_id": "q", "title": "Q", "keywords": ["needle"]})
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE hb_gram_meta SET value = ? WHERE ns = ?", (_RETIRED_FINGERPRINT, legacy_ns))
        connection.commit()
    assert sorted(_stage(tmp_path, "gram-turns", contained="find the needle here")) == ["q", "x", "z"]
    assert _gram_namespaces(path)["hb_gram_keywords"] == {"sql_sparse_gram"}


@pytest.mark.slow
def test_sql_sparse_gram_finds_a_short_keyword_another_backend_indexed_after_a_query(tmp_path):
    """Short keyword widths are read per query, so a width that another backend instance indexed later is not missed."""
    first = _open_shared(tmp_path, "gram-width-a")
    first.register(GramNativeDoc)
    first.upsert(GramNativeDoc, {"object_id": "x", "title": "X", "keywords": ["needle"]})
    second = _open_shared(tmp_path, "gram-width-b")
    try:
        second.register(GramNativeDoc)
        assert _contained(first, "find the needle here") == ["x"]
        second.upsert(GramNativeDoc, {"object_id": "z", "title": "Z", "keywords": ["ab"]})
        assert _contained(first, "ab here") == ["z"] and _contained(second, "ab here") == ["z"]
    finally:
        second.release()
        first.release()


@pytest.mark.slow
def test_sql_sparse_gram_retires_the_0_1_2_2_index_only_after_the_rebuild_commits(tmp_path, monkeypatch):
    """An interrupted rebuild leaves the 0.1.2.2 index in place, so the next open rebuilds and finds its rows."""
    import sqlite3

    from heavenbase.backends.sql.strategies.sparse_gram import SQLSparseGramAdapter

    _stage(tmp_path, "gram-crash", upsert={"object_id": "x", "title": "X", "keywords": ["needle"]})
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        _stage(tmp_path, "gram-crash", upsert={"object_id": "y", "title": "Y", "keywords": ["needle"]})
    real_rebuild = SQLSparseGramAdapter._rebuild_field_index

    def interrupted(self, entity_id, field_name, **kwargs):
        if entity_id == GramNativeDoc.identifier:
            raise sqlite3.OperationalError("database is locked")
        return real_rebuild(self, entity_id, field_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(SQLSparseGramAdapter, "_rebuild_field_index", interrupted)
        with pytest.raises(sqlite3.OperationalError):
            _stage(tmp_path, "gram-crash")
    assert "gram-crash:main:sql_sparse_gram" in _gram_namespaces(tmp_path / "shared.db")["hb_gram_keywords"]
    assert _stage(tmp_path, "gram-crash", contained="find the needle here") == ["x", "y"]
    assert _gram_namespaces(tmp_path / "shared.db")["hb_gram_keywords"] == {"sql_sparse_gram"}


def _gram_doc(width: int):
    class GramWidthDoc(hb.Entity):
        identifier = "gram-width-doc"

        title: hb.ShortText
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple", gram_width=width))

    return GramWidthDoc


def _width_contained(ws, entity, text):
    return ws.query(entity).where(entity.keywords.contained_in(text)).execute().ids


@pytest.mark.slow
def test_sql_sparse_gram_refuses_queries_and_writes_once_another_workspace_rebuilt_the_index_with_other_options(tmp_path):
    """The shared index holds one set of options; a backend whose registration no longer matches it raises instead of missing rows."""
    from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError

    narrow, wide = _gram_doc(3), _gram_doc(5)
    first = _open_shared(tmp_path, "gram-options-a")
    second = _open_shared(tmp_path, "gram-options-b")
    try:
        first.register(narrow)
        first.upsert_many(narrow, [{"object_id": "a1", "title": "A", "keywords": ["blue whale"]}, {"object_id": "a2", "title": "B", "keywords": ["red fox"]}])
        assert _width_contained(first, narrow, "the blue whale swims") == ["a1"]
        second.register(wide)
        assert _width_contained(second, wide, "the blue whale swims") == ["a1"]
        with pytest.raises(SparseGramIndexConflictError, match="other SparseGramIndex options"):
            _width_contained(first, narrow, "a red fox")
        with pytest.raises(SparseGramIndexConflictError):
            first.upsert(narrow, {"object_id": "a3", "title": "C", "keywords": ["green turtle"]})
        assert second.get("a3", entity=wide) is None  # the refused write left no row behind
        second.upsert(wide, {"object_id": "a3", "title": "C", "keywords": ["green turtle"]})
        assert _width_contained(second, wide, "one green turtle") == ["a3"]
        # Registering again rebuilds the index with this registration's options; now the other one is refused.
        first.register(narrow, allow_replace=True)
        assert _width_contained(first, narrow, "a red fox") == ["a2"] and _width_contained(first, narrow, "one green turtle") == ["a3"]
        with pytest.raises(SparseGramIndexConflictError):
            _width_contained(second, wide, "a red fox")
    finally:
        second.release()
        first.release()


@pytest.mark.slow
def test_sql_sparse_gram_refuses_queries_once_another_workspace_stopped_indexing_the_field(tmp_path):
    """Registering the entity without the keyword index elsewhere deletes the shared index; a backend that still binds it raises."""
    from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError

    class PlainGramNativeDoc(hb.Entity):
        identifier = GramNativeDoc.identifier

        title: hb.ShortText
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main")

    indexed = _open_shared(tmp_path, "gram-unbound-a")
    plain = _open_shared(tmp_path, "gram-unbound-b")
    try:
        indexed.register(GramNativeDoc)
        indexed.upsert(GramNativeDoc, {"object_id": "b1", "title": "B", "keywords": ["blue whale"]})
        assert _contained(indexed, "the blue whale swims") == ["b1"]
        plain.register(PlainGramNativeDoc, gram=False, allow_replace=True)
        with pytest.raises(SparseGramIndexConflictError, match="deleted it"):
            _contained(indexed, "the blue whale swims")
        indexed.register(GramNativeDoc, allow_replace=True)
        assert _contained(indexed, "the blue whale swims") == ["b1"]
    finally:
        plain.release()
        indexed.release()


@pytest.mark.slow
def test_sql_sparse_gram_refuses_queries_and_writes_once_another_workspace_registers_the_entity_without_the_field(tmp_path):
    """A definition without the indexed field retires the shared index like gram=False: neither side silently misses rows."""
    from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError

    class BareGramNativeDoc(hb.Entity):
        identifier = GramNativeDoc.identifier

        title: hb.ShortText

    indexed = _open_shared(tmp_path, "gram-fieldless-a")
    bare = _open_shared(tmp_path, "gram-fieldless-b")
    try:
        indexed.register(GramNativeDoc)
        indexed.upsert(GramNativeDoc, {"object_id": "x", "title": "X", "keywords": ["needle"]})
        assert _contained(indexed, "find the needle here") == ["x"]
        bare.register(BareGramNativeDoc)
        with pytest.raises(SparseGramIndexConflictError, match="deleted it"):
            _contained(indexed, "find the needle here")
        assert [row["keywords"] for row in indexed.read_rows(GramNativeDoc, ["x"])] == [["needle"]]
        indexed.register(GramNativeDoc, allow_replace=True)  # rebuilds the shared index from stored rows
        assert _contained(indexed, "find the needle here") == ["x"]
        with pytest.raises(SparseGramIndexConflictError, match="without the field"):
            bare.delete((BareGramNativeDoc, "x"))
        assert _contained(indexed, "find the needle here") == ["x"]
    finally:
        bare.release()
        indexed.release()


class GramOptionalDoc(hb.Entity):
    identifier = "gram-optional-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText])


@pytest.mark.slow
@pytest.mark.parametrize("write", ["upsert", "delete"])
def test_sql_sparse_gram_refuses_writes_without_the_index_once_another_workspace_indexes_the_field(tmp_path, write):
    """A backend that registered the field without the index raises on a write once another registration indexes it."""
    from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError

    def contained(ws):
        return ws.query(GramOptionalDoc).where(GramOptionalDoc.keywords.contained_in("find the needle here")).execute().ids

    plain = _open_shared(tmp_path, "gram-unindexed-a")
    indexed = _open_shared(tmp_path, "gram-unindexed-b")
    try:
        plain.register(GramOptionalDoc, gram=False)
        plain.upsert(GramOptionalDoc, {"object_id": "x", "title": "X", "keywords": ["needle"]})
        indexed.register(GramOptionalDoc, gram=True, allow_replace=True)  # builds the shared index from stored rows
        assert contained(indexed) == ["x"]
        with pytest.raises(SparseGramIndexConflictError, match="without the index"):
            if write == "upsert":
                plain.upsert(GramOptionalDoc, {"object_id": "y", "title": "Y", "keywords": ["needle"]})
            else:
                plain.delete((GramOptionalDoc, "x"))
        assert indexed.get("y", entity=GramOptionalDoc) is None and indexed.get("x", entity=GramOptionalDoc) is not None
        indexed.upsert(GramOptionalDoc, {"object_id": "y", "title": "Y", "keywords": ["needle"]})
        assert contained(indexed) == ["x", "y"]
        # registering without the index again deletes the shared index, so the plain backend writes again
        plain.register(GramOptionalDoc, gram=False, allow_replace=True)
        plain.upsert(GramOptionalDoc, {"object_id": "z", "title": "Z", "keywords": ["needle"]})
        with pytest.raises(SparseGramIndexConflictError, match="deleted it"):
            contained(indexed)
    finally:
        indexed.release()
        plain.release()


@pytest.mark.slow
def test_sql_sparse_gram_rebuild_keeps_a_row_another_backend_writes_while_it_reads_the_stored_rows(tmp_path):
    """The rebuild holds the write lock while it reads the stored rows, so a concurrent indexed write waits and stays indexed."""
    import threading

    first = _open_shared(tmp_path, "gram-rebuild-a")
    second = _open_shared(tmp_path, "gram-rebuild-b")
    try:
        first.register(GramNativeDoc)
        second.register(GramNativeDoc)
        first.upsert(GramNativeDoc, {"object_id": "x", "title": "X", "keywords": ["needle"]})
        backend = first.backends.get("main")
        adapter = backend.strategy_adapter(hb.SparseGramIndex)
        writer = threading.Thread(target=lambda: second.upsert(GramNativeDoc, {"object_id": "y", "title": "Y", "keywords": ["needle"]}))
        real_scan = type(backend).scan

        def scan_while_another_backend_writes(self, entity_id):
            rows = real_scan(self, entity_id)
            if entity_id == GramNativeDoc.identifier and not writer.is_alive() and writer.ident is None:
                writer.start()
                writer.join(timeout=1.0)  # the write waits for the rebuild's transaction to end
            return rows

        fingerprint = adapter._fingerprints[adapter._fingerprint_key(GramNativeDoc.identifier, "keywords")]
        backend.scan = scan_while_another_backend_writes.__get__(backend)
        try:
            adapter._rebuild_field_index(
                GramNativeDoc.identifier, "keywords", meta={adapter._fingerprint_key(GramNativeDoc.identifier, "keywords"): fingerprint}
            )
        finally:
            del backend.scan
        writer.join(timeout=30)
        assert not writer.is_alive()
        assert _contained(first, "find the needle here") == ["x", "y"]
    finally:
        second.release()
        first.release()


def _fail_first_call(patch, owner, name):
    """Make the next call of ``owner.name`` raise ``OSError`` and every later call behave normally."""
    original = getattr(owner, name)
    calls = []

    def failing(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("disk full")
        return original(*args, **kwargs)

    patch.setattr(owner, name, failing)


_NATIVE_KEY = f"fingerprint:{GramNativeDoc.identifier}:keywords"


@pytest.mark.slow
def test_sql_sparse_gram_failed_publication_rolls_back_a_legacy_rebuild(tmp_path, monkeypatch):
    """A failed publication preserves the previous shared index and the legacy writes for a later rebuild."""
    live = _open_shared(tmp_path, "gram-legacy-live")
    other = _open_shared(tmp_path, "gram-legacy-other")
    try:
        live.register(GramNativeDoc)
        live.upsert(GramNativeDoc, {"object_id": "x", "title": "X", "keywords": ["needle"]})
        fingerprint = _gram_meta(tmp_path / "shared.db", "sql_sparse_gram")[_NATIVE_KEY]
        with monkeypatch.context() as patch:
            _act_as_0_1_2_2(patch)
            old = _open_shared(tmp_path, "gram-legacy-old")
            old.register(GramNativeDoc)
            old.upsert(GramNativeDoc, {"object_id": "y", "title": "Y", "keywords": ["needle"]})
            old.release()
        with monkeypatch.context() as patch:
            _fail_first_call(patch, other._metaschema, "publish_schema_rows")
            with pytest.raises(OSError, match="disk full"):
                other.register(GramNativeDoc)
        assert _gram_meta(tmp_path / "shared.db", "sql_sparse_gram")[_NATIVE_KEY] == fingerprint
        assert _contained(live, "find the needle here") == ["x"]
        live.register(GramNativeDoc, allow_replace=True)
        assert _contained(live, "find the needle here") == ["x", "y"]
        live.upsert(GramNativeDoc, {"object_id": "z", "title": "Z", "keywords": ["needle"]})
        assert _contained(live, "find the needle here") == ["x", "y", "z"]
    finally:
        other.release()
        live.release()


@pytest.mark.slow
def test_sql_sparse_gram_failed_options_change_preserves_the_original_index_in_a_fresh_workspace(tmp_path, monkeypatch):
    """Publication rolls back an index rebuild even when the workspace has no live registration."""
    narrow, wide = _gram_doc(3), _gram_doc(5)
    key = f"fingerprint:{narrow.identifier}:keywords"
    other = _open_shared(tmp_path, "gram-optfail-other")
    mine = _open_shared(tmp_path, "gram-optfail-mine")
    try:
        other.register(narrow)
        other.upsert(narrow, {"object_id": "a1", "title": "A", "keywords": ["blue whale"]})
        mine.register(narrow)
        fingerprint = _gram_meta(tmp_path / "shared.db", "sql_sparse_gram")[key]
        mine.release()
        fresh = _open_shared(tmp_path, "gram-optfail-mine")
        with monkeypatch.context() as patch:
            _fail_first_call(patch, fresh._metaschema, "publish_schema_rows")
            with pytest.raises(OSError, match="disk full"):
                fresh.register(wide, allow_replace=True)
        fresh.release()
        assert _gram_meta(tmp_path / "shared.db", "sql_sparse_gram")[key] == fingerprint
        assert _width_contained(other, narrow, "the blue whale swims") == ["a1"]
    finally:
        other.release()


@pytest.mark.slow
def test_sql_sparse_gram_a_failed_live_options_change_rebuilds_the_previous_index(tmp_path, monkeypatch):
    """A live registration re-established after a failed options change rebuilds the index with its options again."""
    narrow, wide = _gram_doc(3), _gram_doc(5)
    key = f"fingerprint:{narrow.identifier}:keywords"
    other = _open_shared(tmp_path, "gram-optlive-other")
    mine = _open_shared(tmp_path, "gram-optlive-mine")
    try:
        other.register(narrow)
        other.upsert(narrow, {"object_id": "a1", "title": "A", "keywords": ["blue whale"]})
        mine.register(narrow)
        fingerprint = _gram_meta(tmp_path / "shared.db", "sql_sparse_gram")[key]
        with monkeypatch.context() as patch:
            _fail_first_call(patch, mine._metaschema, "publish_schema_rows")
            with pytest.raises(OSError, match="disk full"):
                mine.register(wide, allow_replace=True)
        assert _gram_meta(tmp_path / "shared.db", "sql_sparse_gram")[key] == fingerprint
        assert _width_contained(other, narrow, "the blue whale swims") == ["a1"]
        assert _width_contained(mine, narrow, "the blue whale swims") == ["a1"]
    finally:
        mine.release()
        other.release()


_AUTOCOMMIT_ENGINES = pytest.mark.parametrize(
    "engine", [{"isolation_level": "AUTOCOMMIT"}, {"connect_args": {"isolation_level": None}}], ids=["autocommit-isolation", "sqlite3-legacy-none"]
)


def _open_autocommit(tmp_path, ws_id, engine, name="shared.db"):
    return hb.HeavenBase(ws_id, detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": _db(tmp_path, name), **engine}})


@pytest.mark.slow
@_AUTOCOMMIT_ENGINES
def test_sql_sparse_gram_keeps_no_index_on_an_engine_that_may_commit_every_statement(tmp_path, monkeypatch, engine):
    """The index needs each write and its keyword rows in one transaction, so such an engine scans and never writes keyword rows."""
    from heavenbase.backends.sql.strategies.sparse_gram import _SQLGramIndex

    ws = _open_autocommit(tmp_path, "gram-autocommit", engine, "auto.db")
    try:
        adapter = ws.backends.get("main").strategy_adapter(hb.SparseGramIndex)
        assert adapter.maintains_index is False
        ws.register(GramNativeDoc)
        ws.upsert_many(
            GramNativeDoc,
            [
                {"object_id": "x", "title": "X", "keywords": ["needle"]},
                {"object_id": "y", "title": "Y", "keywords": ["needle", "thread"]},
                {"object_id": "z", "title": "Z", "keywords": ["haystack"]},
            ],
        )
        assert _contained(ws, "find the needle here") == ["x", "y"]
        assert GramNativeDoc.identifier not in adapter._fields

        def no_index_write(*args, **kwargs):
            raise AssertionError("an engine that may commit every statement writes no keyword rows")

        monkeypatch.setattr(_SQLGramIndex, "upsert_rows", no_index_write)
        ws.upsert(GramNativeDoc, {"object_id": "w", "title": "W", "keywords": ["needle"]})
        monkeypatch.undo()
        ws.release()
        again = _open_autocommit(tmp_path, "gram-autocommit-again", engine, "auto.db")
        again.register(GramNativeDoc)
        assert _contained(again, "find the needle here") == ["w", "x", "y"]
        again.release()
    finally:
        ws.release()


@pytest.mark.slow
@_AUTOCOMMIT_ENGINES
def test_sql_sparse_gram_an_autocommit_backend_retires_the_shared_index_and_refuses_unindexed_writes(tmp_path, monkeypatch, engine):
    """Such a backend binds the field without the index: its retirement deletes the fingerprint first, and it refuses a write before any row."""
    from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError, _SQLGramIndex

    schema = GramNativeDoc.schema()
    bindings = [
        StorageBinding(schema.entity_id, "object_id", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "title", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "keywords", "main", hb.SparseGramIndex.identifier, "test", {"normalizer": "simple"}),
    ]
    indexed = _open_shared(tmp_path, "gram-mixed-indexed")
    try:
        indexed.register(GramNativeDoc)
        indexed.upsert(GramNativeDoc, {"object_id": "x", "title": "X", "keywords": ["needle"]})
        # a backend opened directly, so only this entity's index is shared with the workspace
        backend = hb.backends.SQLiteBackend("main", database=_db(tmp_path, "shared.db"), ws_id="gram-mixed-auto", **engine)
        adapter = backend.strategy_adapter(hb.SparseGramIndex)
        backend.ensure(schema, bindings)
        assert schema.entity_id not in adapter._fields
        with monkeypatch.context() as patch:
            # each statement commits on its own: the fingerprint delete commits, then the keyword-row delete fails
            _fail_first_call(patch, _SQLGramIndex, "delete_field")
            with pytest.raises(OSError, match="disk full"):
                backend.prune(schema, bindings)
        assert _NATIVE_KEY not in _gram_meta(tmp_path / "shared.db", "sql_sparse_gram")
        assert adapter.posting_count(schema.entity_id, "keywords") > 0
        with pytest.raises(SparseGramIndexConflictError, match="deleted it"):
            _contained(indexed, "find the needle here")
        backend.upsert([RowOp(schema.entity_id, "y", {"object_id": "y", "title": "Y", "keywords": ["needle"]})])
        # the rows left without a fingerprint are never trusted: binding the field again rebuilds it from every stored row
        indexed.register(GramNativeDoc, allow_replace=True)
        assert _contained(indexed, "find the needle here") == ["x", "y"]
        with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
            backend.upsert([RowOp(schema.entity_id, "z", {"object_id": "z", "title": "Z", "keywords": ["needle"]})])
        assert backend.get_many(schema.entity_id, ["z"]) == [None]  # refused before the row, which would commit on its own
        assert _contained(indexed, "find the needle here") == ["x", "y"]
    finally:
        indexed.release()


class _AutoGramDoc(hb.Entity):
    """An entity whose keyword field the default profile indexes."""

    identifier = "gram-auto-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText]).default([])


@pytest.mark.slow
def test_sql_sparse_gram_turning_gram_off_from_a_fresh_process_never_rebuilds_a_0_1_2_2_index(tmp_path, monkeypatch):
    """A gram-only change is applied directly, so turning the index off never first builds it from every stored row."""
    from heavenbase.backends.sql.strategies.sparse_gram import SQLSparseGramAdapter
    from heavenbase.workspace import EntityOptions

    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        old = _open_shared(tmp_path, "gram-upgrade")
        old.register(_AutoGramDoc)
        old.upsert_many(_AutoGramDoc, [{"object_id": f"x{index:02d}", "title": "X", "keywords": ["needle"]} for index in range(20)])
        old.release()
    rebuilt = []
    real = SQLSparseGramAdapter._rebuild_field_index

    def counting(self, entity_id, field_name, **kwargs):
        rebuilt.append((entity_id, field_name))
        return real(self, entity_id, field_name, **kwargs)

    monkeypatch.setattr(SQLSparseGramAdapter, "_rebuild_field_index", counting)
    new = _open_shared(tmp_path, "gram-upgrade")
    try:
        new.register(_AutoGramDoc, gram=False, allow_replace=True)
        assert [entry for entry in rebuilt if entry[0] == _AutoGramDoc.identifier] == []
        assert new.entity_options[_AutoGramDoc.identifier] == EntityOptions(gram=False)
        # the 0.1.2.2 index is retired, so 0.1.2.2 opening the database again rebuilds it from every stored row
        legacy = _gram_meta(tmp_path / "shared.db", "gram-upgrade:main:sql_sparse_gram")
        assert legacy[f"fingerprint:{_AutoGramDoc.identifier}:keywords"] == "retired:shared-namespace"
        new.register(_AutoGramDoc, gram=True, allow_replace=True)
        assert [entry for entry in rebuilt if entry[0] == _AutoGramDoc.identifier] == [(_AutoGramDoc.identifier, "keywords")]
        hits = new.query(_AutoGramDoc).where(_AutoGramDoc.keywords.contained_in("find the needle here")).execute().ids
        assert hits == [f"x{index:02d}" for index in range(20)]
    finally:
        new.release()


class GramTokenDoc(hb.Entity):
    identifier = "gram-token-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText]).store(
        to="main", strategy=hb.SparseGramIndex(normalizer="simple", gram_unit="token", tokenizer="space", gram_width=2)
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    "query, contained",
    [("graphs store", ["c"]), ("concatenate things", []), ("the red fox and a cat", ["d"]), ("a graph store", ["a", "e"])],
)
def test_sql_sparse_gram_negation_splits_rows_with_contained_in_for_token_units(tmp_path, query, contained):
    """With token gram units a keyword must cover whole query tokens, and a negation decides it the same way.

    ``graph`` is a substring of ``graphs store`` and ``cat`` of
    ``concatenate``, but neither covers whole tokens, so neither row matches,
    and each is returned by the negation instead.
    """
    ws = hb.HeavenBase("gram-token-split", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": _db(tmp_path, "token.db")}})
    try:
        ws.register(GramTokenDoc)
        ws.upsert_many(
            GramTokenDoc,
            [
                {"object_id": "a", "title": "A", "keywords": ["graph"]},
                {"object_id": "b", "title": "B", "keywords": ["vector"]},
                {"object_id": "c", "title": "C", "keywords": ["graphs store"]},
                {"object_id": "d", "title": "D", "keywords": ["red fox", "cat"]},
                {"object_id": "e", "title": "E", "keywords": ["Graph Store", "graph store", None]},
                {"object_id": "f", "title": "F", "keywords": []},
            ],
        )

        def ids(where):
            return sorted(row["object_id"] for row in ws.query(GramTokenDoc).where(where).execute().rows())

        leaf = GramTokenDoc.keywords.contained_in(query)
        assert ids(leaf) == contained
        assert ids(~leaf) == sorted(set("abcdef") - set(contained))
        assert ids(~~leaf) == contained
        vector = GramTokenDoc.keywords.contained_in("vector")
        assert ids(vector | ~leaf) == sorted(set(ids(vector)) | set(ids(~leaf)))
        assert ids(~(vector | leaf)) == sorted(set("abcdef") - set(contained) - {"b"})
    finally:
        ws.release()


class GramTornDoc(hb.Entity):
    identifier = "gram-torn-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))


class GramTornTokenDoc(hb.Entity):
    identifier = "gram-torn-token-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText]).store(
        to="main", strategy=hb.SparseGramIndex(normalizer="simple", gram_unit="token", tokenizer="space", gram_width=2)
    )


def _holds(keywords, query):
    # every keyword and query in the torn test is lower case and covers whole query tokens
    return any(keyword is not None and keyword in query for keyword in keywords or [])


@pytest.mark.slow
@pytest.mark.parametrize("doc", [GramTornDoc, GramTornTokenDoc], ids=["char", "token"])
@pytest.mark.parametrize(
    "where, satisfied, present",
    [
        (lambda doc: doc.keywords.contained_in("the redis engine"), lambda keywords: _holds(keywords, "the redis engine"), {"kept"}),
        (
            lambda doc: doc.keywords.contained_in("the redis engine") | doc.keywords.contained_in("a vector store"),
            lambda keywords: _holds(keywords, "the redis engine") or _holds(keywords, "a vector store"),
            {"kept"},
        ),
        (lambda doc: ~doc.keywords.contained_in("the redis engine"), lambda keywords: not _holds(keywords, "the redis engine"), {"other"}),
        (
            lambda doc: doc.keywords.contained_in("the redis engine") & ~doc.keywords.contained_in("a graph store"),
            lambda keywords: _holds(keywords, "the redis engine") and not _holds(keywords, "a graph store"),
            {"kept"},
        ),
    ],
    ids=["contained_in", "or", "not", "and-not"],
)
def test_sql_sparse_gram_rows_written_between_lookup_and_read_are_checked_again(tmp_path, doc, where, satisfied, present):
    """A write through another workspace between the index lookup and the row read never yields a row that fails the filter.

    The writes land right after the first row read: the ``get_many`` that
    reads the candidates, or the scan a ``not`` makes before it looks up the
    leaf it subtracts. Each row is checked against the keywords it stores as
    read: a row that stopped matching is left out, a row a negation would
    return is checked against the negated leaf, and the deleted row is not
    returned as a row of nulls. Rows the writes leave alone are answered as usual.
    """
    path = _db(tmp_path, "torn.db")
    reader = hb.HeavenBase("gram-torn-reader", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": path}})
    writer = hb.HeavenBase("gram-torn-writer", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": path}})
    try:
        reader.register(doc)
        writer.register(doc)
        reader.upsert_many(
            doc,
            [
                {"object_id": "changed", "title": "C", "keywords": ["redis engine"]},
                {"object_id": "deleted", "title": "D", "keywords": ["redis engine"]},
                {"object_id": "kept", "title": "K", "keywords": ["redis engine"]},
                {"object_id": "other", "title": "O", "keywords": ["unrelated"]},
                {"object_id": "vector", "title": "V", "keywords": ["vector store"]},
            ],
        )
        backend = reader.backends.get("main")
        read_rows, scan_rows = type(backend).get_many, type(backend).scan
        armed = [True]

        def write_once(entity_id):
            if armed[0] and entity_id == doc.identifier:
                armed[0] = False
                writer.upsert(doc, {"object_id": "changed", "title": "C", "keywords": ["graph store"]})
                writer.upsert(doc, {"object_id": "vector", "title": "V", "keywords": ["redis engine"]})
                writer.delete((doc, "deleted"))

        def get_many(self, entity_id, object_ids, **kwargs):
            write_once(entity_id)
            return read_rows(self, entity_id, object_ids, **kwargs)

        def scan(self, entity_id, *args, **kwargs):
            rows = scan_rows(self, entity_id, *args, **kwargs)
            write_once(entity_id)
            return rows

        backend.get_many = get_many.__get__(backend)
        backend.scan = scan.__get__(backend)
        rows = reader.query(doc).where(where(doc)).execute().rows()
        assert not armed[0]
        assert present <= {row["object_id"] for row in rows}
        for row in rows:
            assert row["title"] is not None
            assert satisfied(row["keywords"]), row
            for match in row.get("match") or []:
                assert match["keyword"] in row["keywords"]
    finally:
        writer.release()
        reader.release()


@pytest.mark.slow
@pytest.mark.parametrize("doc", [GramTornDoc, GramTornTokenDoc], ids=["char", "token"])
@pytest.mark.parametrize(
    "keywords",
    [["alpha", "redis engine"], ["Redis Engine"], ["redis", "redis engine"], ["redis engine", "alpha"]],
    ids=["prepended", "recased", "widened", "appended"],
)
@pytest.mark.parametrize(
    "where, after_scan",
    [
        (lambda doc: doc.keywords.contained_in("the redis engine"), False),
        (lambda doc: doc.keywords.contained_in("the redis engine") & doc.keywords.contained_in("redis engine now"), False),
        (lambda doc: ~doc.keywords.contained_in("the redis engine too") | doc.keywords.contained_in("the redis engine"), True),
    ],
    ids=["contained_in", "and", "not-or"],
)
def test_sql_sparse_gram_keeps_a_row_that_still_matches_after_its_keywords_change(tmp_path, doc, keywords, where, after_scan):
    """A write that rewrites a matching row's keywords so that it still matches never drops the row from a query in flight.

    The write lands between the index lookup and the row read, or between
    the scan a ``not`` makes and the lookups after it, so the keyword
    positions the lookup saw differ from the row that is read. The row is
    still returned, with ``match`` metadata naming the keywords it holds at
    their positions; when the row is read after the write, the result is the
    one the same query returns once the write is done.
    """
    path = _db(tmp_path, "shift.db")
    reader = hb.HeavenBase("gram-shift-reader", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": path}})
    writer = hb.HeavenBase("gram-shift-writer", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": path}})
    try:
        reader.register(doc)
        writer.register(doc)
        reader.upsert_many(
            doc,
            [
                {"object_id": "kept", "title": "K", "keywords": ["redis engine"]},
                {"object_id": "other", "title": "O", "keywords": ["unrelated"]},
            ],
        )
        backend = reader.backends.get("main")
        read_rows, scan_rows = type(backend).get_many, type(backend).scan
        armed = [True]

        def write_once(entity_id):
            if armed[0] and entity_id == doc.identifier:
                armed[0] = False
                writer.upsert(doc, {"object_id": "kept", "title": "K", "keywords": keywords})

        def get_many(self, entity_id, object_ids, **kwargs):
            write_once(entity_id)
            return read_rows(self, entity_id, object_ids, **kwargs)

        def scan(self, entity_id, *args, **kwargs):
            rows = scan_rows(self, entity_id, *args, **kwargs)
            write_once(entity_id)
            return rows

        backend.get_many = get_many.__get__(backend)
        backend.scan = scan.__get__(backend)
        torn = reader.query(doc).where(where(doc)).execute().rows()
        del backend.get_many, backend.scan
        assert not armed[0]
        assert sorted(row["object_id"] for row in torn) == (["kept", "other"] if after_scan else ["kept"])
        (row,) = [row for row in torn if row["object_id"] == "kept"]
        assert row["match"]
        for match in row["match"]:
            assert match["keyword"] == row["keywords"][match["ordinal"]]
        if not after_scan:
            assert row["keywords"] == keywords
            assert torn == reader.query(doc).where(where(doc)).execute().rows()
    finally:
        writer.release()
        reader.release()


@pytest.mark.slow
@pytest.mark.parametrize("history", ["created-by-this-version", "written-through-another-workspace"])
def test_0_1_2_2_reopening_a_database_this_version_wrote_rebuilds_its_index(tmp_path, monkeypatch, history):
    """0.1.2.2 trusts an index with no fingerprint, so the workspace it reopens through gets a marker.

    A database only this version indexed has no 0.1.2.2 fingerprint at all,
    and the index 0.1.2.2 kept for one workspace is retired when this version
    writes through another; either way that workspace's namespace holds the
    marker, and 0.1.2.2 reopening it rebuilds its index from stored rows
    instead of missing rows.
    """
    if history == "written-through-another-workspace":
        with monkeypatch.context() as patch:
            _act_as_0_1_2_2(patch)
            _stage(tmp_path, "gram-down-a", upsert={"object_id": "x", "title": "X", "keywords": ["needle"]})
        _stage(tmp_path, "gram-down-b", upsert={"object_id": "y", "title": "Y", "keywords": ["needle"]})
    else:
        _stage(tmp_path, "gram-down-a", upsert={"object_id": "x", "title": "X", "keywords": ["needle"]})
        _stage(tmp_path, "gram-down-a", upsert={"object_id": "y", "title": "Y", "keywords": ["needle"]})

    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        assert sorted(_stage(tmp_path, "gram-down-a", contained="find the needle here")) == ["x", "y"]


@pytest.mark.slow
def test_0_1_2_2_reopening_a_database_two_workspaces_of_this_version_wrote_rebuilds_only_the_first(tmp_path, monkeypatch):
    """Only the first workspace's namespace gets the marker, so 0.1.2.2 never rebuilds two namespaces into duplicate keyword ids.

    0.1.2.2 computed keyword ids without the workspace: rebuilding a second
    namespace from the same stored rows would fail with a UNIQUE constraint
    error. Through the second workspace 0.1.2.2 reads an empty index instead,
    as it does for a database written through another workspace.
    """
    from heavenbase.backends.sql.strategies.sparse_gram import _RETIRED_FINGERPRINT

    _stage(tmp_path, "gram-two-a", upsert={"object_id": "x", "title": "X", "keywords": ["needle"]})
    _stage(tmp_path, "gram-two-b", upsert={"object_id": "y", "title": "Y", "keywords": ["needle"]})
    assert set(_gram_meta(tmp_path / "shared.db", "gram-two-a:main:sql_sparse_gram").values()) == {_RETIRED_FINGERPRINT}
    assert _gram_meta(tmp_path / "shared.db", "gram-two-b:main:sql_sparse_gram") == {}

    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        assert sorted(_stage(tmp_path, "gram-two-a", contained="find the needle here")) == ["x", "y"]
        assert _stage(tmp_path, "gram-two-b", contained="find the needle here") == []
    assert sorted(_stage(tmp_path, "gram-two-b", contained="find the needle here")) == ["x", "y"]


@pytest.mark.slow
def test_sql_sparse_gram_rebuilds_when_a_legacy_rewrite_restores_the_same_keyword_ids(tmp_path, monkeypatch):
    old_a, old_b, current = "gram-rewrite-a", "gram-rewrite-b", "gram-rewrite-current"
    needle = {"object_id": "x", "title": "X", "keywords": ["needle"]}
    thread = {**needle, "keywords": ["thread"]}
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        _stage(tmp_path, old_a, upsert=needle)
        _stage(tmp_path, old_b, upsert={"object_id": "y", "title": "Y", "keywords": ["haystack"]})

    assert _stage(tmp_path, current, upsert=thread, contained="find the thread here") == ["x"]
    # Register the updated index before the downgrade as well. A legacy digest
    # invalidated by this release's write would already have been refreshed here.
    assert _stage(tmp_path, current, contained="find the thread here") == ["x"]
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        assert _stage(tmp_path, old_a, upsert=needle, contained="find the needle here") == ["x"]

    ws = _open_shared(tmp_path, current)
    try:
        ws.register(GramNativeDoc)
        assert ws.get("x", entity=GramNativeDoc)["keywords"] == ["needle"]
        assert _contained(ws, "find the needle here") == ["x"]
        assert _contained(ws, "find the thread here") == []
    finally:
        ws.release()
