"""Regression coverage for Chroma metadata filtering and near routing."""

from importlib.util import find_spec
from types import SimpleNamespace

import pytest

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")]


@pytest.fixture
def chroma_workspace(tmp_path):
    """Use isolated persistent Chroma storage."""
    path = hb.utils.path_to_file_uri(tmp_path / "chroma")
    backend = hb.backends.ChromaBackend("vec", path)
    ws = hb.HeavenBase("chroma-routing", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        yield ws, backend
    finally:
        ws.release()


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("near", [False, True], ids=["filter", "near"])
def test_chroma_ranges_keep_overflowing_integers(chroma_workspace, sign, near):
    """Native bounds and filtered near see the same overflowing integers that round-trip by id."""
    ws, backend = chroma_workspace

    class Doc(hb.Entity):
        identifier = "overflow-doc"

        number = hb.field(hb.Integer).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    backend.client.create_collection(name=backend._collection_name(Doc.schema().entity_id), metadata={"hnsw:space": "cosine"})
    ws.register(Doc)
    values = {"huge": sign * 10**400, "other": -sign * 10**400, "zero": 0, "missing": None}
    ws.upsert_many(Doc, [{"object_id": key, "number": value, "emb": [1.0, 0.0]} for key, value in values.items()])
    assert ws.get("huge", entity=Doc)["number"] == values["huge"]
    assert ws.query(Doc).where(Doc.number == values["huge"]).execute().ids == ["huge"]

    strict = Doc.number > 0 if sign > 0 else Doc.number < 0
    inclusive = Doc.number >= 0 if sign > 0 else Doc.number <= 0
    for predicate, expected in ((strict, {"huge"}), (inclusive, {"huge", "zero"})):
        query = ws.query(Doc).where(predicate)
        if near:
            query = query.near(Doc.emb, [1, 0], top_k=4)
        assert set(query.execute().ids) == expected


@pytest.mark.parametrize("definition", ["configuration", "schema"])
@pytest.mark.parametrize("split_where", [False, True], ids=["native-query", "split-where"])
def test_chroma_near_uses_the_provider_configuration(chroma_workspace, monkeypatch, definition, split_where):
    """An explicitly configured IP collection keeps its metric in both Chroma query paths."""
    import chromadb

    from heavenbase.backends.vector import chroma

    ws, backend = chroma_workspace

    class Doc(hb.Entity):
        identifier = "configured-index-doc"

        title = hb.field(hb.ShortText).store(to="vec")
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    entity_id = Doc.schema().entity_id
    if definition == "schema":
        index_schema = chromadb.Schema().create_index(config=chromadb.VectorIndexConfig(space="ip"))
        options = {"schema": index_schema}
    else:
        options = {"configuration": {"hnsw": {"space": "ip"}}}
    collection = backend.client.create_collection(name=backend._collection_name(entity_id), **options)
    assert not collection.metadata
    ws.register(Doc)
    ws.upsert_many(
        Doc,
        [{"object_id": "close", "title": "close", "emb": [1, 0]}, {"object_id": "far", "title": "far", "emb": [10, 0]}],
    )
    if split_where:
        monkeypatch.setattr(chroma, "CHROMA_MAX_WHERE_VARIABLES", 3)
    predicate = Doc.title.in_(["close", "far", "absent", "absent-too"])
    l2 = ws.query(Doc).where(predicate).near(Doc.emb, [1, 0], top_k=1, metric="l2")
    ip = ws.query(Doc).where(predicate).near(Doc.emb, [1, 0], top_k=1, metric="ip")
    if not split_where:
        assert l2.execute().ids == ["close"]
    assert ip.execute().ids == ["far"]
    assert l2.execute().ids == ["close"]
    assert backend.near_space(entity_id) == "ip"


@pytest.mark.parametrize("filtered", [False, True], ids=["near", "filtered-near"])
def test_chroma_near_uses_the_requested_vector_field(chroma_workspace, filtered):
    """Chroma ranks by the requested field even when it is stored in metadata."""
    ws, backend = chroma_workspace

    class Doc(hb.Entity):
        identifier = "two-vector-doc"

        title = hb.field(hb.ShortText).store(to="vec")
        first_vec = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
        second_vec = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    backend.client.create_collection(name=backend._collection_name(Doc.schema().entity_id), metadata={"hnsw:space": "cosine"})
    ws.register(Doc)
    ws.upsert_many(
        Doc,
        [
            {"object_id": "a", "title": "same", "first_vec": [1, 0], "second_vec": [0, 1]},
            {"object_id": "b", "title": "same", "first_vec": [0, 1], "second_vec": [1, 0]},
        ],
    )
    assert ws.get("b", entity=Doc)["second_vec"] == [1, 0]

    def query(field):
        result = ws.query(Doc).near(field, [1, 0], top_k=1)
        return result.where(Doc.title == "same") if filtered else result

    assert query(Doc.first_vec).execute().ids == ["a"]
    assert query(Doc.second_vec).execute().ids == ["b"]


@pytest.mark.parametrize("legacy", [False, True], ids=["labeled-collection", "unlabeled-collection"])
def test_chroma_reregistration_preserves_the_embedding_field(chroma_workspace, legacy):
    """Reordering a schema preserves reads, ranking and writes for both vectors."""
    ws, backend = chroma_workspace

    class Original(hb.Entity):
        identifier = "reordered-vector-doc"

        first_vec = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
        second_vec = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Reordered(hb.Entity):
        identifier = "reordered-vector-doc"

        second_vec = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
        first_vec = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    entity_id = Original.schema().entity_id
    if legacy:
        backend.client.create_collection(name=backend._collection_name(entity_id), metadata={"hnsw:space": "cosine"})
    ws.register(Original)
    ws.upsert_many(
        Original,
        [
            {"object_id": "a", "first_vec": [1, 0], "second_vec": [0, 1]},
            {"object_id": "b", "first_vec": [0, 1], "second_vec": [1, 0]},
        ],
    )
    assert ws.query(Original).near(Original.second_vec, [1, 0], top_k=1, metric="cosine").execute().ids == ["b"]

    ws.register(Reordered, allow_replace=True)
    row = ws.get("b", entity=Reordered)
    assert (row["first_vec"], row["second_vec"]) == ([0, 1], [1, 0])
    assert backend.near_vector_field(entity_id) == "first_vec"
    assert ws.query(Reordered).near(Reordered.first_vec, [1, 0], top_k=1, metric="cosine").execute().ids == ["a"]
    assert ws.query(Reordered).near(Reordered.second_vec, [1, 0], top_k=1, metric="cosine").execute().ids == ["b"]

    ws.upsert(Reordered, {"object_id": "b", "first_vec": [1, 1], "second_vec": [1, -1]})
    reader = backend if legacy else hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(backend.path), ws_id=backend.ws_id)
    if not legacy:
        reader.ensure(Reordered.schema(), [])
    row = reader.get_many(entity_id, ["b"])[0]
    assert reader.near_vector_field(entity_id) == "first_vec"
    assert row["first_vec"] == pytest.approx([1, 1])
    assert row["second_vec"] == [1, -1]


def _split_items(main_dim=2, chroma_dim=2, *, chroma_first=False):
    """Return an entity with one Vector field on `main` and one on Chroma, the first one declared first unless `chroma_first`."""

    def on_main():
        return hb.field(hb.Vector[main_dim]).store(to="main")

    def on_chroma():
        return hb.field(hb.Vector[chroma_dim] if chroma_dim else hb.Vector).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Item(hb.Entity):
        identifier = "split-vector-item"

        title = hb.field(hb.ShortText).store(to="vec")
        if chroma_first:
            description = on_chroma()
            thumbnail = on_main()
        else:
            thumbnail = on_main()
            description = on_chroma()

    return Item


def _legacy_collection(backend, entity_id, width):
    """Create a collection as 0.1.2.2 left it: no recorded embedding field and one row whose embedding is `width` wide."""
    collection = backend.client.create_collection(name=backend._collection_name(entity_id), metadata={"hnsw:space": "cosine"})
    collection.upsert(ids=["lamp"], metadatas=[{"title": "Brass lamp"}], documents=["Brass lamp"], embeddings=[[0.1] * width])
    return collection


def _bindings():
    """Return the bindings of the entity's Chroma fields, as a workspace passes them to `ensure`."""
    return [SimpleNamespace(backend="vec", field_name=name) for name in ("title", "description")]


def test_an_older_collection_with_generated_embeddings_stays_writable(chroma_workspace):
    """0.1.2.2 sent no embeddings when another backend stored the first Vector field; Chroma stored 384-wide ones."""
    ws, backend = chroma_workspace
    Item = _split_items()
    entity_id = Item.schema().entity_id
    collection = _legacy_collection(backend, entity_id, 384)
    ws.register(Item)
    assert backend.near_vector_field(entity_id) is None
    assert "description" not in (backend.get_many(entity_id, ["lamp"])[0] or {})

    ws.upsert(Item, {"object_id": "desk", "title": "Desk", "thumbnail": [1, 0], "description": [0, 1]})
    ws.upsert(Item, {"object_id": "lamp", "title": "Brass lamp", "thumbnail": [1, 0], "description": [1, 0]})
    ws.set({"object_id": "lamp", "title": "Brass lamp 2"}, entity=Item)
    assert ws.get("lamp", entity=Item) == {"object_id": "lamp", "title": "Brass lamp 2", "thumbnail": [1, 0], "description": [1, 0]}
    assert ws.query(Item).where(Item.title == "Desk").execute().ids == ["desk"]
    assert ws.query(Item).near(Item.description, [0, 1], top_k=1).execute().ids == ["desk"]
    assert {len(vector) for vector in collection.get(include=["embeddings"])["embeddings"]} == {384}

    reader = hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(backend.path), ws_id=backend.ws_id)
    reader.ensure(Item.schema(), _bindings())
    assert reader.near_vector_field(entity_id) is None


@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "chroma_dim", "chroma_first"),
    [(2, 2, False), (384, 384, False), (384, None, False), (384, 2, True), (3, 2, False)],
    ids=["own-width", "own-width-384", "no-dimension", "declared-first", "another-width"],
)
def test_other_older_collections_keep_their_embedding_field(chroma_workspace, width, chroma_dim, chroma_first):
    """Only a collection whose width no vector of the field can have, and that 0.1.2.2 sent none to, loses its embedding field."""
    ws, backend = chroma_workspace
    Item = _split_items(chroma_dim=chroma_dim, chroma_first=chroma_first)
    entity_id = Item.schema().entity_id
    collection = _legacy_collection(backend, entity_id, width)
    ws.register(Item)
    assert backend.near_vector_field(entity_id) == "description"
    if chroma_dim is None or width == chroma_dim:  # the stored vector may be the caller's, so a patch keeps it
        ws.set({"object_id": "lamp", "title": "Brass lamp 2"}, entity=Item)
        assert ws.get("lamp", entity=Item)["description"] == pytest.approx([0.1] * width)
        assert list(collection.get(ids=["lamp"], include=["embeddings"])["embeddings"][0]) == pytest.approx([0.1] * width)


@pytest.mark.parametrize("chroma_dim", [2, 384, None], ids=["two", "384", "no-dimension"])
def test_a_new_collection_keeps_its_embedding_field_and_vectors(chroma_workspace, chroma_dim):
    """A collection created by this release records its embedding field, whatever the width of its vectors."""
    ws, backend = chroma_workspace
    Item = _split_items(chroma_dim=chroma_dim)
    entity_id = Item.schema().entity_id
    ws.register(Item)
    vector = [0.1] * (chroma_dim or 384)
    ws.upsert(Item, {"object_id": "lamp", "title": "Brass lamp", "thumbnail": [1, 0], "description": vector})

    reader = hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(backend.path), ws_id=backend.ws_id)
    reader.ensure(Item.schema(), _bindings())
    assert reader.near_vector_field(entity_id) == "description"
    assert reader.get_many(entity_id, ["lamp"])[0]["description"] == pytest.approx(vector)
