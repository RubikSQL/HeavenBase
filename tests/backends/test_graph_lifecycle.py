"""Cross-backend GraphEdge write and deletion lifecycle contracts."""

from __future__ import annotations

import os
from importlib.util import find_spec

import pytest

import heavenbase as hb

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry

_POSTGRES_URL = os.environ.get("HVNB_POSTGRES_URL", "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer")


class _GraphTagInMemBackend(hb.backends.InMemBackend):
    """Importable InMem test Backend whose static tags are registered per test."""

    identifier = "graph-tag-inmem-test"


def _register_graph_tag_backend(**overrides):
    """Publish an InMem-equivalent test Backend with explicit behavior tags."""
    modules = hb.DEFAULT_CONTEXT.modules()
    row = modules.inspect("backend_type", "inmem")[0]["record"]
    tags = dict(row["meta"]["tags"])
    _GraphTagInMemBackend.register(tags={**tags, **overrides}, resolver=modules)
    return modules


def _require_postgres() -> None:
    """Require an authenticated PostgreSQL test service."""
    import sqlalchemy as sa

    engine = sa.create_engine(_POSTGRES_URL, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if "HVNB_POSTGRES_URL" in os.environ:
            pytest.fail(f"configured PostgreSQL test service is unavailable: {detail}", pytrace=False)
        pytest.skip(f"default PostgreSQL endpoint is not an authenticated test service: {detail}")
    finally:
        engine.dispose()


class _LifeDoc(hb.Entity):
    identifier = "graph-life-doc"

    title = hb.field(hb.ShortText)
    cites = (
        hb.field(
            hb.HyperG[
                "cited" : hb.Identifier["graph-life-doc"],
                "weight" : {"type": hb.Float, "required": False},
            ]
        )
        .store(strategy=hb.GraphEdge)
        .optional()
    )


class _DetachDoc(hb.Entity):
    identifier = "graph-detach-doc"

    title = hb.field(hb.ShortText)
    cites = hb.field(hb.HyperG["cited" : hb.Identifier["graph-detach-doc"]]).store(strategy=hb.GraphEdge(on_delete="detach")).optional()


class _MixedDoc(hb.Entity):
    identifier = "graph-mixed-doc"

    title = hb.field(hb.ShortText)
    cites = hb.field(hb.HyperG["cited" : hb.Identifier["graph-mixed-doc"]]).store(strategy=hb.GraphEdge).optional()
    sees = hb.field(hb.HyperG["seen" : hb.Identifier["graph-mixed-doc"]]).store(strategy=hb.GraphEdge(on_delete="detach")).optional()


def _edge_count(cites):
    return len(cites or [])


class _DerivedDoc(hb.Entity):
    identifier = "graph-derived-doc"

    title = hb.field(hb.ShortText)
    cites = hb.field(hb.HyperG["cited" : hb.Identifier["graph-derived-doc"]]).store(strategy=hb.GraphEdge).optional()
    edge_count = hb.field(hb.Integer).compute(_edge_count, inputs=["cites"])


class _Paper(hb.Entity):
    identifier = "graph-life-paper"

    title = hb.field(hb.ShortText)


class _Author(hb.Entity):
    identifier = "graph-life-author"

    name = hb.field(hb.ShortText)
    wrote = (
        hb.field(
            hb.HyperG[
                "paper" : hb.Identifier[_Paper],
                "year" : hb.Integer,
            ]
        )
        .store(strategy=hb.GraphEdge(target="paper"))
        .optional()
    )


def test_explicit_graph_field_requires_its_entity_class_to_be_present():
    """Graph reads and maintenance must resolve field ownership through workspace schema authority."""

    class UnknownGraphDoc(hb.Entity):
        identifier = "unknown-graph-doc"

        links = hb.field(hb.HyperG["target" : hb.Identifier["unknown-graph-doc"]]).store(strategy=hb.GraphEdge).optional()

    workspace = hb.HeavenBase("graph-unregistered-field", detached=True, backends={"main": {"type": "inmem"}})
    calls = (
        lambda: workspace.traverse(UnknownGraphDoc.links, "source"),
        lambda: workspace.shortest_path(UnknownGraphDoc.links, "source", "target"),
        lambda: workspace.check_graph(UnknownGraphDoc.links),
        lambda: workspace.scrub_dangling(UnknownGraphDoc.links),
    )

    for call in calls:
        with pytest.raises(KeyError, match="has no registered Entity class"):
            call()


@pytest.fixture(params=("inmem", "sqlite", "json", "pickle"))
def graph_workspace(request, tmp_path):
    """Provide the portable GraphEdge contract over each built-in row representation."""
    kind = request.param
    ws_id = f"graph-lifecycle-{kind}"
    if kind == "inmem":
        backend = hb.backends.InMemBackend("main")
    elif kind == "sqlite":
        backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "graph.db"))
    elif kind == "json":
        backend = hb.backends.JsonFileBackend("main", hb.utils.path_to_file_uri(tmp_path / "json"))
    else:
        backend = hb.backends.PickleFileBackend("main", hb.utils.path_to_file_uri(tmp_path / "pickle"))
    workspace = hb.HeavenBase(ws_id, backends={"main": backend})
    for entity in (_LifeDoc, _DetachDoc, _MixedDoc, _DerivedDoc, _Paper, _Author):
        workspace.register(entity)
    try:
        yield workspace
    finally:
        workspace.drop()
        WORKSPACES.pop(ws_id, None)


def test_graph_writes_are_strong_batch_aware_and_non_partial(graph_workspace):
    """Targets may be in the same batch, while missing or parallel endpoints reject before row mutation."""
    ws = graph_workspace
    assert ws.upsert_many(
        _LifeDoc,
        [
            {"object_id": "d1", "title": "one", "cites": [{"cited": "d2", "weight": 0.5}]},
            {"object_id": "d2", "title": "two", "cites": []},
        ],
    ) == ["d1", "d2"]
    assert ws.get("d1", entity=_LifeDoc)["cites"] == [{"cited": "d2", "weight": 0.5}]

    with pytest.raises(ValueError, match="endpoint does not exist"):
        ws.upsert(_LifeDoc, {"object_id": "missing-source", "title": "bad", "cites": [{"cited": "ghost"}]})
    assert ws.get("missing-source", entity=_LifeDoc) is None

    with pytest.raises(ValueError, match="endpoint does not exist"):
        ws.upsert_many(
            _LifeDoc,
            [
                {"object_id": "would-be-good", "title": "good", "cites": []},
                {"object_id": "would-be-bad", "title": "bad", "cites": [{"cited": "ghost"}]},
            ],
        )
    assert ws.get_many(["would-be-good", "would-be-bad"], entity=_LifeDoc) == [None, None]

    with pytest.raises(ValueError, match="endpoint does not exist"):
        ws.upsert(_LifeDoc, {"object_id": "d1", "title": "replacement", "cites": [{"cited": "ghost"}]})
    assert ws.get("d1", entity=_LifeDoc)["title"] == "one"
    assert ws.get("d1", entity=_LifeDoc)["cites"] == [{"cited": "d2", "weight": 0.5}]

    with pytest.raises(ValueError, match="parallel edges"):
        ws.upsert(
            _LifeDoc,
            {
                "object_id": "parallel-source",
                "title": "bad",
                "cites": [{"cited": "d2", "weight": 0.1}, {"cited": "d2", "weight": 0.9}],
            },
        )
    assert ws.get("parallel-source", entity=_LifeDoc) is None

    ws.upsert(_LifeDoc, {"object_id": "self", "title": "self", "cites": [{"cited": "self"}]})
    assert ws.get("self", entity=_LifeDoc)["cites"] == [{"cited": "self"}]


def test_upsert_replaces_edges_while_set_preserves_omitted_edges(graph_workspace):
    """Full replacement and patch mutation have explicit, backend-independent GraphEdge semantics."""
    ws = graph_workspace
    ws.upsert_many(
        _LifeDoc,
        [
            {"object_id": "d1", "title": "one", "cites": [{"cited": "d2"}]},
            {"object_id": "d2", "title": "two", "cites": []},
        ],
    )

    ws.set({"object_id": "d1", "title": "patched"}, entity=_LifeDoc)
    assert ws.get("d1", entity=_LifeDoc)["cites"] == [{"cited": "d2"}]

    ws.set({"object_id": "d1", "cites": []}, entity=_LifeDoc)
    assert ws.get("d1", entity=_LifeDoc)["cites"] == []
    ws.set({"object_id": "d1", "cites": [{"cited": "d2"}]}, entity=_LifeDoc)

    ws.upsert(_LifeDoc, {"object_id": "d1", "title": "replaced"})
    assert ws.get("d1", entity=_LifeDoc).get("cites") in (None, [])
    assert ws.traverse(_LifeDoc.cites, "d1") == []
    assert ws.query(_LifeDoc).where(_LifeDoc.cites.exists(cited="d2")).execute().ids == []


def test_delete_restrict_and_explicit_detach_have_one_portable_postcondition(graph_workspace):
    """Restrict is non-mutating; detach removes incoming edges before a successful logical delete."""
    ws = graph_workspace
    ws.upsert_many(
        _LifeDoc,
        [
            {"object_id": "d1", "title": "one", "cites": [{"cited": "d2"}]},
            {"object_id": "d2", "title": "two", "cites": [{"cited": "d3"}]},
            {"object_id": "d3", "title": "three", "cites": []},
        ],
    )

    with pytest.raises(hb.GraphDeleteRestrictedError) as raised:
        ws.delete((_LifeDoc, "d2"))
    assert raised.value.references == [
        {
            "entity": "graph-life-doc",
            "field": "cites",
            "source": "d1",
            "target_entity": "graph-life-doc",
            "target": "d2",
        }
    ]
    assert ws.get("d2", entity=_LifeDoc)["title"] == "two"
    assert ws.get("d1", entity=_LifeDoc)["cites"] == [{"cited": "d2"}]
    assert [row["object_id"] for row in ws.traverse(_LifeDoc.cites, "d2", direction="in")] == ["d1"]

    assert ws.delete((_LifeDoc, "d2"), edges="detach") is True
    assert ws.get("d2", entity=_LifeDoc) is None
    assert ws.get("d1", entity=_LifeDoc)["cites"] == []
    assert ws.query(_LifeDoc).where(_LifeDoc.cites.exists(cited="d2")).execute().ids == []
    assert ws.traverse(_LifeDoc.cites, "d1") == []
    assert ws.traverse(_LifeDoc.cites, "d2", direction="in") == []
    assert ws.check_graph() == []

    ws.upsert(_LifeDoc, {"object_id": "d2", "title": "recreated", "cites": []})
    assert ws.get("d1", entity=_LifeDoc)["cites"] == []
    assert ws.traverse(_LifeDoc.cites, "d1") == []
    assert ws.traverse(_LifeDoc.cites, "d2", direction="in") == []

    ws.upsert(_LifeDoc, {"object_id": "outgoing", "title": "outgoing", "cites": [{"cited": "d3"}]})
    assert ws.delete((_LifeDoc, "outgoing")) is True
    assert ws.get("d3", entity=_LifeDoc) is not None

    with pytest.raises(ValueError, match="GraphEdge delete mode"):
        ws.delete((_LifeDoc, "d3"), edges="cascade")
    assert ws.get("d3", entity=_LifeDoc) is not None


def test_schema_detach_policy_is_used_by_default(graph_workspace):
    """A field-level detach policy cleans surviving sources without a command override."""
    ws = graph_workspace
    ws.upsert_many(
        _DetachDoc,
        [
            {"object_id": "d1", "title": "one", "cites": [{"cited": "d2"}]},
            {"object_id": "d2", "title": "two", "cites": []},
        ],
    )

    assert ws.delete((_DetachDoc, "d2")) is True
    assert ws.get("d1", entity=_DetachDoc)["cites"] == []
    assert ws.check_graph(_DetachDoc.cites) == []

    ws.upsert_many(
        _DetachDoc,
        [
            {"object_id": "d3", "title": "three", "cites": [{"cited": "d4"}]},
            {"object_id": "d4", "title": "four", "cites": []},
        ],
    )
    with pytest.raises(hb.GraphDeleteRestrictedError):
        ws.delete((_DetachDoc, "d4"), edges="restrict")
    assert ws.get("d3", entity=_DetachDoc)["cites"] == [{"cited": "d4"}]


def test_delete_many_treats_internal_edges_as_owned_by_the_batch(graph_workspace):
    """Cycles and self-loops do not block their own batch, while one surviving source blocks all targets."""
    ws = graph_workspace
    ws.upsert_many(
        _LifeDoc,
        [
            {"object_id": "a", "title": "a", "cites": [{"cited": "b"}]},
            {"object_id": "b", "title": "b", "cites": [{"cited": "a"}]},
            {"object_id": "self", "title": "self", "cites": [{"cited": "self"}]},
        ],
    )
    assert ws.delete_many([(_LifeDoc, "a"), (_LifeDoc, "b"), (_LifeDoc, "self")]) == [True, True, True]

    ws.upsert_many(
        _LifeDoc,
        [
            {"object_id": "x", "title": "x", "cites": [{"cited": "y"}]},
            {"object_id": "y", "title": "y", "cites": []},
            {"object_id": "survivor", "title": "survivor", "cites": [{"cited": "y"}]},
        ],
    )
    with pytest.raises(hb.GraphDeleteRestrictedError):
        ws.delete_many([(_LifeDoc, "x"), (_LifeDoc, "y")])
    assert ws.get("x", entity=_LifeDoc) is not None
    assert ws.get("y", entity=_LifeDoc) is not None
    assert ws.get("survivor", entity=_LifeDoc)["cites"] == [{"cited": "y"}]

    assert ws.delete_many([(_LifeDoc, "x"), (_LifeDoc, "y")], edges="detach") == [True, True]
    assert ws.get("survivor", entity=_LifeDoc)["cites"] == []
    assert ws.delete_many([(_LifeDoc, "missing"), (_LifeDoc, "survivor")]) == [False, True]
    ws.upsert(_LifeDoc, {"object_id": "duplicate", "title": "duplicate", "cites": []})
    assert ws.delete_many([(_LifeDoc, "duplicate"), (_LifeDoc, "duplicate")]) == [True, True]
    assert ws.get("duplicate", entity=_LifeDoc) is None


def test_mixed_field_policies_preflight_before_any_row_mutation(graph_workspace):
    """Schema mode aggregates all fields and refuses a mixed batch when any effective policy restricts."""
    ws = graph_workspace
    ws.upsert_many(
        _MixedDoc,
        [
            {"object_id": "source", "title": "source", "cites": [{"cited": "restricted"}], "sees": [{"seen": "detached"}]},
            {"object_id": "restricted", "title": "restricted", "cites": [], "sees": []},
            {"object_id": "detached", "title": "detached", "cites": [], "sees": []},
        ],
    )

    with pytest.raises(hb.GraphDeleteRestrictedError):
        ws.delete_many([(_MixedDoc, "restricted"), (_MixedDoc, "detached")])
    assert ws.get("restricted", entity=_MixedDoc) is not None
    assert ws.get("detached", entity=_MixedDoc) is not None
    assert ws.get("source", entity=_MixedDoc)["sees"] == [{"seen": "detached"}]

    assert ws.delete((_MixedDoc, "detached")) is True
    assert ws.get("source", entity=_MixedDoc)["sees"] == []
    with pytest.raises(hb.GraphDeleteRestrictedError):
        ws.delete((_MixedDoc, "restricted"))


def test_cross_entity_targets_obey_the_same_delete_contract(graph_workspace):
    """Typed cross-entity endpoints restrict and detach just like self-referential edges."""
    ws = graph_workspace
    assert ws.upsert_many(
        _Author,
        [
            _Author.from_dict({"object_id": "batch-author", "name": "Batch", "wrote": [{"paper": "batch-paper", "year": 2024}]}),
            _Paper.from_dict({"object_id": "batch-paper", "title": "Created later in the batch"}),
        ],
    ) == ["batch-author", "batch-paper"]
    assert [row["object_id"] for row in ws.traverse(_Author.wrote, "batch-author")] == ["batch-paper"]
    ws.upsert_many(
        _Paper,
        [
            {"object_id": "p1", "title": "one"},
            {"object_id": "p2", "title": "two"},
        ],
    )
    ws.upsert(_Author, {"object_id": "a1", "name": "Ada", "wrote": [{"paper": "p1", "year": 2020}, {"paper": "p2", "year": 2021}]})

    with pytest.raises(hb.GraphDeleteRestrictedError) as raised:
        ws.delete((_Paper, "p1"))
    assert raised.value.references[0]["target_entity"] == "graph-life-paper"

    assert ws.delete((_Paper, "p1"), edges="detach") is True
    assert ws.get("a1", entity=_Author)["wrote"] == [{"paper": "p2", "year": 2021}]
    assert [row["object_id"] for row in ws.traverse(_Author.wrote, "a1")] == ["p2"]
    assert ws.check_graph() == []


def test_detach_keeps_rewrites_before_deletes_across_mixed_uniqueness_owners():
    """Graph dependency order must survive cross-entity uniqueness-owner routing."""
    events = []

    class SpyBackend(hb.backends.InMemBackend):
        def upsert(self, rows):
            row_list = list(rows)
            events.append((self.name, [(row.entity_id, row.op) for row in row_list]))
            return super().upsert(row_list)

    class MixedOwnerTarget(hb.Entity):
        identifier = "graph-mixed-owner-target"

        slug = hb.field(hb.ShortText).store(to="left").store(to="right").unique(to="right")

    class MixedOwnerSource(hb.Entity):
        identifier = "graph-mixed-owner-source"

        email = hb.field(hb.ShortText).store(to="left").store(to="right").unique(to="left")
        cites = hb.field(hb.HyperG["target" : hb.Identifier[MixedOwnerTarget]]).store(to="right", strategy=hb.GraphEdge(on_delete="detach")).optional()

    ws = hb.HeavenBase(
        "graph-mixed-owner-order",
        detached=True,
        backends={"left": SpyBackend("left"), "right": SpyBackend("right")},
    )
    ws.register(MixedOwnerTarget)
    ws.register(MixedOwnerSource)
    ws.upsert(MixedOwnerTarget, {"object_id": "target", "slug": "target"})
    ws.upsert(
        MixedOwnerSource,
        {
            "object_id": "source",
            "email": "source@example.com",
            "cites": [{"target": "target"}],
        },
    )
    events.clear()

    assert ws.delete((MixedOwnerTarget, "target")) is True

    right_ops = [op for backend_name, batch in events if backend_name == "right" for op in batch]
    source_rewrite = right_ops.index((MixedOwnerSource.identifier, "upsert"))
    target_delete = right_ops.index((MixedOwnerTarget.identifier, "delete"))
    assert source_rewrite < target_delete
    assert ws.get("source", entity=MixedOwnerSource)["cites"] == []
    assert ws.get("target", entity=MixedOwnerTarget) is None


def test_detach_scans_each_source_entity_once(monkeypatch):
    """Deletion work is grouped by source entity rather than scanning once per edge field or target."""
    ws = hb.HeavenBase("graph-scan-count", backends={"main": hb.backends.InMemBackend("main")})
    try:
        ws.register(_MixedDoc)
        ws.upsert_many(
            _MixedDoc,
            [
                {"object_id": "source", "title": "source", "cites": [{"cited": "target"}], "sees": [{"seen": "target"}]},
                {"object_id": "target", "title": "target", "cites": [], "sees": []},
            ],
        )
        calls = []
        universe = ws._row_store.universe

        def counted(entity_id, **kwargs):
            calls.append(entity_id)
            return universe(entity_id, **kwargs)

        monkeypatch.setattr(ws._row_store, "universe", counted)
        assert ws.delete((_MixedDoc, "target"), edges="detach") is True
        assert calls == ["graph-mixed-doc"]
        row = ws.get("source", entity=_MixedDoc)
        assert row["cites"] == [] and row["sees"] == []
    finally:
        ws.drop()
        WORKSPACES.pop("graph-scan-count", None)


def test_native_incoming_lookup_avoids_source_entity_scan():
    """A complete backend candidate lookup replaces the portable full-source scan."""
    modules = _register_graph_tag_backend(**{"graph-incoming-lookup-native": True})

    class NativeLookupBackend(_GraphTagInMemBackend):
        def __init__(self, name):
            super().__init__(name)
            self.lookups = []

        def scan(self, entity_id):
            raise AssertionError(f"native GraphEdge delete must not scan {entity_id}")

        def graph_incoming_sources(self, entity_id, field_name, target_entity, target_ids):
            self.lookups.append((entity_id, field_name, target_entity, list(target_ids)))
            targets = set(target_ids)
            return [
                object_id
                for object_id, row in self.data.get(self.ws_id, {}).get(entity_id, {}).items()
                if any(str(edge.get("cited")) in targets for edge in row.get(field_name) or [])
            ]

    backend = NativeLookupBackend("main")
    ws_id = "graph-native-incoming"
    ws = hb.HeavenBase(ws_id, backends={"main": backend})
    try:
        ws.register(_LifeDoc)
        ws.upsert_many(
            _LifeDoc,
            [
                {"object_id": "source", "title": "Source", "cites": [{"cited": "target"}]},
                {"object_id": "target", "title": "Target", "cites": []},
            ],
        )
        assert ws.delete((_LifeDoc, "target"), edges="detach") is True
        assert ws.get("source", entity=_LifeDoc)["cites"] == []
        assert backend.lookups == [
            (_LifeDoc.identifier, "cites", _LifeDoc.identifier, ["target"]),
        ]
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)
        modules.unregister("backend_type", _GraphTagInMemBackend.identifier)


def test_detach_recomputes_fields_derived_from_the_edge_value():
    """Internal source rewrites use patch semantics so computed dependants cannot remain stale."""
    ws = hb.HeavenBase("graph-derived-rewrite", backends={"main": hb.backends.InMemBackend("main")})
    try:
        ws.register(_DerivedDoc)
        ws.upsert_many(
            _DerivedDoc,
            [
                {"object_id": "source", "title": "source", "cites": [{"cited": "target"}]},
                {"object_id": "target", "title": "target", "cites": []},
            ],
        )
        assert ws.get("source", entity=_DerivedDoc)["edge_count"] == 1
        assert ws.delete((_DerivedDoc, "target"), edges="detach") is True
        row = ws.get("source", entity=_DerivedDoc)
        assert row["cites"] == [] and row["edge_count"] == 0
    finally:
        ws.drop()
        WORKSPACES.pop("graph-derived-rewrite", None)


def test_delete_fails_closed_when_an_edge_source_backend_cannot_scan(monkeypatch):
    """A backend that cannot prove inbound-reference absence must not permit the target delete."""
    backend = hb.backends.InMemBackend("main")
    ws = hb.HeavenBase("graph-scan-required", backends={"main": backend})
    try:
        ws.register(_LifeDoc)
        ws.upsert_many(
            _LifeDoc,
            [
                {"object_id": "source", "title": "source", "cites": [{"cited": "target"}]},
                {"object_id": "target", "title": "target", "cites": []},
            ],
        )

        def unavailable(_entity_id):
            raise NotImplementedError("scan unavailable")

        monkeypatch.setattr(backend, "scan", unavailable)
        with pytest.raises(RuntimeError, match="GraphEdge integrity requires backend"):
            ws.delete((_LifeDoc, "target"), edges="detach")
        assert backend.get_many("graph-life-doc", ["source", "target"])[1] is not None
    finally:
        ws.drop()
        WORKSPACES.pop("graph-scan-required", None)


def test_portable_traversal_fails_closed_when_an_edge_owner_cannot_scan(monkeypatch):
    """Portable traversal must not turn an incomplete source scan into a false empty path."""
    backend = hb.backends.InMemBackend("main")
    ws = hb.HeavenBase("graph-traverse-scan-required", backends={"main": backend})
    try:
        ws.register(_LifeDoc)
        ws.upsert_many(
            _LifeDoc,
            [
                {"object_id": "source", "title": "source", "cites": [{"cited": "target"}]},
                {"object_id": "target", "title": "target", "cites": []},
            ],
        )

        def unavailable(_entity_id):
            raise NotImplementedError("scan unavailable")

        monkeypatch.setattr(backend, "scan", unavailable)
        with pytest.raises(RuntimeError, match="GraphEdge integrity requires backend"):
            ws.traverse(_LifeDoc.cites, "source")
        with pytest.raises(RuntimeError, match="GraphEdge integrity requires backend"):
            ws.shortest_path(_LifeDoc.cites, "source", "target")
    finally:
        ws.drop()
        WORKSPACES.pop("graph-traverse-scan-required", None)


def test_replicated_graph_field_rejects_conflicting_lifecycle_metadata():
    """Every physical copy of one logical GraphEdge field must agree on endpoint and delete policy."""

    class ConflictingDoc(hb.Entity):
        identifier = "graph-conflicting-doc"

        title = hb.field(hb.ShortText)
        cites = (
            hb.field(hb.HyperG["cited" : hb.Identifier["graph-conflicting-doc"]])
            .store(to="left", strategy=hb.GraphEdge)
            .store(to="right", strategy=hb.GraphEdge(on_delete="detach"))
        )

    ws = hb.HeavenBase(
        "graph-conflicting-policy",
        backends={
            "left": hb.backends.InMemBackend("left"),
            "right": hb.backends.InMemBackend("right"),
        },
    )
    try:
        with pytest.raises(ValueError, match="conflicting target or on_delete"):
            ws.register(ConflictingDoc)
    finally:
        ws.drop()
        WORKSPACES.pop("graph-conflicting-policy", None)


def test_graph_schema_contract_rejects_optional_endpoints_and_compound_properties_portably():
    """Provider-independent placement rejects weak endpoints and non-scalar edge properties before ensure."""

    class EnsureGuardBackend(hb.backends.InMemBackend):
        def ensure(self, schema, bindings):
            if schema.entity_id.startswith("graph-invalid-schema-"):
                raise AssertionError("invalid GraphEdge schema reached provider ensure")
            return super().ensure(schema, bindings)

    class OptionalEndpoint(hb.Entity):
        identifier = "graph-invalid-schema-optional"

        links = hb.field(hb.HyperG["target" : {"type": hb.Identifier["graph-invalid-schema-optional"], "required": False}]).store(strategy=hb.GraphEdge)

    invalid_properties = {
        "json": hb.Json,
        "array": hb.Array[hb.ShortText],
        "vector": hb.Vector[2],
        "artifact": hb.Artifact,
    }
    invalid_entities = []
    for suffix, dtype in invalid_properties.items():
        entity_id = f"graph-invalid-schema-{suffix}"
        invalid_entities.append(
            type(
                f"Invalid{suffix.title()}EdgeProperty",
                (hb.Entity,),
                {
                    "__module__": __name__,
                    "identifier": entity_id,
                    "links": hb.field(
                        hb.HyperG[
                            "target" : hb.Identifier[entity_id],
                            "metadata":{"type": dtype, "required": False},
                        ]
                    ).store(strategy=hb.GraphEdge),
                },
            )
        )

    ws_id = "graph-invalid-schema"
    ws = hb.HeavenBase(ws_id, backends={"main": EnsureGuardBackend("main")})
    try:
        with pytest.raises(ValueError, match="endpoint column 'target' must be required"):
            ws.register(OptionalEndpoint)
        for entity in invalid_entities:
            with pytest.raises(ValueError, match="non-scalar edge-property"):
                ws.register(entity)
        assert not ({OptionalEndpoint.identifier} | {entity.identifier for entity in invalid_entities}) & set(ws.entities)
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)


def test_graph_rewrites_hydrate_split_rows_before_full_replacement():
    """Detach and repair preserve fields held by a routed backend that cannot scan."""

    class LookupOnlyBackend(hb.backends.InMemBackend):
        def scan(self, entity_id):
            raise AssertionError(f"auxiliary route must not be scanned for {entity_id}")

    class SplitDoc(hb.Entity):
        identifier = "graph-split-hydration-doc"

        title = hb.field(hb.ShortText).store(to="aux")
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["graph-split-hydration-doc"]]).store(to="edge", strategy=hb.GraphEdge).optional()

    ws_id = "graph-split-hydration"
    ws = hb.HeavenBase(
        ws_id,
        backends={
            "edge": hb.backends.InMemBackend("edge"),
            "aux": LookupOnlyBackend("aux"),
        },
    )
    try:
        ws.register(SplitDoc)
        ws.upsert_many(
            SplitDoc,
            [
                {"object_id": "source-detach", "title": "keep detach", "cites": [{"cited": "target-detach"}]},
                {"object_id": "target-detach", "title": "target", "cites": []},
                {"object_id": "source-repair", "title": "keep repair", "cites": [{"cited": "target-repair"}]},
                {"object_id": "target-repair", "title": "target", "cites": []},
            ],
        )

        assert ws.delete((SplitDoc, "target-detach"), edges="detach") is True
        assert ws.get("source-detach", entity=SplitDoc) == {
            "object_id": "source-detach",
            "title": "keep detach",
            "cites": [],
        }

        for backend_name in ws._row_store.routed_backend_names(SplitDoc.identifier):
            ws.backends.get(backend_name).delete(SplitDoc.identifier, ["target-repair"])
        assert ws.scrub_dangling(confirm=True)["removed"] == 1
        assert ws.get("source-repair", entity=SplitDoc) == {
            "object_id": "source-repair",
            "title": "keep repair",
            "cites": [],
        }
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)


def test_alter_entity_rejects_graph_endpoint_reinterpretation():
    """Changing a GraphEdge endpoint binding requires an explicit data migration."""

    class Original(hb.Entity):
        identifier = "graph-alter-endpoint"

        links = hb.field(
            hb.HyperG[
                "left" : hb.Identifier["graph-alter-endpoint"],
                "right" : hb.Identifier["graph-alter-endpoint"],
            ]
        ).store(strategy=hb.GraphEdge(target="left"))

    class Reinterpreted(hb.Entity):
        identifier = "graph-alter-endpoint"

        links = hb.field(
            hb.HyperG[
                "left" : hb.Identifier["graph-alter-endpoint"],
                "right" : hb.Identifier["graph-alter-endpoint"],
            ]
        ).store(strategy=hb.GraphEdge(target="right"))

    ws_id = "graph-alter-endpoint"
    ws = hb.HeavenBase(ws_id, backends={"main": hb.backends.InMemBackend("main")})
    try:
        ws.register(Original)
        with pytest.raises(ValueError, match="cannot change storage"):
            ws.alter_entity(Reinterpreted, confirm=Original.identifier)
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)


def test_identity_anchors_are_deleted_without_becoming_logical_row_routes():
    """Under one coherent StoragePlan, derived anchors delete without establishing logical existence."""
    modules = _register_graph_tag_backend(**{"graph-endpoint-materialization": "identity_anchor"})

    class AnchorBackend(_GraphTagInMemBackend):
        pass

    class Target(hb.Entity):
        identifier = "graph-anchor-target"

        title = hb.field(hb.ShortText).store(to="row")

    class Source(hb.Entity):
        identifier = "graph-anchor-source"

        title = hb.field(hb.ShortText).store(to="row")
        links = hb.field(hb.HyperG["target" : hb.Identifier[Target]]).store(to="graph", strategy=hb.GraphEdge).optional()

    ws_id = "graph-anchor-routing"
    graph = AnchorBackend("graph")
    ws = hb.HeavenBase(
        ws_id,
        backends={
            "row": hb.backends.InMemBackend("row"),
            "graph": graph,
        },
    )
    try:
        ws.register(Target)
        ws.register(Source)
        ws.upsert(Target, {"object_id": "target", "title": "Target"})
        ws.upsert(Source, {"object_id": "source", "title": "Source", "links": [{"target": "target"}]})
        graph.upsert(
            [
                hb.ext.RowOp(Target.identifier, "target", {"object_id": "target"}),
                hb.ext.RowOp(Target.identifier, "anchor-only", {"object_id": "anchor-only"}),
            ]
        )

        assert ws.exists("anchor-only", entity=Target) is False
        assert ws.delete((Target, "target"), edges="detach") is True
        assert graph.get_many(Target.identifier, ["target"])[0] is None
        assert ws.get("source", entity=Source)["links"] == []
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)
        modules.unregister("backend_type", _GraphTagInMemBackend.identifier)


def test_replicated_graph_field_round_trips_and_detaches_consistently():
    """Same-policy GraphEdge replicas expose one logical value and receive one detach result."""

    class ReplicatedDoc(hb.Entity):
        identifier = "graph-replicated-doc"

        title = hb.field(hb.ShortText).store(to="left")
        cites = (
            hb.field(hb.HyperG["cited" : hb.Identifier["graph-replicated-doc"]])
            .store(to="left", strategy=hb.GraphEdge)
            .store(to="right", strategy=hb.GraphEdge(target="cited"))
            .optional()
        )

    ws_id = "graph-replicated"
    left = hb.backends.InMemBackend("left")
    right = hb.backends.InMemBackend("right")
    ws = hb.HeavenBase(ws_id, backends={"left": left, "right": right})
    try:
        ws.register(ReplicatedDoc)
        ws.upsert_many(
            ReplicatedDoc,
            [
                {
                    "object_id": "source",
                    "title": "Source",
                    "cites": [{"cited": "target-b"}, {"cited": "target-a"}],
                },
                {"object_id": "target-a", "title": "Target A", "cites": []},
                {"object_id": "target-b", "title": "Target B", "cites": []},
            ],
        )
        right.upsert(
            [
                hb.ext.RowOp(
                    ReplicatedDoc.identifier,
                    "source",
                    {"object_id": "source", "cites": [{"cited": "target-a"}, {"cited": "target-b"}]},
                )
            ]
        )
        assert ws.get("source", entity=ReplicatedDoc)["cites"] == [
            {"cited": "target-b"},
            {"cited": "target-a"},
        ]
        assert ws.delete((ReplicatedDoc, "target-a"), edges="detach") is True
        assert ws.get("source", entity=ReplicatedDoc)["cites"] == [{"cited": "target-b"}]
        assert left.get_many(ReplicatedDoc.identifier, ["source"])[0]["cites"] == [{"cited": "target-b"}]
        assert right.get_many(ReplicatedDoc.identifier, ["source"])[0]["cites"] == [{"cited": "target-b"}]
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)


def test_mixed_strategy_graph_replica_conflict_fails_closed_until_authoritative_rewrite():
    """Every physical owner participates in lifecycle discovery, even when only one uses GraphEdge."""
    modules = _register_graph_tag_backend(
        **{
            "graph-traversal-native": True,
            "graph-incoming-lookup-native": True,
        }
    )

    class NativeGraphBackend(_GraphTagInMemBackend):
        def __init__(self, name):
            super().__init__(name)
            self.incoming_lookups = []

        def graph_incoming_sources(self, entity_id, field_name, target_entity, target_ids):
            self.incoming_lookups.append((entity_id, field_name, target_entity, list(target_ids)))
            targets = set(target_ids)
            return [
                object_id
                for object_id, row in self.data.get(self.ws_id, {}).get(entity_id, {}).items()
                if any(str(edge.get("cited")) in targets for edge in row.get(field_name) or [])
            ]

        def graph_traverse_refs(self, *args, **kwargs):
            return []

        def graph_traverse(self, *args, **kwargs):
            return []

        def graph_shortest_path_refs(self, *args, **kwargs):
            return None

    class MixedDoc(hb.Entity):
        identifier = "graph-mixed-strategy-doc"

        title = hb.field(hb.ShortText).store(to="inline")
        cites = (
            hb.field(hb.HyperG["cited" : hb.Identifier["graph-mixed-strategy-doc"]])
            .store(to="graph", strategy=hb.GraphEdge)
            .store(to="inline", strategy=hb.InlineColumn)
            .optional()
        )

    ws_id = "graph-mixed-strategy-conflict"
    graph = NativeGraphBackend("graph")
    inline = hb.backends.InMemBackend("inline")
    ws = hb.HeavenBase(ws_id, backends={"graph": graph, "inline": inline})
    try:
        ws.register(MixedDoc)
        ws.upsert_many(
            MixedDoc,
            [
                {"object_id": "source", "title": "Source", "cites": [{"cited": "target"}]},
                {"object_id": "target", "title": "Target", "cites": []},
            ],
        )
        step = next(item for item in ws.query(MixedDoc).traverse(MixedDoc.cites, "source").explain()["steps"] if item.get("node") == "traverse")
        assert step["mode"] == "py_traverse"
        assert step["backends"] == ["graph", "inline"]
        assert "backend" not in step
        graph.upsert([hb.ext.RowOp(MixedDoc.identifier, "source", {"object_id": "source", "cites": []})])

        with pytest.raises(ValueError, match="not unique across routed backends"):
            ws.traverse(MixedDoc.cites, "source")
        with pytest.raises(ValueError, match="not unique across routed backends"):
            ws.delete((MixedDoc, "target"), edges="detach")
        assert graph.incoming_lookups == [(MixedDoc.identifier, "cites", MixedDoc.identifier, ["target"])]
        assert ws.exists("target", entity=MixedDoc) is True
        with pytest.raises(ValueError, match="not unique across routed backends"):
            ws.check_graph()
        with pytest.raises(ValueError, match="not unique across routed backends"):
            ws.scrub_dangling(confirm=True)

        ws.upsert(MixedDoc, {"object_id": "source", "title": "Source", "cites": [{"cited": "target"}]})
        assert ws.delete((MixedDoc, "target"), edges="detach") is True
        assert ws.get("source", entity=MixedDoc)["cites"] == []
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)
        modules.unregister("backend_type", _GraphTagInMemBackend.identifier)


def test_partial_fanout_conflict_fails_closed_until_authoritative_rewrite():
    """Repair never guesses between divergent replicas after an apply-phase backend failure."""

    class FailOnceBackend(hb.backends.InMemBackend):
        fail_next = False

        def upsert(self, rows):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("injected replica failure")
            return super().upsert(rows)

    class ReplicatedDoc(hb.Entity):
        identifier = "graph-partial-fanout-doc"

        title = hb.field(hb.ShortText).store(to="left")
        cites = (
            hb.field(hb.HyperG["cited" : hb.Identifier["graph-partial-fanout-doc"]])
            .store(to="left", strategy=hb.GraphEdge)
            .store(to="right", strategy=hb.GraphEdge)
            .optional()
        )

    ws_id = "graph-partial-fanout"
    left = hb.backends.InMemBackend("left")
    right = FailOnceBackend("right")
    ws = hb.HeavenBase(ws_id, backends={"left": left, "right": right})
    try:
        ws.register(ReplicatedDoc)
        ws.upsert_many(
            ReplicatedDoc,
            [
                {"object_id": "source", "title": "Source", "cites": [{"cited": "target"}]},
                {"object_id": "target", "title": "Target", "cites": []},
            ],
        )
        right.fail_next = True
        with pytest.raises(RuntimeError, match="injected replica failure"):
            ws.delete((ReplicatedDoc, "target"), edges="detach")

        conflict = "not unique across routed backends"
        with pytest.raises(ValueError, match=conflict):
            ws.get("source", entity=ReplicatedDoc)
        with pytest.raises(ValueError, match=conflict):
            ws.check_graph()
        with pytest.raises(ValueError, match=conflict):
            ws.scrub_dangling(confirm=True)
        with pytest.raises(ValueError, match=conflict):
            ws.delete((ReplicatedDoc, "target"), edges="detach")

        ws.upsert(ReplicatedDoc, {"object_id": "source", "title": "Source", "cites": []})
        assert ws.delete((ReplicatedDoc, "target"), edges="detach") is True
        assert ws.get("source", entity=ReplicatedDoc)["cites"] == []
        assert ws.check_graph() == []
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)


def test_detach_emits_source_rewrite_before_same_backend_target_delete():
    """The coordinator orders a surviving-source rewrite before its target deletion."""

    class RecordingBackend(hb.backends.InMemBackend):
        def __init__(self, name):
            super().__init__(name)
            self.batches = []

        def upsert(self, rows):
            self.batches.append(list(rows))
            return super().upsert(rows)

    class OrderedDoc(hb.Entity):
        identifier = "graph-ordered-delete-doc"

        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["graph-ordered-delete-doc"]]).store(strategy=hb.GraphEdge).optional()

    ws_id = "graph-ordered-delete"
    backend = RecordingBackend("main")
    ws = hb.HeavenBase(ws_id, backends={"main": backend})
    try:
        ws.register(OrderedDoc)
        ws.upsert_many(
            OrderedDoc,
            [
                {"object_id": "source", "title": "Source", "cites": [{"cited": "target"}]},
                {"object_id": "target", "title": "Target", "cites": []},
            ],
        )
        backend.batches.clear()

        assert ws.delete((OrderedDoc, "target"), edges="detach") is True

        logical_ops = [row for batch in backend.batches for row in batch if row.entity_id == OrderedDoc.identifier]
        assert [(row.op, row.object_id) for row in logical_ops] == [("upsert", "source"), ("delete", "target")]
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)


def _assert_provider_lifecycle(workspace):
    """Exercise the server/local-SQL lifecycle without inspecting provider internals."""
    workspace.register(_LifeDoc)
    workspace.upsert_many(
        _LifeDoc,
        [
            {"object_id": "source", "title": "source", "cites": [{"cited": "target"}]},
            {"object_id": "target", "title": "target", "cites": []},
        ],
    )
    with pytest.raises(hb.GraphDeleteRestrictedError):
        workspace.delete((_LifeDoc, "target"))
    assert workspace.delete((_LifeDoc, "target"), edges="detach") is True
    assert workspace.get("source", entity=_LifeDoc)["cites"] == []
    assert workspace.query(_LifeDoc).where(_LifeDoc.cites.exists(cited="target")).execute().ids == []
    assert workspace.traverse(_LifeDoc.cites, "source") == []
    assert workspace.check_graph() == []
    workspace.upsert(_LifeDoc, {"object_id": "target", "title": "recreated", "cites": []})
    assert workspace.get("source", entity=_LifeDoc)["cites"] == []
    with pytest.raises(ValueError, match="endpoint does not exist"):
        workspace.upsert(_LifeDoc, {"object_id": "bad", "title": "bad", "cites": [{"cited": "ghost"}]})
    assert workspace.get("bad", entity=_LifeDoc) is None


@pytest.mark.full
def test_duckdb_graph_lifecycle_contract(tmp_path):
    """DuckDB applies the same strong-reference and detach contract in one SQL backend batch."""
    pytest.importorskip("duckdb_engine")
    ws_id = "graph-lifecycle-duckdb"
    backend = hb.backends.DuckDBBackend("main", hb.utils.path_to_file_uri(tmp_path / "graph.duckdb"))
    workspace = hb.HeavenBase(ws_id, backends={"main": backend})
    try:
        _assert_provider_lifecycle(workspace)
    finally:
        workspace.drop()
        WORKSPACES.pop(ws_id, None)


@pytest.mark.external
def test_postgres_graph_lifecycle_contract():
    """A live PostgreSQL row backend satisfies the same logical delete and recreation postcondition."""
    if find_spec("psycopg2") is None:
        pytest.skip("psycopg2 is not installed")
    _require_postgres()
    ws_id = "graph-lifecycle-postgres"
    backend = hb.backends.PostgresBackend("main", url=_POSTGRES_URL)
    workspace = hb.HeavenBase(ws_id, backends={"main": backend})
    try:
        _assert_provider_lifecycle(workspace)
    finally:
        workspace.drop()
        WORKSPACES.pop(ws_id, None)
