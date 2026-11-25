from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import heavenbase as hb
import pytest
from heavenbase.query import Near, OrderKey, Traverse
from heavenbase.utils.config import ConfigManager
from heavenbase.workspace.query.service import _QueryService

pytestmark = [pytest.mark.fast, pytest.mark.unit]


def test_query_spec_is_a_structurally_immutable_request():
    """Request-owned collections should be copied into immutable containers."""
    select = ["title"]
    order = [OrderKey("title", True)]
    notes = {"route": "vector"}
    spec = hb.QuerySpec(
        "query-spec-doc",
        near=Near("embedding", [1, 0], top_k=3, notes=notes),
        select=select,
        order=order,
        offset=1,
        limit=5,
        traverse=Traverse("links", "doc-1", max_hops=2, direction="any"),
    )

    select.append("body")
    order.append(OrderKey("body"))
    notes["route"] = "changed"

    assert spec.select == ("title",)
    assert spec.order == (OrderKey("title", True),)
    assert dict(spec.near.notes) == {"route": "vector"}
    assert "merge_rule" not in spec.to_dict()
    with pytest.raises(FrozenInstanceError):
        spec.limit = 10
    with pytest.raises(TypeError):
        spec.near.notes["route"] = "changed"


def test_query_spec_derivation_uses_dataclass_replace():
    """Routing should derive a new immutable request instead of mutating input."""
    original = hb.QuerySpec("query-spec-doc", select=["title"])
    derived = replace(original, limit=2)

    assert original.limit is None
    assert derived.limit == 2
    assert derived.select == original.select


def test_query_value_branches_without_changing_existing_spec():
    """Deriving a query should not change a previously observed request value."""
    ws = hb.HeavenBase("query-spec-value")

    class QueryDoc(hb.Entity):
        title = hb.field(hb.ShortText)

    ws.register(QueryDoc)
    query = ws.query(QueryDoc).where(QueryDoc.title != "draft").where(QueryDoc.title != "archive")
    spec = query.spec
    query.where(QueryDoc.title == "published").select("title").order_by("title")

    assert len(spec.filter.clauses) == 2
    assert spec.select == ()
    assert spec.order == ()


def test_query_spec_carries_explicit_unsafe_execution_policy():
    """Unsafe authorization should be frozen request data and default off."""
    ws = hb.HeavenBase("query-spec-unsafe")

    class UnsafeDoc(hb.Entity):
        title = hb.field(hb.ShortText)

    ws.register(UnsafeDoc)
    default = ws.query(UnsafeDoc).spec
    unsafe = ws.query(UnsafeDoc).unsafe().spec

    assert default.unsafe is False
    assert unsafe.unsafe is True
    assert unsafe.to_dict()["unsafe"] is True
    with pytest.raises(TypeError, match="unsafe enabled must be a boolean"):
        ws.query(UnsafeDoc).unsafe("yes")


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: hb.QuerySpec(""), "entity_id must not be empty"),
        (lambda: hb.QuerySpec("doc", offset=-1), "offset must be non-negative"),
        (lambda: hb.QuerySpec(None), "entity_id must be a string"),
        (lambda: hb.QuerySpec("doc", select=[""]), "select field must not be empty"),
        (lambda: hb.QuerySpec("doc", select="title"), "select must be a sequence"),
        (lambda: hb.QuerySpec("doc", order=["title"]), "order entries must be OrderKey"),
        (lambda: hb.QuerySpec("doc", unsafe=1), "unsafe must be a boolean"),
        (lambda: Near("embedding", [1, 0], top_k=0), "top_k must be positive"),
        (lambda: OrderKey("title", 1), "desc must be a boolean"),
        (lambda: Traverse("links", "doc-1", min_hops=3, max_hops=2), "min_hops 3 cannot exceed max_hops 2"),
        (lambda: Traverse("links", "doc-1", direction="sideways"), "Unsupported traverse direction"),
    ],
)
def test_query_spec_rejects_invalid_normalized_requests(factory, message):
    """Invalid normalized requests should fail before workspace routing."""
    with pytest.raises((TypeError, ValueError), match=message):
        factory()


def test_raw_query_policy_binds_only_at_the_executing_workspace():
    """One unbound request must inherit defaults and limits from its consumer."""

    def service(top_k, metric, max_top_k, max_hops):
        manager = ConfigManager(default={}, setup=False)
        manager.set("heavenbase.query.near.default_top_k", top_k)
        manager.set("heavenbase.query.near.default_metric", metric)
        manager.set("heavenbase.query.near.max_top_k", max_top_k)
        manager.set("heavenbase.query.graph.max_hops", max_hops)
        workspace = SimpleNamespace(
            context=SimpleNamespace(config=manager),
            _schema=SimpleNamespace(require=lambda entity: entity),
            _router=SimpleNamespace(execute=lambda query: query),
            _query_explain=SimpleNamespace(explain=lambda query: query),
        )
        return _QueryService(workspace)

    raw = hb.QuerySpec("query-spec-doc", near=Near("embedding", [1, 0]))
    first = service(1, "cosine", 1, 1).execute(raw)
    second = service(2, "dot", 2, 3).explain(raw)

    assert raw.near.top_k is None
    assert raw.near.metric is None
    assert (first.near.top_k, first.near.metric) == (1, "cosine")
    assert (second.near.top_k, second.near.metric) == (2, "dot")
    traversal = hb.QuerySpec("query-spec-doc", traverse=Traverse("links", "doc-1", max_hops=2))
    with pytest.raises(ValueError, match="max_hops 2 exceeds configured maximum 1"):
        service(1, "cosine", 1, 1).execute(traversal)
    assert service(1, "cosine", 1, 3).execute(traversal).traverse.max_hops == 2


@pytest.mark.parametrize("command", ("execute", "explain"))
def test_direct_query_spec_requires_a_registered_entity(command):
    """Direct normalized requests must use the same class authority as Query creation."""
    workspace = hb.HeavenBase(f"query-spec-unregistered-{command}", detached=True, backends={"main": {"type": "inmem"}})
    spec = hb.QuerySpec(f"unknown-query-spec-{command}")

    with pytest.raises(KeyError, match="has no registered Entity class"):
        getattr(workspace, command)(spec)
