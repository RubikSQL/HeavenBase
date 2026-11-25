"""Provider-contract tests for the extended vector backend family."""

from __future__ import annotations

import json
import math
import os
import socket
import sys
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import pytest

import heavenbase as hb
from heavenbase.backends.vector.milvus import MILVUS_MAX_TOP_K, MilvusBackend, MilvusLiteBackend
from heavenbase.backends.vector.qdrant import QdrantBackend
from heavenbase.backends.vector.weaviate import WeaviateBackend
from heavenbase.backends.vector.handler.milvus import milvus_filter_handler, milvus_near_handler
from heavenbase.backends.vector.handler.qdrant import qdrant_filter_handler, qdrant_near_handler
from heavenbase.backends.vector.handler.weaviate import weaviate_filter_handler, weaviate_near_handler
from heavenbase.backends.vector.handler.chroma import chroma_near_handler
from heavenbase.backends.vector.handler.lance import lance_near_handler
from heavenbase.backends.vector.handler.pgvector import pgvector_near_handler
from heavenbase.backends.vector.handler.pinecone import pinecone_near_handler
from heavenbase.backends.vector.handler.redisearch import redisearch_near_handler
from heavenbase.execution.contracts import QueryFragment, RowOp
from heavenbase.placement import StorageBinding
from heavenbase.schema import EntitySchema, FieldSchema
from heavenbase.utils.config import CM_HVNB
from heavenbase.utils.serialize import loads_b64

pytestmark = pytest.mark.fast
_requires_milvus_lite = pytest.mark.skipif(
    find_spec("pymilvus") is None or find_spec("milvus_lite") is None,
    reason="pymilvus and milvus-lite are both required",
)


def _schema(*, vector_type=None) -> EntitySchema:
    return EntitySchema(
        "vector-extension-doc",
        "VectorExtensionDoc",
        fields={
            "object_id": FieldSchema("object_id", hb.Identifier(), pk=True, required=True),
            "title": FieldSchema("title", hb.ShortText(), required=True),
            "embedding": FieldSchema("embedding", vector_type or hb.Vector[2], required=True),
            "attrs": FieldSchema("attrs", hb.Json()),
            "links": FieldSchema("links", hb.HyperG["target" : hb.Identifier]),
        },
    )


def _bindings(schema: EntitySchema, backend_name: str) -> list[StorageBinding]:
    return [
        StorageBinding(schema.entity_id, "object_id", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "title", backend_name, hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "embedding", backend_name, hb.VectorIndex.identifier, "test"),
        StorageBinding(schema.entity_id, "attrs", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "links", "graph", hb.GraphEdge.identifier, "test"),
    ]


class _ConstructionConfig:
    """Minimal bound config authority for constructor-default tests."""

    def __init__(self, root: Path, backend_defaults: dict):
        self.root = str(root)
        self.default = {"heavenbase": {"backend_defaults": backend_defaults}}

    def get(self, path: str, default=None):
        value = self.default
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class _Value:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeQdrantModels:
    class Distance:
        COSINE = "Cosine"

    class VectorParams(_Value):
        pass

    class PointStruct(_Value):
        pass

    class PointIdsList(_Value):
        pass

    class MatchValue(_Value):
        pass

    class MatchAny(_Value):
        pass

    class Range(_Value):
        pass

    class FieldCondition(_Value):
        pass

    class Filter(_Value):
        pass


def _qdrant_condition_matches(payload: dict, condition) -> bool:
    if isinstance(condition, _FakeQdrantModels.Filter):
        must = getattr(condition, "must", []) or []
        should = getattr(condition, "should", []) or []
        must_not = getattr(condition, "must_not", []) or []
        return (
            all(_qdrant_condition_matches(payload, item) for item in must)
            and (not should or any(_qdrant_condition_matches(payload, item) for item in should))
            and not any(_qdrant_condition_matches(payload, item) for item in must_not)
        )
    actual = payload.get(condition.key)
    match = getattr(condition, "match", None)
    if isinstance(match, _FakeQdrantModels.MatchValue):
        if isinstance(actual, list):
            return match.value in actual
        return actual == match.value
    if isinstance(match, _FakeQdrantModels.MatchAny):
        if isinstance(actual, list):
            return any(value in match.any for value in actual)
        return actual in match.any
    range_value = getattr(condition, "range", None)
    if range_value is not None:
        return all(
            {
                "lt": actual < value,
                "lte": actual <= value,
                "gt": actual > value,
                "gte": actual >= value,
            }[name]
            for name, value in range_value.__dict__.items()
        )
    return False


class _FakeQdrantClient:
    def __init__(self, *, collections=None, collection_configs=None):
        self.collections: dict[str, dict[str, _Value]] = collections if collections is not None else {}
        self.collection_configs: dict[str, dict[str, _Value]] = collection_configs if collection_configs is not None else {}
        self.closed = False

    def collection_exists(self, name):
        return name in self.collections

    def get_collections(self):
        return _Value(collections=[_Value(name=name) for name in self.collections])

    def create_collection(self, *, collection_name, vectors_config):
        assert vectors_config
        self.collections[collection_name] = {}
        self.collection_configs[collection_name] = dict(vectors_config)

    def get_collection(self, *, collection_name):
        return _Value(config=_Value(params=_Value(vectors=self.collection_configs[collection_name])))

    def upsert(self, *, collection_name, points, wait):
        assert wait is True
        for point in points:
            self.collections[collection_name][point.id] = _Value(
                id=point.id,
                payload=dict(point.payload),
                vector={key: list(value) for key, value in point.vector.items()},
            )

    def delete(self, *, collection_name, points_selector, wait):
        assert wait is True
        for point_id in points_selector.points:
            self.collections[collection_name].pop(point_id, None)

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        assert with_payload and with_vectors
        return [self.collections[collection_name][point_id] for point_id in ids if point_id in self.collections[collection_name]]

    def scroll(self, *, collection_name, scroll_filter, limit, offset, with_payload, with_vectors):
        assert with_payload and with_vectors
        points = list(self.collections[collection_name].values())
        if scroll_filter is not None:
            points = [point for point in points if _qdrant_condition_matches(point.payload, scroll_filter)]
        start = int(offset or 0)
        page = points[start : start + limit]
        next_offset = start + len(page) if start + len(page) < len(points) else None
        return page, next_offset

    def query_points(
        self,
        *,
        collection_name,
        query,
        using,
        query_filter,
        limit,
        with_payload,
        with_vectors,
    ):
        assert with_payload and with_vectors
        points = list(self.collections[collection_name].values())
        if query_filter is not None:
            points = [point for point in points if _qdrant_condition_matches(point.payload, query_filter)]
        for point in points:
            vector = point.vector[using]
            norm = math.sqrt(sum(value * value for value in vector)) * math.sqrt(sum(value * value for value in query))
            point.score = sum(left * right for left, right in zip(vector, query)) / norm if norm else 0.0
        return _Value(points=sorted(points, key=lambda point: point.score, reverse=True)[:limit])

    def count(self, *, collection_name, exact):
        assert exact is True
        return _Value(count=len(self.collections[collection_name]))

    def delete_collection(self, *, collection_name):
        self.collections.pop(collection_name, None)
        self.collection_configs.pop(collection_name, None)

    def close(self):
        self.closed = True


class _FakeWeaviateProperty(_Value):
    pass


class _FakeWeaviateFilterBuilder:
    def __init__(self, field):
        self.field = field

    def equal(self, value):
        return ("eq", self.field, value)

    def less_than(self, value):
        return ("lt", self.field, value)

    def less_or_equal(self, value):
        return ("lte", self.field, value)

    def greater_than(self, value):
        return ("gt", self.field, value)

    def greater_or_equal(self, value):
        return ("gte", self.field, value)

    def contains_any(self, value):
        return ("contains_any", self.field, list(value))


class _FakeWeaviateFilter:
    @staticmethod
    def by_property(field):
        return _FakeWeaviateFilterBuilder(field)

    @staticmethod
    def all_of(children):
        return ("and", children)

    @staticmethod
    def any_of(children):
        return ("or", children)

    @staticmethod
    def not_(child):
        return ("not", child)


class _FakeWeaviateClasses:
    class config:
        class VectorDistances:
            COSINE = "cosine"

        class DataType:
            TEXT = "text"
            TEXT_ARRAY = "text[]"
            INT = "int"
            INT_ARRAY = "int[]"
            NUMBER = "number"
            NUMBER_ARRAY = "number[]"
            BOOL = "bool"
            BOOL_ARRAY = "bool[]"

        class Tokenization:
            FIELD = "field"

        Property = _FakeWeaviateProperty

        class Configure:
            class VectorIndex:
                @staticmethod
                def hnsw(*, distance_metric):
                    return _Value(index_type="hnsw", distance=distance_metric)

            class Vectors:
                @staticmethod
                def self_provided(*, name, vector_index_config):
                    return _Value(
                        name=name,
                        vectorizer=_Value(vectorizer="none"),
                        vectorIndexType="hnsw",
                        vectorIndexConfig=vector_index_config,
                    )

    class query:
        Filter = _FakeWeaviateFilter

        class MetadataQuery(_Value):
            pass


def _weaviate_matches(properties: dict, condition) -> bool:
    if condition is None:
        return True
    op = condition[0]
    if op == "and":
        return all(_weaviate_matches(properties, child) for child in condition[1])
    if op == "or":
        return any(_weaviate_matches(properties, child) for child in condition[1])
    if op == "not":
        return not _weaviate_matches(properties, condition[1])
    actual = properties.get(condition[1])
    value = condition[2]
    if op == "eq":
        return actual == value
    if op == "contains_any":
        return any(item in value for item in actual) if isinstance(actual, list) else actual in value
    return {
        "lt": actual < value,
        "lte": actual <= value,
        "gt": actual > value,
        "gte": actual >= value,
    }[op]


class _FakeWeaviateCollection:
    def __init__(self):
        self.objects: dict[str, _Value] = {}
        self.data = _FakeWeaviateData(self)
        self.query = _FakeWeaviateQuery(self)
        self.aggregate = _FakeWeaviateAggregate(self)
        self.config = None

    def iterator(self, *, include_vector):
        assert include_vector is True
        return iter(self.objects.values())


class _FakeProviderError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class _FakeWeaviateData:
    def __init__(self, collection):
        self.collection = collection
        self.calls = []
        self.exists_results = []
        self.insert_errors = []
        self.replace_errors = []

    def exists(self, object_uuid):
        self.calls.append(("exists", object_uuid))
        if self.exists_results:
            return self.exists_results.pop(0)
        return object_uuid in self.collection.objects

    def insert(self, *, uuid, properties, vector):
        self.calls.append(("insert", uuid))
        if self.insert_errors:
            raise self.insert_errors.pop(0)
        self.collection.objects[uuid] = _Value(uuid=uuid, properties=dict(properties), vector=dict(vector))

    def replace(self, *, uuid, properties, vector):
        self.calls.append(("replace", uuid))
        if self.replace_errors:
            raise self.replace_errors.pop(0)
        self.collection.objects[uuid] = _Value(uuid=uuid, properties=dict(properties), vector=dict(vector))

    def delete_by_id(self, object_uuid):
        self.calls.append(("delete", object_uuid))
        self.collection.objects.pop(object_uuid, None)


class _FakeWeaviateQuery:
    def __init__(self, collection):
        self.collection = collection
        self.fetch_calls = []

    def fetch_object_by_id(self, *, uuid, include_vector):
        assert include_vector is True
        return self.collection.objects.get(uuid)

    def fetch_objects(self, *, filters, limit, after, include_vector):
        assert include_vector is True
        objects = [item for item in self.collection.objects.values() if _weaviate_matches(item.properties, filters)]
        start = 0
        if after is not None:
            cursors = [str(item.uuid) for item in objects]
            start = cursors.index(str(after)) + 1
        self.fetch_calls.append({"filters": filters, "limit": limit, "after": after})
        return _Value(objects=objects[start : start + limit])

    def near_vector(
        self,
        *,
        near_vector,
        target_vector,
        filters,
        limit,
        include_vector,
        return_metadata,
    ):
        assert include_vector is True and return_metadata is not None
        objects = [item for item in self.collection.objects.values() if _weaviate_matches(item.properties, filters)]
        ranked = []
        for item in objects:
            vector = item.vector[target_vector]
            norm = math.sqrt(sum(value * value for value in vector)) * math.sqrt(sum(value * value for value in near_vector))
            score = sum(left * right for left, right in zip(vector, near_vector)) / norm if norm else 0.0
            ranked.append(
                _Value(
                    uuid=item.uuid,
                    properties=item.properties,
                    vector=item.vector,
                    metadata=_Value(distance=1.0 - score),
                )
            )
        return _Value(objects=sorted(ranked, key=lambda item: item.metadata.distance)[:limit])


class _FakeWeaviateAggregate:
    def __init__(self, collection):
        self.collection = collection

    def over_all(self, *, total_count):
        assert total_count is True
        return _Value(total_count=len(self.collection.objects))


class _FakeWeaviateConfig:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def get(self):
        return self.descriptor


class _FakeWeaviateCollections:
    def __init__(self, *, items=None, created=None):
        self.items: dict[str, _FakeWeaviateCollection] = items if items is not None else {}
        self.created: dict[str, dict] = created if created is not None else {}

    def exists(self, name):
        return name in self.items

    def create(self, **kwargs):
        name = kwargs["name"]
        collection = _FakeWeaviateCollection()
        vector_config = {}
        for item in kwargs["vector_config"]:
            vector_config[item.name] = _Value(
                vectorizer=item.vectorizer,
                vector_index_type=item.vectorIndexType,
                vector_index_config=_Value(
                    distance_metric=item.vectorIndexConfig.distance,
                    index_type=item.vectorIndexConfig.index_type,
                ),
            )
        collection.config = _FakeWeaviateConfig(
            _Value(
                properties=list(kwargs["properties"]),
                vector_config=vector_config,
            )
        )
        self.items[name] = collection
        self.created[name] = kwargs

    def use(self, name):
        return self.items[name]

    def delete(self, name):
        self.items.pop(name, None)

    def list_all(self, *, simple):
        assert simple is True
        return dict(self.items)


class _FakeWeaviateClient:
    def __init__(self, *, collections=None):
        self.collections = collections if collections is not None else _FakeWeaviateCollections()
        self.closed = False

    def close(self):
        self.closed = True


def _fake_milvus_key_filter_ids(filter: str) -> list[str]:
    """Decode the escaped ``object_id in [...]`` key filter that point reads and deletes send."""
    prefix = "object_id in "
    assert filter.startswith(prefix + "["), filter
    # A filtered key read appends `` and (<filter>)``, which this fake does not evaluate.
    keys, _end = json.JSONDecoder().raw_decode(filter[len(prefix) :])
    return [str(item) for item in keys]


def _fake_milvus_filtered(rows: list[dict], filter: str, output_fields: list[str]) -> list[dict]:
    """Apply the key filters a Milvus fake understands; other expressions return every row, as before."""
    if filter.startswith("object_id in ["):
        wanted = set(_fake_milvus_key_filter_ids(filter))
        rows = [row for row in rows if str(row["object_id"]) in wanted]
    elif filter.startswith("object_id > "):
        after = json.loads(filter[len("object_id > ") :])
        rows = [row for row in rows if row["object_id"] > after]
    if output_fields == ["*"]:
        return [dict(row) for row in rows]
    return [{name: row[name] for name in output_fields} for row in rows]


def _install_fake_milvus(monkeypatch):
    module = ModuleType("pymilvus")

    class DataType:
        ARRAY = "ARRAY"
        BOOL = "BOOL"
        DOUBLE = "DOUBLE"
        FLOAT_VECTOR = "FLOAT_VECTOR"
        INT64 = "INT64"
        JSON = "JSON"
        VARCHAR = "VARCHAR"

    class Schema:
        def __init__(self):
            self.fields = []

        def add_field(self, name, dtype, **kwargs):
            self.fields.append({"name": name, "dtype": dtype, **kwargs})

    class Index:
        def __init__(self):
            self.indexes = []

        def add_index(self, **kwargs):
            self.indexes.append(dict(kwargs))
            return kwargs

    class Iterator:
        def __init__(self, rows):
            self.rows = list(rows)
            self.done = False

        def next(self):
            if self.done:
                return []
            self.done = True
            return list(self.rows)

        def close(self):
            return None

    class MilvusClient:
        stores: dict[str, dict[str, dict[str, dict]]] = {}
        schemas: dict[str, dict[str, dict]] = {}
        indexes: dict[str, dict[str, dict[str, dict]]] = {}

        def __init__(self, *, uri, **kwargs):
            del kwargs
            self.uri = uri
            Path(uri).parent.mkdir(parents=True, exist_ok=True)
            Path(uri).touch(exist_ok=True)
            self.store = self.stores.setdefault(uri, {})
            self.schema_store = self.schemas.setdefault(uri, {})
            self.index_store = self.indexes.setdefault(uri, {})

        def close(self):
            return None

        def create_schema(self, **kwargs):
            del kwargs
            return Schema()

        def prepare_index_params(self):
            return Index()

        def has_collection(self, name):
            return name in self.store

        def create_collection(self, *, collection_name, schema, index_params, **kwargs):
            del kwargs
            self.store[collection_name] = {}
            self.schema_store[collection_name] = {
                "enable_dynamic_field": False,
                "fields": [
                    {
                        "name": field["name"],
                        "type": field["dtype"],
                        **{key: value for key, value in field.items() if key not in ("name", "dtype")},
                    }
                    for field in schema.fields
                ],
            }
            self.index_store[collection_name] = {
                item["field_name"]: {
                    "field_name": item["field_name"],
                    "index_name": item["field_name"],
                    "metric_type": item.get("metric_type", ""),
                    "index_type": item["index_type"],
                }
                for item in index_params.indexes
            }

        def describe_collection(self, *, collection_name):
            assert collection_name in self.store
            return self.schema_store[collection_name]

        def load_collection(self, *, collection_name):
            assert collection_name in self.store
            self.loaded = [*getattr(self, "loaded", []), collection_name]

        def list_indexes(self, *, collection_name, field_name=""):
            indexes = self.index_store[collection_name]
            return [name for name, descriptor in indexes.items() if not field_name or descriptor["field_name"] == field_name]

        def describe_index(self, *, collection_name, index_name):
            return self.index_store[collection_name][index_name]

        def list_collections(self):
            return list(self.store)

        def drop_collection(self, *, collection_name):
            self.store.pop(collection_name, None)
            self.schema_store.pop(collection_name, None)
            self.index_store.pop(collection_name, None)

        def upsert(self, *, collection_name, data):
            for row in data:
                self.store[collection_name][str(row["object_id"])] = dict(row)

        def delete(self, *, collection_name, filter):
            for object_id in _fake_milvus_key_filter_ids(filter):
                self.store[collection_name].pop(object_id, None)

        def get(self, **kwargs):
            pytest.fail("MilvusClient.get quotes string keys without escaping them; key reads must use an escaped filter")

        def query_iterator(self, *, collection_name, **kwargs):
            del kwargs
            return Iterator(self.store[collection_name].values())

        def query(self, *, collection_name, output_fields, filter="", **kwargs):
            del kwargs
            if output_fields == ["count(*)"]:
                return [{"count(*)": len(self.store[collection_name])}]
            return _fake_milvus_filtered(list(self.store[collection_name].values()), filter, output_fields)

        def flush(self, **kwargs):
            del kwargs

    module.DataType = DataType
    module.MilvusClient = MilvusClient
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    return MilvusClient


def _install_fake_remote_milvus(monkeypatch):
    module = ModuleType("pymilvus")

    class MilvusClient:
        databases: dict[str, dict[str, dict]] = {"default": {}}
        instances = []
        fail_drop = False

        def __init__(self, *, uri, token="", db_name=None, **kwargs):
            del uri, token, kwargs
            self.db_name = db_name or "default"
            self.closed = False
            self.instances.append(self)

        @property
        def store(self):
            return self.databases.setdefault(self.db_name, {})

        def close(self):
            self.closed = True

        def list_databases(self):
            return list(self.databases)

        def create_database(self, name):
            self.databases.setdefault(name, {})

        def drop_database(self, db_name):
            if self.fail_drop:
                raise RuntimeError("injected database cleanup failure")
            self.databases.pop(db_name, None)

        def list_collections(self):
            return list(self.store)

        def drop_collection(self, *, collection_name):
            self.store.pop(collection_name, None)

    module.MilvusClient = MilvusClient
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    return MilvusClient


def _attach_qdrant(backend: QdrantBackend, client: _FakeQdrantClient) -> None:
    backend.client = client
    backend._models = _FakeQdrantModels
    backend._connect_pending = False


def _attach_weaviate(backend: WeaviateBackend, client: _FakeWeaviateClient) -> None:
    backend.client = client
    backend._wvc = _FakeWeaviateClasses
    backend._connect_pending = False


def test_config_defaults_drive_lazy_qdrant_and_weaviate_construction(tmp_path):
    config = _ConstructionConfig(
        tmp_path,
        {
            "qdrant": {"url": "http://qdrant.internal:6333", "api_key": "q-key", "timeout": 2.5},
            "weaviate": {
                "url": "https://weaviate.internal:8443",
                "grpc_host": "grpc.weaviate.internal",
                "grpc_port": 5443,
                "api_key": "w-key",
            },
        },
    )

    qdrant = QdrantBackend("vec", config=config)
    weaviate = WeaviateBackend("vec", config=config)

    assert qdrant.client is None
    assert qdrant.url == "http://qdrant.internal:6333"
    assert qdrant.api_key == "q-key"
    assert qdrant._client_kwargs["timeout"] == 2.5
    assert weaviate.client is None
    assert weaviate.url == "https://weaviate.internal:8443"
    assert weaviate.grpc_host == "grpc.weaviate.internal"
    assert weaviate.grpc_port == 5443
    assert weaviate.api_key == "w-key"


@pytest.mark.parametrize("exact", [False, True])
def test_milvus_native_search_reads_current_data(monkeypatch, tmp_path, exact):
    """Both native modes request Strong consistency, including cross-client writes."""
    client_cls = _install_fake_milvus(monkeypatch)
    calls = []

    def search(self, **kwargs):
        calls.append(kwargs)
        return [[]]

    monkeypatch.setattr(client_cls, "search", search, raising=False)
    backend = MilvusBackend("vec", str(tmp_path / "strong.db"), index_type="FLAT")
    monkeypatch.setattr(backend, "_configured_metric", lambda: "L2")
    schema = _unique_schema("strong")
    try:
        backend.ensure(schema, _bindings(schema, "vec"))
        fragment = QueryFragment(
            backend.name,
            "near",
            "embedding",
            {"vector": [1.0, 0.0], "top_k": 1, "metric": "l2", "exact": exact},
            notes={"kind": "milvus_search", "mode": "native", "entity_id": schema.entity_id},
        )
        backend.execute(fragment)
        assert calls[0]["consistency_level"] == "Strong"
        if exact:
            describe = backend.client.describe_index
            monkeypatch.setattr(backend.client, "describe_index", lambda **kwargs: {**describe(**kwargs), "index_type": "HNSW"})
            with pytest.raises(RuntimeError, match="live FLAT index"):
                backend.execute(fragment)
    finally:
        backend._close_client()


def test_milvus_server_near_above_the_search_limit_ranks_through_the_portable_scan(monkeypatch, tmp_path):
    """A top_k above a Milvus server's search maximum is never sent to client.search, so no neighbour is silently dropped."""
    client_cls = _install_fake_milvus(monkeypatch)

    def search(self, **kwargs):
        pytest.fail(f"A Milvus server rejects a search limit above {MILVUS_MAX_TOP_K}")

    monkeypatch.setattr(client_cls, "search", search, raising=False)
    assert MilvusBackend.provider_max_top_k == MILVUS_MAX_TOP_K
    assert MilvusLiteBackend.provider_max_top_k is None

    class Context:
        backend = "vec"
        backend_identifier = "milvus"
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = SimpleNamespace(get=lambda path, default=None: "L2")
        top_k = MILVUS_MAX_TOP_K + 1
        metric = "l2"
        exact = False

    field = FieldSchema("embedding", hb.Vector[2])
    scanned = milvus_near_handler(field, [1.0, 0.0], Context())
    assert scanned.mode() == "py_scan"
    assert f"at most {MILVUS_MAX_TOP_K} neighbours" in scanned.notes["fallback_reason"]
    Context.backend_identifier = "milvus-lite"
    assert milvus_near_handler(field, [1.0, 0.0], Context()).mode() == "native"
    Context.backend_identifier = "milvus"
    Context.top_k = MILVUS_MAX_TOP_K
    assert milvus_near_handler(field, [1.0, 0.0], Context()).mode() == "native"

    backend = MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "top-k.db"), ws_id="top-k-limit")
    monkeypatch.setattr(backend, "_configured_metric", lambda: "L2")
    monkeypatch.setattr(backend, "provider_max_top_k", MILVUS_MAX_TOP_K)
    schema = _unique_schema("top-k")
    try:
        backend.ensure(schema, _bindings(schema, "vec"))
        backend.upsert(
            [
                RowOp(schema.entity_id, "near", {"title": "near", "embedding": [1.0, 0.0]}),
                RowOp(schema.entity_id, "far", {"title": "far", "embedding": [0.0, 1.0]}),
            ]
        )
        # A fragment marked native by other code takes the same scan at execution time.
        native = QueryFragment(
            backend.name,
            "near",
            "embedding",
            {"vector": [1.0, 0.0], "top_k": MILVUS_MAX_TOP_K + 1, "metric": "l2"},
            returns=["score"],
            notes={**scanned.notes, "mode": "native", "entity_id": schema.entity_id},
        )
        assert backend.execute(native).ids == ["near", "far"]
    finally:
        backend._close_client()


def test_milvus_lite_resolves_workspace_file_reopens_and_drops(monkeypatch, tmp_path):
    _install_fake_milvus(monkeypatch)
    uri = hb.utils.path_to_file_uri(tmp_path / "milvus-lite.db")
    schema = _schema()
    bindings = _bindings(schema, "vec")

    first = MilvusLiteBackend("vec", uri, ws_id="workspace-one")
    sibling = MilvusLiteBackend("vec", uri, ws_id="workspace-two")
    assert first.client is None
    assert Path(first.uri).is_absolute()
    assert first.uri != sibling.uri
    assert Path(first.uri).name.count("workspace_one") == 1
    assert "workspace_one" not in first._collection(schema.entity_id)

    first.ensure(schema, bindings)
    first.upsert([RowOp(schema.entity_id, "a", {"title": "alpha", "embedding": [1.0, 0.0]})])
    assert Path(first.uri).exists()
    assert first.count(schema.entity_id) == 1

    reopened = MilvusLiteBackend("vec", uri, ws_id="workspace-one")
    reopened.ensure(schema, bindings)
    assert reopened.get_many(schema.entity_id, ["a", "missing"])[0]["title"] == "alpha"
    assert reopened.get_many(schema.entity_id, ["a", "missing"])[1] is None

    reopened.drop()
    assert not Path(reopened.uri).exists()
    assert Path(sibling.uri) != Path(reopened.uri)


def test_milvus_lite_workspace_file_owners_resist_normalized_name_collisions(monkeypatch, tmp_path):
    """Distinct public workspace IDs cannot share or delete one embedded database."""
    _install_fake_milvus(monkeypatch)
    uri = hb.utils.path_to_file_uri(tmp_path / "milvus-lite.db")
    schema = _schema()
    first = MilvusLiteBackend("vec", uri, ws_id="team-a")
    sibling = MilvusLiteBackend("vec", uri, ws_id="team_a")

    assert first.uri != sibling.uri

    first.ensure(schema, _bindings(schema, first.name))
    sibling.ensure(schema, _bindings(schema, sibling.name))
    first.upsert([RowOp(schema.entity_id, "first", {"title": "first", "embedding": [1.0, 0.0]})])
    sibling.upsert([RowOp(schema.entity_id, "sibling", {"title": "sibling", "embedding": [0.0, 1.0]})])
    first.drop()

    assert sibling.get_many(schema.entity_id, ["sibling"])[0]["title"] == "sibling"
    sibling.drop()


def test_milvus_lite_rebind_detaches_failed_close_state(tmp_path):
    """A provider close failure cannot retain the prior workspace file/client."""

    class CloseFailure:
        def close(self):
            raise RuntimeError("close failed")

    backend = MilvusLiteBackend(
        "vec",
        hb.utils.path_to_file_uri(tmp_path / "milvus-rebind.db"),
        ws_id="workspace-old",
    )
    old_uri = backend.uri
    backend.client = CloseFailure()
    backend._lazy_connect_pending = False
    backend._schemas["old"] = _schema()
    backend._owned_fields["old"] = {"embedding"}
    backend._collections["old"] = "old_collection"

    with pytest.raises(RuntimeError, match="close failed"):
        backend.bind_workspace("workspace-new")

    assert backend.ws_id == "workspace-new"
    assert backend.client is None
    assert backend.uri != old_uri
    assert backend._schemas == {}
    assert backend._owned_fields == {}
    assert backend._collections == {}
    assert backend._lazy_connect_pending is True


def test_milvus_lite_rejects_packaged_resource_uri():
    with pytest.raises(ValueError, match="resource"):
        MilvusLiteBackend("vec", "file://&/milvus-lite.db")


def test_milvus_split_placement_persists_only_fields_routed_to_its_backend(monkeypatch, tmp_path):
    _install_fake_milvus(monkeypatch)
    schema = _schema()
    backend = MilvusLiteBackend(
        "vec",
        hb.utils.path_to_file_uri(tmp_path / "milvus-split.db"),
        ws_id="milvus-split-workspace",
    )
    backend.ensure(schema, _bindings(schema, backend.name))
    backend.upsert(
        [
            RowOp(
                schema.entity_id,
                "split",
                {
                    "title": "owned",
                    "embedding": [1.0, 0.0],
                    "attrs": {"not": "owned"},
                    "links": [{"target": "other"}],
                },
            )
        ]
    )

    stored = backend.client.store[backend._collection(schema.entity_id)]["split"]
    assert set(stored) == {"object_id", "title", "embedding"}
    assert backend.get_many(schema.entity_id, ["split"])[0] == {
        "object_id": "split",
        "title": "owned",
        "embedding": [1.0, 0.0],
    }
    backend.drop()


@pytest.mark.parametrize("operation", ["scan", "filter"])
def test_milvus_iterator_reads_skip_duplicate_only_pages(monkeypatch, operation):
    """Milvus scans and filters deduplicate rows without treating duplicates as exhaustion."""
    module = ModuleType("pymilvus")

    class Iterator:
        def __init__(self):
            self.pages = [
                [{"object_id": "first", "title": "match"}],
                [{"object_id": "first", "title": "stale"}],
                [{"object_id": "last", "title": "match"}],
                [],
            ]
            self.closed = False

        def next(self):
            return self.pages.pop(0)

        def close(self):
            self.closed = True

    class MilvusClient:
        def __init__(self, **kwargs):
            del kwargs
            self.iterator = None
            self.iterator_calls = []

        @staticmethod
        def has_collection(name):
            del name
            return True

        def query_iterator(self, **kwargs):
            self.iterator_calls.append(kwargs)
            self.iterator = Iterator()
            return self.iterator

    module.MilvusClient = MilvusClient
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    backend = MilvusBackend("vec", uri="mock://milvus")
    entity_id = "milvus-iterator-pages"
    filter_expr = ""
    if operation == "scan":
        rows = backend.scan(entity_id)
        batch_config = "scan_limit"
    else:
        filter_expr = 'title == "match"'
        fragment = QueryFragment(
            backend.name,
            "eq",
            "title",
            filter_expr,
            notes={"kind": "milvus_filter", "mode": "native", "entity_id": entity_id},
        )
        rows = backend.execute(fragment).rows()
        batch_config = "filter_limit"

    assert [row["object_id"] for row in rows] == ["first", "last"]
    assert backend.client.iterator.closed is True
    assert backend.client.iterator_calls == [
        {
            "collection_name": backend._collection(entity_id),
            "batch_size": int(CM_HVNB.get(f"heavenbase.backend_defaults.milvus.{batch_config}", default=10000)),
            "filter": filter_expr,
            "output_fields": ["*"],
            "consistency_level": "Strong",
        }
    ]


def test_remote_milvus_backend_name_isolates_databases_and_drop(monkeypatch):
    client_cls = _install_fake_remote_milvus(monkeypatch)
    first = MilvusBackend("vec", uri="mock://milvus", ws_id="shared-workspace")
    sibling = MilvusBackend("vec_archive", uri="mock://milvus", ws_id="shared-workspace")
    first_client = first.client
    sibling_client = sibling.client
    first_client.store["first_collection"] = {"a": {"object_id": "a"}}
    sibling_client.store["sibling_collection"] = {"a": {"object_id": "a"}}

    assert first.database != sibling.database
    assert first.database in client_cls.databases
    assert sibling.database in client_cls.databases
    first.drop()

    assert first.database not in client_cls.databases
    assert sibling.database in client_cls.databases
    assert sibling_client.store["sibling_collection"]["a"]["object_id"] == "a"
    assert first_client.closed is True
    assert sibling_client.closed is False
    assert client_cls.instances[-1].closed is True
    sibling.drop()


def test_remote_milvus_drop_surfaces_cleanup_errors_and_closes_clients(monkeypatch):
    client_cls = _install_fake_remote_milvus(monkeypatch)
    backend = MilvusBackend("vec", uri="mock://milvus", ws_id="cleanup-workspace")
    provider_client = backend.client
    client_cls.fail_drop = True

    with pytest.raises(RuntimeError, match="injected database cleanup failure"):
        backend.drop()

    assert "injected database cleanup failure" in backend.client_error
    assert provider_client.closed is True
    assert client_cls.instances[-1].closed is True
    assert backend.client is None


def test_qdrant_full_row_lifecycle_pagination_native_filter_and_near():
    schema = _schema()
    backend = QdrantBackend("vec", url="http://qdrant.invalid", ws_id="qdrant-workspace")
    client = _FakeQdrantClient()
    _attach_qdrant(backend, client)
    backend.ensure(schema, _bindings(schema, backend.name))

    rows = [
        RowOp(
            schema.entity_id,
            str(index),
            {
                "title": "match" if index % 2 == 0 else "other",
                "embedding": [1.0, 0.0] if index == 0 else [0.0, 1.0],
                "attrs": {"must": "not leak"},
            },
        )
        for index in range(257)
    ]
    backend.upsert(rows)
    assert len(backend.scan(schema.entity_id)) == 257
    stored = backend.get_many(schema.entity_id, ["0", "0", "missing"])
    assert [row is not None for row in stored] == [True, True, False]
    assert "attrs" not in stored[0]

    backend.upsert([RowOp(schema.entity_id, "0", {"title": "replaced", "embedding": [1.0, 0.0]})])
    assert backend.get_many(schema.entity_id, ["0"])[0]["title"] == "replaced"
    assert backend.count(schema.entity_id) == 257

    filtered = backend.execute(
        QueryFragment(
            backend.name,
            "eq",
            "title",
            {"op": "eq", "field": "title", "value": "replaced"},
            notes={"kind": "qdrant_filter", "mode": "native", "entity_id": schema.entity_id},
        )
    )
    assert filtered.ids == ["0"]
    near = backend.execute(
        QueryFragment(
            backend.name,
            "near",
            "embedding",
            {"vector": [1.0, 0.0], "top_k": 1, "metric": "cosine"},
            notes={"kind": "qdrant_search", "mode": "native", "entity_id": schema.entity_id},
        )
    )
    assert near.ids == ["0"]

    backend.upsert([RowOp(schema.entity_id, "0", op="delete"), RowOp(schema.entity_id, "missing", op="delete")])
    assert backend.exists_many(schema.entity_id, ["0", "1"]) == [False, True]
    collection = backend._collection(schema.entity_id)
    backend.drop()
    assert collection not in client.collections
    assert client.closed is True


def test_qdrant_missing_owned_collection_fails_upserts_but_keeps_deletes_idempotent():
    """A disappeared collection cannot turn replacement writes into silent success."""
    schema = _schema()
    backend = QdrantBackend("vec", url="http://qdrant.invalid", ws_id="qdrant-missing")
    client = _FakeQdrantClient()
    _attach_qdrant(backend, client)
    backend.ensure(schema, _bindings(schema, backend.name))
    collection = backend._collection(schema.entity_id)
    client.delete_collection(collection_name=collection)

    backend.upsert([RowOp(schema.entity_id, "gone", op="delete")])
    with pytest.raises(RuntimeError, match="disappeared after ensure"):
        backend.upsert([RowOp(schema.entity_id, "lost", {"title": "lost", "embedding": [1.0, 0.0]})])


def test_qdrant_concurrent_collection_create_validates_winner():
    """A same-owner create race is accepted only after physical schema validation."""
    schema = _schema()
    backend = QdrantBackend("vec", url="http://qdrant.invalid", ws_id="qdrant-create-race")
    client = _FakeQdrantClient()
    original_create = client.create_collection

    def racing_create(**kwargs):
        original_create(**kwargs)
        raise RuntimeError("already exists")

    client.create_collection = racing_create
    _attach_qdrant(backend, client)

    backend.ensure(schema, _bindings(schema, backend.name))

    assert backend._collection(schema.entity_id) in client.collections


def test_qdrant_rebind_detaches_failed_close_state():
    """A provider close failure cannot retain prior workspace collection state."""

    class CloseFailure(_FakeQdrantClient):
        def close(self):
            raise RuntimeError("close failed")

    backend = QdrantBackend("vec", url="http://qdrant.invalid", ws_id="workspace-old")
    client = CloseFailure()
    _attach_qdrant(backend, client)
    schema = _schema()
    backend._schemas[schema.entity_id] = schema
    backend._owned_fields[schema.entity_id] = {"embedding"}
    old_collection = backend._collection(schema.entity_id)

    with pytest.raises(RuntimeError, match="close failed"):
        backend.bind_workspace("workspace-new")

    assert backend.ws_id == "workspace-new"
    assert backend.client is None
    assert backend._models is None
    assert backend._schemas == {}
    assert backend._owned_fields == {}
    assert backend._collections == {}
    assert backend._connect_pending is True
    assert backend._collection(schema.entity_id) != old_collection


@pytest.mark.skipif(find_spec("qdrant_client") is None, reason="qdrant-client is not installed")
def test_qdrant_local_base_path_isolates_concurrent_workspaces_and_reopens(tmp_path):
    """One configured local root yields independently lockable persistent workspaces."""
    schema = _schema()
    base_uri = hb.utils.path_to_file_uri(tmp_path / "qdrant")
    first = QdrantBackend("vec", path=base_uri, ws_id="team-a")
    sibling = QdrantBackend("vec", path=base_uri, ws_id="team_a")

    assert first.path != sibling.path
    first.ensure(schema, _bindings(schema, first.name))
    sibling.ensure(schema, _bindings(schema, sibling.name))
    first.upsert([RowOp(schema.entity_id, "first", {"title": "first", "embedding": [1.0, 0.0]})])
    sibling.upsert([RowOp(schema.entity_id, "sibling", {"title": "sibling", "embedding": [0.0, 1.0]})])

    first._close_client()
    reopened = QdrantBackend("vec", path=base_uri, ws_id="team-a")
    reopened.ensure(schema, _bindings(schema, reopened.name))

    assert reopened.get_many(schema.entity_id, ["first"])[0]["title"] == "first"
    reopened.drop()
    assert sibling.get_many(schema.entity_id, ["sibling"])[0]["title"] == "sibling"
    sibling.drop()


def test_weaviate_split_schema_full_row_lifecycle_native_filter_and_near():
    schema = _schema()
    backend = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id="weaviate-workspace")
    client = _FakeWeaviateClient()
    _attach_weaviate(backend, client)
    backend.ensure(schema, _bindings(schema, backend.name))

    collection_name = backend._collection(schema.entity_id)
    properties = client.collections.created[collection_name]["properties"]
    assert [item.name for item in properties] == ["object_id", "title"]

    backend.upsert(
        [
            RowOp(
                schema.entity_id,
                "a",
                {
                    "title": "alpha",
                    "embedding": [1.0, 0.0],
                    "attrs": {"must": "not leak"},
                    "links": [{"target": "b"}],
                },
            ),
            RowOp(schema.entity_id, "b", {"title": "beta", "embedding": [0.0, 1.0]}),
        ]
    )
    stored = backend.get_many(schema.entity_id, ["a", "a", "missing"])
    assert [row is not None for row in stored] == [True, True, False]
    assert stored[0]["title"] == "alpha"
    assert "attrs" not in stored[0]
    assert "links" not in stored[0]

    backend.upsert([RowOp(schema.entity_id, "a", {"title": "updated", "embedding": [1.0, 0.0]})])
    assert backend.count(schema.entity_id) == 2
    filtered = backend.execute(
        QueryFragment(
            backend.name,
            "eq",
            "title",
            {"op": "eq", "field": "title", "value": "updated"},
            notes={"kind": "weaviate_filter", "mode": "native", "entity_id": schema.entity_id},
        )
    )
    assert filtered.ids == ["a"]
    near = backend.execute(
        QueryFragment(
            backend.name,
            "near",
            "embedding",
            {"vector": [1.0, 0.0], "top_k": 1, "metric": "cosine"},
            notes={"kind": "weaviate_search", "mode": "native", "entity_id": schema.entity_id},
        )
    )
    assert near.ids == ["a"]

    backend.upsert([RowOp(schema.entity_id, "a", op="delete"), RowOp(schema.entity_id, "missing", op="delete")])
    assert backend.exists_many(schema.entity_id, ["a", "b"]) == [False, True]
    backend.drop()
    assert collection_name not in client.collections.items
    assert client.closed is True


def test_weaviate_missing_owned_collection_fails_upserts_but_keeps_deletes_idempotent():
    """A disappeared collection cannot turn replacement writes into silent success."""
    schema = _schema()
    backend = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id="weaviate-missing")
    client = _FakeWeaviateClient()
    _attach_weaviate(backend, client)
    backend.ensure(schema, _bindings(schema, backend.name))
    client.collections.delete(backend._collection(schema.entity_id))

    backend.upsert([RowOp(schema.entity_id, "gone", op="delete")])
    with pytest.raises(RuntimeError, match="disappeared after ensure"):
        backend.upsert([RowOp(schema.entity_id, "lost", {"title": "lost", "embedding": [1.0, 0.0]})])


def test_weaviate_concurrent_collection_create_validates_winner():
    """A same-owner create race is accepted only after physical schema validation."""
    schema = _schema()
    backend = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id="weaviate-create-race")
    client = _FakeWeaviateClient()
    original_create = client.collections.create

    def racing_create(**kwargs):
        original_create(**kwargs)
        raise RuntimeError("already exists")

    client.collections.create = racing_create
    _attach_weaviate(backend, client)

    backend.ensure(schema, _bindings(schema, backend.name))

    assert backend._collection(schema.entity_id) in client.collections.items


def test_weaviate_rebind_detaches_failed_close_state():
    """A provider close failure cannot retain prior workspace collection state."""

    class CloseFailure(_FakeWeaviateClient):
        def close(self):
            raise RuntimeError("close failed")

    backend = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id="workspace-old")
    client = CloseFailure()
    _attach_weaviate(backend, client)
    schema = _schema()
    backend._schemas[schema.entity_id] = schema
    backend._owned_fields[schema.entity_id] = {"embedding"}
    old_collection = backend._collection(schema.entity_id)

    with pytest.raises(RuntimeError, match="close failed"):
        backend.bind_workspace("workspace-new")

    assert backend.ws_id == "workspace-new"
    assert backend.client is None
    assert backend._wvc is None
    assert backend._schemas == {}
    assert backend._owned_fields == {}
    assert backend._collections == {}
    assert backend._connect_pending is True
    assert backend._collection(schema.entity_id) != old_collection


def test_qdrant_backend_name_is_part_of_collection_and_point_ownership():
    schema = _schema()
    collections: dict[str, dict[str, _Value]] = {}
    configs: dict[str, dict[str, _Value]] = {}
    first = QdrantBackend("vec", url="http://qdrant.invalid", ws_id="shared-workspace")
    sibling = QdrantBackend("vec_archive", url="http://qdrant.invalid", ws_id="shared-workspace")
    first_client = _FakeQdrantClient(collections=collections, collection_configs=configs)
    sibling_client = _FakeQdrantClient(collections=collections, collection_configs=configs)
    _attach_qdrant(first, first_client)
    _attach_qdrant(sibling, sibling_client)
    first.ensure(schema, _bindings(schema, first.name))
    sibling.ensure(schema, _bindings(schema, sibling.name))

    first_collection = first._collection(schema.entity_id)
    sibling_collection = sibling._collection(schema.entity_id)
    assert first_collection != sibling_collection
    assert first._point_id(schema.entity_id, "same-id") != sibling._point_id(schema.entity_id, "same-id")

    first.upsert([RowOp(schema.entity_id, "same-id", {"title": "first", "embedding": [1.0, 0.0]})])
    sibling.upsert([RowOp(schema.entity_id, "same-id", {"title": "sibling", "embedding": [0.0, 1.0]})])
    first.drop()

    assert first_collection not in collections
    assert sibling_collection in collections
    assert sibling.get_many(schema.entity_id, ["same-id"])[0]["title"] == "sibling"
    assert first_client.closed is True
    assert sibling_client.closed is False


def test_weaviate_backend_name_is_part_of_collection_and_object_ownership():
    schema = _schema()
    shared = _FakeWeaviateCollections()
    first = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id="shared-workspace")
    sibling = WeaviateBackend("vec_archive", url="http://weaviate.invalid", ws_id="shared-workspace")
    first_client = _FakeWeaviateClient(collections=shared)
    sibling_client = _FakeWeaviateClient(collections=shared)
    _attach_weaviate(first, first_client)
    _attach_weaviate(sibling, sibling_client)
    first.ensure(schema, _bindings(schema, first.name))
    sibling.ensure(schema, _bindings(schema, sibling.name))

    first_collection = first._collection(schema.entity_id)
    sibling_collection = sibling._collection(schema.entity_id)
    assert first_collection != sibling_collection
    assert first._object_uuid(schema.entity_id, "same-id") != sibling._object_uuid(schema.entity_id, "same-id")

    first.upsert([RowOp(schema.entity_id, "same-id", {"title": "first", "embedding": [1.0, 0.0]})])
    sibling.upsert([RowOp(schema.entity_id, "same-id", {"title": "sibling", "embedding": [0.0, 1.0]})])
    first.drop()

    assert first_collection not in shared.items
    assert sibling_collection in shared.items
    assert sibling.get_many(schema.entity_id, ["same-id"])[0]["title"] == "sibling"
    assert first_client.closed is True
    assert sibling_client.closed is False


@pytest.mark.parametrize("mismatch", ["vector-name", "dimension", "distance"])
def test_qdrant_rejects_incompatible_existing_named_vector_schema(mismatch):
    schema = _schema()
    backend = QdrantBackend("vec", url="http://qdrant.invalid", ws_id=f"qdrant-schema-{mismatch}")
    client = _FakeQdrantClient()
    _attach_qdrant(backend, client)
    backend.ensure(schema, _bindings(schema, backend.name))
    collection = backend._collection(schema.entity_id)
    if mismatch == "vector-name":
        client.collection_configs[collection] = {"other": _FakeQdrantModels.VectorParams(size=2, distance=_FakeQdrantModels.Distance.COSINE)}
    elif mismatch == "dimension":
        client.collection_configs[collection]["embedding"].size = 3
    else:
        client.collection_configs[collection]["embedding"].distance = "Dot"

    reopened = QdrantBackend("vec", url="http://qdrant.invalid", ws_id=f"qdrant-schema-{mismatch}")
    _attach_qdrant(reopened, client)
    with pytest.raises(RuntimeError, match="does not match HeavenBase physical schema"):
        reopened.ensure(schema, _bindings(schema, reopened.name))


@pytest.mark.parametrize(
    "mismatch",
    ["property", "vector-name", "nonempty-vector-dimension", "distance", "index-type", "vectorizer"],
)
def test_weaviate_rejects_incompatible_existing_collection_schema(mismatch):
    schema = _schema()
    backend = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id=f"weaviate-schema-{mismatch}")
    client = _FakeWeaviateClient()
    _attach_weaviate(backend, client)
    backend.ensure(schema, _bindings(schema, backend.name))
    collection_name = backend._collection(schema.entity_id)
    collection = client.collections.use(collection_name)
    descriptor = collection.config.descriptor
    if mismatch == "property":
        descriptor.properties = [prop for prop in descriptor.properties if prop.name != "title"]
    elif mismatch == "vector-name":
        descriptor.vector_config = {"other": _Value(name="other")}
    elif mismatch == "nonempty-vector-dimension":
        collection.data.insert(
            uuid=backend._object_uuid(schema.entity_id, "wrong-dim"),
            properties={"object_id": "wrong-dim", "title": "bad"},
            vector={"embedding": [1.0, 0.0, 0.0]},
        )
    elif mismatch == "distance":
        descriptor.vector_config["embedding"].vector_index_config.distance_metric = "dot"
    elif mismatch == "index-type":
        descriptor.vector_config["embedding"].vector_index_type = "flat"
    else:
        descriptor.vector_config["embedding"].vectorizer.vectorizer = "text2vec-openai"

    reopened = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id=f"weaviate-schema-{mismatch}")
    _attach_weaviate(reopened, client)
    with pytest.raises(RuntimeError, match="does not match HeavenBase physical schema"):
        reopened.ensure(schema, _bindings(schema, reopened.name))


def test_weaviate_reopen_checks_each_named_vector_beyond_first_object():
    """Dimension validation searches until every represented named vector is observed."""
    schema = _schema()
    schema.fields["secondary"] = FieldSchema("secondary", hb.Vector[3], required=False)
    bindings = [
        *_bindings(schema, "vec"),
        StorageBinding(schema.entity_id, "secondary", "vec", hb.VectorIndex.identifier, "test"),
    ]
    backend = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id="weaviate-multi-vector")
    client = _FakeWeaviateClient()
    _attach_weaviate(backend, client)
    backend.ensure(schema, bindings)
    collection = client.collections.use(backend._collection(schema.entity_id))
    collection.data.insert(
        uuid=backend._object_uuid(schema.entity_id, "first"),
        properties={"object_id": "first", "title": "first"},
        vector={"embedding": [1.0, 0.0]},
    )
    collection.data.insert(
        uuid=backend._object_uuid(schema.entity_id, "second"),
        properties={"object_id": "second", "title": "second"},
        vector={"secondary": [1.0, 0.0]},
    )

    reopened = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id="weaviate-multi-vector")
    _attach_weaviate(reopened, client)
    with pytest.raises(RuntimeError, match="secondary.dim"):
        reopened.ensure(schema, bindings)


@pytest.mark.parametrize("mismatch", ["missing", "distance", "index-type"])
def test_milvus_rejects_incompatible_existing_vector_index(monkeypatch, tmp_path, mismatch):
    """Reopen validation proves the index contract used by native near search."""
    client_cls = _install_fake_milvus(monkeypatch)
    uri = hb.utils.path_to_file_uri(tmp_path / f"milvus-index-{mismatch}.db")
    schema = _schema()
    backend = MilvusLiteBackend("vec", uri, ws_id=f"milvus-index-{mismatch}")
    backend.ensure(schema, _bindings(schema, backend.name))
    collection = backend._collection(schema.entity_id)
    indexes = client_cls.indexes[backend.uri][collection]
    if mismatch == "missing":
        indexes.pop("embedding")
    elif mismatch == "distance":
        indexes["embedding"]["metric_type"] = "L2"
    else:
        indexes["embedding"]["index_type"] = "AUTOINDEX"

    reopened = MilvusLiteBackend("vec", uri, ws_id=f"milvus-index-{mismatch}")
    with pytest.raises(RuntimeError, match="does not match HeavenBase physical schema"):
        reopened.ensure(schema, _bindings(schema, reopened.name))


def test_milvus_rejects_existing_array_capacity_below_physical_contract(monkeypatch, tmp_path):
    """Reopen validation includes the array length accepted by logical storage."""
    client_cls = _install_fake_milvus(monkeypatch)
    uri = hb.utils.path_to_file_uri(tmp_path / "milvus-array-capacity.db")
    schema = _schema()
    schema.fields["tags"] = FieldSchema("tags", hb.Array[hb.ShortText], required=False)
    bindings = [
        *_bindings(schema, "vec"),
        StorageBinding(schema.entity_id, "tags", "vec", hb.InlineColumn.identifier, "test"),
    ]
    backend = MilvusLiteBackend("vec", uri, ws_id="milvus-array-capacity")
    backend.ensure(schema, bindings)
    collection = backend._collection(schema.entity_id)
    fields = client_cls.schemas[backend.uri][collection]["fields"]
    next(field for field in fields if field["name"] == "tags")["max_capacity"] = 2

    reopened = MilvusLiteBackend("vec", uri, ws_id="milvus-array-capacity")
    with pytest.raises(RuntimeError, match="max_capacity"):
        reopened.ensure(schema, bindings)


def test_milvus_missing_owned_collection_fails_upserts_but_keeps_deletes_idempotent(monkeypatch, tmp_path):
    """A disappeared collection cannot turn replacement writes into silent success."""
    _install_fake_milvus(monkeypatch)
    schema = _schema()
    backend = MilvusLiteBackend(
        "vec",
        hb.utils.path_to_file_uri(tmp_path / "milvus-missing.db"),
        ws_id="milvus-missing",
    )
    backend.ensure(schema, _bindings(schema, backend.name))
    backend.client.drop_collection(collection_name=backend._collection(schema.entity_id))

    backend.upsert([RowOp(schema.entity_id, "gone", op="delete")])
    with pytest.raises(RuntimeError, match="disappeared after ensure"):
        backend.upsert([RowOp(schema.entity_id, "lost", {"title": "lost", "embedding": [1.0, 0.0]})])


def test_milvus_concurrent_collection_create_validates_winner(monkeypatch, tmp_path):
    """A same-owner create race is accepted only after index/schema validation."""
    _install_fake_milvus(monkeypatch)
    schema = _schema()
    backend = MilvusLiteBackend(
        "vec",
        hb.utils.path_to_file_uri(tmp_path / "milvus-create-race.db"),
        ws_id="milvus-create-race",
    )
    backend._need_client()
    original_create = backend.client.create_collection

    def racing_create(**kwargs):
        original_create(**kwargs)
        raise RuntimeError("already exists")

    backend.client.create_collection = racing_create

    backend.ensure(schema, _bindings(schema, backend.name))

    assert backend.client.has_collection(backend._collection(schema.entity_id))
    assert backend.client.loaded == [backend._collection(schema.entity_id)]


def test_weaviate_native_filter_exhausts_cursor_pages_beyond_256_objects():
    schema = _schema()
    backend = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id="weaviate-cursor-workspace")
    client = _FakeWeaviateClient()
    _attach_weaviate(backend, client)
    backend.ensure(schema, _bindings(schema, backend.name))
    backend.upsert([RowOp(schema.entity_id, str(index), {"title": "match", "embedding": [1.0, 0.0]}) for index in range(513)])

    result = backend.execute(
        QueryFragment(
            backend.name,
            "eq",
            "title",
            {"op": "eq", "field": "title", "value": "match"},
            notes={"kind": "weaviate_filter", "mode": "native", "entity_id": schema.entity_id},
        )
    )

    assert len(result) == 513
    query = client.collections.use(backend._collection(schema.entity_id)).query
    assert len(query.fetch_calls) == 3
    assert query.fetch_calls[0]["after"] is None
    assert query.fetch_calls[1]["after"] is not None
    assert query.fetch_calls[2]["after"] is not None
    assert len({str(row["object_id"]) for row in result.rows()}) == 513


@pytest.mark.parametrize(
    "backend_cls,client_factory,attach",
    [
        (QdrantBackend, _FakeQdrantClient, _attach_qdrant),
        (WeaviateBackend, _FakeWeaviateClient, _attach_weaviate),
    ],
)
def test_lazy_remote_vector_availability_tracks_pending_failure_ready_and_drop(monkeypatch, backend_cls, client_factory, attach):
    pending = backend_cls("vec", url="http://provider.invalid", ws_id=f"{backend_cls.identifier}-pending")
    assert pending.tag("backend-availability").value == "unknown"

    failed = backend_cls("vec", url="http://provider.invalid", ws_id=f"{backend_cls.identifier}-failed")

    def fail_connect():
        failed.client = None
        failed.client_error = "injected connection failure"

    monkeypatch.setattr(failed, "_connect", fail_connect)
    with pytest.raises(RuntimeError, match="injected connection failure"):
        failed._need_client()
    assert failed.tag("backend-availability").value == "unavailable"

    ready = backend_cls("vec", url="http://provider.invalid", ws_id=f"{backend_cls.identifier}-ready")
    client = client_factory()
    attach(ready, client)
    assert ready.tag("backend-availability").value == "ready"
    ready.drop()
    assert ready.tag("backend-availability").value == "unavailable"
    assert client.closed is True


@pytest.mark.parametrize(
    "backend_cls,client_factory,attach",
    [
        (QdrantBackend, _FakeQdrantClient, _attach_qdrant),
        (WeaviateBackend, _FakeWeaviateClient, _attach_weaviate),
    ],
)
def test_text_payload_vector_backends_roundtrip_non_utf8_artifacts(backend_cls, client_factory, attach):
    provider = backend_cls.identifier
    workspace_id = f"{provider}-artifact-workspace"
    backend = backend_cls("vec", url="http://provider.invalid", ws_id=workspace_id)
    client = client_factory()
    attach(backend, client)
    ws = hb.HeavenBase(
        workspace_id,
        backends={
            "main": hb.backends.InMemBackend("main", ws_id=workspace_id),
            "vec": backend,
        },
    )

    class BinaryVectorThing(hb.Entity):
        identifier = f"{provider}-binary-vector-thing"

        blob = hb.field(hb.Artifact).store(to="vec")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    payload = b"\x00\xff\xfe\x80HeavenBase"
    try:
        ws.register(BinaryVectorThing)
        ws.upsert(BinaryVectorThing, {"object_id": "binary", "blob": payload, "embedding": [1.0, 0.0]})
        assert backend.tag("binary-storage").value == "base64"
        assert ws.get("binary", entity=BinaryVectorThing)["blob"] == payload

        collection_name = backend._collection(BinaryVectorThing.identifier)
        if backend_cls is QdrantBackend:
            encoded = next(iter(client.collections[collection_name].values())).payload["blob"]
        else:
            encoded = next(iter(client.collections.use(collection_name).objects.values())).properties["blob"]
        assert isinstance(encoded, str)
        assert loads_b64(encoded) == payload
    finally:
        ws.drop()


@pytest.mark.parametrize(
    "replace_missing,insert_conflict,expected_calls",
    [
        (False, True, ["exists", "insert", "replace"]),
        (True, False, ["exists", "replace", "insert"]),
        (True, True, ["exists", "replace", "insert", "replace"]),
    ],
)
def test_weaviate_upsert_retries_concurrent_missing_and_conflict_races(replace_missing, insert_conflict, expected_calls):
    schema = _schema()
    workspace_id = f"weaviate-race-{str(replace_missing).lower()}-{str(insert_conflict).lower()}"
    backend = WeaviateBackend("vec", url="http://weaviate.invalid", ws_id=workspace_id)
    client = _FakeWeaviateClient()
    _attach_weaviate(backend, client)
    backend.ensure(schema, _bindings(schema, backend.name))
    collection = client.collections.use(backend._collection(schema.entity_id))
    object_uuid = backend._object_uuid(schema.entity_id, "race")
    collection.objects[object_uuid] = _Value(
        uuid=object_uuid,
        properties={"object_id": "race", "title": "old"},
        vector={"embedding": [0.0, 1.0]},
    )
    data = collection.data
    data.exists_results = [replace_missing]
    if replace_missing:
        data.replace_errors = [_FakeProviderError("object not found", 404)]
    if insert_conflict:
        data.insert_errors = [_FakeProviderError("object already exists", 409)]

    backend.upsert([RowOp(schema.entity_id, "race", {"title": "new", "embedding": [1.0, 0.0]})])

    assert [call[0] for call in data.calls] == expected_calls
    assert collection.objects[object_uuid].properties["title"] == "new"


@pytest.mark.parametrize("backend_cls,attach", [(QdrantBackend, _attach_qdrant), (WeaviateBackend, _attach_weaviate)])
def test_new_remote_vector_backends_reject_dimensionless_schema(backend_cls, attach):
    schema = _schema(vector_type=hb.Vector())
    backend = backend_cls("vec", url="http://provider.invalid")
    client = _FakeQdrantClient() if backend_cls is QdrantBackend else _FakeWeaviateClient()
    attach(backend, client)

    with pytest.raises(ValueError, match="fixed-dimension"):
        backend.ensure(schema, _bindings(schema, backend.name))


@pytest.mark.parametrize(
    "filter_factory,near_factory,native_kind",
    [
        (qdrant_filter_handler, qdrant_near_handler, "qdrant_filter"),
        (weaviate_filter_handler, weaviate_near_handler, "weaviate_filter"),
    ],
)
def test_new_vector_handlers_claim_only_exact_native_operations(filter_factory, near_factory, native_kind):
    class Context:
        backend = "vec"
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = CM_HVNB
        top_k = 3
        metric = "cosine"

        @staticmethod
        def fallback_reason(op, dtype):
            return f"portable fallback for {op}/{dtype}"

    ctx = Context()
    scalar = FieldSchema("title", hb.ShortText())
    native = filter_factory("eq")(scalar, "alpha", ctx)
    assert native.mode() == "native"
    assert native.kind() == native_kind

    unsupported = filter_factory("contains")(scalar, "alp", ctx)
    assert unsupported.mode() == "py_scan"
    assert "fallback_reason" in unsupported.notes

    vector = FieldSchema("embedding", hb.Vector[2])
    near = near_factory(vector, [1.0, 0.0], ctx)
    assert near.mode() == "native"
    ctx.metric = "l2"
    exact_fallback = near_factory(vector, [1.0, 0.0], ctx)
    assert exact_fallback.mode() == "py_scan"


@pytest.mark.skipif(find_spec("qdrant_client") is None, reason="qdrant-client is not installed")
def test_qdrant_filter_claims_respect_strict_match_model_value_domains():
    """Unsupported float/bool membership values fall back before model validation."""
    from qdrant_client import models

    class Context:
        backend = "vec"
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = CM_HVNB
        top_k = 50

        @staticmethod
        def fallback_reason(op, dtype):
            return f"portable fallback for {op}/{dtype}"

    float_field = FieldSchema("weight", hb.Float())
    bool_field = FieldSchema("active", hb.Boolean())
    float_array = FieldSchema("weights", hb.Array[hb.Float])
    unsupported = [
        qdrant_filter_handler("eq")(float_field, 1.5, Context()),
        qdrant_filter_handler("in")(float_field, [1.5, 2.5], Context()),
        qdrant_filter_handler("in")(bool_field, [True, False], Context()),
        qdrant_filter_handler("array_contains")(float_array, 1.5, Context()),
    ]
    assert all(fragment.mode() == "py_scan" for fragment in unsupported)

    backend = QdrantBackend("vec", url="http://qdrant.invalid")
    backend._models = models
    native_bool = qdrant_filter_handler("eq")(bool_field, True, Context())
    native_ints = qdrant_filter_handler("in")(FieldSchema("rank", hb.Integer()), [1, 2], Context())
    native_strings = qdrant_filter_handler("in")(FieldSchema("title", hb.ShortText()), ["a", "b"], Context())
    assert all(fragment.mode() == "native" for fragment in (native_bool, native_ints, native_strings))
    for fragment in (native_bool, native_ints, native_strings):
        backend._field_condition(fragment.payload)


@pytest.mark.parametrize(
    "provider,filter_factory,values",
    [
        ("qdrant", qdrant_filter_handler, ([], [None], ["alpha", None])),
        ("weaviate", weaviate_filter_handler, ([], [None], ["alpha", None])),
        ("milvus", milvus_filter_handler, ([], [None], ["alpha", None])),
    ],
)
def test_vector_filter_handlers_fall_back_for_none_bearing_and_empty_membership(provider, filter_factory, values):
    class Context:
        backend = "vec"
        backend_identifier = provider
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = CM_HVNB
        top_k = 50

        @staticmethod
        def fallback_reason(op, dtype):
            return f"portable fallback for {op}/{dtype}"

    field = FieldSchema("title", hb.ShortText(), required=False)
    for value in values:
        fragment = filter_factory("in")(field, value, Context())
        assert fragment.mode() == "py_scan"
        assert "fallback_reason" in fragment.notes


@pytest.mark.parametrize(
    "provider,filter_factory",
    [
        ("qdrant", qdrant_filter_handler),
        ("weaviate", weaviate_filter_handler),
        ("milvus", milvus_filter_handler),
    ],
)
def test_vector_filter_handlers_fall_back_for_nullable_null_semantics(provider, filter_factory):
    class Context:
        backend = "vec"
        backend_identifier = provider
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = CM_HVNB
        top_k = 50

        @staticmethod
        def fallback_reason(op, dtype):
            return f"nullable fallback for {op}/{dtype}"

    nullable = FieldSchema("title", hb.ShortText(), required=False)
    assert filter_factory("eq")(nullable, None, Context()).mode() == "py_scan"
    if provider == "milvus":
        for op, value in (
            ("eq", "alpha"),
            ("ne", "alpha"),
            ("in", ["alpha"]),
            ("lt", "z"),
        ):
            assert filter_factory(op)(nullable, value, Context()).mode() == "py_scan"


@_requires_milvus_lite
def test_milvus_lite_rejects_optional_routed_vectors_before_writes(tmp_path):
    """Milvus cannot represent a missing FLOAT_VECTOR in an otherwise present row."""
    schema = _schema()
    schema.fields["embedding"].required = False
    backend = MilvusLiteBackend(
        "vec",
        hb.utils.path_to_file_uri(tmp_path / "milvus-optional-vector.db"),
        ws_id="milvus-optional-vector",
    )
    try:
        with pytest.raises(ValueError, match="routed Vector fields must be required"):
            backend.ensure(schema, _bindings(schema, backend.name))
    finally:
        backend.drop()


@_requires_milvus_lite
def test_milvus_lite_rejects_collection_with_wrong_primary_key(tmp_path):
    """Reopen validation requires HeavenBase object IDs to be the provider key."""
    from pymilvus import DataType

    schema = _schema()
    backend = MilvusLiteBackend(
        "vec",
        hb.utils.path_to_file_uri(tmp_path / "milvus-wrong-primary.db"),
        ws_id="milvus-wrong-primary",
    )
    try:
        backend._need_client()
        collection = backend._collection(schema.entity_id)
        schema_def = backend.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema_def.add_field("object_id", DataType.VARCHAR, max_length=backend._varchar_len(hb.Identifier()))
        schema_def.add_field(
            "title",
            DataType.VARCHAR,
            is_primary=True,
            max_length=backend._varchar_len(schema.fields["title"].dtype),
        )
        schema_def.add_field("embedding", DataType.FLOAT_VECTOR, dim=2)
        indexes = backend.client.prepare_index_params()
        indexes.add_index(field_name="embedding", index_type="FLAT", metric_type="COSINE")
        backend.client.create_collection(
            collection_name=collection,
            schema=schema_def,
            index_params=indexes,
            consistency_level="Strong",
        )

        with pytest.raises(RuntimeError, match=r"object_id\.is_primary"):
            backend.ensure(schema, _bindings(schema, backend.name))
    finally:
        backend.drop()


@_requires_milvus_lite
def test_milvus_lite_nullable_scalar_not_uses_two_valued_scan_semantics(tmp_path):
    """NOT(eq) includes explicit-null and absent rows through portable evaluation."""
    schema = EntitySchema(
        "milvus-nullable-not",
        "MilvusNullableNot",
        fields={
            "object_id": FieldSchema("object_id", hb.Identifier(), pk=True, required=True),
            "rank": FieldSchema("rank", hb.Integer(), required=False),
            "embedding": FieldSchema("embedding", hb.Vector[2], required=True),
        },
    )
    bindings = [
        StorageBinding(schema.entity_id, "object_id", "vec", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "rank", "vec", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "embedding", "vec", hb.VectorIndex.identifier, "test"),
    ]
    backend = MilvusLiteBackend(
        "vec",
        hb.utils.path_to_file_uri(tmp_path / "milvus-nullable-not.db"),
        ws_id="milvus-nullable-not",
    )

    class Context:
        backend = "vec"
        backend_identifier = "milvus-lite"
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = CM_HVNB
        top_k = 50

        @staticmethod
        def fallback_reason(op, dtype):
            return f"nullable fallback for {op}/{dtype}"

    try:
        backend.ensure(schema, bindings)
        backend.upsert(
            [
                RowOp(schema.entity_id, "one", {"rank": 1, "embedding": [1.0, 0.0]}),
                RowOp(schema.entity_id, "two", {"rank": 2, "embedding": [0.0, 1.0]}),
                RowOp(schema.entity_id, "null", {"rank": None, "embedding": [1.0, 1.0]}),
                RowOp(schema.entity_id, "absent", {"embedding": [0.5, 0.5]}),
            ]
        )
        leaf = milvus_filter_handler("eq")(schema.fields["rank"], 1, Context())
        negated = QueryFragment("vec", "not", "*", [leaf]).bind(schema.entity_id)

        assert leaf.mode() == "py_scan"
        assert set(backend.execute(negated).ids) == {"two", "null", "absent"}
    finally:
        backend.drop()


@pytest.mark.parametrize(
    "provider,near_factory",
    [
        ("qdrant", qdrant_near_handler),
        ("weaviate", weaviate_near_handler),
        ("milvus", milvus_near_handler),
    ],
)
def test_vector_metric_mismatch_uses_matching_portable_scorer_and_unknown_metrics_fail(provider, near_factory):
    class Context:
        backend = "vec"
        backend_identifier = provider
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = CM_HVNB
        top_k = 2
        metric = "l2"
        exact = False

    field = FieldSchema("embedding", hb.Vector[2])
    fragment = near_factory(field, [1.0, 0.0], Context())
    scorer = fragment.notes["row_payload"]
    assert fragment.mode() == "py_scan"
    assert scorer({"embedding": [1.0, 0.0]}) > scorer({"embedding": [4.0, 0.0]})
    assert scorer({"embedding": None}) is None  # an empty vector is dropped, never ranked above real L2 rows

    Context.metric = "not-a-metric"
    with pytest.raises(ValueError, match="unsupported metric"):
        near_factory(field, [1.0, 0.0], Context())


class _IndexMetricConfig:
    """CM_HVNB view that answers one backend's index-metric key with a chosen value."""

    def __init__(self, key: str | None, value: str | None):
        self.key, self.value = key, value

    def get(self, key, default=None, **kwargs):
        return self.value if key == self.key else CM_HVNB.get(key, default=default, **kwargs)


# provider -> (near handler, index-metric config key, index metric that ranks like l2, like ip)
_SCAN_SCORER_PROVIDERS = {
    "lance": (lance_near_handler, None, None, None),
    "pgvector": (pgvector_near_handler, None, None, None),
    "chroma": (chroma_near_handler, "heavenbase.backend_defaults.chroma.metric", "l2", "ip"),
    "pinecone": (pinecone_near_handler, "heavenbase.backend_defaults.pinecone.metric", "euclidean", "dotproduct"),
    "redisearch": (redisearch_near_handler, "heavenbase.backend_defaults.redisearch.metric", "l2", "ip"),
}


def _scan_scorer_near(provider: str, metric: str, index_metric: str | None):
    near_factory, key, _, _ = _SCAN_SCORER_PROVIDERS[provider]

    class Context:
        backend = "vec"
        backend_identifier = provider
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = _IndexMetricConfig(key, index_metric)
        top_k = 2
        exact = False

    Context.metric = metric
    return near_factory(FieldSchema("embedding", hb.Vector[2]), [1.0, 0.0], Context())


@pytest.mark.parametrize("provider", sorted(_SCAN_SCORER_PROVIDERS))
@pytest.mark.parametrize("metric", ["l2", "euclidean", "ip", "inner", "inner_product", "dot"])
def test_vector_near_scan_scorers_rank_by_the_requested_metric(provider, metric):
    """Every vector backend's near scan scorer ranks by the requested metric and drops vector-less rows under it.

    Regression: the Lance, Chroma, Pinecone and pgvector scorers always ranked by cosine and scored
    a row without a vector 0.0, so a filter that sent their near through the scan changed the
    nearest neighbour under metric='l2' and ranked an empty row above every real one.
    """
    _, _, like_l2, like_ip = _SCAN_SCORER_PROVIDERS[provider]
    l2_family = metric in ("l2", "euclidean")
    fragment = _scan_scorer_near(provider, metric, like_l2 if l2_family else like_ip)
    scorer = fragment.notes["row_payload"]
    far, close = {"embedding": [10.0, 1.0]}, {"embedding": [1.0, 0.5]}  # cosine prefers far, L2 prefers close

    assert fragment.mode() == "native"  # the index (or the native search) ranks by this metric
    if l2_family:
        assert scorer(close) > scorer(far)
        assert scorer(close) == pytest.approx(-0.5)
    else:
        assert (scorer(far), scorer(close)) == (pytest.approx(10.0), pytest.approx(1.0))
    assert scorer({"embedding": None}) is None  # never ranked above a real neighbour
    assert _scan_scorer_near(provider, "cosine", "cosine").notes["row_payload"]({"embedding": None}) is None  # nor does native search
    with pytest.raises(ValueError, match="unsupported metric"):
        _scan_scorer_near(provider, "not-a-metric", "cosine")


@pytest.mark.parametrize("provider", ["chroma", "redisearch"])
def test_vector_near_whose_metric_is_not_the_index_metric_ranks_through_the_scan(provider):
    """A metric the collection or index does not rank by scans with that metric instead of ranking natively by another."""
    fragment = _scan_scorer_near(provider, "l2", "cosine")
    scorer = fragment.notes["row_payload"]

    assert fragment.mode() == "py_scan"
    assert "ranks through the portable scan scorer" in fragment.fallback_reason()
    assert scorer({"embedding": [1.0, 0.5]}) > scorer({"embedding": [10.0, 1.0]})


def test_pinecone_near_refuses_a_metric_its_index_does_not_rank_by():
    """Pinecone has no row scan, so a near metric the index does not rank by is refused rather than answered by the index metric."""
    with pytest.raises(ValueError, match="the index ranks by 'cosine'"):
        _scan_scorer_near("pinecone", "l2", "cosine")


def test_pinecone_near_checks_the_metric_its_index_was_created_with():
    """A config change after an index exists does not change how the index ranks, so the near is checked against the index.

    Regression: the check read heavenbase.backend_defaults.pinecone.metric alone, so once it named
    another metric a near requesting that metric ran natively and ranked by the index's own metric.
    """

    class Description:
        host = "http://localhost:5080"
        metric = "dotproduct"

    class Client:
        def has_index(self, name):
            return True

        def describe_index(self, name):
            return Description()

    schema = _schema()
    pinecone = hb.backends.PineconeBackend("pinecone")
    pinecone.client = Client()
    pinecone._schemas[schema.entity_id] = schema

    class Context:
        backend = "pinecone"
        backend_identifier = "pinecone"
        backend_instance = pinecone
        entity_id = schema.entity_id
        resolver = hb.DEFAULT_CONTEXT.modules()
        config = _IndexMetricConfig("heavenbase.backend_defaults.pinecone.metric", "euclidean")  # changed after the index was created
        top_k = 2
        exact = False

    def _near(metric):
        Context.metric = metric
        return pinecone_near_handler(schema.fields["embedding"], [1.0, 0.0], Context())

    assert _near("ip").payload["metric"] == "dotproduct"
    for metric in ("l2", "cosine"):
        with pytest.raises(ValueError, match="the index ranks by 'dotproduct'"):
            _near(metric)
    assert pinecone.near_metric(schema.entity_id) == "dotproduct"
    pinecone._schemas.clear()  # no index exists for an entity not stored here: the configured metric applies
    assert pinecone.near_metric(schema.entity_id) == "cosine"


def test_pinecone_euclidean_scores_rank_higher_for_nearer_rows():
    """A euclidean index reports distances, lowest first; the near score is their negation, so higher is nearer."""

    class Index:
        def query(self, **kwargs):
            return {"matches": [{"id": "close", "score": 0.25, "metadata": {}}, {"id": "far", "score": 82.0, "metadata": {}}]}

    backend = hb.backends.PineconeBackend("pinecone")
    backend._index = lambda name: Index()
    fragment = QueryFragment("pinecone", "near", "embedding", {"vector": [1.0, 0.0], "top_k": 2, "metric": "euclidean"}, returns=["score"])

    rows = backend._query("index", fragment).rows()

    assert [(row["object_id"], row["score"]) for row in rows] == [("close", -0.25), ("far", -82.0)]


@pytest.mark.skipif(find_spec("pgvector") is None or find_spec("sqlalchemy") is None, reason="pgvector is not installed")
@pytest.mark.parametrize("metric", ["cosine", "l2", "ip"])
def test_pgvector_native_near_does_not_score_a_vector_less_row_as_a_match(monkeypatch, metric):
    """A row without a vector has no pgvector distance, so it is left out under every metric, never scored 1.0."""
    from contextlib import nullcontext

    from pgvector.sqlalchemy import Vector
    from sqlalchemy import Column, MetaData, String, Table

    table = Table("docs", MetaData(), Column("object_id", String, primary_key=True), Column("embedding", Vector(2)))

    class Response:
        def to_dicts(self):
            return [{"object_id": "close", "_distance": 0.25}, {"object_id": "empty", "_distance": None}]

    class Database:
        def orm_execute(self, stmt, readonly=False):
            return Response()

    backend = hb.backends.PgVectorBackend("pgvector", database="docs", host="localhost")
    monkeypatch.setattr(backend, "_read_storage", lambda entity_id: (Database(), table, {}))
    monkeypatch.setattr(backend, "_select_cols", lambda entity_id, table: [table.c.object_id])
    monkeypatch.setattr(backend, "_read_scope", nullcontext)
    fragment = QueryFragment(
        "pgvector", "near", "embedding", {"vector": [1.0, 0.0], "top_k": 2, "metric": metric}, returns=["score"], notes={"kind": "pgvector_search"}
    )

    rows = backend.execute(fragment.bind("docs")).rows()

    expected = 0.75 if metric == "cosine" else -0.25
    assert [(row["object_id"], row["score"]) for row in rows] == [("close", expected)]


@pytest.mark.skipif(find_spec("pgvector") is None or find_spec("sqlalchemy") is None, reason="pgvector is not installed")
@pytest.mark.parametrize("metric", ["cosine", "l2", "ip"])
def test_pgvector_native_near_leaves_a_vector_less_row_out_and_orders_by_the_bare_distance(monkeypatch, metric):
    """Native search on every other backend never returns a row without a vector, so pgvector leaves it out in SQL.

    Regression: Postgres ordered the NULL distance last and scored it 1.0, a perfect cosine match.
    The ORDER BY stays on the bare distance, which a pgvector ANN index can serve.
    """
    from contextlib import nullcontext

    from pgvector.sqlalchemy import Vector
    from sqlalchemy import Column, MetaData, String, Table
    from sqlalchemy.dialects import postgresql

    table = Table("docs", MetaData(), Column("object_id", String, primary_key=True), Column("embedding", Vector(2)))
    statements = []

    class Response:
        def to_dicts(self):
            return []

    class Database:
        def orm_execute(self, stmt, readonly=False):
            statements.append(str(stmt.compile(dialect=postgresql.dialect())))
            return Response()

    backend = hb.backends.PgVectorBackend("pgvector", database="docs", host="localhost")
    monkeypatch.setattr(backend, "_read_storage", lambda entity_id: (Database(), table, {}))
    monkeypatch.setattr(backend, "_select_cols", lambda entity_id, table: [table.c.object_id])
    monkeypatch.setattr(backend, "_read_scope", nullcontext)
    fragment = QueryFragment(
        "pgvector", "near", "embedding", {"vector": [1.0, 0.0], "top_k": 2, "metric": metric}, returns=["score"], notes={"kind": "pgvector_search"}
    )

    backend.execute(fragment.bind("docs"))

    where, order_by = statements[0].split("WHERE", 1)[1].split("ORDER BY", 1)
    assert "docs.embedding IS NOT NULL" in where
    assert order_by.split("LIMIT", 1)[0].strip() == "_distance"  # the labelled bare distance, no coalesce


def _vector_less_near_workspace(tmp_path, backend, name: str):
    ws = hb.HeavenBase(name, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})

    class MetricDoc(hb.Entity):
        identifier = f"{name}-doc"

        title = hb.field(hb.ShortText).store(to="vec")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    ws.register(MetricDoc)
    return ws, MetricDoc


def _near_ranking(ws, entity, metric: str, *, filtered: bool):
    query = ws.query(entity)
    if filtered:
        query = query.where({"title": {"starts_with": "x"}})  # admits every row, but LanceDB/Chroma cannot push it down
    query = query.near("embedding", [1.0, 0.0], top_k=3, metric=metric).select("object_id", "score")
    step = [step for step in query.explain()["steps"] if step.get("node") == "near"][0]
    return step, [(row["object_id"], round(row["score"], 6)) for row in query.execute().rows()]


@pytest.mark.skipif(find_spec("lancedb") is None, reason="lancedb runtime is not installed")
def test_lance_near_ranks_by_the_requested_metric_with_or_without_a_scanned_filter(tmp_path):
    """A filter that admits every row must not change a Lance near's neighbours or let a vector-less row in."""
    name = f"lance-metric-{uuid4().hex[:8]}"
    ws, doc = _vector_less_near_workspace(tmp_path, hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / name)), name)
    try:
        ws.upsert_many(
            doc,
            [
                {"object_id": "far", "title": "x-far", "embedding": [10.0, 1.0]},
                {"object_id": "close", "title": "x-close", "embedding": [1.0, 0.5]},
                {"object_id": "empty", "title": "x-empty", "embedding": None},
            ],
        )
        metrics = (
            ("l2", ["close", "far"]),
            ("euclidean", ["close", "far"]),
            ("ip", ["far", "close"]),
            ("inner_product", ["far", "close"]),
            ("dot", ["far", "close"]),
        )
        for metric, ids in metrics:
            native_step, native = _near_ranking(ws, doc, metric, filtered=False)
            scan_step, scanned = _near_ranking(ws, doc, metric, filtered=True)
            assert native_step["handler_mode"] == "native" and scan_step["near_filter_mode"] == "combined_scan"
            assert [object_id for object_id, _ in native] == ids == [object_id for object_id, _ in scanned], metric
        # The inner product is the same similarity natively and through the scan.
        assert _near_ranking(ws, doc, "ip", filtered=False)[1] == [("far", 10.0), ("close", 1.0)] == _near_ranking(ws, doc, "ip", filtered=True)[1]
        # 'dot', which LanceDB names its inner product, is the same metric on the exact route too.
        for metric in ("dot", "ip"):
            exact = ws.query(doc).near("embedding", [1.0, 0.0], top_k=3, metric=metric, exact=True).select("object_id", "score").execute().rows()
            assert [(row["object_id"], round(row["score"], 6)) for row in exact] == [("far", 10.0), ("close", 1.0)], metric
        with pytest.raises(ValueError, match="unsupported metric"):
            _near_ranking(ws, doc, "not-a-metric", filtered=False)
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("lancedb") is None, reason="lancedb runtime is not installed")
def test_lance_cosine_near_keeps_its_rows_when_a_filter_sends_it_through_the_scan(tmp_path):
    """A filter that admits every row must not let a vector-less row displace a real cosine neighbour.

    Regression: LanceDB native search skips a row without a vector, but the scan scorer gave it the
    neutral cosine 0.0, so a filter LanceDB cannot push down put it above a row with cosine -1.
    """
    name = f"lance-cosine-{uuid4().hex[:8]}"
    ws, doc = _vector_less_near_workspace(tmp_path, hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / name)), name)
    try:
        ws.upsert_many(
            doc,
            [
                {"object_id": "a", "title": "x-a", "embedding": [10.0, 1.0]},
                {"object_id": "neg", "title": "x-neg", "embedding": [-1.0, 0.0]},
                {"object_id": "empty", "title": "x-empty", "embedding": None},
            ],
        )
        expected = [("a", round(10.0 / math.sqrt(101.0), 6)), ("neg", -1.0)]
        for top_k in (2, 3):
            for filtered in (False, True):
                for exact in (False, True):
                    query = ws.query(doc)
                    if filtered:
                        query = query.where({"title": {"starts_with": "x"}})
                    query = query.near("embedding", [1.0, 0.0], top_k=top_k, metric="cosine", exact=exact).select("object_id", "score")
                    ranked = [(row["object_id"], round(row["score"], 6)) for row in query.execute().rows()]
                    assert ranked == expected, (top_k, filtered, exact)
    finally:
        ws.release()


@pytest.mark.parametrize("provider", ["chroma", "lance", "milvus", "neo4j", "pgvector", "pinecone", "qdrant", "redisearch", "surrealdb", "weaviate"])
@pytest.mark.parametrize("metric", ["cosine", "l2", "ip"])
def test_native_vector_near_scan_scorers_leave_a_vector_less_row_out(provider, metric):
    """Native vector search never returns a row without a vector, so no native backend's scan scorer ranks one.

    Regression: under cosine every scan scorer gave such a row 0.0, so a filter that sent a native
    near through the scan let it displace real neighbours.
    """
    from heavenbase.backends.neo4j.handler.runtime import neo4j_near_handler
    from heavenbase.backends.surreal.handler.runtime import surreal_near_handler
    from heavenbase.execution import HandlerContext
    from heavenbase.strategies import VectorIndex

    handlers = {
        "chroma": chroma_near_handler,
        "lance": lance_near_handler,
        "milvus": milvus_near_handler,
        "neo4j": neo4j_near_handler,
        "pgvector": pgvector_near_handler,
        "pinecone": pinecone_near_handler,
        "qdrant": qdrant_near_handler,
        "redisearch": redisearch_near_handler,
        "surrealdb": surreal_near_handler,
        "weaviate": weaviate_near_handler,
    }
    # Chroma, Pinecone and RediSearch rank natively by their index metric: name the one the near requests.
    chroma_like, pinecone_like = {"cosine": ("cosine", "cosine"), "l2": ("l2", "euclidean"), "ip": ("ip", "dotproduct")}[metric]
    key, value = {
        "chroma": ("heavenbase.backend_defaults.chroma.metric", chroma_like),
        "pinecone": ("heavenbase.backend_defaults.pinecone.metric", pinecone_like),
        "redisearch": ("heavenbase.backend_defaults.redisearch.metric", chroma_like),
    }.get(provider, (None, None))
    ctx = HandlerContext(
        "vec",
        provider,
        VectorIndex.identifier,
        top_k=2,
        metric=metric,
        resolver=hb.DEFAULT_CONTEXT.modules(),
        config=_IndexMetricConfig(key, value),
    )
    scorer = handlers[provider](FieldSchema("embedding", hb.Vector[2]), [1.0, 0.0], ctx).notes["row_payload"]

    assert scorer({"embedding": None}) is None
    assert scorer({"embedding": [1.0, 0.0]}) is not None


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_near_ranks_by_the_requested_metric_with_or_without_a_scanned_filter(tmp_path):
    """A Chroma near whose metric is not the collection space ranks by that metric, filtered or not."""
    name = f"chroma-metric-{uuid4().hex[:8]}"
    ws, doc = _vector_less_near_workspace(tmp_path, hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / name), ws_id=name), name)
    try:
        ws.upsert_many(
            doc, [{"object_id": "far", "title": "x-far", "embedding": [10.0, 1.0]}, {"object_id": "close", "title": "x-close", "embedding": [1.0, 0.5]}]
        )
        cosine_step, cosine = _near_ranking(ws, doc, "cosine", filtered=False)
        assert cosine_step["handler_mode"] == "native" and [object_id for object_id, _ in cosine] == ["far", "close"]
        for filtered in (False, True):
            step, ranked = _near_ranking(ws, doc, "l2", filtered=filtered)
            assert step["handler_mode"] == "py_scan"
            assert ranked == [("close", -0.5), ("far", round(-math.sqrt(82.0), 6))], filtered
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_near_routes_by_the_space_the_collection_was_created_with(tmp_path):
    """A config change after a collection exists does not change how the collection ranks, so it must not change the route.

    Regression: the route was chosen from heavenbase.backend_defaults.chroma.metric alone, so after it
    named 'l2' a near(metric='l2') on a collection created under cosine ran natively and ranked by cosine.
    """
    name = f"chroma-space-{uuid4().hex[:8]}"
    ws, doc = _vector_less_near_workspace(tmp_path, hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / name), ws_id=name), name)
    config = ws.context.config
    metric_key = "heavenbase.backend_defaults.chroma.metric"
    scope = f"{config.base_scope}.{ws.id}"
    default_metric = config.get(metric_key)
    try:
        ws.upsert_many(
            doc, [{"object_id": "far", "title": "x-far", "embedding": [10.0, 1.0]}, {"object_id": "close", "title": "x-close", "embedding": [1.0, 0.5]}]
        )
        config.set(metric_key, "l2", scope=scope)
        assert config.get(metric_key) == default_metric
        with config.scoped(scope):
            assert config.get(metric_key) == "l2"

        l2_step, l2 = _near_ranking(ws, doc, "l2", filtered=False)
        assert l2 == [("close", -0.5), ("far", round(-math.sqrt(82.0), 6))]
        assert l2_step["handler_mode"] == "py_scan" and "uses the 'cosine' space" in l2_step["fallback_reason"]
        cosine_step, cosine = _near_ranking(ws, doc, "cosine", filtered=False)
        assert cosine_step["handler_mode"] == "native" and [object_id for object_id, _ in cosine] == ["far", "close"]
        assert ws.backends.get("vec").near_space(doc.schema().entity_id) == "cosine"
    finally:
        config.remove(scope=scope)
        ws.release()


def _unique_schema(provider: str) -> EntitySchema:
    schema = _schema()
    schema.entity_id = f"{provider}-live-{uuid4().hex[:12]}"
    return schema


def _service_failure_or_skip(provider: str, env_name: str, exc: Exception) -> None:
    detail = f"{type(exc).__name__}: {exc}"
    if env_name in os.environ:
        pytest.fail(f"configured {provider} service is unavailable: {detail}", pytrace=False)
    pytest.skip(f"default {provider} service is unavailable: {detail}")


def _probe_http_endpoint(provider: str, env_name: str, url: str) -> None:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.75):
            return
    except OSError as exc:
        _service_failure_or_skip(provider, env_name, exc)


@_requires_milvus_lite
def test_real_milvus_lite_reopens_and_executes_near(tmp_path):
    """Exercise local persistence and FLAT search through the real Milvus Lite client."""
    uri = hb.utils.path_to_file_uri(tmp_path / "real-milvus-lite.db")
    workspace = f"milvus-lite-live-{uuid4().hex[:12]}"
    schema = _unique_schema("milvus-lite")
    bindings = _bindings(schema, "vec")
    first = MilvusLiteBackend("vec", uri, ws_id=workspace)
    reopened = None
    try:
        first.ensure(schema, bindings)
        first.upsert(
            [
                RowOp(schema.entity_id, "near", {"title": "near", "embedding": [1.0, 0.0]}),
                RowOp(schema.entity_id, "far", {"title": "far", "embedding": [0.0, 1.0]}),
            ]
        )
        assert first.count(schema.entity_id) == 2
        first._close_client()

        reopened = MilvusLiteBackend("vec", uri, ws_id=workspace)
        reopened.ensure(schema, bindings)
        assert reopened.get_many(schema.entity_id, ["near", "missing"])[0]["title"] == "near"
        assert reopened.get_many(schema.entity_id, ["near", "missing"])[1] is None
        result = reopened.execute(
            QueryFragment(
                reopened.name,
                "near",
                "embedding",
                {"vector": [1.0, 0.0], "top_k": 1, "metric": "cosine"},
                notes={"kind": "milvus_search", "mode": "native", "entity_id": schema.entity_id},
            )
        )
        assert result.ids == ["near"]
    finally:
        owner = reopened or first
        try:
            owner.drop()
        finally:
            owner._close_client()


@pytest.mark.external
def test_real_qdrant_external_lifecycle_and_near():
    """Exercise exact lifecycle and named-vector search against Qdrant."""
    if find_spec("qdrant_client") is None:
        pytest.skip("qdrant-client is not installed")
    env_name = "HVNB_QDRANT_URL"
    url = os.environ.get(env_name, "http://localhost:6333")
    _probe_http_endpoint("Qdrant", env_name, url)

    workspace = f"qdrant-live-{uuid4().hex[:12]}"
    schema = _unique_schema("qdrant")
    backend = QdrantBackend(
        "vec",
        url=url,
        api_key=os.environ.get("HVNB_QDRANT_API_KEY"),
        ws_id=workspace,
        timeout=3.0,
    )
    ready = False
    try:
        try:
            backend._need_client().get_collections()
            ready = True
        except Exception as exc:
            _service_failure_or_skip("Qdrant", env_name, exc)
        backend.ensure(schema, _bindings(schema, backend.name))
        backend.upsert(
            [
                RowOp(schema.entity_id, "near", {"title": "near", "embedding": [1.0, 0.0]}),
                RowOp(schema.entity_id, "far", {"title": "far", "embedding": [0.0, 1.0]}),
                RowOp(schema.entity_id, "missing", op="delete"),
            ]
        )
        assert backend.count(schema.entity_id) == 2
        assert backend.exists_many(schema.entity_id, ["near", "missing"]) == [True, False]
        result = backend.execute(
            QueryFragment(
                backend.name,
                "near",
                "embedding",
                {"vector": [1.0, 0.0], "top_k": 1, "metric": "cosine"},
                notes={"kind": "qdrant_search", "mode": "native", "entity_id": schema.entity_id},
            )
        )
        assert result.ids == ["near"]
        backend.upsert([RowOp(schema.entity_id, "near", op="delete")])
        assert backend.get_many(schema.entity_id, ["near"]) == [None]
    finally:
        if ready:
            backend.drop()
        else:
            backend._close_client()


@pytest.mark.external
def test_real_remote_milvus_backend_names_isolate_database_drop():
    """Two names in one workspace own independent Milvus databases and rows."""
    if find_spec("pymilvus") is None:
        pytest.skip("pymilvus is not installed")
    env_name = "HVNB_MILVUS_URI"
    uri = os.environ.get(env_name, "http://localhost:19530")
    _probe_http_endpoint("Milvus", env_name, uri)

    workspace = f"milvus-namespace-live-{uuid4().hex[:12]}"
    schema = _unique_schema("milvus-namespace")
    token = os.environ.get("HVNB_MILVUS_TOKEN")
    first = MilvusBackend("vec", uri=uri, token=token, ws_id=workspace)
    sibling = MilvusBackend("vec_archive", uri=uri, token=token, ws_id=workspace)
    first_dropped = False
    sibling_ready = False
    try:
        try:
            first._need_client()
            sibling._need_client()
            sibling_ready = True
        except Exception as exc:
            _service_failure_or_skip("Milvus", env_name, exc)
        assert first.database != sibling.database
        first.ensure(schema, _bindings(schema, first.name))
        sibling.ensure(schema, _bindings(schema, sibling.name))
        first.upsert([RowOp(schema.entity_id, "same-id", {"title": "first", "embedding": [1.0, 0.0]})])
        sibling.upsert([RowOp(schema.entity_id, "same-id", {"title": "sibling", "embedding": [0.0, 1.0]})])
        assert first.get_many(schema.entity_id, ["same-id"])[0]["title"] == "first"
        assert sibling.get_many(schema.entity_id, ["same-id"])[0]["title"] == "sibling"

        first.drop()
        first_dropped = True
        assert sibling.get_many(schema.entity_id, ["same-id"])[0]["title"] == "sibling"
        assert sibling.count(schema.entity_id) == 1
    finally:
        if not first_dropped and first.client is not None:
            first.drop()
        if sibling_ready and sibling.client is not None:
            sibling.drop()


@pytest.mark.external
def test_real_weaviate_external_lifecycle_and_near():
    """Exercise exact lifecycle and named-vector search against Weaviate."""
    if find_spec("weaviate") is None:
        pytest.skip("weaviate-client is not installed")
    env_name = "HVNB_WEAVIATE_URL"
    url = os.environ.get(env_name, "http://localhost:8082")
    grpc_port = int(os.environ.get("HVNB_WEAVIATE_GRPC_PORT", "50051"))
    _probe_http_endpoint("Weaviate", env_name, url)

    workspace = f"weaviate-live-{uuid4().hex[:12]}"
    schema = _unique_schema("weaviate")
    backend = WeaviateBackend(
        "vec",
        url=url,
        grpc_port=grpc_port,
        api_key=os.environ.get("HVNB_WEAVIATE_API_KEY"),
        ws_id=workspace,
    )
    ready = False
    try:
        try:
            client = backend._need_client()
            if callable(getattr(client, "is_ready", None)) and not client.is_ready():
                raise RuntimeError("client readiness check returned false")
            ready = True
        except Exception as exc:
            _service_failure_or_skip("Weaviate", env_name, exc)
        backend.ensure(schema, _bindings(schema, backend.name))
        backend.upsert(
            [
                RowOp(schema.entity_id, "near", {"title": "near", "embedding": [1.0, 0.0]}),
                RowOp(schema.entity_id, "far", {"title": "far", "embedding": [0.0, 1.0]}),
                RowOp(schema.entity_id, "missing", op="delete"),
            ]
        )
        assert backend.count(schema.entity_id) == 2
        assert backend.exists_many(schema.entity_id, ["near", "missing"]) == [True, False]
        result = backend.execute(
            QueryFragment(
                backend.name,
                "near",
                "embedding",
                {"vector": [1.0, 0.0], "top_k": 1, "metric": "cosine"},
                notes={"kind": "weaviate_search", "mode": "native", "entity_id": schema.entity_id},
            )
        )
        assert result.ids == ["near"]
        backend.upsert([RowOp(schema.entity_id, "near", op="delete")])
        assert backend.get_many(schema.entity_id, ["near"]) == [None]
    finally:
        if ready:
            backend.drop()
        else:
            backend._close_client()


def test_milvus_lite_enumerates_by_sorted_key_not_its_unordered_iterator(monkeypatch, tmp_path):
    """Milvus Lite 3 answers queries in storage order, so a primary-key cursor would skip rows."""
    client_cls = _install_fake_milvus(monkeypatch)
    queries = []

    def query(self, *, collection_name, output_fields, filter="", **kwargs):
        if output_fields == ["count(*)"]:
            return [{"count(*)": len(self.store[collection_name])}]
        queries.append(filter)
        # storage (insertion) order, as Lite returns it
        return _fake_milvus_filtered(list(self.store[collection_name].values()), filter, output_fields)

    def no_iterator(self, **kwargs):
        pytest.fail("Milvus Lite must not page through its query iterator's primary-key cursor")

    monkeypatch.setattr(client_cls, "query", query)
    monkeypatch.setattr(client_cls, "query_iterator", no_iterator)
    backend = MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lite-order.db"), ws_id="lite-order")
    schema = _schema()
    try:
        backend.ensure(schema, _bindings(schema, "vec"))
        ids = ["p00", "p01", "p02", "p03", "Zeta", "alpha", 'q"uote']
        backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, 0.0]}) for object_id in ids])

        assert [row["object_id"] for row in backend.iter_scan(schema.entity_id, batch_size=2)] == sorted(ids)
        pages, after = [], None
        while page := backend.scan_page(schema.entity_id, after=after, batch_size=3, columns=["title"]):
            pages.append(page)
            after = page[-1]["object_id"]
        assert [[row["object_id"] for row in page] for page in pages] == [["Zeta", "alpha", "p00"], ["p01", "p02", "p03"], ['q"uote']]
        assert pages[0][0] == {"object_id": "Zeta", "title": "Zeta"}
        assert sorted(row["object_id"] for row in backend.scan(schema.entity_id)) == sorted(ids)
        # iter_scan, the whole scan_page loop and scan each list the keys once; rows are then read by escaped key filters
        assert [query for query in queries if not query.startswith("object_id in [")] == ['object_id != ""'] * 3
        assert 'object_id in ["p01", "p02", "p03"]' in queries and 'object_id in ["q\\"uote"]' in queries
    finally:
        backend._close_client()


class _ChromaGetLog:
    """Provider requests seen by ``_ChromaGetSpy``."""

    def __init__(self):
        self.work: list[int] = []
        self.row_reads: list[int] = []
        self.named: list[int] = []
        self.wheres: list = []
        self.writes: list[tuple[str, int]] = []

    def clear(self) -> None:
        for requests in (self.work, self.row_reads, self.named, self.wheres, self.writes):
            requests.clear()


class _ChromaGetSpy:
    """Delegate to a Chroma collection while recording what each ``get`` and ``query`` asks for."""

    def __init__(self, collection, log: _ChromaGetLog):
        self._collection = collection
        self._log = log

    def get(self, *args, **kwargs):
        answer = self._collection.get(*args, **kwargs)
        self._log.work.append(int(kwargs.get("offset") or 0) + len(answer.get("ids") or []))
        self._log.named.append(len(kwargs.get("ids") or []))
        self._log.wheres.append(kwargs.get("where"))
        if kwargs.get("include") != []:
            self._log.row_reads.append(len(answer.get("ids") or []))
        return answer

    def query(self, *args, **kwargs):
        answer = self._collection.query(*args, **kwargs)
        self._log.wheres.append(kwargs.get("where"))
        if {"metadatas", "embeddings"} & set(kwargs.get("include") or []):
            self._log.row_reads.append(len(answer.get("ids", [[]])[0]))
        return answer

    def upsert(self, *args, **kwargs):
        self._log.writes.append(("upsert", len(kwargs["ids"])))
        return self._collection.upsert(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._log.writes.append(("delete", len(kwargs["ids"])))
        return self._collection.delete(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._collection, name)


def _chroma_spied(monkeypatch, tmp_path, name: str):
    backend = hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / name), ws_id=name)
    log = _ChromaGetLog()
    collection = backend._collection
    monkeypatch.setattr(backend, "_collection", lambda entity_id, **options: _ChromaGetSpy(collection(entity_id, **options), log))
    schema = _schema()
    backend.ensure(schema, _bindings(schema, "vec"))
    return backend, schema, log


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_iter_scan_is_one_linear_pass_in_chroma_order(monkeypatch, tmp_path):
    """Chroma ``iter_scan`` reads each row a bounded number of times, so one pass is linear, not an offset walk."""
    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-linear")
    ids = [f"r{(position * 37) % 300:03d}" for position in range(300)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    listed = backend._collection(schema.entity_id).get(include=[])["ids"]
    log.clear()

    rows = list(backend.iter_scan(schema.entity_id, batch_size=10, columns=["title"]))

    assert [row["object_id"] for row in rows] == listed and sorted(listed) == sorted(ids)
    assert rows[0] == {"object_id": listed[0], "title": listed[0]}
    # An offset walk skips 0 + 10 + ... + 290 rows before it reads them: about 15 times the collection.
    assert sum(log.work) <= 2 * len(ids)


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_iter_scan_keeps_live_rows_when_an_earlier_row_is_deleted(monkeypatch, tmp_path):
    """Deleting a row already yielded must not shift a Chroma scan past a row it has not reached."""
    backend, schema, _ = _chroma_spied(monkeypatch, tmp_path, "chroma-delete")
    ids = [f"d{position}" for position in range(6)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, 0.0]}) for object_id in ids])

    rows = iter(backend.iter_scan(schema.entity_id, batch_size=2))
    seen = [next(rows)["object_id"] for _ in range(3)]
    backend.upsert([RowOp(schema.entity_id, seen[0], op="delete")])
    seen.extend(row["object_id"] for row in rows)

    assert sorted(seen) == ids


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_scans_read_rows_through_bounded_id_lists(monkeypatch, tmp_path):
    """Chroma binds a SQL variable per id, so no scanning read may ask for every row of a collection at once."""
    from heavenbase.backends.vector import chroma

    monkeypatch.setattr(chroma, "CHROMA_MAX_GET_IDS", 4, raising=False)
    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-bounded")
    ids = [f"b{position:02d}" for position in range(10)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, 0.0]}) for object_id in ids])
    listed = backend._collection(schema.entity_id).get(include=[])["ids"]
    log.clear()

    assert [row["object_id"] for row in backend.scan(schema.entity_id)] == listed
    assert [row["object_id"] for row in backend.iter_scan(schema.entity_id, batch_size=50)] == listed
    assert log.row_reads and max(log.row_reads) <= 4


def _chroma_where(backend, schema, where: dict, row_payload) -> QueryFragment:
    return QueryFragment(
        backend.name, "eq", "title", where, notes={"kind": "chroma_where", "mode": "native", "entity_id": schema.entity_id, "row_payload": row_payload}
    )


def _chroma_near(backend, schema, top_k: int, where: QueryFragment | None = None) -> QueryFragment:
    from heavenbase.types import cosine_score

    def score(row: dict) -> float:
        return cosine_score(row["embedding"], [1.0, 0.0]) if row.get("embedding") is not None else 0.0

    notes = {"kind": "chroma_search", "mode": "native", "entity_id": schema.entity_id, "top_k": top_k, "row_payload": score}
    if where is not None:
        notes["filter"] = where
    return QueryFragment(backend.name, "near", "embedding", {"vector": [1.0, 0.0], "top_k": top_k, "metric": "cosine"}, returns=["score"], notes=notes)


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_point_lookups_read_bounded_id_lists(monkeypatch, tmp_path):
    """Chroma ``get_many`` and ``exists_many`` name at most ``CHROMA_MAX_GET_IDS`` ids per provider request."""
    from heavenbase.backends.vector import chroma

    monkeypatch.setattr(chroma, "CHROMA_MAX_GET_IDS", 4)
    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-lookups")
    ids = [f"p{position:02d}" for position in range(10)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    asked = [*reversed(ids), "missing", ids[3], ids[3]]
    log.clear()

    rows = backend.get_many(schema.entity_id, asked, columns=["title"])
    flags = backend.exists_many(schema.entity_id, asked)

    assert [row and row["title"] for row in rows] == [*reversed(ids), None, ids[3], ids[3]]
    assert backend.get_many(schema.entity_id, [ids[7]])[0]["embedding"] == pytest.approx([1.0, 7.0])
    assert flags == [True] * 10 + [False, True, True]
    assert log.named and max(log.named) <= 4


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_native_filter_reads_matches_through_bounded_id_lists(monkeypatch, tmp_path):
    """A native Chroma filter lists its matches once and reads their rows at most ``CHROMA_MAX_GET_IDS`` at a time."""
    from heavenbase.backends.vector import chroma

    monkeypatch.setattr(chroma, "CHROMA_MAX_GET_IDS", 4)
    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-filter")
    ids = [f"f{position:02d}" for position in range(10)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    listed = backend._collection(schema.entity_id).get(include=[])["ids"]
    log.clear()

    every = backend.execute(_chroma_where(backend, schema, {"title": {"$ne": "zzz"}}, lambda row: row.get("title") != "zzz"))
    some = backend.execute(_chroma_where(backend, schema, {"title": {"$in": ids[2:9]}}, lambda row: row.get("title") in ids[2:9]))

    assert every.ids == listed
    assert some.ids == [object_id for object_id in listed if object_id in ids[2:9]]
    assert every.rows()[0]["embedding"] == pytest.approx([1.0, float(ids.index(listed[0]))])
    assert log.row_reads and max(log.row_reads) <= 4


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_long_in_lists_stay_native_within_sqlites_variable_limit(monkeypatch, tmp_path):
    """Chroma binds one SQL variable per string ``in`` value and two per number; a native filter never binds more than the cap."""
    from heavenbase.backends.vector import chroma
    from heavenbase.backends.vector.handler import chroma as chroma_handlers

    field = _schema().get("title")
    ctx = hb.ext.HandlerContext("vec", "chroma", hb.InlineColumn.identifier)
    assert chroma_handlers.chroma_filter_handler("in")(field, ["a"] * 40_000, ctx).notes["mode"] == "native"

    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-in-cap")
    ids = [f"c{position:02d}" for position in range(6)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    listed = backend._collection(schema.entity_id).get(include=[])["ids"]
    at_cap = _strings_to_cap(chroma, lambda pad: {"title": {"$in": [*ids[:2], *pad]}})
    numbers = list(range((chroma.CHROMA_MAX_WHERE_VARIABLES - chroma.CHROMA_CLAUSE_VARIABLES) // 2))
    # A filter that fits one statement goes with the by-id read of its matches too, which names fewer
    # ids than the variables it leaves; a larger one is checked on each row read by its predicate.
    cases = [
        (at_cap, 2, [object_id for object_id in listed if object_id in ids[:2]]),
        ({"title": {"$in": [*at_cap["title"]["$in"], ids[2]]}}, 2, [object_id for object_id in listed if object_id in ids[:3]]),
        ({"title": {"$in": numbers}}, 1, []),
        ({"title": {"$in": [*numbers, -1]}}, 2, []),
        (_strings_to_cap(chroma, lambda pad: {"$and": [{"title": {"$in": numbers[: len(numbers) // 2]}}, {"title": {"$in": pad}}]}), 1, []),
    ]
    for where, statements, expected in cases:
        log.clear()
        assert backend.execute(_chroma_where(backend, schema, where, lambda row: row.get("title") in expected)).ids == expected
        sent = [chroma._where_variables(request) for request in log.wheres if request is not None]
        assert len(sent) == statements and max(sent) <= chroma.CHROMA_MAX_WHERE_VARIABLES


def _strings_to_cap(chroma, build):
    """Return ``build(pad)`` with string ``pad`` values that bring its ``where`` to exactly the variable cap."""
    return build([f"w{position}" for position in range(chroma.CHROMA_MAX_WHERE_VARIABLES - chroma._where_variables(build([])))])


_CAP_IDS = [f"m{position:04d}" for position in range(800)]
# Each shape binds more SQL variables beyond its values than a single string ``in`` does.
_CAP_SHAPES = {
    "one-in": lambda pad: {"title": {"$in": [*_CAP_IDS, *pad]}},
    "and-of-ins": lambda pad: {"$and": [{"title": {"$in": [*_CAP_IDS, *pad]}}, {"title": {"$in": _CAP_IDS}}]},
    "in-and-comparison": lambda pad: {"$and": [{"title": {"$in": [*_CAP_IDS, *pad]}}, {"title": {"$ne": "zzz"}}]},
    "or-with-numbers": lambda pad: {
        "$or": [{"title": {"$in": [*_CAP_IDS[:400], *pad]}}, {"title": {"$in": _CAP_IDS[400:]}}, {"title": {"$in": list(range(2000))}}]
    },
    "or-of-ten-ins": lambda pad: {
        "$or": [{"title": {"$in": [*_CAP_IDS[:80], *pad]}}, *({"title": {"$in": _CAP_IDS[80 * part : 80 * part + 80]}} for part in range(1, 10))]
    },
}


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
@pytest.mark.parametrize("shape", list(_CAP_SHAPES))
def test_chroma_rereads_the_matches_of_a_filter_at_the_cap_in_requests_that_fit_one_statement(monkeypatch, tmp_path, shape):
    """A by-id read carrying a ``where`` at the variable cap names at most the ids left of SQLite's limit, whatever its shape."""
    from heavenbase.backends.vector import chroma

    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, f"chroma-cap-reread-{shape}")
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, 0.0]}) for object_id in _CAP_IDS])
    where = _strings_to_cap(chroma, _CAP_SHAPES[shape])
    assert chroma._where_variables(where) == chroma.CHROMA_MAX_WHERE_VARIABLES
    log.clear()

    rows = backend.execute(_chroma_where(backend, schema, where, lambda row: row.get("title") in _CAP_IDS)).rows()

    first = chroma.CHROMA_MAX_STATEMENT_VARIABLES - chroma.CHROMA_MAX_WHERE_VARIABLES
    assert sorted(row["object_id"] for row in rows) == _CAP_IDS
    assert log.row_reads == [first, len(_CAP_IDS) - first]


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_filters_above_the_variable_cap_split_into_native_statements(monkeypatch, tmp_path):
    """A filter too large for one statement is read natively per operand and per run of ``in`` values, in Chroma order."""
    from heavenbase.backends.vector import chroma

    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-split")
    ids = [f"s{(position * 5) % 8:02d}" for position in range(8)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    listed = backend._collection(schema.entity_id).get(include=[])["ids"]
    first = _chroma_where(backend, schema, {"title": {"$in": ids[:5]}}, lambda row: row.get("title") in ids[:5])
    second = _chroma_where(backend, schema, {"title": {"$in": ids[3:7]}}, lambda row: row.get("title") in ids[3:7])
    both = QueryFragment(backend.name, "and", "*", [first, second], notes={"entity_id": schema.entity_id})
    either = QueryFragment(backend.name, "or", "*", [first, second], notes={"entity_id": schema.entity_id})
    native = {top_k: backend.execute(_chroma_near(backend, schema, top_k, either)).rows() for top_k in (3, 8)}
    cap = chroma.CHROMA_CLAUSE_VARIABLES + 3
    monkeypatch.setattr(chroma, "CHROMA_MAX_WHERE_VARIABLES", cap)
    log.clear()

    assert backend.execute(both).ids == [object_id for object_id in listed if object_id in ids[3:5]]
    assert backend.execute(either).ids == [object_id for object_id in listed if object_id in ids[:7]]
    for top_k, expected in native.items():
        split = backend.execute(_chroma_near(backend, schema, top_k, either)).rows()
        assert [row["object_id"] for row in split] == [row["object_id"] for row in expected]
        assert [row["score"] for row in split] == pytest.approx([row["score"] for row in expected], abs=1e-6)
        assert split[0]["title"] == expected[0]["title"] and split[0]["embedding"] == pytest.approx(expected[0]["embedding"])
    sent = [request for request in log.wheres if request is not None]
    assert sent and max(chroma._where_variables(request) for request in sent) <= cap


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_chained_or_filters_stay_native_past_chromas_nesting_limit(monkeypatch, tmp_path):
    """``a | b | c ...`` nests one ``or`` per operand; Chroma rejects 64 levels, so nested ``or``s and ``and``s are merged."""
    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-chained")
    ids = [f"o{position:03d}" for position in range(100)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    listed = backend._collection(schema.entity_id).get(include=[])["ids"]

    def leaf(object_id: str) -> QueryFragment:
        return _chroma_where(backend, schema, {"title": {"$eq": object_id}}, lambda row, object_id=object_id: row.get("title") == object_id)

    def chain(op: str, leaves: list[QueryFragment]) -> QueryFragment:
        fragment = leaves[0]
        for other in leaves[1:]:
            fragment = QueryFragment(backend.name, op, "*", [fragment, other], notes={"entity_id": schema.entity_id})
        return fragment

    either = chain("or", [leaf(object_id) for object_id in ids[:90]])
    both = chain(
        "and",
        [
            _chroma_where(backend, schema, {"title": {"$ne": object_id}}, lambda row, object_id=object_id: row.get("title") != object_id)
            for object_id in ids[:90]
        ],
    )
    log.clear()

    assert backend.execute(either).ids == [object_id for object_id in listed if object_id in ids[:90]]
    assert backend.execute(both).ids == [object_id for object_id in listed if object_id in ids[90:]]
    assert [row["object_id"] for row in backend.execute(_chroma_near(backend, schema, 5, either)).rows()] == ids[:5]
    sent = [request for request in log.wheres if request is not None]
    assert sent and all(_where_shape(request) == (90, 2) for request in sent)


def _where_shape(where) -> tuple[int, int]:
    """Return the field clauses of a Chroma ``where`` and its ``$and``/``$or`` depth, checking every list is short."""
    from heavenbase.backends.vector import chroma

    ((key, value),) = where.items()
    if key not in ("$and", "$or"):
        return 1, 0
    assert 2 <= len(value) <= chroma.CHROMA_MAX_WHERE_OPERANDS
    shapes = [_where_shape(child) for child in value]
    return sum(clauses for clauses, _ in shapes), 1 + max(depth for _, depth in shapes)


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
@pytest.mark.parametrize("op,groups,size", [("or", 1, 1200), ("or", 2, 500), ("or", 10, 120), ("and", 2, 600)])
def test_chroma_wide_and_or_filters_stay_native_past_sqlites_expression_depth(monkeypatch, tmp_path, op, groups, size):
    """An ``or`` of ``or``s (or ``and`` of ``and``s) of about 1,000 clauses or more runs natively.

    SQLite rejects a flat list of about 990 operands, so the merged list is regrouped into short lists.
    """

    class WideDoc(hb.Entity):
        identifier = "wide-doc"

        title = hb.field(hb.ShortText).required().store(to="vec")
        embedding = hb.field(hb.Vector[2]).required().store(to="vec", strategy=hb.VectorIndex)

    backend = hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / "wide"))
    log = _ChromaGetLog()
    collection = backend._collection
    monkeypatch.setattr(backend, "_collection", lambda entity_id, **options: _ChromaGetSpy(collection(entity_id, **options), log))
    ws = hb.HeavenBase("wide-ws", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(WideDoc)
        ws.upsert_many(WideDoc, [{"object_id": f"k{index:02}", "title": f"t{index}", "embedding": [1.0, index / 10]} for index in range(40)])
        listed = backend._collection(WideDoc.identifier).get(include=[])["ids"]
        # The clauses name titles t20 onwards: the ``or`` matches k20-k39 and the ``and`` k00-k19.
        if op == "or":
            clauses = [[WideDoc.title == f"t{20 + group * size + offset}" for offset in range(size)] for group in range(groups)]
            expected = [object_id for object_id in listed if int(object_id[1:]) >= 20]
            combine = hb.filters.Or
        else:
            clauses = [[WideDoc.title != f"t{20 + group * size + offset}" for offset in range(size)] for group in range(groups)]
            expected = [object_id for object_id in listed if int(object_id[1:]) < 20]
            combine = hb.filters.And
        expr = combine(clauses[0]) if groups == 1 else combine([combine(group) for group in clauses])
        log.clear()

        assert ws.query(WideDoc).where(expr).execute().ids == expected
        sent = [request for request in log.wheres if request is not None]
        # 1,000 clauses fit two levels of at most 32 operands, and 1,200 three.
        assert sent and all(_where_shape(request) == (groups * size, 2 if groups * size <= 1024 else 3) for request in sent)
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
@pytest.mark.parametrize("space", ["cosine", "ip", "l2"])
def test_chroma_scores_match_a_native_query_in_every_distance_space(tmp_path, space):
    """Ranking a split filter's matches reports the ``1 - distance`` a native Chroma query reports for the same space."""
    import chromadb

    from heavenbase.backends.vector import chroma

    collection = chromadb.PersistentClient(path=str(tmp_path / space)).get_or_create_collection("scores", metadata={"hnsw:space": space})
    vectors = [[1.0, 0.0, 0.5], [0.2, 0.9, -0.3], [-0.4, 0.1, 0.8], [0.3, -0.7, 0.2]]
    collection.add(ids=[f"v{position}" for position in range(len(vectors))], embeddings=vectors)
    query = [0.6, 0.2, -0.1]
    answer = collection.query(query_embeddings=[query], n_results=len(vectors), include=["distances"])
    by_id = dict(zip(answer["ids"][0], answer["distances"][0]))

    assert collection.metadata["hnsw:space"] == space
    for position, vector in enumerate(vectors):
        assert chroma._chroma_score(space, vector, query) == pytest.approx(1.0 - by_id[f"v{position}"], abs=1e-5)


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_near_with_a_large_top_k_reads_rows_through_bounded_id_lists(monkeypatch, tmp_path):
    """Chroma reads every hit's metadata in one statement, so a ``top_k`` above the id cap reads hit rows by id."""
    from heavenbase.backends.vector import chroma

    monkeypatch.setattr(chroma, "CHROMA_MAX_GET_IDS", 4)
    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-topk")
    ids = [f"n{position:02d}" for position in range(10)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    log.clear()

    small = backend.execute(_chroma_near(backend, schema, 3))
    large = backend.execute(_chroma_near(backend, schema, 10))

    assert small.ids == ids[:3]
    assert large.ids == ids
    rows = large.rows()
    assert [row["title"] for row in rows] == ids
    assert rows[1]["embedding"] == pytest.approx([1.0, 1.0])
    assert [row["score"] for row in rows] == sorted((row["score"] for row in rows), reverse=True)
    assert [row["score"] for row in rows[:3]] == pytest.approx([row["score"] for row in small.rows()])
    assert log.row_reads and max(log.row_reads[1:]) <= 4


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_upserts_go_to_chroma_in_calls_of_at_most_the_client_batch_size(monkeypatch, tmp_path):
    """Chroma rejects an upsert above the client's ``get_max_batch_size()``, so upserts are sent in order in calls of at most that size."""
    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-write-split")
    monkeypatch.setattr(backend.client, "get_max_batch_size", lambda: 3)
    ids = [f"w{position}" for position in range(8)]

    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    assert log.writes == [("upsert", 3), ("upsert", 3), ("upsert", 2)]
    assert [row["title"] for row in backend.get_many(schema.entity_id, ids)] == ids

    log.clear()
    with pytest.raises(ValueError, match="needs a 'embedding' vector for new rows \\['new'\\]"):
        backend.upsert([*(RowOp(schema.entity_id, object_id, {"title": "changed"}) for object_id in ids), RowOp(schema.entity_id, "new", {"title": "new"})])
    assert log.writes == [] and [row["title"] for row in backend.get_many(schema.entity_id, ids)] == ids

    log.clear()
    backend.upsert([RowOp(schema.entity_id, object_id, op="delete") for object_id in ids])
    assert log.writes == [("delete", 8)]
    assert backend.count(schema.entity_id) == 0


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_rows_keeping_their_vectors_read_them_through_bounded_id_lists(monkeypatch, tmp_path):
    """An upsert of rows without vectors reads the stored vectors at most ``CHROMA_MAX_GET_IDS`` ids per ``get``."""
    from heavenbase.backends.vector import chroma

    backend, schema, log = _chroma_spied(monkeypatch, tmp_path, "chroma-keep-vectors")
    ids = [f"k{position}" for position in range(8)]
    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, float(position)]}) for position, object_id in enumerate(ids)])
    monkeypatch.setattr(chroma, "CHROMA_MAX_GET_IDS", 3)
    log.clear()

    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id.upper()}) for object_id in ids])

    assert log.named == [3, 3, 2] and log.writes == [("upsert", 8)]
    rows = backend.get_many(schema.entity_id, ids)
    assert [row["title"] for row in rows] == [object_id.upper() for object_id in ids]
    assert [value for row in rows for value in row["embedding"]] == pytest.approx([value for position in range(len(ids)) for value in (1.0, float(position))])


@pytest.mark.slow
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_upsert_keeps_more_stored_vectors_than_one_get_can_name(tmp_path):
    """One upsert of 33,000 rows without vectors keeps them all; one chromadb 1.5.9 ``get`` names at most 32,763 ids."""
    backend = hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / "chroma-keep-large"), ws_id="chroma-keep-large")
    schema = _schema()
    backend.ensure(schema, _bindings(schema, "vec"))
    ids = [f"K{position:05d}" for position in range(33_000)]
    for start in range(0, len(ids), 3_000):
        backend.upsert(
            [
                RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, position / 1000]})
                for position, object_id in enumerate(ids[start : start + 3_000], start)
            ]
        )

    backend.upsert([RowOp(schema.entity_id, object_id, {"title": object_id.lower()}) for object_id in ids])

    rows = backend.get_many(schema.entity_id, ids)
    assert [row["title"] for row in rows] == [object_id.lower() for object_id in ids]
    assert [row["embedding"][1] for row in rows] == pytest.approx([position / 1000 for position in range(len(ids))])


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_write_batch_size_follows_the_client(tmp_path):
    """The write batch size is the client's ``get_max_batch_size()``, its older ``max_batch_size``, or a bounded default."""
    from types import SimpleNamespace

    from heavenbase.backends.vector import chroma

    backend = hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / "chroma-size"), ws_id="chroma-size")
    assert backend._write_batch_size() == backend.client.get_max_batch_size() > 0
    backend.client = SimpleNamespace(max_batch_size=7)
    assert backend._write_batch_size() == 7
    backend.client = SimpleNamespace()
    assert backend._write_batch_size() == chroma.CHROMA_MAX_GET_IDS


@pytest.mark.slow
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_reads_a_collection_larger_than_one_sqlite_statement(tmp_path):
    """Every Chroma read of a collection above SQLite's 32,766 variables per statement still succeeds in full."""
    backend = hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / "chroma-large"), ws_id="chroma-large")
    schema = _schema()
    backend.ensure(schema, _bindings(schema, "vec"))
    ids = [f"L{position:05d}" for position in range(33_000)]
    for start in range(0, len(ids), 3_000):
        backend.upsert(
            [
                RowOp(schema.entity_id, object_id, {"title": object_id, "embedding": [1.0, position / 1000]})
                for position, object_id in enumerate(ids[start : start + 3_000], start)
            ]
        )

    assert sorted(row["object_id"] for row in backend.scan(schema.entity_id)) == ids
    assert sum(1 for _ in backend.iter_scan(schema.entity_id, batch_size=40_000, columns=["title"])) == len(ids)
    assert [row["title"] for row in backend.get_many(schema.entity_id, ids, columns=["title"])] == ids
    assert all(backend.exists_many(schema.entity_id, ids))
    assert sorted(backend.execute(_chroma_where(backend, schema, {"title": {"$ne": "zzz"}}, lambda row: True)).ids) == ids
    near = backend.execute(_chroma_near(backend, schema, len(ids)))
    scores = near.column("score")
    assert len(near.ids) > 4_096 and len(set(near.ids)) == len(near.ids) and scores == sorted(scores, reverse=True)
    every = _chroma_where(backend, schema, {"title": {"$in": ids}}, lambda row: True)
    assert sorted(backend.execute(every).ids) == ids
    assert backend.execute(_chroma_near(backend, schema, 5, every)).ids == ids[:5]


class _CappedConfig:
    """Config stand-in that answers a few keys and otherwise returns each caller's default."""

    def __init__(self, values):
        self.values = values

    def get(self, path=None, default=None):
        return self.values.get(path, default)


def _long_id_schema(max_chars: int) -> EntitySchema:
    return EntitySchema(
        "milvus-long-id",
        "MilvusLongId",
        fields={
            "object_id": FieldSchema("object_id", hb.Identifier(max_chars=max_chars), pk=True, required=True),
            "embedding": FieldSchema("embedding", hb.Vector[2], required=True),
        },
    )


@pytest.mark.slow
def test_milvus_object_id_length_takes_the_entity_domain_and_the_optional_config_cap(tmp_path):
    """A new collection's object_id length follows the entity's domain; the config key only lowers it."""
    backend = MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "length.db"), ws_id="milvus-length")
    assert backend._object_id_max_length(_long_id_schema(200)) == 200
    assert backend._object_id_max_length(_schema()) == hb.Identifier().max_chars
    backend._config = _CappedConfig({"heavenbase.backend_defaults.milvus.object_id.max_length": 120})
    assert backend._object_id_max_length(_long_id_schema(200)) == 120
    assert backend._object_id_max_length(_schema()) == hb.Identifier().max_chars
    backend._config = _CappedConfig({"heavenbase.backend_defaults.milvus.varchar.max_length": 150})
    assert backend._object_id_max_length(_long_id_schema(200)) == 150


@_requires_milvus_lite
@pytest.mark.parametrize("catalog", ["none", "full"])
def test_milvus_lite_holds_ids_up_to_the_entity_object_id_domain(tmp_path, catalog):
    """An entity whose object_id domain exceeds 63 characters writes and reads such ids through a Milvus route."""

    class MilvusLongDoc(hb.Entity):
        identifier = "milvus-long-doc"

        object_id = hb.field(hb.Identifier(max_chars=200)).pk()
        emb = hb.field(hb.Vector[2]).required().store(to="vec", strategy=hb.VectorIndex.identifier)

    workspace = hb.HeavenBase(
        f"milvus-long-doc-{catalog}",
        detached=True,
        backends={
            "main": hb.backends.InMemBackend("main"),
            "vec": MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "long.db"), ws_id=f"milvus-long-doc-{catalog}"),
        },
    )
    try:
        workspace.register(MilvusLongDoc, catalog=catalog)
        ids = ["a" * 60, "b" * 100, "c" * 200]
        workspace.upsert_many(MilvusLongDoc, [{"object_id": object_id, "emb": [1.0, float(len(object_id))]} for object_id in ids])
        for object_id in ids:
            assert workspace.get(object_id, entity=MilvusLongDoc)["emb"] == [1.0, float(len(object_id))]
        with pytest.raises(ValueError):
            workspace.upsert(MilvusLongDoc, {"object_id": "d" * 201, "emb": [0.0, 1.0]})
    finally:
        workspace.backends.get("vec").drop()


@_requires_milvus_lite
def test_milvus_lite_ensure_loads_an_existing_collection_that_was_released(tmp_path):
    """A collection reopened in the released state, as Milvus Lite leaves it after a restart, is loaded so reads work."""
    schema = _long_id_schema(63)
    backend = MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "released.db"), ws_id="milvus-released")
    bindings = [
        StorageBinding(schema.entity_id, "object_id", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "embedding", backend.name, hb.VectorIndex.identifier, "test"),
    ]
    try:
        backend.ensure(schema, bindings)
        backend.upsert([RowOp(schema.entity_id, "d1", {"object_id": "d1", "embedding": [1.0, 0.0]})])
        backend.client.release_collection(collection_name=backend._collection(schema.entity_id))
        with pytest.raises(RuntimeError, match="released|load"):
            backend.get_many(schema.entity_id, ["d1"])

        backend.ensure(schema, bindings)
        assert backend.get_many(schema.entity_id, ["d1"])[0]["object_id"] == "d1"
        # ensuring a collection that is already loaded leaves it readable
        backend.ensure(schema, bindings)
        assert backend.get_many(schema.entity_id, ["d1"])[0]["object_id"] == "d1"
    finally:
        backend.drop()


@_requires_milvus_lite
def test_milvus_lite_opens_a_collection_whose_object_id_an_earlier_release_sized_at_63(tmp_path):
    """A collection created with a 63-character object_id still validates for a longer domain and keeps ids that fit."""
    from pymilvus import DataType

    schema = _long_id_schema(200)
    backend = MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "legacy.db"), ws_id="milvus-legacy-length")
    try:
        backend._need_client()
        schema_def = backend.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema_def.add_field("object_id", DataType.VARCHAR, is_primary=True, max_length=63)
        schema_def.add_field("embedding", DataType.FLOAT_VECTOR, dim=2)
        indexes = backend.client.prepare_index_params()
        indexes.add_index(field_name="embedding", index_type="FLAT", metric_type="COSINE")
        backend.client.create_collection(
            collection_name=backend._collection(schema.entity_id), schema=schema_def, index_params=indexes, consistency_level="Strong"
        )

        bindings = [
            StorageBinding(schema.entity_id, "object_id", "main", hb.InlineColumn.identifier, "test"),
            StorageBinding(schema.entity_id, "embedding", backend.name, hb.VectorIndex.identifier, "test"),
        ]
        backend.ensure(schema, bindings)
        backend.upsert([RowOp(schema.entity_id, "a" * 63, {"object_id": "a" * 63, "embedding": [1.0, 0.0]})])
        assert backend.get_many(schema.entity_id, ["a" * 63])[0]["object_id"] == "a" * 63
        # the collection keeps its declared length, so Milvus itself rejects a longer id until it is recreated
        with pytest.raises(Exception, match="max_length"):
            backend.upsert([RowOp(schema.entity_id, "b" * 100, {"object_id": "b" * 100, "embedding": [0.0, 1.0]})])
    finally:
        backend.drop()
