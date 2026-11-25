"""Milvus JSON columns keep Json numbers, Booleans, objects, arrays and tagged values."""

from __future__ import annotations

import datetime
import json

import pytest

import heavenbase as hb
from heavenbase.backends.vector import milvus
from heavenbase.execution.contracts import QueryFragment

pytestmark = pytest.mark.fast

# The key of the object a Milvus JSON column holds a scalar's JSON text under (milvus.MILVUS_JSON_TEXT_KEY).
WRAPPER_KEY = "__heavenbase_json__"

JSON_VALUES = [
    {"k": [1, 2], "nested": {"d": "x"}},
    [1, "two", None],
    [],
    {},
    5,
    -2.5,
    True,
    False,
    {"text": "5", "empty": ""},
    (1, 2),
    {"when": datetime.date(2020, 1, 2)},
    2**70,
    {WRAPPER_KEY: "looks like the wrapper"},
    {WRAPPER_KEY: "5"},
    {WRAPPER_KEY: "5", "__heavenbase_json_version__": 9007199254740993},
    {WRAPPER_KEY: "5", "__heavenbase_json_version__": 9007199254740992.0},
    {WRAPPER_KEY: "x", "other": 1},
]


def _same(left, right) -> bool:
    return type(left) is type(right) and left == right


@pytest.mark.parametrize("value", JSON_VALUES, ids=repr)
def test_milvus_json_column_round_trips_as_a_milvus_server_returns_it(value):
    """A Milvus server keeps the JSON bytes written and pymilvus returns them parsed; the Json value survives that."""
    dtype = hb.Json()
    stored = json.loads(milvus.milvus_json_value(dtype.encode(value)))
    assert _same(dtype.decode(milvus.milvus_json_text(stored)), value)


@pytest.mark.parametrize("text", ["5", "-2.5", "true", "null", '"text"', '"5"', '""'])
def test_milvus_json_column_returns_the_json_text_written(text):
    """Top-level numbers, Booleans and strings come back as the JSON text written, not as a re-parsed or bare value."""
    assert milvus.milvus_json_text(json.loads(milvus.milvus_json_value(text))) == text


def test_milvus_json_column_keeps_objects_and_arrays_as_written():
    """JSON objects and arrays keep the layout 0.1.2.2 wrote, so rows written by it still read back."""
    assert milvus.MILVUS_JSON_TEXT_KEY == WRAPPER_KEY
    text = hb.Json().encode({"k": [1, 2]})
    assert milvus.milvus_json_value(text) == text
    assert milvus.milvus_json_value("[1,2]") == "[1,2]"
    assert milvus.milvus_json_value(None) is None


@pytest.mark.parametrize("text", ["x", "[1]", '{"a":1}', "5", "true", "null", '"text"', '{"__heavenbase_json__":"x"}'])
def test_milvus_json_column_reads_an_object_0122_wrote_with_the_wrapper_key_as_written(text):
    """Legacy objects keep their type even when their value is valid scalar JSON text."""
    dtype = hb.Json()
    stored = {WRAPPER_KEY: text}
    assert json.loads(dtype.encode(stored)) == stored
    assert milvus.milvus_json_text(stored) is stored
    assert dtype.decode(milvus.milvus_json_text(stored)) == stored


def test_milvus_json_column_writes_legacy_wrapper_key_objects_as_objects():
    """The single-key object uses the same storage layout as in 0.1.2.2."""
    text = hb.Json().encode({WRAPPER_KEY: "5"})
    assert milvus.milvus_json_value(text) == text


def test_milvus_lite_stores_every_json_value(tmp_path):
    """Milvus Lite stores a number, Boolean, object, array or tagged Json value and get, scan and near return it unchanged."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    backends = {
        "main": hb.backends.InMemBackend("main"),
        "vec": hb.backends.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "json-values.db")),
    }
    ws = hb.HeavenBase("milvus-lite-json-values", detached=True, backends=backends)

    class Doc(hb.Entity):
        identifier = "milvus-lite-json-values-doc"

        js = hb.field(hb.Json).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).required()

    try:
        ws.register(Doc)
        rows = [{"object_id": f"r{position}", "js": value, "emb": [1.0, float(position)]} for position, value in enumerate([*JSON_VALUES, None])]
        ws.upsert_many(Doc, rows)
        backend = backends["vec"]
        collection = backend._collection(Doc.identifier)
        # Write directly to Milvus using the 0.1.2.2 JSON-column layout.
        legacy = next(row for row in rows if row["js"] == {WRAPPER_KEY: "5"})
        backend.client.upsert(collection_name=collection, data=[{**legacy, "js": hb.Json().encode(legacy["js"])}])
        backend.client.flush(collection_name=collection)
        expected = {row["object_id"]: row["js"] for row in rows}
        for object_id, value in expected.items():
            assert _same(ws.get(object_id, entity=Doc)["js"], value), object_id
        scanned = {row["object_id"]: row["js"] for row in ws.query(Doc).execute().rows()}
        assert scanned.keys() == expected.keys()
        assert all(_same(scanned[object_id], value) for object_id, value in expected.items())
        near = {row["object_id"]: row["js"] for row in ws.query(Doc).near(Doc.emb, [1.0, 0.0], top_k=len(rows)).execute().rows()}
        assert near.keys() == expected.keys()
        assert all(_same(near[object_id], value) for object_id, value in expected.items())
    finally:
        ws.release()


class MilvusMapping(hb.LogicalType):
    """A custom type whose backend representation is a mapping."""

    identifier = "milvus-mapping-test"


@pytest.mark.parametrize("metric", ["cosine", "l2"], ids=["binary64-scan", "native-search"])
def test_milvus_lite_preserves_custom_type_values_matching_the_json_scalar_wrapper(tmp_path, monkeypatch, metric):
    """A custom logical type keeps its stored mapping on all Milvus read paths."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    MilvusMapping.register()
    backend = milvus.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "mapping.db"))
    monkeypatch.setattr(backend, "_configured_metric", lambda: metric.upper())
    ws = hb.HeavenBase(
        "milvus-lite-custom-mapping",
        detached=True,
        backends={"main": hb.backends.InMemBackend("main"), "vec": backend},
    )

    class Doc(hb.Entity):
        identifier = "milvus-lite-custom-mapping-doc"

        custom = hb.field(MilvusMapping).store(to="vec")
        scalar = hb.field(hb.Json).store(to="vec")
        emb = hb.field(hb.Vector[2]).required().store(to="vec", strategy=hb.VectorIndex.identifier)

    value = {WRAPPER_KEY: "5", "__heavenbase_json_version__": 9007199254740993}
    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "a", "custom": value, "scalar": 5, "emb": [1.0, 0.0]})
        entity_id = Doc.identifier
        collection = backend._collection(entity_id)
        backend.client.flush(collection_name=collection)
        stored = backend.client.query(collection_name=collection, filter='object_id == "a"', output_fields=["custom"])
        assert stored[0]["custom"] == value

        assert ws.get("a", entity=Doc)["custom"] == value
        assert backend.scan(entity_id)[0]["custom"] == value
        assert list(backend.iter_scan(entity_id, batch_size=1))[0]["custom"] == value
        assert backend.scan_page(entity_id, after=None, batch_size=1)[0]["custom"] == value
        assert ws.query(Doc).near(Doc.emb, [1.0, 0.0], top_k=1).execute().rows()[0]["custom"] == value
        near = QueryFragment(
            "vec",
            "near",
            "emb",
            {"vector": [1.0, 0.0], "metric": metric, "top_k": 1},
            returns=["score"],
            notes={"kind": "milvus_search", "mode": "py_scan" if metric == "cosine" else "native"},
        ).bind(entity_id)
        row = backend.execute(near).rows()[0]
        assert row["custom"] == value
        assert row["scalar"] == "5"
    finally:
        ws.release()
