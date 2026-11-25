"""Query results take their columns from the query and entity schema, not from which rows matched."""

import pytest

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.integration]


class Ticket(hb.Entity):
    identifier = "result-columns-ticket"

    queue = hb.field(hb.ShortText)
    rank = hb.field(hb.Integer)
    note = hb.field(hb.ShortText).optional()
    meta = hb.field(hb.Json).optional()
    embedding = hb.field(hb.Vector[2]).optional()


class IdleTicket(hb.Entity):
    identifier = "result-columns-idle-ticket"

    queue = hb.field(hb.ShortText)
    rank = hb.field(hb.Integer)


COLUMNS = ["object_id", "embedding", "meta", "note", "queue", "rank"]
BACKENDS = {"inmem": {"type": "inmem"}, "json": {"type": "json"}, "sqlite": {"type": "sqlite", "database": ":memory:"}}


@pytest.fixture(scope="module", params=sorted(BACKENDS))
def workspace(request, tmp_path_factory):
    """Share one read-only workspace per backend; IdleTicket never stores a row."""
    config = dict(BACKENDS[request.param])
    if config["type"] == "json":
        config["path"] = hb.utils.path_to_file_uri(tmp_path_factory.mktemp("result-columns-json"))
    ws = hb.HeavenBase(f"result-columns-{request.param}", detached=True, backends={"main": config})
    ws.register(Ticket)
    ws.register(IdleTicket)
    ws.upsert_many(
        Ticket,
        [
            {"object_id": "t1", "queue": "ops", "rank": 2, "embedding": [1, 0]},
            {"object_id": "t2", "queue": "ops", "rank": 1, "embedding": [0, 1]},
        ],
    )
    yield ws
    ws.release()


def _nothing(ws):
    return ws.query(Ticket).where(Ticket.rank > 100)


@pytest.mark.slow
@pytest.mark.parametrize(
    "shape",
    [
        lambda query: query,
        lambda query: query.order_by("rank"),
        lambda query: query.order_by(Ticket.rank, desc=True).offset(1).limit(5),
        lambda query: query.order_by("queue").order_by("rank", desc=True).order_by("meta.venue"),
    ],
    ids=["unordered", "ordered", "desc-paged", "multi-key"],
)
def test_query_matching_nothing_answers_an_empty_frame_with_entity_columns(workspace, shape):
    """Ordering and pagination over an empty result keep every declared column instead of failing."""
    frame = shape(_nothing(workspace)).execute()
    assert frame.rows() == [] and frame.col_names() == COLUMNS


def test_ordered_query_over_an_entity_without_rows_keeps_entity_columns(workspace):
    """An entity with no stored rows answers its declared shape too."""
    frame = workspace.query(IdleTicket).order_by("rank", desc=True).execute()
    assert frame.rows() == [] and frame.col_names() == ["object_id", "queue", "rank"]


def test_selected_ordered_query_matching_nothing_keeps_the_selected_columns(workspace):
    """A projection over an empty result answers exactly the requested columns."""
    frame = _nothing(workspace).select("queue", "meta.venue").order_by("rank").execute()
    assert frame.rows() == [] and frame.col_names() == ["object_id", "queue", "meta.venue"]
    spec = {"filter": {"queue": "absent"}, "select": ["rank", "note"], "order": ["-rank", "object_id"]}
    assert workspace.query_json(Ticket, spec).execute().col_names() == ["object_id", "rank", "note"]


@pytest.mark.parametrize("exact", [False, True], ids=["ann", "exact"])
def test_near_query_matching_nothing_keeps_the_score_column(workspace, exact):
    """A near clause declares `score`, so ordering or selecting it works on an empty result."""
    query = _nothing(workspace).near("embedding", [1, 0], exact=exact)
    assert query.order_by("score", desc=True).execute().col_names() == [*COLUMNS, "score"]
    assert query.select("rank", "score").order_by("score").execute().col_names() == ["object_id", "rank", "score"]


@pytest.mark.parametrize(
    "shape",
    [
        lambda query: query,
        lambda query: query.order_by("rank"),
        lambda query: query.near("embedding", [1, 0], exact=True),
        lambda query: query.near("embedding", [1, 0]),
    ],
    ids=["unordered", "ordered", "exact-near", "ann-near"],
)
def test_populated_result_carries_the_columns_no_matched_row_stored(workspace, shape):
    """A populated result holds every declared column, a field no matched row stored as null, like an empty one."""
    populated = shape(workspace.query(Ticket).where(Ticket.queue == "ops")).execute()
    empty = shape(_nothing(workspace)).execute()
    assert sorted(populated.ids) == ["t1", "t2"]
    assert populated.col_names() == empty.col_names()
    assert {row["object_id"]: (row["note"], row["meta"]) for row in populated.rows()} == {"t1": (None, None), "t2": (None, None)}


class WeightedTicket(hb.Entity):
    identifier = "result-columns-weighted-ticket"

    title = hb.field(hb.ShortText)
    weight = hb.field(hb.Integer).optional()
    embedding = hb.field(hb.Vector[2])


@pytest.mark.slow
@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_result_columns_keep_one_order_whichever_rows_matched(backend, tmp_path):
    """The identity, the other fields by name, then `score`, whichever optional fields the matched rows stored."""
    config = dict(BACKENDS[backend])
    if config["type"] == "json":
        config["path"] = hb.utils.path_to_file_uri(tmp_path)
    ws = hb.HeavenBase(f"result-columns-order-{backend}", detached=True, backends={"main": config})
    ws.register(WeightedTicket)
    rows = [{"object_id": "w", "title": "full", "weight": 1, "embedding": [1, 0]}, {"object_id": "x", "title": "bare", "embedding": [0, 1]}]
    ws.upsert_many(WeightedTicket, rows)
    columns = ["object_id", "embedding", "title", "weight"]
    for title, ids in (("full", ["w"]), ("bare", ["x"]), ("none", [])):
        query = ws.query(WeightedTicket).where(WeightedTicket.title == title)
        assert (query.execute().ids, query.execute().col_names()) == (ids, columns)
        for exact in (False, True):
            near = query.near("embedding", [1, 0], exact=exact).execute()
            assert (near.ids, near.col_names()) == (ids, [*columns, "score"])
    ws.release()


def test_optional_field_that_no_matched_row_stores_still_orders_and_selects(workspace):
    """A declared field missing from every matched row reads as null, and ordering by it keeps the row shape."""
    query = workspace.query(Ticket).where(Ticket.queue == "ops")
    ordered = query.order_by("note").order_by("rank").execute()
    assert ordered.ids == ["t2", "t1"] and ordered.col_names() == query.order_by("rank").execute().col_names()
    assert query.select("note").order_by("rank").execute().rows() == [{"object_id": "t2", "note": None}, {"object_id": "t1", "note": None}]


@pytest.mark.parametrize("matched", [True, False], ids=["populated", "empty"])
@pytest.mark.parametrize(
    ("shape", "unknown"),
    [
        (lambda query: query.order_by("no_such_field"), "no_such_field"),
        (lambda query: query.order_by("no_such_field").order_by("rank"), "no_such_field"),
        (lambda query: query.select("no_such_field").order_by("rank"), "no_such_field"),
        (lambda query: query.select("rank", "score"), "score"),
    ],
    ids=["order", "first-order-key", "select-with-order", "score-without-near"],
)
def test_unknown_result_column_fails_naming_it_whether_or_not_rows_match(workspace, matched, shape, unknown):
    """An undeclared order or select name fails the same way on populated and empty results."""
    query = workspace.query(Ticket).where(Ticket.rank > (0 if matched else 100))
    with pytest.raises(ValueError, match=f"Unsupported result column '{unknown}'"):
        shape(query).execute()


def test_grouped_ordered_query_matching_nothing_keeps_the_aggregate_columns(workspace):
    """Grouping, HAVING, ordering, and pagination over no rows answer the aggregate output columns."""
    grouped = _nothing(workspace).group_by(Ticket.queue).aggregate(n=Ticket.object_id.count(), top=Ticket.rank.max())
    frame = grouped.having({"n": {"$gt": 0}}).order_by("n", desc=True).order_by("queue").limit(3).execute()
    assert frame.rows() == [] and frame.col_names() == ["queue", "n", "top"]
    assert _nothing(workspace).aggregate(n=Ticket.object_id.count()).order_by("n").execute().rows() == [{"n": 0}]
    with pytest.raises(ValueError, match="Unsupported result column 'rank'"):
        grouped.order_by("rank").execute()  # an entity field that is not an aggregate output stays unknown
