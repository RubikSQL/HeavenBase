"""Deterministic contracts for MongoDB, TinyDB, and OpenSearch providers."""

from __future__ import annotations

from fnmatch import fnmatchcase
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType, SimpleNamespace
import copy
import os
import sys
from uuid import uuid4

import pytest

from heavenbase.backends.mongodb.backend import MongoDBBackend
from heavenbase.backends.opensearch.backend import OpenSearchBackend
from heavenbase.backends.opensearch.handler import opensearch_filter_handler
from heavenbase.backends.tinydb.backend import TinyDBBackend
from heavenbase.execution.contracts import RowOp
from heavenbase.modules import module_resolver
from heavenbase.schema.model import EntitySchema, FieldSchema
from heavenbase.types import Identifier, Integer, ShortText
from heavenbase.utils import CM_HVNB, path_to_file_uri

pytestmark = pytest.mark.fast


def _schema() -> EntitySchema:
    """Return the shared document schema used by provider fakes."""
    return EntitySchema(
        "provider-document",
        "ProviderDocument",
        fields={
            "object_id": FieldSchema("object_id", Identifier(), pk=True, required=True),
            "title": FieldSchema("title", ShortText()),
            "rank": FieldSchema("rank", Integer()),
        },
    )


class _Plan:
    """Small resolved-plan stand-in for provider index tests."""

    bindings: list[object] = []

    def __init__(self, indexes=(), constraints=()):
        self._indexes = [SimpleNamespace(fields=tuple(fields)) for fields in indexes]
        self._constraints = [SimpleNamespace(fields=tuple(fields)) for fields in constraints]

    def backend_indexes(self, backend):
        """Return configured index bindings."""
        del backend
        return list(self._indexes)

    def backend_constraints(self, backend):
        """Return configured uniqueness bindings."""
        del backend
        return list(self._constraints)


class _FakeMongoCollection:
    """Minimal native-document collection used by MongoDB tests."""

    def __init__(self):
        self.documents = {}
        self.indexes = {}
        self.fail_find = False

    def create_index(self, keys, **options):
        """Record one provider index declaration."""
        name = options.get("name")
        self.indexes[name] = (tuple(keys), copy.deepcopy(options))
        return name

    def index_information(self):
        """Return provider-shaped managed index metadata."""
        information = {"_id_": {"key": [("_id", 1)], "unique": True}}
        for name, (keys, options) in self.indexes.items():
            information[name] = {
                "key": list(keys),
                "unique": options.get("unique", False),
            }
            if "partialFilterExpression" in options:
                information[name]["partialFilterExpression"] = copy.deepcopy(options["partialFilterExpression"])
        return information

    def drop_index(self, name):
        """Drop one managed index."""
        self.indexes.pop(name, None)

    def replace_one(self, query, document, upsert=False):
        """Replace one full document."""
        assert upsert is True
        self.documents[str(query["_id"])] = copy.deepcopy(document)

    def delete_many(self, query):
        """Delete every selected private id."""
        ids = {str(value) for value in query["_id"]["$in"]}
        for object_id in ids:
            self.documents.pop(object_id, None)

    def find(self, query):
        """Return matching provider documents."""
        if self.fail_find:
            raise OSError("mongo read failed")
        documents = list(self.documents.values())
        if query:
            wanted = {str(value) for value in query["_id"]["$in"]}
            documents = [document for document in documents if str(document["_id"]) in wanted]
        return [copy.deepcopy(document) for document in documents]

    def count_documents(self, query):
        """Return the exact collection count."""
        assert query == {}
        return len(self.documents)


class _FakeMongoDatabase:
    """Minimal MongoDB database with collection discovery and drop."""

    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        """Return or create a collection."""
        return self.collections.setdefault(name, _FakeMongoCollection())

    def list_collection_names(self):
        """Return physical collection names."""
        return list(self.collections)

    def drop_collection(self, name):
        """Drop one physical collection idempotently."""
        self.collections.pop(name, None)


class _FakeMongoClient:
    """MongoClient replacement sharing state by database name."""

    databases = {}

    def __init__(self, uri, **kwargs):
        self.uri = uri
        self.kwargs = kwargs

    def __getitem__(self, name):
        """Return or create a fake database."""
        return self.databases.setdefault(name, _FakeMongoDatabase())


def _install_fake_mongodb(monkeypatch):
    """Install a deterministic pymongo module."""
    _FakeMongoClient.databases = {}
    module = ModuleType("pymongo")
    module.MongoClient = _FakeMongoClient
    monkeypatch.setitem(sys.modules, "pymongo", module)


def test_mongodb_replacement_lookup_indexes_workspace_and_drop(monkeypatch):
    """MongoDB should use native documents with isolated physical namespaces."""
    _install_fake_mongodb(monkeypatch)
    backend = MongoDBBackend("docs", "mongodb://fake", ws_id="mongo-workspace")
    schema = _schema()
    backend.ensure_plan(schema, _Plan(indexes=[("title",), ("rank", "title")], constraints=[("rank",)]))

    collection_name = backend._collection_name(schema.entity_id)
    collection = backend.db[collection_name]
    index_names = set(collection.indexes)
    title_index = backend._index_spec(("title",), False)[0]
    rank_title_index = backend._index_spec(("rank", "title"), False)[0]
    rank_unique = backend._index_spec(("rank",), True)[0]
    assert backend.database == "mongo_workspace"
    assert collection_name == f"{backend._collection_scope()}_provider_document"
    assert {"hb_object_id", title_index, rank_title_index, rank_unique} <= index_names
    assert collection.indexes[rank_unique][1]["partialFilterExpression"] == {"rank": {"$type": ["bool", "date", "decimal", "double", "int", "long", "string"]}}

    backend.ensure_plan(schema, _Plan(indexes=[("title",)]))
    assert set(collection.indexes) == {"hb_object_id", title_index}

    backend.upsert(
        [
            RowOp(schema.entity_id, "a", {"title": "old", "rank": 1, "stale": True}),
            RowOp(schema.entity_id, "a", {"title": "new", "rank": 2}),
            RowOp(schema.entity_id, "b", {"title": "other", "rank": 3}),
            RowOp(schema.entity_id, "missing", op="delete"),
        ]
    )
    assert backend.get_many(schema.entity_id, ["b", "missing", "a", "a"]) == [
        {"object_id": "b", "title": "other", "rank": 3},
        None,
        {"object_id": "a", "title": "new", "rank": 2},
        {"object_id": "a", "title": "new", "rank": 2},
    ]
    assert backend.exists_many(schema.entity_id, ["a", "missing"]) == [True, False]
    assert backend.count(schema.entity_id) == backend.exact_count(schema.entity_id) == 2
    assert all("stale" not in row for row in backend.scan(schema.entity_id))

    backend.bind_workspace("mongo-other")
    assert backend.database == "mongo_other"
    assert backend.scan(schema.entity_id) == []
    backend.bind_workspace("mongo-workspace")
    backend.db["foreign_collection"].replace_one({"_id": "x"}, {"_id": "x"}, upsert=True)
    backend.drop()
    assert collection_name not in backend.db.list_collection_names()
    assert "foreign_collection" in backend.db.list_collection_names()


def test_mongodb_managed_index_names_preserve_structured_field_identity(monkeypatch):
    """Composite and underscore-bearing scalar fields receive distinct indexes."""
    _install_fake_mongodb(monkeypatch)
    backend = MongoDBBackend("docs", "mongodb://fake", ws_id="mongo-index-collision")
    schema = _schema()
    schema.fields["title_rank"] = FieldSchema("title_rank", ShortText())
    backend.ensure_plan(
        schema,
        _Plan(
            constraints=[
                ("title", "rank"),
                ("title_rank",),
            ]
        ),
    )

    collection = backend.db[backend._collection_name(schema.entity_id)]
    composite_name = backend._index_spec(("title", "rank"), True)[0]
    scalar_name = backend._index_spec(("title_rank",), True)[0]

    assert composite_name != scalar_name
    assert collection.indexes[composite_name][0] == (("title", 1), ("rank", 1))
    assert collection.indexes[scalar_name][0] == (("title_rank", 1),)


def test_mongodb_fresh_drop_discovers_exact_scope_and_preserves_sibling_name(monkeypatch):
    """MongoDB drop should discover only the exact hashed backend-owner scope."""
    _install_fake_mongodb(monkeypatch)
    uri = "mongodb://user:mongodb-secret@fake"
    workspace = "mongo-shared"
    schema = _schema()
    primary = MongoDBBackend("docs", uri, ws_id=workspace)
    sibling = MongoDBBackend("docs_archive", uri, ws_id=workspace)
    primary.ensure(schema, [])
    sibling.ensure(schema, [])
    primary.upsert([RowOp(schema.entity_id, "primary", {"title": "Primary"})])
    sibling.upsert([RowOp(schema.entity_id, "sibling", {"title": "Sibling"})])

    primary_name = primary._collection_name(schema.entity_id)
    sibling_name = sibling._collection_name(schema.entity_id)
    assert primary_name != sibling_name
    assert primary_name.startswith(f"{primary._collection_scope()}_")
    assert sibling_name.startswith(f"{sibling._collection_scope()}_")
    assert "mongodb-secret" not in repr(primary.to_dict())

    fresh = MongoDBBackend("docs", uri, ws_id=workspace)
    assert fresh._collections == {}
    fresh.drop()

    names = set(sibling.db.list_collection_names())
    assert primary_name not in names
    assert sibling_name in names
    assert sibling.get_many(schema.entity_id, ["sibling"]) == [{"title": "Sibling", "object_id": "sibling"}]


def test_mongodb_unavailable_and_operation_failures_are_contextual(monkeypatch):
    """MongoDB should fail clearly without leaking a stale in-process mirror."""
    missing = ModuleType("pymongo")
    monkeypatch.setitem(sys.modules, "pymongo", missing)
    unavailable = MongoDBBackend("docs", "mongodb://missing")
    with pytest.raises(RuntimeError, match="MongoDB backend 'docs' is unavailable"):
        unavailable.upsert([RowOp("provider-document", "a", {"title": "no"})])

    _install_fake_mongodb(monkeypatch)
    backend = MongoDBBackend("docs", "mongodb://fake", ws_id="mongo-failure")
    backend._collection("provider-document").fail_find = True
    with pytest.raises(RuntimeError, match="MongoDB scan failed.*mongo read failed"):
        backend.scan("provider-document")


class _FakeTinyTable:
    """TinyDB table replacement backed by one mutable list."""

    def __init__(self, rows):
        self.rows = rows

    def remove(self, condition):
        """Remove documents matching a TinyDB query callable."""
        self.rows[:] = [row for row in self.rows if not condition(row)]

    def insert_multiple(self, rows):
        """Insert isolated replacement documents."""
        self.rows.extend(copy.deepcopy(list(rows)))

    def all(self):
        """Return isolated provider documents."""
        return copy.deepcopy(self.rows)

    def __len__(self):
        return len(self.rows)


class _FakeTinyDB:
    """TinyDB replacement that persists state by physical file path."""

    storage = {}

    def __init__(self, path, **kwargs):
        self.path = path
        self.kwargs = kwargs
        Path(path).touch(exist_ok=True)
        self.tables_by_name = self.storage.setdefault(path, {})
        self.closed = False

    def table(self, name):
        """Return or create one table."""
        return _FakeTinyTable(self.tables_by_name.setdefault(name, []))

    def close(self):
        """Close the fake provider handle."""
        self.closed = True


def _install_fake_tinydb(monkeypatch):
    """Install a deterministic TinyDB module."""
    _FakeTinyDB.storage = {}
    module = ModuleType("tinydb")
    module.TinyDB = _FakeTinyDB
    monkeypatch.setitem(sys.modules, "tinydb", module)


def test_tinydb_persists_replacements_and_isolates_workspace_files(tmp_path, monkeypatch):
    """TinyDB should persist provider rows and derive one file per workspace."""
    _install_fake_tinydb(monkeypatch)
    base_uri = path_to_file_uri(tmp_path / "tiny.json")
    backend = TinyDBBackend("docs", base_uri, ws_id="tiny-workspace")
    first_path = backend.path
    assert Path(first_path).name.startswith("tiny_hb_tiny_workspace_docs_")
    assert first_path.endswith(".json")

    backend.upsert(
        [
            RowOp("provider-document", "a", {"title": "old", "stale": True}),
            RowOp("provider-document", "a", {"title": "new"}),
            RowOp("provider-document", "b", {"title": "other"}),
            RowOp("provider-document", "missing", op="delete"),
        ]
    )
    assert backend.scan("provider-document") == [
        {"title": "new", "object_id": "a"},
        {"title": "other", "object_id": "b"},
    ]
    assert backend.get_many("provider-document", ["b", "none", "a", "a"]) == [
        {"title": "other", "object_id": "b"},
        None,
        {"title": "new", "object_id": "a"},
        {"title": "new", "object_id": "a"},
    ]
    assert backend.exists_many("provider-document", ["a", "none"]) == [True, False]
    assert backend.count("provider-document") == backend.exact_count("provider-document") == 2

    reopened = TinyDBBackend("docs", base_uri, ws_id="tiny-workspace")
    assert reopened.scan("provider-document") == backend.scan("provider-document")
    reopened.bind_workspace("tiny-other")
    assert reopened.path != first_path
    assert reopened.scan("provider-document") == []
    reopened.bind_workspace("tiny-workspace")
    reopened.drop()
    assert not Path(first_path).exists()
    assert backend.path == first_path


def test_tinydb_workspace_file_owners_resist_normalized_name_collisions(tmp_path, monkeypatch):
    """Distinct public workspace IDs cannot share or delete one JSON database."""
    _install_fake_tinydb(monkeypatch)
    base_uri = path_to_file_uri(tmp_path / "tiny.json")
    first = TinyDBBackend("docs", base_uri, ws_id="team-a")
    sibling = TinyDBBackend("docs", base_uri, ws_id="team_a")

    assert first.path != sibling.path
    first.upsert([RowOp("provider-document", "first", {"title": "first"})])
    sibling.upsert([RowOp("provider-document", "sibling", {"title": "sibling"})])
    first.drop()

    assert sibling.get_many("provider-document", ["sibling"]) == [{"title": "sibling", "object_id": "sibling"}]
    sibling.drop()


def test_tinydb_rejects_non_uri_paths_and_reports_optional_driver_failure(tmp_path, monkeypatch):
    """TinyDB path validation and unavailable-driver errors should be explicit."""
    with pytest.raises(ValueError, match="Local file locations must use"):
        TinyDBBackend("docs", str(tmp_path / "tiny.json"))

    missing = ModuleType("tinydb")
    monkeypatch.setitem(sys.modules, "tinydb", missing)
    backend = TinyDBBackend("docs", path_to_file_uri(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="TinyDB backend 'docs' is unavailable"):
        backend.scan("provider-document")


def test_tinydb_invalid_second_replacement_preserves_ordered_partial_result(tmp_path, monkeypatch):
    """TinyDB should apply the first row while preserving the invalid row's prior value."""
    _install_fake_tinydb(monkeypatch)
    backend = TinyDBBackend(
        "docs",
        path_to_file_uri(tmp_path / "ordered-tiny.json"),
        ws_id="tiny-ordered",
    )
    backend.upsert(
        [
            RowOp("provider-document", "a", {"title": "old-a", "rank": 1}),
            RowOp("provider-document", "b", {"title": "old-b", "rank": 2}),
        ]
    )

    with pytest.raises(RuntimeError, match="TinyDB mutation failed"):
        backend.upsert(
            [
                RowOp("provider-document", "a", {"title": "new-a", "rank": 3}),
                RowOp("provider-document", "b", {"title": "invalid-b", "rank": float("nan")}),
            ]
        )

    assert backend.get_many("provider-document", ["a", "b"]) == [
        {"title": "new-a", "rank": 3, "object_id": "a"},
        {"title": "old-b", "rank": 2, "object_id": "b"},
    ]


@pytest.mark.skipif(find_spec("tinydb") is None, reason="tinydb is not installed")
def test_tinydb_real_local_reopen_roundtrip(tmp_path):
    """Persist replacement rows through the real TinyDB JSON storage driver."""
    database = path_to_file_uri(tmp_path / "real-tinydb.json")
    workspace = f"tinydb-real-{uuid4().hex[:10]}"
    backend = TinyDBBackend("docs", database, ws_id=workspace)
    reopened = None
    try:
        backend.upsert(
            [
                RowOp("provider-document", "a", {"title": "old", "stale": True}),
                RowOp("provider-document", "a", {"title": "new"}),
                RowOp("provider-document", "b", {"title": "other"}),
                RowOp("provider-document", "missing", op="delete"),
            ]
        )
        path = backend.path
        backend._close()
        reopened = TinyDBBackend("docs", database, ws_id=workspace)
        assert reopened.path == path
        assert reopened.get_many("provider-document", ["b", "none", "a"]) == [
            {"title": "other", "object_id": "b"},
            None,
            {"title": "new", "object_id": "a"},
        ]
        assert reopened.count("provider-document") == 2
        assert all("stale" not in row for row in reopened.scan("provider-document"))
    finally:
        if reopened is not None:
            reopened.drop()
        else:
            backend.drop()


def _opensearch_match(query, document):
    """Evaluate the small Query DSL subset emitted by the provider handler."""
    if "term" in query:
        field, value = next(iter(query["term"].items()))
        return document.get(field) == value
    if "terms" in query:
        field, values = next(iter(query["terms"].items()))
        return document.get(field) in values
    if "range" in query:
        field, bounds = next(iter(query["range"].items()))
        current = document.get(field)
        return all(
            {
                "lt": current < value,
                "lte": current <= value,
                "gt": current > value,
                "gte": current >= value,
            }[operator]
            for operator, value in bounds.items()
        )
    if "wildcard" in query:
        field, spec = next(iter(query["wildcard"].items()))
        current = str(document.get(field, ""))
        pattern = str(spec["value"])
        if spec.get("case_insensitive"):
            current, pattern = current.lower(), pattern.lower()
        return fnmatchcase(current, pattern)
    if "bool" in query:
        boolean = query["bool"]
        if "must" in boolean and not all(_opensearch_match(child, document) for child in boolean["must"]):
            return False
        if "should" in boolean and not any(_opensearch_match(child, document) for child in boolean["should"]):
            return False
        if "must_not" in boolean and any(_opensearch_match(child, document) for child in boolean["must_not"]):
            return False
        return True
    if "match_all" in query:
        return True
    raise AssertionError(f"Unexpected OpenSearch query: {query!r}")


class _FakeOpenSearchIndices:
    """OpenSearch index API replacement."""

    def __init__(self, owner):
        self.owner = owner

    def exists(self, index):
        return index in self.owner.data

    def create(self, index, body):
        self.owner.data[index] = {"mapping": copy.deepcopy(body), "documents": {}}

    def get_mapping(self, index):
        return {index: copy.deepcopy(self.owner.data[index]["mapping"])}

    def get_alias(self, index, ignore=None):
        """Discover existing indexes through the provider wildcard API."""
        assert ignore == [404]
        self.owner.alias_calls.append(index)
        matches = {name: {"aliases": {}} for name in self.owner.data if fnmatchcase(name, index)}
        return matches or {"status": 404}

    def put_mapping(self, index, body):
        properties = self.owner.data[index]["mapping"]["mappings"]["properties"]
        properties.update(copy.deepcopy(body["properties"]))

    def delete(self, index, ignore=None):
        assert ignore == [404]
        self.owner.data.pop(index, None)

    def refresh(self, index):
        assert index in self.owner.data
        self.owner.refresh_calls.append(index)


class _FakeOpenSearch:
    """OpenSearch client replacement with native query evaluation."""

    stores = {}

    def __init__(self, hosts, **kwargs):
        self.hosts = hosts
        self.kwargs = kwargs
        self.data = self.stores.setdefault(repr(hosts), {})
        self.indices = _FakeOpenSearchIndices(self)
        self.last_search = None
        self.scan_calls = []
        self.alias_calls = []
        self.refresh_calls = []
        self.fail_search = False

    def index(self, index, id, body):
        """Replace one source document."""
        self.data[index]["documents"][str(id)] = copy.deepcopy(body)

    def delete(self, index, id, ignore=None):
        """Delete one source document idempotently."""
        assert ignore == [404]
        self.data.get(index, {}).get("documents", {}).pop(str(id), None)

    def mget(self, index, body):
        """Return ordered multi-get documents."""
        documents = self.data[index]["documents"]
        return {
            "docs": [
                (
                    {"_id": object_id, "_source": copy.deepcopy(documents[object_id]), "found": True}
                    if object_id in documents
                    else {"_id": object_id, "found": False}
                )
                for object_id in body["ids"]
            ]
        }

    def count(self, index):
        """Return the exact provider count."""
        return {"count": len(self.data[index]["documents"])}

    def search(self, index, body):
        """Evaluate emitted Query DSL over fake source documents."""
        if self.fail_search:
            raise OSError("search unavailable")
        self.last_search = copy.deepcopy(body)
        hits = [
            {"_id": object_id, "_source": copy.deepcopy(document), "_score": 1.0}
            for object_id, document in self.data[index]["documents"].items()
            if _opensearch_match(body["query"], document)
        ]
        return {"hits": {"hits": hits[: body["size"]]}}


def _install_fake_opensearch(monkeypatch):
    """Install deterministic opensearchpy and helpers modules."""
    _FakeOpenSearch.stores = {}
    package = ModuleType("opensearchpy")
    package.__path__ = []
    package.OpenSearch = _FakeOpenSearch
    helpers = ModuleType("opensearchpy.helpers")

    def scan(client, query=None, *, index, size=1000, preserve_order=False, **kwargs):
        if client.fail_search:
            raise OSError("search unavailable")
        client.scan_calls.append(
            {
                "query": copy.deepcopy(query),
                "index": index,
                "size": size,
                "preserve_order": preserve_order,
                "kwargs": dict(kwargs),
            }
        )
        return [
            {"_id": object_id, "_source": copy.deepcopy(document), "_score": 0.25}
            for object_id, document in client.data[index]["documents"].items()
            if _opensearch_match(query["query"], document)
        ]

    helpers.scan = scan
    monkeypatch.setitem(sys.modules, "opensearchpy", package)
    monkeypatch.setitem(sys.modules, "opensearchpy.helpers", helpers)


def _handler_context(*, scalar_indexed=True):
    """Return the protocol subset used by a provider scalar compiler."""
    return SimpleNamespace(
        backend="search",
        resolver=module_resolver(),
        config=CM_HVNB,
        top_k=50,
        scalar_indexed=scalar_indexed,
        fallback_reason=lambda op, dtype: f"fallback {op} {dtype}",
    )


def test_opensearch_rows_mapping_native_query_and_fallback(monkeypatch):
    """OpenSearch should own native rows and route only semantics-safe filters."""
    _install_fake_opensearch(monkeypatch)
    backend = OpenSearchBackend(
        "search",
        hosts="https://fake:9200",
        ws_id="open-workspace",
        http_auth=("admin", "client-secret"),
        use_ssl=True,
        verify_certs=False,
    )
    schema = _schema()
    backend.ensure_plan(schema, _Plan(indexes=[("title",), ("rank",)]))
    index = backend._index(schema.entity_id)
    assert index == f"{backend._index_scope()}-provider-document"
    assert backend.client.data[index]["mapping"]["mappings"]["properties"]["title"]["type"] == "wildcard"
    assert backend.client.kwargs == {
        "http_auth": ("admin", "client-secret"),
        "use_ssl": True,
        "verify_certs": False,
        "timeout": 1.0,
    }
    assert "client-secret" not in repr(backend.to_dict())

    backend.upsert(
        [
            RowOp(schema.entity_id, "a", {"title": "Alpha", "rank": 1, "stale": True}),
            RowOp(schema.entity_id, "a", {"title": "Apricot", "rank": 2, "score": 9}),
            RowOp(schema.entity_id, "b", {"title": "Beta", "rank": 3}),
            RowOp(schema.entity_id, "missing", op="delete"),
        ]
    )
    assert backend.get_many(schema.entity_id, ["b", "none", "a"]) == [
        {"title": "Beta", "rank": 3, "object_id": "b"},
        None,
        {"title": "Apricot", "rank": 2, "score": 9, "object_id": "a"},
    ]
    assert backend.exists_many(schema.entity_id, ["a", "none"]) == [True, False]
    assert backend.count(schema.entity_id) == backend.exact_count(schema.entity_id) == 2
    assert all("stale" not in row for row in backend.scan(schema.entity_id))
    assert backend.client.scan_calls == [
        {
            "query": {"query": {"match_all": {}}},
            "index": index,
            "size": 10000,
            "preserve_order": False,
            "kwargs": {},
        }
    ]

    title = schema.fields["title"]
    native = opensearch_filter_handler("starts_with")(title, "A", _handler_context()).bind(schema.entity_id)
    assert native.kind() == "opensearch_query"
    native_frame = backend.execute(native)
    assert native_frame.ids == ["a"]
    assert native_frame.rows() == [{"object_id": "a", "rank": 2, "score": 9, "title": "Apricot"}]
    assert backend.client.scan_calls[-1] == {
        "query": {"query": {"wildcard": {"title": {"value": "A*", "case_insensitive": False}}}},
        "index": index,
        "size": 10000,
        "preserve_order": False,
        "kwargs": {},
    }

    regex = opensearch_filter_handler("regex")(title, "^A", _handler_context()).bind(schema.entity_id)
    wildcard = opensearch_filter_handler("wildcard")(title, "[AB]*", _handler_context()).bind(schema.entity_id)
    no_index = opensearch_filter_handler("eq")(title, "Beta", _handler_context(scalar_indexed=False)).bind(schema.entity_id)
    empty_in = opensearch_filter_handler("in")(title, [], _handler_context()).bind(schema.entity_id)
    nullable_in = opensearch_filter_handler("in")(title, ["Beta", None], _handler_context()).bind(schema.entity_id)
    assert regex.mode() == wildcard.mode() == no_index.mode() == empty_in.mode() == nullable_in.mode() == "py_scan"
    assert backend.execute(regex).ids == ["a"]
    assert backend.execute(wildcard).ids == ["a", "b"]
    assert backend.execute(no_index).ids == ["b"]

    backend.drop()
    assert index not in backend.client.data


@pytest.mark.parametrize(
    ("op", "pattern"),
    [
        ("wildcard", "*"),
        ("like", "%"),
        ("ilike", "%"),
    ],
)
def test_opensearch_patterns_matching_empty_text_scan_optional_nulls(monkeypatch, op, pattern):
    """Native wildcard queries cannot omit optional nulls accepted by portable predicates."""
    _install_fake_opensearch(monkeypatch)
    backend = OpenSearchBackend("search", hosts="http://fake", ws_id=f"open-null-{op}")
    schema = _schema()
    backend.ensure_plan(schema, _Plan(indexes=[("title",)]))
    backend.upsert(
        [
            RowOp(schema.entity_id, "text", {"title": "Alpha", "rank": 1}),
            RowOp(schema.entity_id, "null", {"title": None, "rank": 2}),
            RowOp(schema.entity_id, "absent", {"rank": 3}),
        ]
    )

    fragment = opensearch_filter_handler(op)(schema.fields["title"], pattern, _handler_context()).bind(schema.entity_id)

    assert fragment.mode() == "py_scan"
    assert set(backend.execute(fragment).ids) == {"text", "null", "absent"}
    retained_native = opensearch_filter_handler("wildcard")(schema.fields["title"], "A*", _handler_context()).bind(schema.entity_id)
    assert retained_native.mode() == "native"
    assert backend.execute(retained_native).ids == ["text"]


def test_opensearch_unicode_casefold_filters_use_portable_scan(monkeypatch):
    """Unicode-aware match and ilike cannot use ASCII-only provider folding."""
    _install_fake_opensearch(monkeypatch)
    backend = OpenSearchBackend("search", hosts="http://fake", ws_id="open-unicode")
    schema = _schema()
    backend.ensure_plan(schema, _Plan(indexes=[("title",)]))
    backend.upsert([RowOp(schema.entity_id, "accented", {"title": "ÉCLAIR", "rank": 1})])

    match = opensearch_filter_handler("match")(schema.fields["title"], "écl", _handler_context()).bind(schema.entity_id)
    ilike = opensearch_filter_handler("ilike")(schema.fields["title"], "%écl%", _handler_context()).bind(schema.entity_id)

    assert match.mode() == ilike.mode() == "py_scan"
    assert backend.execute(match).ids == ["accented"]
    assert backend.execute(ilike).ids == ["accented"]


def test_opensearch_unknown_delete_is_idempotent_without_refreshing_missing_index(monkeypatch):
    """Deleting from an unknown entity must not refresh a non-existent index."""
    _install_fake_opensearch(monkeypatch)
    backend = OpenSearchBackend("search", hosts="http://fake", ws_id="open-missing-delete")

    backend.upsert([RowOp("unknown-entity", "missing", op="delete")])

    assert backend.client.refresh_calls == []


def test_opensearch_refreshes_successful_writes_before_reporting_later_failure(monkeypatch):
    """A later batch failure cannot leave earlier accepted writes invisible."""
    _install_fake_opensearch(monkeypatch)
    backend = OpenSearchBackend("search", hosts="http://fake", ws_id="open-partial-write")
    schema = _schema()
    backend.ensure(schema, [])
    index = backend._index(schema.entity_id)
    pending = {}
    original_refresh = backend.client.indices.refresh

    def staged_index(*, index, id, body):
        if id == "bad":
            raise OSError("injected write failure")
        pending[str(id)] = copy.deepcopy(body)

    def publish_pending(*, index):
        backend.client.data[index]["documents"].update(pending)
        pending.clear()
        original_refresh(index)

    backend.client.index = staged_index
    backend.client.indices.refresh = publish_pending

    with pytest.raises(RuntimeError, match="injected write failure"):
        backend.upsert(
            [
                RowOp(schema.entity_id, "good", {"title": "visible", "rank": 1}),
                RowOp(schema.entity_id, "bad", {"title": "failed", "rank": 2}),
            ]
        )

    assert backend.get_many(schema.entity_id, ["good", "bad"]) == [
        {"title": "visible", "rank": 1, "object_id": "good"},
        None,
    ]
    assert backend.client.refresh_calls == [index]


def test_opensearch_native_scan_is_exhaustive_past_filter_page_size(monkeypatch):
    """Native filtering should exhaust helper scan results beyond its page size."""
    _install_fake_opensearch(monkeypatch)

    class _SmallPageConfig:
        @staticmethod
        def get(path, default=None):
            if path == "heavenbase.backend_defaults.opensearch.filter_limit":
                return 2
            return default

    backend = OpenSearchBackend(
        "search",
        hosts="http://fake",
        ws_id="open-pagination",
        resolver=module_resolver(),
        config=_SmallPageConfig(),
    )
    schema = _schema()
    backend.ensure_plan(schema, _Plan(indexes=[("rank",)]))
    backend.upsert([RowOp(schema.entity_id, str(rank), {"title": f"row-{rank}", "rank": rank}) for rank in range(5)])
    fragment = opensearch_filter_handler("gte")(
        schema.fields["rank"],
        0,
        _handler_context(),
    ).bind(schema.entity_id)

    assert backend.execute(fragment).ids == ["0", "1", "2", "3", "4"]
    assert backend.client.scan_calls[-1]["size"] == 2
    assert backend.client.scan_calls[-1]["query"] == {"query": {"range": {"rank": {"gte": 0}}}}


def test_opensearch_fresh_drop_discovers_scope_and_preserves_sibling(monkeypatch):
    """Fresh-instance drop should discover exact owned indexes without prefix bleed."""
    _install_fake_opensearch(monkeypatch)
    hosts = "https://user:url-secret@fake:9200"
    workspace = "open-shared"
    schema = _schema()
    primary = OpenSearchBackend(
        "search",
        hosts=hosts,
        ws_id=workspace,
        http_auth=("admin", "auth-secret"),
    )
    sibling = OpenSearchBackend(
        "search_archive",
        hosts=hosts,
        ws_id=workspace,
        http_auth=("admin", "auth-secret"),
    )
    primary.ensure(schema, [])
    sibling.ensure(schema, [])
    primary.upsert([RowOp(schema.entity_id, "primary", {"title": "Primary"})])
    sibling.upsert([RowOp(schema.entity_id, "sibling", {"title": "Sibling"})])
    primary_index = primary._index(schema.entity_id)
    sibling_index = sibling._index(schema.entity_id)
    assert primary_index != sibling_index
    assert primary_index.startswith(f"{primary._index_scope()}-")
    assert sibling_index.startswith(f"{sibling._index_scope()}-")
    serialized = repr(primary.to_dict())
    assert "url-secret" not in serialized
    assert "auth-secret" not in serialized

    fresh = OpenSearchBackend(
        "search",
        hosts=hosts,
        ws_id=workspace,
        http_auth=("admin", "auth-secret"),
    )
    assert fresh._indices == {}
    fresh.drop()

    assert fresh.client.alias_calls == [f"{fresh._index_scope()}-*"]
    assert primary_index not in fresh.client.data
    assert sibling_index in fresh.client.data
    assert sibling.get_many(schema.entity_id, ["sibling"]) == [{"title": "Sibling", "object_id": "sibling"}]


def test_opensearch_rejects_immutable_existing_mapping_changes(monkeypatch):
    """A fresh backend should reject incompatible index-policy changes."""
    _install_fake_opensearch(monkeypatch)
    schema = _schema()
    original = OpenSearchBackend("search", hosts="http://fake", ws_id="open-mapping")
    original.ensure_plan(schema, _Plan(indexes=[("title",)]))
    index = original._index(schema.entity_id)
    assert original.client.data[index]["mapping"]["mappings"]["properties"]["title"] == {"type": "wildcard"}

    fresh = OpenSearchBackend("search", hosts="http://fake", ws_id="open-mapping")
    with pytest.raises(
        RuntimeError,
        match=r"OpenSearch ensure failed.*mapping for provider-document\.(rank|title) is incompatible",
    ):
        fresh.ensure_plan(schema, _Plan(indexes=[("rank",)]))


def test_opensearch_unavailable_and_query_failures_are_contextual(monkeypatch):
    """OpenSearch should surface optional-driver and provider-operation failures."""
    missing = ModuleType("opensearchpy")
    monkeypatch.setitem(sys.modules, "opensearchpy", missing)
    unavailable = OpenSearchBackend("search", hosts="http://missing")
    with pytest.raises(RuntimeError, match="OpenSearch backend 'search' is unavailable"):
        unavailable.scan("provider-document")

    _install_fake_opensearch(monkeypatch)
    backend = OpenSearchBackend("search", hosts="http://fake", ws_id="open-failure")
    backend.ensure(_schema(), [])
    backend.upsert([RowOp("provider-document", "a", {"title": "Alpha", "rank": 1})])
    backend.client.fail_search = True
    with pytest.raises(RuntimeError, match="OpenSearch scan failed.*search unavailable"):
        backend.scan("provider-document")

    fragment = opensearch_filter_handler("eq")(
        _schema().fields["title"],
        "Alpha",
        _handler_context(),
    ).bind("provider-document")
    with pytest.raises(RuntimeError, match="OpenSearch query failed.*search unavailable"):
        backend.execute(fragment)


@pytest.mark.external
def test_mongodb_external_row_lifecycle():
    """Exercise the pymongo row lifecycle against an explicitly configured service."""
    uri = os.environ.get("HVNB_MONGODB_URI")
    if not uri:
        pytest.skip("set HVNB_MONGODB_URI to run the authenticated MongoDB smoke test")

    workspace = f"mongodb-live-{uuid4().hex[:10]}"
    entity_id = f"mongodb-live-document-{uuid4().hex[:10]}"
    backend = MongoDBBackend(
        "docs",
        uri,
        ws_id=workspace,
        serverSelectionTimeoutMS=3000,
    )
    try:
        try:
            backend.client.admin.command("ping")
        except Exception as exc:
            pytest.fail(f"configured MongoDB service is unavailable: {type(exc).__name__}: {exc}", pytrace=False)
        schema = _schema()
        schema.entity_id = entity_id
        backend.ensure_plan(schema, _Plan(indexes=[("title",), ("rank",)]))
        backend.upsert(
            [
                RowOp(entity_id, "a", {"title": "old", "rank": 1, "stale": True}),
                RowOp(entity_id, "a", {"title": "new", "rank": 2}),
                RowOp(entity_id, "b", {"title": "other", "rank": 3}),
                RowOp(entity_id, "missing", op="delete"),
            ]
        )
        assert backend.get_many(entity_id, ["b", "none", "a"]) == [
            {"title": "other", "rank": 3, "object_id": "b"},
            None,
            {"title": "new", "rank": 2, "object_id": "a"},
        ]
        assert backend.exists_many(entity_id, ["a", "none"]) == [True, False]
        assert backend.count(entity_id) == 2
        assert all("stale" not in row for row in backend.scan(entity_id))
        assert uri not in repr(backend.to_dict())

        unique_schema = _schema()
        unique_schema.entity_id = f"{entity_id}-unique"
        unique_schema.fields["title_rank"] = FieldSchema("title_rank", ShortText())
        backend.ensure_plan(
            unique_schema,
            _Plan(
                constraints=[
                    ("title", "rank"),
                    ("title_rank",),
                ]
            ),
        )
        backend.upsert([RowOp(unique_schema.entity_id, "base", {"title": "same", "rank": 7, "title_rank": "scalar"})])
        with pytest.raises(RuntimeError, match="duplicate key"):
            backend.upsert([RowOp(unique_schema.entity_id, "composite-conflict", {"title": "same", "rank": 7, "title_rank": "other"})])
        with pytest.raises(RuntimeError, match="duplicate key"):
            backend.upsert([RowOp(unique_schema.entity_id, "scalar-conflict", {"title": "different", "rank": 8, "title_rank": "scalar"})])
    finally:
        backend.drop()
        if backend.client is not None:
            backend.client.close()


@pytest.mark.external
def test_opensearch_py3_live_row_and_scan_contract():
    """Exercise OpenSearch-py 3.x auth/TLS and request signatures against a configured service."""
    opensearch_url = os.environ.get("HVNB_OPENSEARCH_URL", "https://localhost:9201")
    username = os.environ.get("HVNB_OPENSEARCH_USERNAME", "admin")
    password = os.environ.get("HVNB_OPENSEARCH_PASSWORD")
    if not password:
        pytest.skip("set HVNB_OPENSEARCH_PASSWORD to run the authenticated OpenSearch smoke test")

    import opensearchpy

    major = int(str(getattr(opensearchpy, "__versionstr__", "0")).split(".", 1)[0])
    if major < 3:
        pytest.skip(f"OpenSearch-py 3.x smoke test requires major version 3, found {major}")

    entity_id = f"opensearch-live-{uuid4().hex[:10]}"
    backend = OpenSearchBackend(
        "search",
        hosts=opensearch_url,
        ws_id=f"opensearch-live-ws-{uuid4().hex[:10]}",
        http_auth=(username, password),
        use_ssl=opensearch_url.startswith("https://"),
        verify_certs=False,
        ssl_assert_hostname=False,
        ssl_show_warn=False,
    )
    try:
        try:
            backend.client.info()
        except Exception as exc:
            if "HVNB_OPENSEARCH_URL" in os.environ:
                pytest.fail(f"configured OpenSearch service is unavailable: {type(exc).__name__}: {exc}", pytrace=False)
            pytest.skip(f"default OpenSearch service is unavailable: {type(exc).__name__}: {exc}")

        schema = _schema()
        schema.entity_id = entity_id
        backend.ensure_plan(schema, _Plan(indexes=[("title",), ("rank",)]))
        backend.upsert(
            [
                RowOp(entity_id, "a", {"title": "Alpha", "rank": 1}),
                RowOp(entity_id, "b", {"title": "Beta", "rank": 2}),
                RowOp(entity_id, "c", {"title": "ÉCLAIR", "rank": 3}),
                RowOp(f"{entity_id}-missing", "absent", op="delete"),
            ]
        )
        assert backend.get_many(entity_id, ["b", "none", "a"]) == [
            {"title": "Beta", "rank": 2, "object_id": "b"},
            None,
            {"title": "Alpha", "rank": 1, "object_id": "a"},
        ]
        assert {row["object_id"] for row in backend.scan(entity_id)} == {"a", "b", "c"}
        assert backend.count(entity_id) == 3
        native = opensearch_filter_handler("starts_with")(schema.fields["title"], "A", _handler_context()).bind(entity_id)
        assert native.mode() == "native"
        assert backend.execute(native).ids == ["a"]
        for op, value in (("match", "écl"), ("ilike", "%écl%")):
            unicode_filter = opensearch_filter_handler(op)(schema.fields["title"], value, _handler_context()).bind(entity_id)
            assert unicode_filter.mode() == "py_scan"
            assert backend.execute(unicode_filter).ids == ["c"]
        assert password not in repr(backend.to_dict())
    finally:
        backend.drop()
