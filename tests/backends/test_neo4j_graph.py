"""Tests for the GraphEdge graph layer: storage codec, traversal, the 6B routed op, and §9.2 tools.

Offline tests use the in-memory backend, where a ``GraphEdge`` field degrades to a JSON edge list
and traversal runs the portable Python fallback — so they exercise ``ws.traverse`` /
``ws.shortest_path`` / the routed ``.traverse()`` op / ``check_graph`` / ``scrub_dangling`` with no
server. Live behaviour (real Neo4j relationships, native Cypher traversal, strict-target enforcement)
is in the ``@pytest.mark.external`` suite, gated on a reachable server + the ``neo4j`` driver.
"""

import os
import socket
import sys
from importlib.util import find_spec
from urllib.parse import urlparse

import pytest

sys.path.insert(0, "src")

import heavenbase as hb
from heavenbase.backends import Neo4jBackend
from heavenbase.backends.neo4j.handler import neo4j_graph_exists_handler
from heavenbase.execution import HandlerContext
from heavenbase.workspace.query.traverse import pushable_endpoint_preds

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry

_NEO4J_URL = os.environ.get("HVNB_NEO4J_URL", "bolt://localhost:7687")


def _inmem_ws(ws_id: str) -> hb.HeavenBase:
    return _reset_ws(hb.HeavenBase(ws_id, backends={"inmem": hb.backends.InMemBackend("inmem")}))


def _reset_ws(ws: hb.HeavenBase) -> hb.HeavenBase:
    """Drop prior backend state and return a newly initialized facade over the same backends."""
    ws_id = ws.id
    backends = dict(ws.backends.items())
    ws.drop()
    return hb.HeavenBase(ws_id, backends=backends)


def _toy_graph(ws: hb.HeavenBase, doc_cls: type) -> None:
    """d1 -> d2 -> d3, d4 -> d2 (cites)."""
    ws.register(doc_cls)
    ws.upsert_many(
        doc_cls,
        [
            {"object_id": "d1", "title": "one", "cites": [{"cited": "d2"}]},
            {"object_id": "d2", "title": "two", "cites": [{"cited": "d3"}]},
            {"object_id": "d3", "title": "three", "cites": []},
            {"object_id": "d4", "title": "four", "cites": [{"cited": "d2"}]},
        ],
    )


# --------------------------------------------------------------------------- offline (in-memory fallback)


def test_neo4j_publishes_graph_tag():
    """The neo4j Backend's static graph claim nominates it for graph routing."""
    assert hb.DEFAULT_CONTEXT.modules().tag("backend_type", "neo4j", "graph").value is True


def test_neo4j_provider_batch_preserves_anchor_before_later_delete():
    """A later endpoint delete must run after an earlier source upsert's anchor and edge sync."""
    from types import SimpleNamespace

    class OrderedDoc(hb.Entity):
        identifier = "unit-neo4j-ordered-doc"

        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-neo4j-ordered-doc"]]).store(strategy=hb.GraphEdge).optional()

    calls = []

    class Result:
        @staticmethod
        def single():
            return {"n": 1}

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, query, **params):
            calls.append((query, params))
            return Result()

        def commit(self):
            calls.append(("COMMIT", {}))

    class Session:
        def __init__(self):
            self.tx = Transaction()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def begin_transaction(self):
            return self.tx

    class Driver:
        @staticmethod
        def session():
            return Session()

    backend = object.__new__(Neo4jBackend)
    hb.backends.Backend.__init__(backend, "neo4j")
    backend.driver = Driver()
    backend.client_error = ""
    backend._schemas = {OrderedDoc.identifier: OrderedDoc.schema()}
    backend._graph_specs = {
        OrderedDoc.identifier: {
            "cites": SimpleNamespace(
                field_name="cites",
                rel_type="cites",
                endpoint_col="cited",
                target_entity=OrderedDoc.identifier,
                prop_cols=[],
            )
        }
    }

    backend.upsert(
        [
            hb.ext.RowOp(OrderedDoc.identifier, "source", {"title": "Source", "cites": [{"cited": "target"}]}),
            hb.ext.RowOp(OrderedDoc.identifier, "target", op="delete"),
        ]
    )

    queries = [query for query, _params in calls]
    anchor_index = next(index for index, query in enumerate(queries) if query.startswith("UNWIND"))
    delete_index = next(index for index, query in enumerate(queries) if "DETACH DELETE" in query)
    edge_index = next(index for index, query in enumerate(queries) if "MERGE (a)-[r:" in query)
    assert anchor_index < edge_index < delete_index


def test_graph_edge_field_degrades_to_json_on_inmem():
    """A GraphEdge field binds to a non-graph backend as a plain HyperG edge list (round-trips, props included)."""
    ws = _inmem_ws("unit-graph-degrade")

    class Doc(hb.Entity):
        identifier = "unit-graph-degrade-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-degrade-doc"], "weight" : {"type": hb.Float, "required": False}]).store(
            strategy=hb.GraphEdge
        )

    try:
        ws.register(Doc)
        ws.upsert_many(
            Doc,
            [
                {"object_id": "d1", "title": "one", "cites": [{"cited": "d2", "weight": 0.9}]},
                {"object_id": "d2", "title": "two", "cites": []},
            ],
        )
        binding = ws.storage_plans["unit-graph-degrade-doc"].first("cites")
        assert binding.backend == "inmem" and binding.strategy == hb.GraphEdge.identifier
        assert ws.get("d1", entity=Doc)["cites"] == [{"cited": "d2", "weight": 0.9}]
        assert ws.get("d2", entity=Doc)["cites"] == []
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-degrade", None)


def test_python_fallback_traverse_and_shortest_path():
    """ws.traverse / ws.shortest_path run the portable BFS fallback on a non-graph backend (out/in, multi-hop)."""
    ws = _inmem_ws("unit-graph-fallback")

    class Doc(hb.Entity):
        identifier = "unit-graph-fallback-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-fallback-doc"]]).store(strategy=hb.GraphEdge)

    try:
        _toy_graph(ws, Doc)
        assert sorted(r["object_id"] for r in ws.traverse(Doc.cites, "d1")) == ["d2"]
        assert sorted(r["object_id"] for r in ws.traverse(Doc.cites, "d1", max_hops=2)) == ["d2", "d3"]
        assert sorted(r["object_id"] for r in ws.traverse(Doc.cites, "d2", direction="in")) == ["d1", "d4"]
        assert sorted(r["object_id"] for r in ws.traverse(Doc.cites, "d2", direction="any")) == ["d1", "d3", "d4"]
        assert ws.shortest_path(Doc.cites, "d1", "d3") == [
            ("unit-graph-fallback-doc", "d1"),
            ("unit-graph-fallback-doc", "d2"),
            ("unit-graph-fallback-doc", "d3"),
        ]
        assert ws.shortest_path(Doc.cites, "d3", "d1") is None
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-fallback", None)


def test_routed_traverse_op_composes_with_where():
    """ws.query(Doc).traverse(...).where(...) folds the reachable set into the filter; explain reports py_traverse."""
    ws = _inmem_ws("unit-graph-routed")

    class Doc(hb.Entity):
        identifier = "unit-graph-routed-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-routed-doc"]]).store(strategy=hb.GraphEdge)

    try:
        _toy_graph(ws, Doc)
        assert sorted(ws.query(Doc).traverse(Doc.cites, "d1", max_hops=2).execute().ids) == ["d2", "d3"]
        assert ws.query(Doc).traverse(Doc.cites, "d1", max_hops=2).where(Doc.title == "three").execute().ids == ["d3"]
        assert ws.query_json(Doc, {"traverse": {"field": "cites", "start": "d1", "max_hops": 2}, "filter": {"title": "three"}}).execute().ids == ["d3"]
        assert ws.query(Doc).traverse(Doc.cites, "d3").execute().ids == []  # terminal node reaches nothing
        plan = ws.query(Doc).traverse(Doc.cites, "d1").explain()
        step = next(s for s in plan["steps"] if s["node"] == "traverse")
        assert step["mode"] == "py_traverse" and step["field"] == "cites"
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-routed", None)


def test_exists_filter_on_non_graph_backend():
    """The 1-hop exists filter resolves on a non-graph backend via the portable scan (no missing-handler error)."""
    ws = _inmem_ws("unit-graph-exists")

    class Doc(hb.Entity):
        identifier = "unit-graph-exists-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-exists-doc"]]).store(strategy=hb.GraphEdge)

    try:
        _toy_graph(ws, Doc)
        assert sorted(ws.query(Doc).where(Doc.cites.exists(cited="d2")).execute().ids) == ["d1", "d4"]
        step = next(s for s in ws.query(Doc).where(Doc.cites.exists(cited="d2")).explain()["steps"] if s.get("node") == "filter")
        assert step.get("handler_mode") in (None, "py_scan")  # portable scan on a non-graph backend
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-exists", None)


def test_traverse_hop_cap():
    """max_hops above the configured ceiling raises rather than running a deep/cyclic traversal away."""
    ws = _inmem_ws("unit-graph-cap")

    class Doc(hb.Entity):
        identifier = "unit-graph-cap-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-cap-doc"]]).store(strategy=hb.GraphEdge)

    try:
        _toy_graph(ws, Doc)
        with pytest.raises(ValueError, match="exceeds the cap"):
            ws.traverse(Doc.cites, "d1", max_hops=10_000)
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-cap", None)


def test_check_and_scrub_dangling_edges():
    """Audit and repair remove a legacy dangling edge introduced outside the workspace mutation path."""
    ws = _inmem_ws("unit-graph-scrub")

    class Doc(hb.Entity):
        identifier = "unit-graph-scrub-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-scrub-doc"]]).store(strategy=hb.GraphEdge)

    try:
        _toy_graph(ws, Doc)
        assert ws.check_graph() == []
        ws.backends.get("inmem").delete(Doc.identifier, ["d3"])
        dangling = ws.check_graph()
        assert dangling == [{"entity": "unit-graph-scrub-doc", "field": "cites", "source": "d2", "target": "d3"}]
        assert ws.scrub_dangling()["removed"] == 0  # dry run leaves the edge
        assert ws.check_graph() != []
        assert ws.scrub_dangling(confirm=True)["removed"] == 1
        assert ws.check_graph() == []
        assert ws.get("d2", entity=Doc)["cites"] == []
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-scrub", None)


def test_graph_maintenance_batches_audit_and_validated_repair(monkeypatch):
    """Audit scans one source entity once and repair validates one grouped rewrite batch."""
    ws = _inmem_ws("unit-graph-maintenance-batch")

    class Doc(hb.Entity):
        identifier = "unit-graph-maintenance-batch-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-maintenance-batch-doc"]]).store(strategy=hb.GraphEdge)
        sees = hb.field(hb.HyperG["seen" : hb.Identifier["unit-graph-maintenance-batch-doc"]]).store(strategy=hb.GraphEdge)

    try:
        ws.register(Doc)
        ws.upsert_many(
            Doc,
            [
                {"object_id": "a", "title": "A", "cites": [{"cited": "b"}], "sees": [{"seen": "c"}]},
                {"object_id": "b", "title": "B", "cites": [], "sees": []},
                {"object_id": "c", "title": "C", "cites": [], "sees": []},
                {"object_id": "d", "title": "D", "cites": [{"cited": "c"}], "sees": [{"seen": "b"}]},
            ],
        )
        backend = ws.backends.get("inmem")
        backend.delete(Doc.identifier, ["b", "c"])

        scans = []
        lookups = []
        validations = []
        original_scan = backend.scan
        original_exists_many = ws._row_lookup.id_exists_many
        original_validate = ws._graph_mutation.validate_upserts

        def tracked_scan(entity_id):
            scans.append(entity_id)
            return original_scan(entity_id)

        def tracked_exists_many(object_ids, entity=None, *, unique=True):
            ordered_ids = list(object_ids)
            lookups.append((entity.schema().entity_id if entity is not None else None, ordered_ids))
            return original_exists_many(ordered_ids, entity=entity, unique=unique)

        def tracked_validate(rows):
            row_list = list(rows)
            validations.append(len(row_list))
            return original_validate(row_list)

        monkeypatch.setattr(backend, "scan", tracked_scan)
        monkeypatch.setattr(ws._row_lookup, "id_exists_many", tracked_exists_many)
        monkeypatch.setattr(ws._graph_mutation, "validate_upserts", tracked_validate)

        expected = [
            {"entity": Doc.identifier, "field": "cites", "source": "a", "target": "b"},
            {"entity": Doc.identifier, "field": "cites", "source": "d", "target": "c"},
            {"entity": Doc.identifier, "field": "sees", "source": "a", "target": "c"},
            {"entity": Doc.identifier, "field": "sees", "source": "d", "target": "b"},
        ]
        assert ws.check_graph() == expected
        assert scans == [Doc.identifier]
        assert lookups == [(Doc.identifier, ["b", "c"])]

        scans.clear()
        lookups.clear()
        result = ws.scrub_dangling(Doc.cites, confirm=True)
        assert result == {"dangling": expected, "removed": 4}
        assert scans == [Doc.identifier]
        assert lookups == [(Doc.identifier, ["b", "c"])]
        assert validations == [2]
        assert ws.get("a", entity=Doc)["cites"] == []
        assert ws.get("a", entity=Doc)["sees"] == []
        assert ws.get("d", entity=Doc)["cites"] == []
        assert ws.get("d", entity=Doc)["sees"] == []
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-maintenance-batch", None)


def test_traverse_rejects_non_graph_field():
    """Traversing a non-GraphEdge field raises (a typo must not silently return empty)."""
    ws = _inmem_ws("unit-graph-badfield")

    class Doc(hb.Entity):
        identifier = "unit-graph-badfield-doc"
        title = hb.field(hb.ShortText)

    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "d1", "title": "one"})
        with pytest.raises(ValueError, match="not a GraphEdge field"):
            ws.traverse(Doc.title, "d1")
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-badfield", None)


def test_routed_traverse_cross_entity_raises():
    """The routed .traverse() op requires a self-referential edge; a cross-entity edge raises (use ws.traverse)."""
    ws = _inmem_ws("unit-graph-cross")

    class Author(hb.Entity):
        identifier = "unit-graph-cross-author"
        name = hb.field(hb.ShortText)

    class Doc(hb.Entity):
        identifier = "unit-graph-cross-doc"
        title = hb.field(hb.ShortText)
        authored_by = hb.field(hb.HyperG["author" : hb.Identifier["unit-graph-cross-author"]]).store(strategy=hb.GraphEdge)

    try:
        ws.register(Author)
        ws.register(Doc)
        ws.upsert(Author, {"object_id": "a1", "name": "Ada"})
        ws.upsert(Doc, {"object_id": "d1", "title": "one", "authored_by": [{"author": "a1"}]})
        # cross-entity traversal via the delegate is fine
        assert [r["object_id"] for r in ws.traverse(Doc.authored_by, "d1")] == ["a1"]
        # but the routed op restricts the queried entity to its own reachable set -> must reject,
        # and explain() must reject identically (no over-optimistic plan for a query execute() rejects)
        with pytest.raises(ValueError, match="self-referential"):
            ws.query(Doc).traverse(Doc.authored_by, "d1").execute()
        with pytest.raises(ValueError, match="self-referential"):
            ws.query(Doc).traverse(Doc.authored_by, "d1").explain()
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-cross", None)


def test_cross_entity_traverse_in_and_shortest_path_are_typed():
    """Cross-entity delegate traversal keeps logical entity ids for reverse traversal and shortest paths."""
    ws = _inmem_ws("unit-graph-cross-in")

    class Author(hb.Entity):
        identifier = "unit-graph-cross-in-author"
        name = hb.field(hb.ShortText)

    class Doc(hb.Entity):
        identifier = "unit-graph-cross-in-doc"
        title = hb.field(hb.ShortText)
        authored_by = hb.field(hb.HyperG["author" : hb.Identifier["unit-graph-cross-in-author"]]).store(strategy=hb.GraphEdge)

    try:
        ws.register(Author)
        ws.register(Doc)
        ws.upsert_many(Author, [{"object_id": "a1", "name": "Ada"}, {"object_id": "a2", "name": "Grace"}])
        ws.upsert_many(
            Doc,
            [
                {"object_id": "d1", "title": "one", "authored_by": [{"author": "a1"}]},
                {"object_id": "d2", "title": "two", "authored_by": [{"author": "a1"}]},
                {"object_id": "d3", "title": "three", "authored_by": [{"author": "a2"}]},
            ],
        )
        assert [r["object_id"] for r in ws.traverse(Doc.authored_by, "d1")] == ["a1"]
        assert sorted(r["object_id"] for r in ws.traverse(Doc.authored_by, "a1", direction="in")) == ["d1", "d2"]
        assert sorted(r["object_id"] for r in ws.traverse(Doc.authored_by, "a1", direction="any")) == ["d1", "d2"]
        assert ws.shortest_path(Doc.authored_by, "d1", "a1") == [
            ("unit-graph-cross-in-doc", "d1"),
            ("unit-graph-cross-in-author", "a1"),
        ]
        assert ws.shortest_path(Doc.authored_by, "a1", "d1", direction="in") == [
            ("unit-graph-cross-in-author", "a1"),
            ("unit-graph-cross-in-doc", "d1"),
        ]
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-cross-in", None)


def test_routed_traverse_pagination_tiebreak():
    """A routed traverse with order_by + limit paginates stably: object_id breaks ties on equal sort values (§8.5)."""
    ws = _inmem_ws("unit-graph-page")

    class Doc(hb.Entity):
        identifier = "unit-graph-page-doc"
        rank = hb.field(hb.Integer)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-page-doc"]]).store(strategy=hb.GraphEdge)

    try:
        ws.register(Doc)
        ws.upsert_many(
            Doc,
            [
                {"object_id": "d1", "rank": 0, "cites": [{"cited": "d2"}, {"cited": "d3"}, {"cited": "d4"}, {"cited": "d5"}]},
                {"object_id": "d2", "rank": 1, "cites": []},
                {"object_id": "d3", "rank": 1, "cites": []},
                {"object_id": "d4", "rank": 1, "cites": []},
                {"object_id": "d5", "rank": 1, "cites": []},
            ],
        )
        # all reached have rank=1 (a tie); without the object_id tiebreak pages could overlap/gap
        page1 = ws.query(Doc).traverse(Doc.cites, "d1").order_by(Doc.rank).offset(0).limit(2).execute().ids
        page2 = ws.query(Doc).traverse(Doc.cites, "d1").order_by(Doc.rank).offset(2).limit(2).execute().ids
        assert page1 == ["d2", "d3"] and page2 == ["d4", "d5"]  # stable, no overlap or gap
        step = next(s for s in ws.query(Doc).traverse(Doc.cites, "d1").explain()["steps"] if s["node"] == "traverse")
        assert step["endpoint_filter"] == "none"  # no .where
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-page", None)


def test_fallback_multi_hop_any_path_semantics():
    """min_hops>1 uses 'reachable by a path of length in range' (diamond): a node on a shorter path is still reached at hop 2."""
    ws = _inmem_ws("unit-graph-diamond")

    class Doc(hb.Entity):
        identifier = "unit-graph-diamond-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-diamond-doc"]]).store(strategy=hb.GraphEdge)

    try:
        # diamond: a -> b, a -> c, b -> c. 'c' is reachable at hop 1 (a->c) AND hop 2 (a->b->c).
        ws.register(Doc)
        ws.upsert_many(
            Doc,
            [
                {"object_id": "a", "title": "a", "cites": [{"cited": "b"}, {"cited": "c"}]},
                {"object_id": "b", "title": "b", "cites": [{"cited": "c"}]},
                {"object_id": "c", "title": "c", "cites": []},
            ],
        )
        assert sorted(r["object_id"] for r in ws.traverse(Doc.cites, "a", min_hops=1, max_hops=1)) == ["b", "c"]
        assert sorted(r["object_id"] for r in ws.traverse(Doc.cites, "a", min_hops=2, max_hops=2)) == ["c"]  # via a->b->c
        assert sorted(r["object_id"] for r in ws.traverse(Doc.cites, "a", min_hops=1, max_hops=2)) == ["b", "c"]
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-diamond", None)


def test_traverse_input_validation():
    """An invalid direction and an inverted hop range both raise (consistently on the fallback)."""
    ws = _inmem_ws("unit-graph-validate")

    class Doc(hb.Entity):
        identifier = "unit-graph-validate-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-validate-doc"]]).store(strategy=hb.GraphEdge)

    try:
        _toy_graph(ws, Doc)
        with pytest.raises(ValueError, match="direction"):
            ws.traverse(Doc.cites, "d1", direction="sideways")
        with pytest.raises(ValueError, match="cannot exceed"):
            ws.traverse(Doc.cites, "d1", min_hops=3, max_hops=2)
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-validate", None)


def test_multiple_graph_edge_fields_on_one_entity():
    """An entity with two GraphEdge fields rebuilds and detaches each field independently."""
    ws = _inmem_ws("unit-graph-multi")

    class Doc(hb.Entity):
        identifier = "unit-graph-multi-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-multi-doc"]]).store(strategy=hb.GraphEdge)
        sees = hb.field(hb.HyperG["seen" : hb.Identifier["unit-graph-multi-doc"]]).store(strategy=hb.GraphEdge)

    try:
        ws.register(Doc)
        ws.upsert_many(
            Doc,
            [
                {"object_id": "d1", "title": "one", "cites": [{"cited": "d2"}], "sees": [{"seen": "d3"}]},
                {"object_id": "d2", "title": "two", "cites": [], "sees": []},
                {"object_id": "d3", "title": "three", "cites": [], "sees": []},
            ],
        )
        row = ws.get("d1", entity=Doc)
        assert row["cites"] == [{"cited": "d2"}] and row["sees"] == [{"seen": "d3"}]
        assert [r["object_id"] for r in ws.traverse(Doc.cites, "d1")] == ["d2"]
        assert [r["object_id"] for r in ws.traverse(Doc.sees, "d1")] == ["d3"]
        ws.delete((Doc, "d3"), edges="detach")
        assert ws.check_graph() == []
        after = ws.get("d1", entity=Doc)
        assert after["cites"] == [{"cited": "d2"}] and after["sees"] == []
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-multi", None)


def test_graph_on_missing_option_is_removed():
    """Neo4j cannot weaken the workspace's backend-independent strong-reference contract."""
    with pytest.raises(TypeError, match="graph_on_missing"):
        Neo4jBackend("n", url="bolt://127.0.0.1:1", graph_on_missing="stubb")


def test_non_scalar_edge_property_rejected_offline():
    """ensure() rejects a GraphEdge whose edge property is non-scalar (plan §8.1 forbids json/array/vector/hyper-g)."""
    bindings = [type("B", (), {"backend": "n", "strategy": hb.GraphEdge.identifier, "field_name": "cites"})()]

    class JsonEdge(hb.Entity):
        identifier = "unit-graph-jsonedge-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-jsonedge-doc"], "meta" : {"type": hb.Json, "required": False}]).store(
            strategy=hb.GraphEdge
        )

    class VectorEdge(hb.Entity):
        identifier = "unit-graph-vecedge-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-vecedge-doc"], "vec" : {"type": hb.Vector[3], "required": False}]).store(
            strategy=hb.GraphEdge
        )

    for entity in (JsonEdge, VectorEdge):  # vector is storable by neo4j but forbidden by the spec — both must reject
        backend = Neo4jBackend("n", url="bolt://127.0.0.1:1")  # offline (driver=None); ensure still builds graph specs
        with pytest.raises(ValueError, match="non-scalar edge-property"):
            backend.ensure(entity.schema(), bindings)


def test_graph_edge_target_param_and_multi_identifier():
    """GraphEdge(target='col') names the endpoint, carries the param to the binding, and allows >1 Identifier column."""
    ws = _inmem_ws("unit-graph-target")

    class User(hb.Entity):
        identifier = "unit-graph-target-user"
        name = hb.field(hb.ShortText)

    class Doc(hb.Entity):
        identifier = "unit-graph-target-doc"
        title = hb.field(hb.ShortText)
        # two Identifier columns: 'cited' (plain, self-ref endpoint) + 'by' (Identifier[User], an edge property-ref)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier, "by" : hb.Identifier[User]]).store(strategy=hb.GraphEdge(target="cited"))

    try:
        ws.register(User)
        ws.register(Doc)
        ws.upsert(User, {"object_id": "u1", "name": "Ada"})
        ws.upsert_many(Doc, [{"object_id": "d1", "title": "1", "cites": [{"cited": "d2", "by": "u1"}]}, {"object_id": "d2", "title": "2", "cites": []}])
        binding = ws.storage_plans["unit-graph-target-doc"].first("cites")
        assert binding.params == {"target": "cited"} and binding.to_dict()["params"] == {"target": "cited"}  # param carried + serialized
        assert ws.get("d1", entity=Doc)["cites"] == [{"cited": "d2", "by": "u1"}]  # endpoint + property-ref both round-trip
        assert [r["object_id"] for r in ws.traverse(Doc.cites, "d1")] == ["d2"]  # endpoint = the named 'cited' column (plain Identifier -> self-ref)
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-target", None)


def test_graph_edge_target_validation():
    """GraphEdge(target=...) naming a missing or non-Identifier column raises a clear error."""
    ws = _inmem_ws("unit-graph-target-bad")

    class BadCol(hb.Entity):
        identifier = "unit-graph-badcol-doc"
        cites = hb.field(hb.HyperG["cited" : hb.Identifier]).store(strategy=hb.GraphEdge(target="nope"))

    class BadType(hb.Entity):
        identifier = "unit-graph-badtype-doc"
        cites = hb.field(hb.HyperG["cited" : hb.Identifier, "weight" : hb.Float]).store(strategy=hb.GraphEdge(target="weight"))

    try:
        with pytest.raises(ValueError, match="not a column"):
            ws.register(BadCol)
            ws.upsert(BadCol, {"object_id": "d1", "cites": []})
        with pytest.raises(ValueError, match="must be an Identifier"):
            ws.register(BadType)
            ws.upsert(BadType, {"object_id": "d1", "cites": []})
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-target-bad", None)


def test_native_traverse_endpoint_predicates_are_normalized():
    """Endpoint predicates fused into native traversal use the entity field dtype, not raw JSON/string operands."""
    from heavenbase.placement import StorageBinding, StoragePlan

    class RankDoc(hb.Entity):
        identifier = "unit-graph-pushdown-normalize-doc"
        rank = hb.field(hb.Integer)

    schema = RankDoc.schema()
    plan = StoragePlan(schema.entity_id, [StorageBinding(schema.entity_id, "rank", "g", "inline")])
    assert pushable_endpoint_preds(RankDoc.rank == "2", schema, plan, "g") == [("rank", "eq", 2)]
    assert pushable_endpoint_preds(RankDoc.rank.in_(["1", "2"]), schema, plan, "g") == [("rank", "in", [1, 2])]


def test_native_edge_exists_predicates_are_normalized():
    """GraphEdge exists predicates pushed to Neo4j use HyperG column dtypes before Cypher parameters are built."""

    class EdgeDoc(hb.Entity):
        identifier = "unit-graph-exists-normalize-doc"
        title = hb.field(hb.ShortText)
        authored_by = hb.field(hb.HyperG["author" : hb.Identifier["unit-graph-exists-normalize-doc"], "position" : hb.Integer]).store(
            strategy=hb.GraphEdge(target="author")
        )

    fragment = neo4j_graph_exists_handler(
        EdgeDoc.schema().fields["authored_by"],
        {"author": "d2", "position": "1"},
        HandlerContext("n", "neo4j", hb.GraphEdge.identifier),
    )

    assert fragment.notes["mode"] == "native"
    assert fragment.payload["predicates"] == {"author": "d2", "position": 1}


def test_shortest_path_start_equals_end_is_zero_length():
    """shortest_path(x, x) is the zero-length path [x] on every backend (native Neo4j shortestPath would return None — the dispatcher short-circuits for parity)."""
    ws = _inmem_ws("unit-graph-selfpath")

    class Doc(hb.Entity):
        identifier = "unit-graph-selfpath-doc"
        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-selfpath-doc"]]).store(strategy=hb.GraphEdge)

    try:
        _toy_graph(ws, Doc)
        assert ws.shortest_path(Doc.cites, "d2", "d2") == [("unit-graph-selfpath-doc", "d2")]
        assert ws.shortest_path(Doc.cites, "d2", "d2", direction="in") == [("unit-graph-selfpath-doc", "d2")]
        assert ws.shortest_path(Doc.cites, "d2", "d2", direction="any") == [("unit-graph-selfpath-doc", "d2")]
        assert ws.shortest_path(Doc.cites, "d1", "d3") is not None  # a real multi-hop path is unaffected
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-selfpath", None)


def test_cross_entity_any_traverse_records_ordered_by_object_id():
    """A cross-entity `any` traverse returns records ordered by object_id (not by entity) — matching native ORDER BY oid."""
    ws = _inmem_ws("unit-graph-anyorder")

    class Author(hb.Entity):
        identifier = "zzz-anyorder-author"  # entity id deliberately sorts AFTER the doc entity

        name = hb.field(hb.ShortText)

    class Doc(hb.Entity):
        identifier = "aaa-anyorder-doc"
        title = hb.field(hb.ShortText)
        authored_by = hb.field(hb.HyperG["author" : hb.Identifier["zzz-anyorder-author"]]).store(strategy=hb.GraphEdge)

    try:
        ws.register(Author)
        ws.register(Doc)
        ws.upsert(Author, {"object_id": "m1", "name": "Ada"})
        ws.upsert_many(
            Doc,
            [{"object_id": "n1", "title": "1", "authored_by": [{"author": "m1"}]}, {"object_id": "n2", "title": "2", "authored_by": [{"author": "m1"}]}],
        )
        # 2-hop 'any' from n1 reaches Author 'm1' (hop 1) + sibling Doc 'n2' (hop 2) -> records from BOTH entities.
        result = [r["object_id"] for r in ws.traverse(Doc.authored_by, "n1", direction="any", max_hops=2)]
        assert result == ["m1", "n2"]  # by object_id; the old entity-first order would have been ["n2", "m1"]
    finally:
        ws.drop()
        WORKSPACES.pop("unit-graph-anyorder", None)


def test_pushable_endpoint_preds_skip_off_graph_fields():
    """A traverse endpoint predicate on a field NOT stored on the graph backend is not fused (it would NULL-exclude / over-prune the native traverse)."""
    from heavenbase.placement import StorageBinding, StoragePlan

    class ColocDoc(hb.Entity):
        identifier = "unit-graph-coloc-doc"
        year = hb.field(hb.Integer)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["unit-graph-coloc-doc"]]).store(strategy=hb.GraphEdge)

    schema = ColocDoc.schema()
    eid = "unit-graph-coloc-doc"
    # 'year' lives on a non-graph backend; the GraphEdge traverse runs on 'g', where 'year' is not a node property
    split_plan = StoragePlan(eid, [StorageBinding(eid, "year", "other", "inline"), StorageBinding(eid, "cites", "g", hb.GraphEdge.identifier)])
    assert pushable_endpoint_preds(ColocDoc.year == 2020, schema, split_plan, "g") == []  # skipped: not co-located on 'g'
    # same field co-located on the graph backend -> still pushed (and normalized through the dtype)
    coloc_plan = StoragePlan(eid, [StorageBinding(eid, "year", "g", "inline")])
    assert pushable_endpoint_preds(ColocDoc.year == "2020", schema, coloc_plan, "g") == [("year", "eq", 2020)]


def test_graph_edge_target_and_policy_survive_manifest_roundtrip():
    """GraphEdge endpoint params and non-default strategy policy survive manifest export and reimport."""

    class ManifestDoc(hb.Entity):
        identifier = "unit-graph-manifest-doc"
        title = hb.field(hb.ShortText).store(to="main")
        # single PLAIN Identifier endpoint named via target=; params is the only record of the endpoint, and
        # auto-detect (0 Identifier[Entity] columns) would raise on the clone if the target were dropped.
        cites = hb.field(hb.HyperG["cited" : hb.Identifier]).store(
            to="main",
            strategy=hb.GraphEdge(target="cited", on_delete="detach"),
        )

    source = hb.HeavenBase("unit-graph-manifest-src", detached=True, backends={"main": {"type": "inmem"}})
    source.register(ManifestDoc)
    data = source.to_manifest()
    assert data["entities"][0]["fields"]["cites"]["stores"] == [
        {
            "backend": "main",
            "strategy": {"type": hb.GraphEdge.identifier, "on_delete": "detach"},
            "optional": False,
            "reason": "field",
            "params": {"target": "cited"},
        }
    ]
    clone = hb.HeavenBase.from_manifest(hb.WorkspaceManifest.from_dict(data), detached=True)
    binding = clone.storage_plans["unit-graph-manifest-doc"].first("cites")
    assert binding.params == {"target": "cited"}
    assert binding.strategy_options == {"on_delete": "detach"}
    clone_doc = clone.entities["unit-graph-manifest-doc"]
    clone.upsert_many(clone_doc, [{"object_id": "d1", "title": "1", "cites": [{"cited": "d2"}]}, {"object_id": "d2", "title": "2", "cites": []}])
    assert [r["object_id"] for r in clone.traverse(clone_doc.cites, "d1")] == ["d2"]  # endpoint resolves to the named 'cited' -> traverse works


# --------------------------------------------------------------------------- live (real Neo4j) — @external


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _live_backend(name: str, *, ws_id: str | None = None) -> Neo4jBackend:
    """Return a connected Neo4jBackend or skip when the driver/server/auth is unavailable."""
    if find_spec("neo4j") is None:
        pytest.skip("neo4j driver is not installed")
    parsed = urlparse(_NEO4J_URL)
    host, port = parsed.hostname or "localhost", parsed.port or 7687
    if not _reachable(host, port):
        pytest.skip(f"neo4j server is not reachable on {host}:{port}")
    backend = Neo4jBackend(name, url=_NEO4J_URL, ws_id=ws_id)
    if backend.driver is None:
        pytest.skip(f"neo4j connect/auth failed: {backend.client_error}")
    return backend


class _LiveDoc(hb.Entity):
    identifier = "docker-neo4j-graph-doc"
    title = hb.field(hb.ShortText)
    cites = hb.field(hb.HyperG["cited" : hb.Identifier["docker-neo4j-graph-doc"], "weight" : {"type": hb.Float, "required": False}]).store(
        strategy=hb.GraphEdge
    )


class _LiveSplitPaper(hb.Entity):
    identifier = "docker-neo4j-split-paper"

    title = hb.field(hb.ShortText).store(to="row")


class _LiveSplitAuthor(hb.Entity):
    identifier = "docker-neo4j-split-author"

    name = hb.field(hb.ShortText).store(to="row")
    wrote = (
        hb.field(hb.HyperG["paper" : hb.Identifier[_LiveSplitPaper], "year" : hb.Integer]).store(to="graph", strategy=hb.GraphEdge(target="paper")).optional()
    )


@pytest.mark.external
def test_neo4j_graph_edge_roundtrip_live():
    """Edges materialise as real relationships and rebuild to the same logical HyperG value on read."""
    ws = hb.HeavenBase("docker-neo4j-graph-rt", backends={"n": _live_backend("ng")})
    try:
        ws = _reset_ws(ws)
        ws.register(_LiveDoc)
        ws.upsert_many(
            _LiveDoc,
            [
                {"object_id": "d1", "title": "one", "cites": [{"cited": "d2", "weight": 0.9}]},
                {"object_id": "d2", "title": "two", "cites": [{"cited": "d3"}]},
                {"object_id": "d3", "title": "three", "cites": []},
                {"object_id": "d4", "title": "four", "cites": [{"cited": "d2"}]},
            ],
        )
        assert ws.get("d1", entity=_LiveDoc)["cites"] == [{"cited": "d2", "weight": 0.9}]
        assert ws.get("d2", entity=_LiveDoc)["cites"] == [{"cited": "d3"}]  # omitted optional prop is absent, as on JSON backends
        assert ws.get("d3", entity=_LiveDoc)["cites"] == []  # terminal node, no out-edges
        # 1-hop edge filter (exists) -> who cites d2
        assert sorted(ws.query(_LiveDoc).where(_LiveDoc.cites.exists(cited="d2")).execute().ids) == ["d1", "d4"]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-rt", None)


@pytest.mark.external
def test_neo4j_graph_strong_target_validation_live():
    """Native graph writes reject missing targets before replacing the source row."""
    ws = hb.HeavenBase("docker-neo4j-graph-strict", backends={"n": _live_backend("ngs")})
    try:
        ws = _reset_ws(ws)
        ws.register(_LiveDoc)
        ws.upsert(_LiveDoc, {"object_id": "d1", "title": "one"})
        with pytest.raises(ValueError, match="endpoint does not exist"):
            ws.upsert(_LiveDoc, {"object_id": "d1", "title": "replacement", "cites": [{"cited": "ghost"}]})
        row = ws.get("d1", entity=_LiveDoc)
        assert row["title"] == "one" and row.get("cites") in (None, [])
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-strict", None)


@pytest.mark.external
def test_neo4j_split_routes_use_non_authoritative_endpoint_anchors_live():
    """Native relationships use typed anchors when logical targets live on another route."""
    graph = _live_backend("graph")
    ws = hb.HeavenBase(
        "docker-neo4j-split-anchor",
        backends={
            "row": hb.backends.InMemBackend("row"),
            "graph": graph,
        },
    )
    try:
        ws = _reset_ws(ws)
        graph = ws.backends.get("graph")
        ws.register(_LiveSplitPaper)
        ws.register(_LiveSplitAuthor)
        ws.upsert_many(
            _LiveSplitAuthor,
            [
                _LiveSplitAuthor.from_dict(
                    object_id="a1",
                    name="A1",
                    wrote=[{"paper": "p1", "year": 2020}],
                ),
                _LiveSplitPaper.from_dict(object_id="p1", title="P1"),
            ],
        )
        assert graph._schemas.get(_LiveSplitPaper.identifier) is None
        assert [row["object_id"] for row in ws.traverse(_LiveSplitAuthor.wrote, "a1")] == ["p1"]
        assert [row["object_id"] for row in ws.traverse(_LiveSplitAuthor.wrote, "p1", direction="in")] == ["a1"]
        assert ws.shortest_path(_LiveSplitAuthor.wrote, "a1", "p1") == [
            (_LiveSplitAuthor.identifier, "a1"),
            (_LiveSplitPaper.identifier, "p1"),
        ]

        assert ws.delete((_LiveSplitPaper, "p1"), edges="detach") is True
        assert ws.get("a1", entity=_LiveSplitAuthor)["wrote"] == []
        with graph.driver.session() as session:
            anchor_count = session.run(
                f"MATCH (n:{graph._ident(graph._label(_LiveSplitPaper.identifier))} {{object_id: $oid}}) RETURN count(n) AS n",
                oid="p1",
            ).single()["n"]
        assert anchor_count == 0

        ws.upsert(_LiveSplitPaper, {"object_id": "p1", "title": "P1 recreated"})
        assert ws.traverse(_LiveSplitAuthor.wrote, "a1") == []
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-split-anchor", None)


@pytest.mark.external
def test_neo4j_native_traverse_and_shortest_path_live():
    """Native variable-length traversal (out/in/any, multi-hop) and shortestPath over real relationships."""
    ws = hb.HeavenBase("docker-neo4j-graph-tr", backends={"n": _live_backend("ngt")})
    try:
        ws = _reset_ws(ws)
        _toy_graph(ws, _LiveDoc)
        assert sorted(r["object_id"] for r in ws.traverse(_LiveDoc.cites, "d1")) == ["d2"]
        assert sorted(r["object_id"] for r in ws.traverse(_LiveDoc.cites, "d1", max_hops=2)) == ["d2", "d3"]
        assert sorted(r["object_id"] for r in ws.traverse(_LiveDoc.cites, "d2", direction="in")) == ["d1", "d4"]
        assert sorted(r["object_id"] for r in ws.traverse(_LiveDoc.cites, "d2", direction="any")) == ["d1", "d3", "d4"]
        assert ws.shortest_path(_LiveDoc.cites, "d1", "d3") == [
            ("docker-neo4j-graph-doc", "d1"),
            ("docker-neo4j-graph-doc", "d2"),
            ("docker-neo4j-graph-doc", "d3"),
        ]
        assert ws.shortest_path(_LiveDoc.cites, "d3", "d1") is None
        assert ws.shortest_path(_LiveDoc.cites, "d2", "d2") == [("docker-neo4j-graph-doc", "d2")]  # start==end -> zero-length path (native parity)
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((_LiveDoc, "d3"))
        assert ws.delete((_LiveDoc, "d3"), edges="detach") is True
        assert ws.get("d2", entity=_LiveDoc)["cites"] == []
        ws.upsert(_LiveDoc, {"object_id": "d3", "title": "recreated", "cites": []})
        assert ws.get("d2", entity=_LiveDoc)["cites"] == []
        assert ws.check_graph() == []
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-tr", None)


@pytest.mark.external
def test_neo4j_routed_traverse_op_live():
    """The routed .traverse().where() op runs native on neo4j; explain reports native_traverse."""
    ws = hb.HeavenBase("docker-neo4j-graph-6b", backends={"n": _live_backend("ng6")})
    try:
        ws = _reset_ws(ws)
        _toy_graph(ws, _LiveDoc)
        assert sorted(ws.query(_LiveDoc).traverse(_LiveDoc.cites, "d1", max_hops=2).execute().ids) == ["d2", "d3"]
        # the .where pushable predicate fuses into the traverse WHERE b.* (§8.7); result matches the
        # delegate-traverse + manual filter, proving the fusion is correct (not over-pruning)
        filtered = ws.query(_LiveDoc).traverse(_LiveDoc.cites, "d1", max_hops=2).where(_LiveDoc.title == "three").execute().ids
        assert filtered == ["d3"]
        assert sorted(r["object_id"] for r in ws.traverse(_LiveDoc.cites, "d1", max_hops=2) if r["title"] == "three") == ["d3"]
        step = next(
            s
            for s in ws.query(_LiveDoc).traverse(_LiveDoc.cites, "d1", max_hops=2).where(_LiveDoc.title == "three").explain()["steps"]
            if s["node"] == "traverse"
        )
        assert step["mode"] == "native_traverse" and step["endpoint_filter"] == "native+postfilter"
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-6b", None)


@pytest.mark.external
def test_neo4j_rejects_parallel_edges_live():
    """Two edges to the same target raise (a relationship is unique per (source, target); no silent collapse)."""
    ws = hb.HeavenBase("docker-neo4j-graph-par", backends={"n": _live_backend("ngp")})
    try:
        ws = _reset_ws(ws)
        ws.register(_LiveDoc)
        ws.upsert(_LiveDoc, {"object_id": "d2", "title": "two"})
        with pytest.raises(ValueError, match="parallel edges"):
            ws.upsert(_LiveDoc, {"object_id": "d1", "title": "one", "cites": [{"cited": "d2", "weight": 0.1}, {"cited": "d2", "weight": 0.9}]})
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-par", None)


@pytest.mark.external
def test_neo4j_native_multi_hop_matches_any_path_semantics_live():
    """Native min_hops>1 on a diamond returns nodes reachable by a path of that length (matches the fallback)."""
    ws = hb.HeavenBase("docker-neo4j-graph-dia", backends={"n": _live_backend("ngd")})
    try:
        ws = _reset_ws(ws)
        ws.register(_LiveDoc)
        ws.upsert_many(
            _LiveDoc,
            [
                {"object_id": "a", "title": "a", "cites": [{"cited": "b"}, {"cited": "c"}]},
                {"object_id": "b", "title": "b", "cites": [{"cited": "c"}]},
                {"object_id": "c", "title": "c", "cites": []},
            ],
        )
        assert sorted(r["object_id"] for r in ws.traverse(_LiveDoc.cites, "a", min_hops=2, max_hops=2)) == ["c"]  # a->b->c
        assert sorted(r["object_id"] for r in ws.traverse(_LiveDoc.cites, "a", min_hops=1, max_hops=1)) == ["b", "c"]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-dia", None)


class _ExistsDoc(hb.Entity):
    identifier = "docker-neo4j-graph-exists-doc"
    title = hb.field(hb.ShortText)
    year = hb.field(hb.Integer)
    cites = hb.field(hb.HyperG["cited" : hb.Identifier["docker-neo4j-graph-exists-doc"], "weight" : {"type": hb.Float, "required": False}]).store(
        strategy=hb.GraphEdge
    )


@pytest.mark.external
def test_neo4j_native_edge_exists_filter_live():
    """exists pushes to a native relationship EXISTS (explain mode 'native'), incl. an edge-property predicate and a combined tree."""
    ws = hb.HeavenBase("docker-neo4j-graph-exists", backends={"n": _live_backend("nge")})
    try:
        ws = _reset_ws(ws)
        ws.register(_ExistsDoc)
        ws.upsert_many(
            _ExistsDoc,
            [
                {"object_id": "d1", "title": "one", "year": 2019, "cites": [{"cited": "d2", "weight": 0.9}]},
                {"object_id": "d2", "title": "two", "year": 2021, "cites": [{"cited": "d3"}]},
                {"object_id": "d3", "title": "three", "year": 2022, "cites": []},
                {"object_id": "d4", "title": "four", "year": 2023, "cites": [{"cited": "d2", "weight": 0.1}]},
            ],
        )
        assert sorted(ws.query(_ExistsDoc).where(_ExistsDoc.cites.exists(cited="d2")).execute().ids) == ["d1", "d4"]
        step = next(s for s in ws.query(_ExistsDoc).where(_ExistsDoc.cites.exists(cited="d2")).explain()["steps"] if s.get("node") == "filter")
        assert step.get("handler_mode") == "native"  # native relationship EXISTS, not a scan
        assert ws.query(_ExistsDoc).where(_ExistsDoc.cites.exists(cited="d2", weight=0.9)).execute().ids == ["d1"]  # edge-property predicate
        assert ws.query(_ExistsDoc).where(_ExistsDoc.cites.exists(cited="d2") & (_ExistsDoc.year > 2022)).execute().ids == ["d4"]  # combined tree, still native
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-exists", None)


class _TgtAuthor(hb.Entity):
    identifier = "docker-neo4j-tgt-author"
    name = hb.field(hb.ShortText)


class _TgtDoc(hb.Entity):
    identifier = "docker-neo4j-tgt-doc"
    title = hb.field(hb.ShortText)
    cites = hb.field(hb.HyperG["cited" : hb.Identifier, "by" : hb.Identifier[_TgtAuthor]]).store(
        strategy=hb.GraphEdge(target="cited")
    )  # self-ref endpoint + property-ref
    authored_by = hb.field(hb.HyperG["author" : hb.Identifier[_TgtAuthor], "position" : hb.Integer]).store(
        strategy=hb.GraphEdge(target="author")
    )  # cross-entity via target=


@pytest.mark.external
def test_neo4j_graph_target_form_live():
    """GraphEdge(target=...) materialises edges natively: a self-ref endpoint + an Identifier property-ref, and a cross-entity edge."""
    ws = hb.HeavenBase("docker-neo4j-graph-tgt", backends={"n": _live_backend("ngtg")})
    try:
        ws = _reset_ws(ws)
        ws.register(_TgtAuthor)
        ws.register(_TgtDoc)
        ws.upsert(_TgtAuthor, {"object_id": "a1", "name": "Ada"})
        ws.upsert_many(
            _TgtDoc,
            [
                {"object_id": "d1", "title": "1", "cites": [{"cited": "d2", "by": "a1"}], "authored_by": [{"author": "a1", "position": 1}]},
                {"object_id": "d2", "title": "2", "cites": [], "authored_by": []},
            ],
        )
        assert ws.get("a1", entity=_TgtAuthor)["name"] == "Ada"  # merge-only identity anchoring preserves a co-located full row
        assert ws.get("d1", entity=_TgtDoc)["cites"] == [{"cited": "d2", "by": "a1"}]  # endpoint + Identifier property-ref both round-trip
        assert [r["object_id"] for r in ws.traverse(_TgtDoc.cites, "d1")] == ["d2"]  # endpoint is the named 'cited' (plain Identifier -> self-ref)
        assert [r["object_id"] for r in ws.traverse(_TgtDoc.authored_by, "d1")] == ["a1"]  # cross-entity -> Author (entity from Identifier[Author])
        assert ws.query(_TgtDoc).where(_TgtDoc.authored_by.exists(author="a1", position="1")).execute().ids == ["d1"]
        assert [r["object_id"] for r in ws.traverse(_TgtDoc.authored_by, "a1", direction="in")] == ["d1"]
        assert ws.shortest_path(_TgtDoc.authored_by, "a1", "d1", direction="in") == [
            ("docker-neo4j-tgt-author", "a1"),
            ("docker-neo4j-tgt-doc", "d1"),
        ]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-tgt", None)


@pytest.mark.external
def test_neo4j_cross_entity_traverse_id_collision_live():
    """A cross-entity traverse keeps a target that shares the start's object_id (object_ids are per-entity namespaced).

    Native must NOT exclude the reached node by object_id alone (it carries the opposite label), matching the
    typed Python fallback, which removes only the typed start ref.
    """
    ws = hb.HeavenBase("docker-neo4j-graph-idcol", backends={"n": _live_backend("ngic")})
    try:
        ws = _reset_ws(ws)
        # both an Author and a Doc exist with object_id 'x1'; d1.authored_by -> Author 'x1'
        ws.register(_TgtAuthor)
        ws.register(_TgtDoc)
        ws.upsert(_TgtAuthor, {"object_id": "x1", "name": "Ada"})
        ws.upsert_many(_TgtDoc, [{"object_id": "x1", "title": "self-named", "cites": [], "authored_by": [{"author": "x1", "position": 1}]}])
        # out: Doc 'x1' -> Author 'x1' must be returned, not dropped by the start-id guard
        assert [r["object_id"] for r in ws.traverse(_TgtDoc.authored_by, "x1")] == ["x1"]
        # in: Author 'x1' <- Doc 'x1' must be returned too
        assert [r["object_id"] for r in ws.traverse(_TgtDoc.authored_by, "x1", direction="in")] == ["x1"]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-graph-idcol", None)
