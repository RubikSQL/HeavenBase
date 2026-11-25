from __future__ import annotations

from types import MappingProxyType

import heavenbase as hb
import pytest

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class JSONDoc(hb.Entity):
    identifier = "query-json-doc"

    title = hb.field(hb.ShortText)
    tags = hb.field(hb.Array[hb.ShortText])
    emb = hb.field(hb.Vector[2])


def test_query_json_lowers_every_clause_to_the_fluent_request():
    """The JSON adapter should validate and lower into the ordinary query request."""
    ws = hb.HeavenBase("query-json-lowering")
    ws.register(JSONDoc)
    query = ws.query_json(
        JSONDoc,
        MappingProxyType(
            {
                "filter": {"tags": MappingProxyType({"contains": "guide"})},
                "near": {"field": "emb", "query": [1, 0], "top_k": 3, "metric": "cosine"},
                "traverse": {"field": "links", "start": "d1", "max_hops": 2, "direction": "any"},
                "select": ["title", "score"],
                "order": ["-title"],
                "offset": 1,
                "limit": 5,
                "unsafe": True,
            }
        ),
    )

    spec = query.spec
    assert spec.filter.to_dict() == {"filter": {"field": "tags", "op": "array_contains", "value": "guide"}}
    assert spec.near.to_dict() == {"field": "emb", "value": [1, 0], "top_k": 3, "metric": "cosine"}
    assert spec.traverse.to_dict() == {"field": "links", "start": "d1", "min_hops": 1, "max_hops": 2, "direction": "any"}
    assert spec.select == ("title", "score")
    assert tuple((order.field, order.desc) for order in spec.order) == (("title", True),)
    assert spec.offset == 1
    assert spec.limit == 5
    assert spec.unsafe is True


def test_query_json_treats_null_optional_clauses_as_omitted():
    """Null optional clauses should preserve the omitted-clause contract."""
    ws = hb.HeavenBase("query-json-null-clauses")
    ws.register(JSONDoc)
    spec = ws.query_json(
        JSONDoc,
        {"filter": None, "available": None, "near": None, "traverse": None},
    ).spec

    assert spec.filter is None
    assert spec.near is None
    assert spec.traverse is None


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ([], "JSON query spec must be an object"),
        ({"limt": 1}, "Unsupported JSON query key 'limt'"),
        ({"filter": []}, "JSON filter clause must be an object"),
        ({"filter": {"$and": {"title": "A"}}}, "JSON filter \\$and must be an array"),
        ({"filter": {"$or": [1]}}, "Filter expression must be a mapping"),
        ({"filter": {"$unknown": []}}, "Unsupported logical filter operator"),
        ({"near": []}, "JSON near clause must be an object"),
        ({"near": {"field": "emb"}}, "JSON near clause requires 'query'"),
        ({"near": {"field": "emb", "value": [1, 0]}}, "Unsupported JSON near key 'value'"),
        ({"traverse": {"field": "links"}}, "JSON traverse clause requires 'start'"),
        ({"traverse": {"field": "links", "start_id": "d1"}}, "Unsupported JSON traverse key 'start_id'"),
        ({"select": "title"}, "JSON select clause must be an array"),
        ({"order": [1]}, "JSON order entries must be strings"),
        ({"unsafe": 1}, "unsafe enabled must be a boolean"),
    ],
)
def test_query_json_rejects_ambiguous_or_malformed_shapes(query, message):
    """Malformed JSON input should fail at the adapter with contextual errors."""
    ws = hb.HeavenBase("query-json-errors")
    ws.register(JSONDoc)
    with pytest.raises((TypeError, ValueError), match=message):
        ws.query_json(JSONDoc, query)


def test_query_json_validates_available_options_before_catalog_lowering():
    """Catalog availability should reject unknown and non-boolean option values."""
    ws = hb.HeavenBase("query-json-available")
    with pytest.raises(ValueError, match="Unsupported Catalog available key 'visible'"):
        ws.query_json(hb.Catalog, {"available": {"visible": True}})
    with pytest.raises(TypeError, match="Catalog available stale must be a boolean"):
        ws.query_json(hb.Catalog, {"available": {"stale": 1}})
