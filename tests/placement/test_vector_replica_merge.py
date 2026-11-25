"""Row merging must treat a vector backend's copy of an embedding as a replica.

A vector store keeps its own copy of the field it indexes so it can search on it. That copy
is not authoritative for row reads: it goes through a lossy float32 round trip, and when one
collection backs several vector fields on the same entity, its read-side attribution follows
schema field order — an order manifest replay does not preserve. Merging it as a peer of the
inline copy therefore produces a conflict where there is no disagreement, and the conflict is
fatal: `merge_id_rows` raises "object_id ... is not unique across routed backends" and every
read of that row dies.

Reported from a two-vector entity (`title_vec`, `desc_vec`) whose single stored embedding was
attributed to whichever field sorted first after replay, so a replayed dashboard read of any
row raised. The fix makes such copies SOFT: they fill a gap, never dispute or override an
inline copy, and a field stored only in the vector backend still reads normally.
"""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from heavenbase.execution.frame.result import ResultFrame
from heavenbase.workspace.rows.store import RowFragment


class _Codec:
    """Identity codec: these tests exercise merging, not encoding."""

    def decode_row_items(self, schema, backend_name, row):
        return dict(row)

    def same_field_value(self, schema, key, left, right):
        return left == right

    def decode_field_value(self, field_schema, value):
        return value


class _UnsettledRoute:
    """A route that cannot settle a hydration miss, like Pinecone without a reported index view."""

    def settle_query_misses(self, entity_id, object_ids, *, _near_fragment=None):
        return {}


def _store(bindings, *, absent_row_error=lambda entity_id, object_id: None):
    """Return a RowStore whose workspace reports ``bindings`` for the probe entity.

    ``absent_row_error`` answers whether a row that no route returned may still
    exist; by default some route shows it is absent.
    """
    from heavenbase.workspace.rows.store import _RowStore

    workspace = SimpleNamespace(
        _row_codec=_Codec(),
        storage_plans={"probe": SimpleNamespace(bindings=bindings)},
        entities={},
        _materializer=None,
        _row_lookup=SimpleNamespace(absent_row_error=absent_row_error),
        _lock=SimpleNamespace(read=nullcontext),
        backends=SimpleNamespace(get=lambda _name: _UnsettledRoute()),
    )
    return _RowStore(workspace)


_SCHEMA = SimpleNamespace(entity_id="probe", pk_name="object_id")

#: One embedding field stored inline AND replicated into a vector backend.
_BINDINGS = [
    SimpleNamespace(field_name="title_vec", backend="main", strategy="inline"),
    SimpleNamespace(field_name="title_vec", backend="vectors", strategy="vector"),
    SimpleNamespace(field_name="desc_vec", backend="main", strategy="inline"),
    SimpleNamespace(field_name="desc_vec", backend="vectors", strategy="vector"),
]


def test_a_vector_replica_never_conflicts_with_the_inline_copy():
    merged = _store(_BINDINGS).merge_id_rows(
        _SCHEMA,
        "row-1",
        [
            RowFragment("main", {"object_id": "row-1", "title_vec": [1.0, 2.0]}),
            # The vector backend answers with the OTHER field's embedding — the
            # misattribution replay produces. As a replica it must be ignored, not fought.
            RowFragment("vectors", {"object_id": "row-1", "title_vec": [9.0, 9.0]}),
        ],
        unique=True,
        validate_schema=False,
    )

    assert merged["title_vec"] == [1.0, 2.0]


def test_the_inline_copy_replaces_a_replica_that_arrived_first():
    """Fragment order is backend order, which no caller controls."""
    merged = _store(_BINDINGS).merge_id_rows(
        _SCHEMA,
        "row-1",
        [
            RowFragment("vectors", {"object_id": "row-1", "title_vec": [9.0, 9.0]}),
            RowFragment("main", {"object_id": "row-1", "title_vec": [1.0, 2.0]}),
        ],
        unique=True,
        validate_schema=False,
    )

    assert merged["title_vec"] == [1.0, 2.0]


def test_a_vector_only_field_still_reads_from_the_vector_backend():
    """With no non-vector owner, the vector route is authoritative and readable."""
    vector_only = [SimpleNamespace(field_name="desc_vec", backend="vectors", strategy="vector")]
    merged = _store(vector_only).merge_id_rows(
        _SCHEMA,
        "row-1",
        [
            RowFragment("main", {"object_id": "row-1", "title": "t"}),
            RowFragment("vectors", {"object_id": "row-1", "desc_vec": [0.5, 0.5]}),
        ],
        unique=True,
        validate_schema=False,
    )

    assert merged["desc_vec"] == [0.5, 0.5]


@pytest.mark.parametrize("vector_first", [False, True])
def test_an_authoritative_null_is_not_replaced_by_a_stale_vector_replica(vector_first):
    """A routed null is a real inline value, not a gap for a replica to fill."""
    inline = RowFragment("main", {"object_id": "row-1", "title_vec": None})
    vector = RowFragment("vectors", {"object_id": "row-1", "title_vec": [9.0, 9.0]})
    merged = _store(_BINDINGS).merge_id_rows(
        _SCHEMA,
        "row-1",
        [vector, inline] if vector_first else [inline, vector],
        unique=True,
        validate_schema=False,
    )

    assert merged["title_vec"] is None


def test_query_hydration_does_not_restore_a_vector_value_over_an_authoritative_null():
    """A near result frame is another replica, not a new row authority."""
    store = _store(_BINDINGS)
    fragments = [
        RowFragment("main", {"object_id": "row-1", "title_vec": None}),
        RowFragment("vectors", {"object_id": "row-1", "title_vec": [9.0, 9.0]}),
    ]

    def rows_for_ids(entity_id, row_ids):
        assert entity_id == "probe"
        assert list(row_ids) == ["row-1"]
        return {"row-1": fragments}

    store.rows_for_ids = rows_for_ids
    schema = SimpleNamespace(
        entity_id="probe",
        pk_name="object_id",
        fields={"object_id": None, "title_vec": None},
        get=lambda _key: None,
    )
    frame = ResultFrame.from_rows(
        [{"object_id": "row-1", "title_vec": [9.0, 9.0], "_score": 0.9}],
        metadata_columns=("_score",),
    )

    hydrated = store.hydrate_query_frame(schema, frame).rows()[0]

    assert hydrated["title_vec"] is None
    assert hydrated["_score"] == 0.9


def test_query_hydration_leaves_out_a_row_that_no_route_holds_any_more():
    """A row deleted between a backend query and hydration is not returned as an id-only row of nulls."""
    store = _store(_BINDINGS)
    store.rows_for_ids = lambda entity_id, row_ids: {"row-1": [RowFragment("main", {"object_id": "row-1", "title_vec": [1.0, 0.0]})], "gone": []}
    schema = SimpleNamespace(
        entity_id="probe",
        pk_name="object_id",
        fields={"object_id": None, "title_vec": None},
        get=lambda _key: None,
    )
    frame = ResultFrame.from_rows(
        [{"object_id": "gone", "_score": 0.95}, {"object_id": "row-1", "_score": 0.9}],
        metadata_columns=("_score",),
    )

    hydrated = store.hydrate_query_frame(schema, frame)

    assert hydrated.ids == ["row-1"]
    assert hydrated.rows()[0]["_score"] == 0.9


def test_query_hydration_keeps_a_row_whose_absence_no_route_settles():
    """A miss that no route settles, like a Pinecone fetch of a recent record, keeps the query's values."""
    store = _store(_BINDINGS, absent_row_error=lambda entity_id, object_id: ValueError("a fetch can miss a recent record"))
    store.rows_for_ids = lambda entity_id, row_ids: {"recent": []}
    schema = SimpleNamespace(
        entity_id="probe",
        pk_name="object_id",
        fields={"object_id": None, "title_vec": None},
        get=lambda _key: None,
    )
    frame = ResultFrame.from_rows([{"object_id": "recent", "title_vec": [1.0, 0.0], "_score": 0.95}], metadata_columns=("_score",))

    [hydrated] = store.hydrate_query_frame(schema, frame).rows()

    assert hydrated["object_id"] == "recent"
    assert hydrated["title_vec"] == [1.0, 0.0]
    assert hydrated["_score"] == 0.95


def test_vector_only_backends_that_disagree_still_raise():
    """Without a non-vector owner, vector routes remain authoritative peers."""
    bindings = [
        SimpleNamespace(field_name="title_vec", backend="left", strategy="vector"),
        SimpleNamespace(field_name="title_vec", backend="right", strategy="vector"),
    ]
    with pytest.raises(ValueError, match="not unique across routed backends"):
        _store(bindings).merge_id_rows(
            _SCHEMA,
            "row-1",
            [
                RowFragment("left", {"object_id": "row-1", "title_vec": [1.0, 2.0]}),
                RowFragment("right", {"object_id": "row-1", "title_vec": [9.0, 9.0]}),
            ],
            unique=True,
            validate_schema=False,
        )


def test_two_non_vector_backends_that_disagree_still_raise():
    """The fix is scoped to vector replicas — a real disagreement is still an error."""
    with pytest.raises(ValueError, match="not unique across routed backends"):
        _store([]).merge_id_rows(
            _SCHEMA,
            "row-1",
            [
                RowFragment("main", {"object_id": "row-1", "title": "left"}),
                RowFragment("other", {"object_id": "row-1", "title": "right"}),
            ],
            unique=True,
            validate_schema=False,
        )
