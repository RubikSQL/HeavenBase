import heavenbase as hb
import pytest

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class QueryDoc(hb.Entity):
    identifier = "query-value-doc"

    title = hb.field(hb.ShortText)
    emb = hb.field(hb.Vector[2])


class ForeignQueryDoc(hb.Entity):
    identifier = "query-value-foreign"

    title = hb.field(hb.ShortText)
    emb = hb.field(hb.Vector[2])
    meta = hb.field(hb.Json)


def _workspace(name):
    """Return a workspace with the query entity registered explicitly."""
    ws = hb.HeavenBase(name)
    ws.register(QueryDoc)
    return ws


def test_query_value_exposes_entity_not_mutable_construction_state():
    """Query internals should be private while entity context stays readable."""
    ws = _workspace("query-value-state")
    query = ws.query(QueryDoc)

    assert isinstance(query, hb.Query)
    assert query.entity is QueryDoc
    for name in ("workspace", "filters", "near_expr", "select_cols", "order_keys", "offset_n", "limit_n", "traverse_spec"):
        assert not hasattr(query, name)


def test_query_value_rejects_duplicate_singleton_clauses():
    """Near and traverse should not silently replace earlier caller intent."""
    ws = _workspace("query-value-singletons")
    near = ws.query(QueryDoc).near(QueryDoc.emb, [1, 0])
    traverse = ws.query(QueryDoc).traverse("links", "doc-1")

    with pytest.raises(ValueError, match="already has a near clause"):
        near.near(QueryDoc.emb, [0, 1])
    with pytest.raises(ValueError, match="already has a traverse clause"):
        traverse.traverse("links", "doc-2")


def test_typed_field_owner_survives_authoring_and_is_removed_after_binding():
    """Python field refs should stay typed until their query becomes target-relative."""
    authored = QueryDoc.title == "draft"
    nested = ForeignQueryDoc.meta["venue"] == "NeurIPS"

    assert authored.entity_id == QueryDoc.identifier
    assert nested.entity_id == ForeignQueryDoc.identifier

    bound = (
        _workspace("query-value-owner-binding")
        .query(QueryDoc)
        .where(authored)
        .select(QueryDoc.title)
        .near(QueryDoc.emb, [1, 0])
        .traverse(QueryDoc.title, "doc-1")
        .order_by(QueryDoc.title)
    )

    assert bound.spec.filter.entity_id is None
    assert bound.spec.select == ("title",)
    assert bound.spec.near.field == "emb"
    assert bound.spec.traverse.field == "title"
    assert bound.spec.order[0].field == "title"


@pytest.mark.parametrize(
    ("operation", "compose"),
    [
        ("where", lambda query: query.where(ForeignQueryDoc.title == "draft")),
        ("select", lambda query: query.select(ForeignQueryDoc.title)),
        ("near", lambda query: query.near(ForeignQueryDoc.emb, [1, 0])),
        ("traverse", lambda query: query.traverse(ForeignQueryDoc.title, "doc-1")),
        ("order_by", lambda query: query.order_by(ForeignQueryDoc.title)),
    ],
)
def test_query_rejects_typed_fields_owned_by_another_entity(operation, compose):
    """Every field-taking fluent clause should reject a foreign typed owner."""
    query = _workspace(f"query-value-foreign-{operation}").query(QueryDoc)

    with pytest.raises(ValueError, match=rf"{operation} field belongs to query-value-foreign, but the query targets query-value-doc"):
        compose(query)


def test_where_checks_typed_owners_inside_nested_expression_trees():
    """Logical composition must not hide a foreign typed filter leaf."""
    query = _workspace("query-value-foreign-nested").query(QueryDoc)
    expr = (QueryDoc.title == "local") & ~(ForeignQueryDoc.meta["venue"] == "remote")

    with pytest.raises(ValueError, match="where field belongs to query-value-foreign"):
        query.where(expr)


def test_raw_field_names_and_json_filter_mappings_remain_target_relative():
    """Untyped adapter inputs should continue to bind to the queried Entity."""
    query = (
        _workspace("query-value-relative-inputs")
        .query(QueryDoc)
        .where({"title": {"$eq": "draft"}})
        .select("title")
        .near("emb", [1, 0])
        .traverse("links", "doc-1")
        .order_by("title")
    )

    assert query.spec.filter.entity_id is None
    assert query.spec.select == ("title",)
    assert query.spec.near.field == "emb"
    assert query.spec.traverse.field == "links"
    assert query.spec.order[0].field == "title"


def test_query_value_branches_immutably_and_requires_explicit_execution():
    """Every fluent transform should branch without changing its source query."""
    ws = _workspace("query-value-snapshot")
    base = ws.query(QueryDoc).where(QueryDoc.title != "draft").where(QueryDoc.title != "archive")
    selected = base.select("title")
    ordered = selected.order_by("title").limit(1)
    unsafe = base.unsafe()
    counted = base.count()

    assert selected is not base
    assert ordered is not selected
    assert unsafe is not base
    assert counted is not base
    assert len(base.spec.filter.clauses) == 2
    assert base.spec.select == ()
    assert base.spec.order == ()
    assert base.spec.limit is None
    assert base.spec.unsafe is False
    assert selected.spec.select == ("title",)
    assert ordered.spec.limit == 1
    assert unsafe.spec.unsafe is True

    # Ignoring a returned branch must be a no-op on the source value.
    base.where(QueryDoc.title == "published")
    base.select("title")
    base.order_by("title")
    base.limit(1)
    base.unsafe()
    assert len(base.spec.filter.clauses) == 2
    assert base.spec.select == ()
    assert base.spec.order == ()
    assert base.spec.limit is None
    assert base.spec.unsafe is False
    with pytest.raises(TypeError, match="not iterable"):
        iter(base)


@pytest.mark.parametrize(
    ("name", "compose"),
    [
        ("where", lambda query: query.where(QueryDoc.title == "published")),
        ("near", lambda query: query.near(QueryDoc.emb, [1, 0])),
        ("traverse", lambda query: query.traverse("links", "doc-1")),
        ("select", lambda query: query.select("title")),
        ("group", lambda query: query.group_by("title")),
        ("aggregate", lambda query: query.aggregate(rows=QueryDoc.object_id.count())),
        ("order", lambda query: query.order_by("title")),
        ("offset", lambda query: query.offset(1)),
        ("limit", lambda query: query.limit(1)),
        ("unsafe", lambda query: query.unsafe()),
    ],
)
def test_each_nonterminal_modifier_returns_a_new_query(name, compose):
    """Ordinary fluent clauses should all preserve the original query value."""
    ws = _workspace(f"query-branch-{name}")
    source = ws.query(QueryDoc)
    original = source.spec

    branch = compose(source)

    assert branch is not source
    assert isinstance(branch, hb.Query)
    assert source.spec == original


def test_having_and_available_also_branch_from_their_sources():
    """Specialized fluent clauses should follow the same immutable value rule."""
    ws = _workspace("query-branch-specialized")
    aggregate = ws.query(QueryDoc).aggregate(rows=QueryDoc.object_id.count())
    aggregate_spec = aggregate.spec
    having = aggregate.having({"rows": {"$gt": 0}})
    catalog = ws.query(hb.Catalog)
    catalog_spec = catalog.spec
    available = catalog.available(now=0)

    assert having is not aggregate
    assert aggregate.spec == aggregate_spec
    assert available is not catalog
    assert catalog.spec == catalog_spec


def test_count_is_source_only_terminal_and_performs_no_io_until_execute():
    """Count should build a terminal query and execute only at an explicit boundary."""

    class Runtime:
        def __init__(self):
            self.executed = []
            self.explained = []
            self.module_resolver = hb.DEFAULT_CONTEXT.modules()

        def execute(self, spec):
            self.executed.append(spec)
            if spec.aggregate is not None:
                return hb.ResultFrame.from_rows([{"count": 1}], identity_column=None)
            return hb.ResultFrame.from_rows([{"object_id": "doc-1", "title": "A"}])

        def explain(self, spec):
            self.explained.append(spec)
            return {"entity_id": spec.entity_id}

    runtime = Runtime()
    query = hb.Query(runtime, QueryDoc)
    count_query = query.count()

    assert isinstance(count_query, hb.Query)
    assert runtime.executed == []
    assert runtime.explained == []

    count_frame = count_query.execute()
    assert count_frame.identity_column is None
    assert count_frame.scalar() == 1
    assert count_query.explain() == {"entity_id": "query-value-doc"}
    assert len(runtime.executed) == 1
    assert len(runtime.explained) == 1

    assert query.execute().ids == ["doc-1"]
    assert len(runtime.executed) == 2


@pytest.mark.parametrize(
    "compose",
    [
        lambda query: query.select("title"),
        lambda query: query.group_by("title"),
        lambda query: query.aggregate(rows=QueryDoc.object_id.count()),
        lambda query: query.aggregate(rows=QueryDoc.object_id.count()).having({"rows": {"$gt": 0}}),
        lambda query: query.order_by("title"),
        lambda query: query.offset(1),
        lambda query: query.limit(1),
    ],
)
def test_count_rejects_result_shaping_clauses(compose):
    """Count should terminate only a source query, never a shaped result query."""
    ws = _workspace("query-count-source-only")

    with pytest.raises(ValueError, match="count"):
        compose(ws.query(QueryDoc)).count()


def test_count_query_rejects_post_terminal_composition():
    """A terminal count query should expose only explain and execute behavior."""
    ws = _workspace("query-count-terminal")
    count_query = ws.query(QueryDoc).where(QueryDoc.title != "draft").count()

    for compose in (
        lambda: count_query.where(QueryDoc.title == "published"),
        lambda: count_query.select("title"),
        lambda: count_query.order_by("title"),
        lambda: count_query.offset(1),
        lambda: count_query.limit(1),
        lambda: count_query.unsafe(),
        lambda: count_query.count(),
    ):
        with pytest.raises(ValueError, match="terminal count"):
            compose()


def test_repeated_execute_recomputes_and_returns_detached_count_frames():
    """A query should retain no result and each execution should read current rows."""
    ws = _workspace("query-count-recompute")
    ws.upsert(QueryDoc, {"object_id": "d1", "title": "first", "emb": [1, 0]})
    query = ws.query(QueryDoc).where(QueryDoc.title != "ignored").count()

    first = query.execute()
    ws.upsert(QueryDoc, {"object_id": "d2", "title": "second", "emb": [0, 1]})
    second = query.execute()

    assert first is not second
    assert first.identity_column is None
    assert second.identity_column is None
    assert first.scalar() == 1
    assert second.scalar() == 2


def test_count_field_and_distinct_options_have_explicit_scalar_exports():
    """Count should support field cardinality without changing its frame boundary."""
    ws = _workspace("query-count-field")
    ws.upsert_many(
        QueryDoc,
        [
            {"object_id": "d1", "title": "shared", "emb": [1, 0]},
            {"object_id": "d2", "title": "shared", "emb": [0, 1]},
            {"object_id": "d3", "title": "other", "emb": [1, 1]},
        ],
    )

    assert ws.query(QueryDoc).count().execute().scalar() == 3
    assert ws.query(QueryDoc).count("title").execute().scalar() == 2
    assert ws.query(QueryDoc).count(QueryDoc.title, distinct=False).execute().scalar() == 3
