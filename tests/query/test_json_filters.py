"""Json field filtering: whole-value scan parity, nested `get` paths, and agent surfaces."""

from types import MappingProxyType
import sqlite3

import pytest

import heavenbase as hb
from heavenbase.execution.handlers.filters import filter_handler
from heavenbase.execution.handlers.seed import filter_ops
from heavenbase.schema import FieldSchema


class _JsonDoc(hb.Entity):
    identifier = "json-filter-doc"

    author = hb.field(hb.ShortText)
    venue = hb.field(hb.ShortText).store(optional=True)  # declared lift of meta.venue for group-by parity checks
    meta = hb.field(hb.Json).store(optional=True)
    payload = hb.field(hb.Artifact).store(optional=True)


class _VectorDoc(hb.Entity):
    identifier = "json-filter-vector-doc"

    embedding = hb.field(hb.Vector[2]).store(to="main", strategy=hb.VectorIndex)


_ROWS = [
    {"object_id": "d1", "author": "Chen", "venue": "NeurIPS", "meta": {"venue": "NeurIPS", "review": {"score": 9}}},
    {"object_id": "d2", "author": "Sato", "venue": "ICML", "meta": {"venue": "ICML", "review": {"score": 7}}},
    {"object_id": "d3", "author": "Chen", "venue": "NeurIPS", "meta": {"venue": "NeurIPS"}},
    {"object_id": "d4", "author": "Novak", "venue": None, "meta": {"tags": ["x"]}},
    {"object_id": "d5", "author": "Ora", "venue": None, "meta": {"venue": None}},
    {"object_id": "d6", "author": "Bo", "venue": None, "meta": {"flag": True}},
    {"object_id": "d7", "author": "Di", "venue": None, "meta": {"flag": 1}},
    {"object_id": "d8", "author": "El", "venue": None, "meta": {"flag": 2}},
    {"object_id": "d9", "author": "Nu", "venue": None, "meta": None},
]


def _workspace(workspace_id: str, backend: str = "sqlite", root=None) -> hb.HeavenBase:
    if backend in ("json", "pickle"):
        if root is None:
            raise ValueError("File backend tests need an isolated root")
        config = {"type": backend, "path": hb.utils.path_to_file_uri(root / backend)}
    else:
        config = {"type": backend}
    ws = hb.HeavenBase(workspace_id, backends={"main": config})
    ws.register(_JsonDoc)
    ws.upsert_many(_JsonDoc, list(_ROWS))
    return ws


@pytest.mark.parametrize("backend", ["sqlite", "inmem", "json", "pickle"])
def test_json_whole_value_filters_compare_decoded_payloads(backend, tmp_path):
    """Whole-value eq/ne/in must decode storage-encoded rows instead of silently matching nothing."""
    ws = _workspace(f"json-whole-{backend}", backend, tmp_path)
    ws.upsert(_JsonDoc, {"object_id": "tuple", "author": "Tu", "meta": {"items": (1, 2)}})

    assert sorted(ws.query(_JsonDoc).where(_JsonDoc.meta == {"tags": ["x"]}).execute().ids) == ["d4"]
    assert sorted(ws.query(_JsonDoc).where(_JsonDoc.meta.in_([{"tags": ["x"]}, {"venue": None}])).execute().ids) == ["d4", "d5"]
    assert sorted(ws.query(_JsonDoc).where(_JsonDoc.meta == '{"tags":["x"]}').execute().ids) == ["d4"]
    json_values = ['{"tags":["x"]}', '{"venue":null}']
    assert sorted(ws.query(_JsonDoc).where(_JsonDoc.meta.in_(json_values)).execute().ids) == ["d4", "d5"]
    assert sorted(ws.query(_JsonDoc).where(_JsonDoc.meta == {"flag": True}).execute().ids) == ["d6"]
    assert sorted(ws.query(_JsonDoc).where(_JsonDoc.meta == {"flag": 1}).execute().ids) == ["d7"]
    assert sorted(ws.query(_JsonDoc).where(_JsonDoc.meta == {"items": (1, 2)}).execute().ids) == ["tuple"]
    excluded = ws.query(_JsonDoc).where(_JsonDoc.meta != {"tags": ["x"]}).execute().ids
    assert "d4" not in excluded and "d9" not in excluded and "d1" in excluded


@pytest.mark.parametrize("backend", ["sqlite", "inmem", "json", "pickle"])
def test_json_get_paths_filter_nested_values(backend, tmp_path):
    """`get` filters walk nested keys with SQL NULL semantics on every backend."""
    ws = _workspace(f"json-get-{backend}", backend, tmp_path)
    ws.upsert(_JsonDoc, {"object_id": "tuple", "author": "Tu", "meta": {"items": (1, 2)}})

    def ids(query):
        return sorted(query.execute().ids)

    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["venue"] == "NeurIPS")) == ["d1", "d3"]
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["review"]["score"] >= 8)) == ["d1"]
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["venue"].contains("eur"))) == ["d1", "d3"]
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["venue"].in_(["ICML", "KDD"]))) == ["d2"]
    # SQL NULL semantics: missing keys and null values match nothing, including ne
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["venue"] != "NeurIPS")) == ["d2"]
    # heterogeneous comparisons match nothing instead of raising mid-scan
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["venue"] < 5)) == []
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["venue"] != 2)) == []
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["flag"] == True)) == ["d6"]  # noqa: E712 - query DSL builds an expression
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["flag"] == 1)) == ["d7"]
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["flag"].in_([1]))) == ["d7"]
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["flag"] != 1)) == ["d8"]
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["tags"].contains("x"))) == []
    assert ids(ws.query(_JsonDoc).where(_JsonDoc.meta["items"] == (1, 2))) == ["tuple"]


def test_json_dotted_paths_and_get_lower_through_query_mappings():
    """Dotted filter keys and the explicit `$get` form lower to the same registered op."""
    ws = _workspace("json-dotted")

    assert sorted(ws.query(_JsonDoc).where({"meta.venue": "NeurIPS"}).execute().ids) == ["d1", "d3"]
    assert sorted(ws.query(_JsonDoc).where({"meta.review.score": {"$gte": 8}}).execute().ids) == ["d1"]
    proxy_condition = MappingProxyType({"$eq": "ICML"})
    assert sorted(ws.query(_JsonDoc).where({"meta.venue": proxy_condition}).execute().ids) == ["d2"]
    assert sorted(ws.query(_JsonDoc).where({"meta": {"$get": {"path": "venue", "eq": "ICML"}}}).execute().ids) == ["d2"]
    proxy_get = MappingProxyType({"path": "venue", "eq": "ICML"})
    assert sorted(ws.query(_JsonDoc).where({"meta": {"$get": proxy_get}}).execute().ids) == ["d2"]
    assert sorted(ws.query_json(_JsonDoc, {"filter": {"meta.venue": "NeurIPS"}}).execute().ids) == ["d1", "d3"]

    combined = ws.query(_JsonDoc).where({"meta.venue": "NeurIPS", "author": "Chen"})
    assert sorted(combined.execute().ids) == ["d1", "d3"]
    filter_steps = [step for step in combined.explain()["steps"] if step.get("node") == "filter"]
    by_field = {step["field"]: step for step in filter_steps}
    assert by_field["meta"]["handler_mode"] == "py_scan"
    assert by_field["meta"]["fallback_reason"] == "text_semantics_not_proven"
    assert by_field["author"]["handler_mode"] == "native"
    assert by_field["author"]["portable_proof"] is True
    numeric = ws.query(_JsonDoc).where({"meta.review.score": {"$gte": 8}})
    assert next(step for step in numeric.explain()["steps"] if step.get("node") == "filter")["handler_mode"] == "native"


def test_json_get_payloads_validate_loudly():
    """Malformed get payloads and non-Json dotted paths fail with contextual errors."""
    ws = _workspace("json-get-guards")

    with pytest.raises(ValueError, match="exactly one comparison"):
        ws.query(_JsonDoc).where({"meta": {"$get": {"path": "venue", "eq": "a", "ne": "b"}}}).execute()
    with pytest.raises(ValueError, match='need a "path"'):
        ws.query(_JsonDoc).where({"meta": {"$get": {"eq": "a"}}}).execute()
    with pytest.raises(ValueError, match="do not support 'regex'"):
        ws.query(_JsonDoc).where({"meta": {"$get": {"path": "venue", "regex": "a"}}}).execute()
    for path in (5, b"venue", {"key": "venue"}, ["review", 0]):
        with pytest.raises(TypeError, match="path"):
            ws.query(_JsonDoc).where({"meta": {"$get": {"path": path, "eq": "a"}}}).execute()
    with pytest.raises(ValueError, match="non-empty path segments"):
        ws.query(_JsonDoc).where({"meta": {"$get": {"path": "review..score", "eq": 8}}}).execute()
    with pytest.raises(TypeError, match="path segments must be strings"):
        _JsonDoc.meta[0]
    with pytest.raises(ValueError, match="must not be empty"):
        _JsonDoc.meta[""]
    with pytest.raises(ValueError, match="finite"):
        ws.query(_JsonDoc).where(_JsonDoc.meta["score"] == float("nan")).execute()
    with pytest.raises(TypeError, match="Json values"):
        ws.query(_JsonDoc).where(_JsonDoc.meta["score"] == {1, 2}).execute()
    with pytest.raises(LookupError, match="dotted paths are not standalone fields"):
        ws.query(_JsonDoc).where({"author.x": 1}).execute()


def test_json_scan_decode_failures_are_not_silenced():
    """Corrupt backend JSON must fail loudly instead of being compared as a raw string."""
    field_schema = FieldSchema("meta", hb.Json())
    ctx = hb.ext.HandlerContext("main", "inmem", hb.JsonField.identifier)
    fragment = filter_handler("eq")(field_schema, {"ok": True}, ctx)
    get_fragment = filter_handler("get")(field_schema, {"path": "ok", "eq": True}, ctx)

    with pytest.raises(ValueError):
        fragment.payload({"meta": "not-json"})
    with pytest.raises(ValueError):
        get_fragment.payload({"meta": "not-json"})
    assert not get_fragment.payload({"meta": '"scalar"'})


@pytest.mark.parametrize("backend", ["sqlite", "inmem"])
def test_json_path_group_by_matches_lifted_declared_column(backend):
    """Dotted group_by folds the same groups as its declared lifted twin, missing/null paths under the None key."""
    ws = _workspace(f"json-group-{backend}", backend)
    spec = {"aggregate": {"n": {"count": {"field": "object_id"}}}, "order": ["-n"]}

    lifted = ws.query_json(_JsonDoc, {**spec, "group_by": ["venue"]}).execute()
    dotted = ws.query_json(_JsonDoc, {**spec, "group_by": ["meta.venue"]}).execute()
    assert dotted.col_names() == ["meta.venue", "n"]
    assert [(row["meta.venue"], row["n"]) for row in dotted.rows()] == [(row["venue"], row["n"]) for row in lifted.rows()]
    assert {(row["meta.venue"], row["n"]) for row in dotted.rows()} == {("NeurIPS", 2), ("ICML", 1), (None, 6)}

    having = ws.query_json(_JsonDoc, {**spec, "group_by": ["meta.venue"], "having": {"meta.venue": "NeurIPS"}}).execute()
    assert having.rows() == [{"meta.venue": "NeurIPS", "n": 2}]

    fluent = ws.query(_JsonDoc).group_by(_JsonDoc.meta["venue"]).aggregate(n=_JsonDoc.object_id.count()).execute()
    assert {(row["meta.venue"], row["n"]) for row in fluent.rows()} == {(row["meta.venue"], row["n"]) for row in dotted.rows()}


@pytest.mark.parametrize("backend", ["sqlite", "inmem"])
def test_json_path_group_by_keeps_dynamic_types_distinct(backend):
    """Dynamic Json groups must be hashable without collapsing distinct typed values."""
    ws = _workspace(f"json-group-types-{backend}", backend)
    ws.upsert_many(
        _JsonDoc,
        [
            {"object_id": "typed-object", "author": "Object", "meta": {"key": {"x": 1}}},
            {"object_id": "typed-string", "author": "String", "meta": {"key": '{"x": 1}'}},
            {"object_id": "typed-set", "author": "Set", "meta": {"key": {1, 2}}},
            {"object_id": "typed-bool", "author": "Bool", "meta": {"key": True}},
            {"object_id": "typed-number", "author": "Number", "meta": {"key": 1}},
        ],
    )

    result = ws.query_json(
        _JsonDoc,
        {
            "filter": {"object_id": {"$in": ["typed-object", "typed-string", "typed-set", "typed-bool", "typed-number"]}},
            "group_by": ["meta.key"],
            "aggregate": {"n": {"count": {"field": "object_id"}}},
        },
    ).execute()

    assert result.rows() == [
        {"meta.key": True, "n": 1},
        {"meta.key": 1, "n": 1},
        {"meta.key": {"x": 1}, "n": 1},
        {"meta.key": {1, 2}, "n": 1},
        {"meta.key": '{"x": 1}', "n": 1},
    ]


@pytest.mark.parametrize("backend", ["sqlite", "inmem"])
def test_json_path_order_and_select(backend):
    """Dotted order sorts missing/null paths last both ways; dotted select names the column by the path."""
    ws = _workspace(f"json-order-{backend}", backend)

    asc = ws.query_json(_JsonDoc, {"order": ["meta.venue"]}).execute()
    assert asc.ids == ["d2", "d1", "d3", "d4", "d5", "d6", "d7", "d8", "d9"]
    assert "meta.venue" not in asc.col_names()  # order-only derivation does not widen the row shape

    desc = ws.query_json(_JsonDoc, {"order": ["-meta.venue"], "select": ["meta.venue", "author"]}).execute()
    assert desc.col_names() == ["object_id", "meta.venue", "author"]
    assert desc.ids == ["d1", "d3", "d2", "d4", "d5", "d6", "d7", "d8", "d9"]
    assert desc.column("meta.venue") == ["NeurIPS", "NeurIPS", "ICML", None, None, None, None, None, None]

    nested = ws.query(_JsonDoc).select(_JsonDoc.meta["review"]["score"]).order_by(_JsonDoc.meta["review"]["score"], desc=True).execute()
    assert nested.column("meta.review.score") == [9, 7, None, None, None, None, None, None, None]


def test_json_path_clauses_explain_the_python_route():
    """Dotted clauses scan while managed declared SQLite text groups stay native."""
    ws = _workspace("json-path-explain")

    dotted = ws.query_json(_JsonDoc, {"group_by": ["meta.venue"], "aggregate": {"n": {"count": {"field": "object_id"}}}})
    step = dotted.explain()["steps"][-1]
    assert step["node"] == "aggregate"
    assert step["handler_mode"] == "py_fold"
    assert step["fallback_reason"] == "json_path_group_by_present"

    lifted = ws.query_json(_JsonDoc, {"group_by": ["venue"], "aggregate": {"n": {"count": {"field": "object_id"}}}})
    lifted_step = lifted.explain()["steps"][-1]
    assert lifted_step["handler_mode"] == "native"
    assert lifted_step["portable_proof"] is True

    ordered = ws.query_json(_JsonDoc, {"order": ["-meta.venue"], "select": ["meta.venue"]})
    path_step = next(item for item in ordered.explain()["steps"] if item["node"] == "json_path")
    assert path_step["handler_mode"] == "py_scan"
    assert path_step["fields"] == ["meta.venue"]
    plain = ws.query_json(_JsonDoc, {"order": ["-author"], "select": ["author"]})
    assert not [item for item in plain.explain()["steps"] if item["node"] == "json_path"]


def test_json_path_clauses_guard_non_json_bases():
    """Dotted clause names with a declared non-Json base fail loudly with the dotted-path hint."""
    ws = _workspace("json-clause-guards")
    count = {"n": {"count": {"field": "object_id"}}}

    with pytest.raises(LookupError, match="dotted paths are not standalone fields"):
        ws.query_json(_JsonDoc, {"group_by": ["author.x"], "aggregate": count}).execute()
    with pytest.raises(LookupError, match="dotted paths are not standalone fields"):
        ws.query_json(_JsonDoc, {"select": ["author.x"]}).execute()
    with pytest.raises(LookupError, match="dotted paths are not standalone fields"):
        ws.query_json(_JsonDoc, {"order": ["author.x"]}).execute()
    with pytest.raises(ValueError, match="Unknown aggregate field"):
        ws.query_json(_JsonDoc, {"group_by": ["nope.x"], "aggregate": count}).execute()
    with pytest.raises(ValueError, match="dotted Json paths apply to group_by, order, and select"):
        ws.query_json(_JsonDoc, {"group_by": ["meta.venue"], "aggregate": {"worst": {"max": {"field": "meta.review.score"}}}}).execute()
    with pytest.raises(ValueError, match="cannot be used as an aggregate group"):
        ws.query_json(_JsonDoc, {"group_by": ["meta"], "aggregate": count}).execute()  # the undotted Json field itself stays ungroupable


def test_json_field_capabilities_reach_agent_surfaces():
    """Agents can discover per-field filter ops and the Json guidance."""
    assert filter_ops(hb.Json) == ("eq", "ne", "in", "get")
    assert filter_ops(hb.Artifact) == ()
    assert filter_ops(hb.Vector) == ()
    assert "get" not in filter_ops(hb.HyperG)  # mini-row structs keep their own shape-specific ops

    ws = _workspace("json-agent-surface")
    ws.enable_extension("agent")
    toolkit = ws.to_mcp(name="json-agent-surface", profile="agent")
    described = toolkit.run("describe_entity", entity="json-filter-doc")
    assert "get" in described["filter_ops"]["meta"]
    assert "eq" in described["filter_ops"]["author"]
    assert described["filter_ops"]["payload"] == []

    guide = toolkit.run("read_skill", skill_name="workspace-guide")
    assert "meta.venue" in guide["text"]
    assert "$get" in guide["text"]
    assert "computed field" in guide["text"]


def test_field_capabilities_intersect_resolved_routes():
    """Agent field capabilities include routed vector equality but exclude non-filter near search."""
    ws = hb.HeavenBase("json-agent-vector-surface", backends={"main": {"type": "inmem"}})
    ws.register(_VectorDoc)
    ws.upsert(_VectorDoc, {"object_id": "v1", "embedding": [1.0, 0.0]})
    ws.enable_extension("agent")

    toolkit = ws.to_mcp(name="json-agent-vector-surface", profile="agent")
    described = toolkit.run("describe_entity", entity=_VectorDoc.identifier)

    assert described["filter_ops"]["embedding"] == ["eq"]


# Sqlite-native pushdown parity: every payload type the portable scan distinguishes,
# at present, missing, null, and nested paths, plus heterogeneous mixes per path.
_PARITY_ROWS = [
    {"object_id": "p01", "meta": {"tag": "alpha", "score": 9, "rate": 2.5, "flag": True, "deep": {"lvl": {"val": 5}}}},
    {"object_id": "p02", "meta": {"tag": "beta", "score": "9", "rate": "high", "flag": False, "deep": {"lvl": {"val": "5"}}}},
    {"object_id": "p03", "meta": {"tag": None, "score": 7.0, "flag": 1, "deep": {"lvl": []}}},
    {"object_id": "p04", "meta": {}},
    {"object_id": "p05", "meta": {"tag": "", "score": 0, "rate": -1.5, "deep": "not-a-dict"}},
    {"object_id": "p06", "meta": {"tag": {"nested": "x"}, "score": [3, 4], "rate": True, "deep": {"lvl": {"val": 9.5}}}},
    {"object_id": "p07", "meta": {"tag": "alphabet", "score": -3, "rate": 0.0, "flag": "true", "deep": {"lvl": {"val": None}}}},
]

_PARITY_PATHS = ("tag", "score", "rate", "flag", "deep.lvl.val", "deep.lvl", "deep.missing", "absent")

_SCALAR_OPERANDS = ["alpha", "", "9", 9, 9.0, 0, 2.5, True, False, None]

_PARITY_CASES = (
    [("eq", operand) for operand in [*_SCALAR_OPERANDS, [3, 4], {"nested": "x"}]]
    + [("ne", operand) for operand in [*_SCALAR_OPERANDS, [3, 4]]]
    + [("in", operand) for operand in ([["alpha", "beta"], [9, "9"], [True, 2.5], [None, 0], [], [[3, 4], "alpha"]])]
    + [(op, operand) for op in ("lt", "lte", "gt", "gte") for operand in ["beta", 8, 7.0, True, None, [3, 5]]]
    + [("contains", operand) for operand in ["alp", "", "9", "true"]]
)


def _native_expected(op, operand) -> bool:
    """Mirror the pushdown envelope: proved non-text scalar comparisons compile natively."""
    if op == "contains":
        return False
    items = list(operand) if op == "in" else [operand]
    return all(item is None or isinstance(item, (bool, int, float)) for item in items)


def test_json_get_sqlite_native_parity_matrix(tmp_path):
    """For every op x operand x path, the sqlite-native row set equals the portable scan row set, and explain stays honest."""
    sql_ws = hb.HeavenBase("json-parity-sqlite", backends={"main": hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "json_parity.db"))})
    mem_ws = hb.HeavenBase("json-parity-inmem", backends={"main": {"type": "inmem"}})
    sql_ws.register(_JsonDoc)
    mem_ws.register(_JsonDoc)
    sql_ws.upsert_many(_JsonDoc, [dict(row) for row in _PARITY_ROWS])
    mem_ws.upsert_many(_JsonDoc, [dict(row) for row in _PARITY_ROWS])

    for path in _PARITY_PATHS:
        for op, operand in _PARITY_CASES:
            spec = {"meta": {"$get": {"path": path, op: operand}}}
            native_query = sql_ws.query(_JsonDoc).where(dict(spec))
            native_ids = sorted(native_query.execute().ids)
            scan_ids = sorted(mem_ws.query(_JsonDoc).where(dict(spec)).execute().ids)
            assert native_ids == scan_ids, f"parity break for path={path!r} {op}={operand!r}: sqlite={native_ids} scan={scan_ids}"
            step = [item for item in native_query.explain()["steps"] if item.get("node") == "filter"][0]
            expected_mode = "native" if _native_expected(op, operand) else "py_scan"
            assert step["handler_mode"] == expected_mode, f"explain mode for path={path!r} {op}={operand!r}"
            if expected_mode == "native":
                assert step["handler_kind"] == "sql_where"
            else:
                assert step.get("fallback_reason")


def test_json_get_native_pushdown_reads_double_encoded_storage(tmp_path):
    """Sqlite stores Json as a JSON-encoded string of JSON; native pushdown must unwrap it, not silently match nothing."""
    db_path = tmp_path / "json_double_encoded.db"
    ws = hb.HeavenBase("json-get-double-encoded", backends={"main": hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(db_path))})
    ws.register(_JsonDoc)
    ws.upsert_many(_JsonDoc, list(_ROWS))

    with sqlite3.connect(db_path) as con:
        stored_type, direct_extract = con.execute(
            "select json_type(meta, '$'), json_extract(meta, '$.venue') from json_filter_doc where object_id = 'd1'"
        ).fetchone()
    assert stored_type == "text"  # the payload really is double-encoded in the column
    assert direct_extract is None  # naive json_extract would lose every row without the unwrap

    query = ws.query(_JsonDoc).where(_JsonDoc.meta["review"]["score"] >= 8)
    assert sorted(query.execute().ids) == ["d1"]
    step = [item for item in query.explain()["steps"] if item.get("node") == "filter"][0]
    assert step["handler_mode"] == "native"
    assert step["handler_kind"] == "sql_where"

    text_query = ws.query(_JsonDoc).where(_JsonDoc.meta["venue"] == "NeurIPS")
    assert sorted(text_query.execute().ids) == ["d1", "d3"]
    text_step = [item for item in text_query.explain()["steps"] if item.get("node") == "filter"][0]
    assert text_step["handler_mode"] == "py_scan"
    assert text_step["fallback_reason"] == "text_semantics_not_proven"


def test_json_get_unsafe_path_keys_fall_back_to_scan():
    """Path keys that cannot be embedded in a SQL JSON path scan honestly instead of compiling something else."""
    ws = _workspace("json-get-unsafe-path")
    ws.upsert(_JsonDoc, {"object_id": "d9", "author": "Quote", "meta": {'we"ird': "hit"}})

    query = ws.query(_JsonDoc).where({"meta": {"$get": {"path": ['we"ird'], "eq": "hit"}}})
    assert sorted(query.execute().ids) == ["d9"]
    step = [item for item in query.explain()["steps"] if item.get("node") == "filter"][0]
    assert step["handler_mode"] == "py_scan"
    assert "path keys" in step["fallback_reason"]
