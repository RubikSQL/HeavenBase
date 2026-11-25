"""A route other than Pinecone settles whether a row is absent; Pinecone alone cannot."""

import pytest

import heavenbase as hb
from heavenbase.entity.registry import load_entity_artifact_export
from heavenbase.execution.contracts import RowOp
from test_pinecone_records import _workspace

pytestmark = pytest.mark.fast


def _tool(ws, method, *entities):
    """Build one MCP workspace tool whose profile sees ``entities``."""
    profile_type = load_entity_artifact_export("sys-toolkit", "profile", "ProfileSpec")
    context_type = load_entity_artifact_export("sys-toolkit", "families.workspace.context", "WorkspaceToolContext")
    build_tool = load_entity_artifact_export("sys-toolkit", f"families.workspace.tools.{method}", "build_tool")
    profile = profile_type(name="newsroom", tools=(method,), entities=tuple(entity.identifier for entity in entities))
    return build_tool(context_type(ws, profile, (method,)))


@pytest.fixture
def newsroom(monkeypatch):
    """Articles keep their title on InMem and their embedding on Pinecone; drafts live on InMem only."""
    ws, indexes = _workspace(monkeypatch, "newsroom")

    class Article(hb.Entity):
        identifier = "newsroom-article"

        title = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    class Draft(hb.Entity):
        identifier = "newsroom-draft"

        title = hb.field(hb.ShortText)

    try:
        ws.register(Article)
        ws.register(Draft)
        ws.upsert(Article, {"object_id": "spring-news", "title": "Spring news", "embedding": [1, 0]})
        ws.upsert(Draft, {"object_id": "working-draft", "title": "Working draft"})
        [index] = indexes.values()
        yield ws, index, Article, Draft
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("scoped", [False, True], ids=["workspace", "mcp"])
@pytest.mark.parametrize("method", ["set", "set_many"])
def test_inferred_set_targets_the_only_entity_holding_the_id(newsroom, method, scoped):
    ws, _index, Article, Draft = newsroom
    patch = {"object_id": "working-draft", "title": "Revised draft"}
    action = _tool(ws, method, Article, Draft) if scoped else getattr(ws, method)
    action(patch if method == "set" else [patch])
    assert ws.get("working-draft", entity=Draft)["title"] == "Revised draft"
    assert ws.get("working-draft", entity=Article) is None


@pytest.mark.slow
@pytest.mark.parametrize("scoped", [False, True], ids=["workspace", "mcp"])
@pytest.mark.parametrize("method", ["delete", "delete_many"])
def test_inferred_delete_removes_the_only_row_holding_the_id(newsroom, method, scoped):
    ws, _index, Article, Draft = newsroom
    if scoped:
        _tool(ws, "delete", Article, Draft)("working-draft")
    elif method == "delete":
        assert ws.delete("working-draft") is True
    else:
        assert ws.delete_many(["working-draft"]) == [True]
    assert not ws.exists("working-draft", entity=Draft)
    assert ws.get("spring-news", entity=Article)["title"] == "Spring news"


def test_inference_still_refuses_an_entity_stored_only_on_pinecone(monkeypatch):
    ws, _indexes = _workspace(monkeypatch, "pinecone-only-inference")

    class Clip(hb.Entity):
        identifier = "pinecone-only-clip"

        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Draft(hb.Entity):
        identifier = "pinecone-only-draft"

        title = hb.field(hb.ShortText)

    try:
        ws.register(Clip)
        ws.register(Draft)
        ws.upsert(Draft, {"object_id": "working-draft", "title": "Working draft"})
        with pytest.raises(ValueError, match="Pinecone backend 'vec'.*specify the target entity explicitly"):
            ws.set({"object_id": "working-draft", "title": "Revised draft"})
        assert ws.get("working-draft", entity=Draft)["title"] == "Working draft"
        ws.set({"object_id": "working-draft", "title": "Revised draft"}, entity=Draft)
        assert ws.get("working-draft", entity=Draft)["title"] == "Revised draft"
    finally:
        ws.release()


@pytest.fixture
def bookmarks(monkeypatch):
    """Bookmarks link articles whose title is on InMem and whose embedding is on Pinecone."""
    ws, _indexes = _workspace(monkeypatch, "newsroom-bookmarks")

    class Article(hb.Entity):
        identifier = "bookmarked-news"

        title = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Bookmark(hb.Entity):
        identifier = "news-bookmark"

        links = hb.field(hb.HyperG["article" : hb.Identifier["bookmarked-news"]]).store(strategy=hb.GraphEdge)

    try:
        ws.register(Article)
        ws.register(Bookmark)
        ws.upsert(Article, {"object_id": "spring-news", "title": "Spring news", "embedding": [1, 0]})
        ws.upsert(Article, {"object_id": "summer-news", "title": "Summer news", "embedding": [0, 1]})
        ws.upsert(Bookmark, {"object_id": "reading-list", "links": [{"article": "spring-news"}]})
        yield ws, Article, Bookmark
    finally:
        ws.release()


def test_deleting_an_absent_graph_target_reports_false(bookmarks):
    ws, Article, _Bookmark = bookmarks
    assert ws.delete((Article, "never-written")) is False
    assert ws.delete_many([(Article, "summer-news"), (Article, "never-written")]) == [True, False]
    assert not ws.exists("summer-news", entity=Article)


def test_confirmed_repair_removes_an_edge_to_a_target_absent_from_every_route(bookmarks):
    ws, Article, Bookmark = bookmarks
    for backend_name in ("main", "vec"):
        ws.backends.get(backend_name).upsert([RowOp(Article.identifier, "spring-news", op="delete")])
    assert ws.get("spring-news", entity=Article) is None
    assert len(ws.check_graph()) == 1
    assert ws.scrub_dangling(confirm=True)["removed"] == 1
    assert ws.get("reading-list", entity=Bookmark)["links"] == []
    assert ws.check_graph() == []


def test_set_creates_a_row_that_a_route_other_than_pinecone_shows_is_new(newsroom):
    ws, index, Article, _Draft = newsroom
    assert ws.set({"object_id": "autumn-news", "title": "Autumn news"}, entity=Article) == "autumn-news"
    assert ws.get("autumn-news", entity=Article) == {"object_id": "autumn-news", "title": "Autumn news"}
    assert "autumn-news" not in index.records
    index.stale.add("spring-news")
    with pytest.raises(ValueError, match="retry it once the record can be read"):
        ws.set({"object_id": "spring-news", "title": "Revised"}, entity=Article)
    assert ws.get("spring-news", entity=Article) == {"object_id": "spring-news", "title": "Spring news", "embedding": [1.0, 0.0]}


@pytest.fixture
def notes(monkeypatch):
    """Notes keep every field on Pinecone; memos keep their title on InMem as well."""
    ws, indexes = _workspace(monkeypatch, "pinecone-create-only")

    class Note(hb.Entity):
        identifier = "pinecone-create-only-note"

        title = hb.field(hb.ShortText).store(to="vec")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Memo(hb.Entity):
        identifier = "pinecone-create-only-memo"

        title = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(Note)
        ws.register(Memo)
        ws.upsert(Note, {"object_id": "meeting-notes", "title": "Original notes", "embedding": [1, 0]})
        ws.upsert(Memo, {"object_id": "meeting-memo", "title": "Original memo", "embedding": [1, 0]})
        pinecone = ws.backends.get("vec")
        yield ws, {entity.identifier: indexes[pinecone._index_name(entity.identifier)] for entity in (Note, Memo)}, Note, Memo
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("batch", [False, True], ids=["upsert", "upsert-many"])
def test_create_only_upsert_does_not_replace_a_record_pinecone_missed(notes, batch):
    ws, indexes, Note, _Memo = notes
    replacement = {"object_id": "meeting-notes", "title": "Replacement", "embedding": [0, 1]}
    indexes[Note.identifier].stale.add("meeting-notes")
    with pytest.raises(ValueError, match="overwrite=False cannot confirm.*pass overwrite=True"):
        ws.upsert_many(Note, [replacement], overwrite=False) if batch else ws.upsert(Note, replacement, overwrite=False)
    assert ws.get("meeting-notes", entity=Note)["title"] == "Original notes"
    with pytest.raises(ValueError, match="overwrite=False cannot confirm"):
        ws.upsert(Note, {"object_id": "agenda", "title": "Agenda", "embedding": [0, 1]}, overwrite=False)
    assert ws.get("agenda", entity=Note) is None
    ws.upsert(Note, {"object_id": "agenda", "title": "Agenda", "embedding": [0, 1]})
    assert ws.get("agenda", entity=Note)["title"] == "Agenda"


@pytest.mark.slow
@pytest.mark.parametrize("batch", [False, True], ids=["upsert", "upsert-many"])
def test_create_only_upsert_relies_on_a_route_that_keeps_every_row(notes, batch):
    ws, indexes, _Note, Memo = notes
    indexes[Memo.identifier].stale.add("meeting-memo")
    replacement = {"object_id": "meeting-memo", "title": "Replacement", "embedding": [0, 1]}
    with pytest.raises(ValueError, match="already exists"):
        ws.upsert_many(Memo, [replacement], overwrite=False) if batch else ws.upsert(Memo, replacement, overwrite=False)
    assert ws.get("meeting-memo", entity=Memo)["title"] == "Original memo"
    agenda = {"object_id": "agenda-memo", "title": "Agenda", "embedding": [0, 1]}
    ws.upsert_many(Memo, [agenda], overwrite=False) if batch else ws.upsert(Memo, agenda, overwrite=False)
    assert ws.get("agenda-memo", entity=Memo)["title"] == "Agenda"


def test_delete_after_a_missed_fetch_also_removes_the_catalog_row(notes):
    ws, indexes, Note, Memo = notes
    indexes[Note.identifier].stale.add("meeting-notes")
    assert ws.delete((Note, "meeting-notes")) is False
    assert indexes[Note.identifier].records == {}
    assert ws.query(hb.Catalog).where(hb.Catalog.target_entity == Note.identifier).execute().rows() == []
    assert ws.delete((Memo, "never-written")) is False
    assert len(ws.query(hb.Catalog).where(hb.Catalog.target_entity == Memo.identifier).execute().rows()) == 1


def _replaceable_note(with_summary):
    """Return a note entity stored only on Pinecone, or the same entity with a summary on InMem."""

    class Note(hb.Entity):
        identifier = "replaced-route-note"

        title = hb.field(hb.ShortText).store(to="vec")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
        if with_summary:
            summary = hb.field(hb.ShortText).store(to="main").optional()

    return Note


def test_rewritten_rows_restore_absence_checks_on_a_route_a_replacement_added(monkeypatch):
    """A route added by a replacement holds no earlier row until the documented rewrite."""
    ws, indexes = _workspace(monkeypatch, "replaced-route")

    class Draft(hb.Entity):
        identifier = "replaced-route-draft"

        title = hb.field(hb.ShortText)

    try:
        ws.register(_replaceable_note(False))
        ws.register(Draft)
        ws.upsert(_replaceable_note(False).identifier, {"object_id": "meeting-notes", "title": "Original notes", "embedding": [1, 0]})
        ws.upsert(Draft, {"object_id": "meeting-notes", "title": "Draft notes"})
        Note = ws.register(_replaceable_note(True), allow_replace=True)
        assert ws.backends.get("main").get_many(Note.identifier, ["meeting-notes"]) == [None]
        ws.upsert_many(Note, ws.get_many(["meeting-notes"], entity=Note))
        assert ws.backends.get("main").get_many(Note.identifier, ["meeting-notes"])[0]["object_id"] == "meeting-notes"
        [index] = indexes.values()
        index.stale.add("meeting-notes")
        with pytest.raises(ValueError, match="already exists"):
            ws.upsert(Note, {"object_id": "meeting-notes", "title": "Replacement", "embedding": [0, 1]}, overwrite=False)
        index.stale.add("meeting-notes")
        with pytest.raises(ValueError, match="exists in multiple entities"):
            ws.set({"object_id": "meeting-notes", "title": "Revised"})
        assert ws.get("meeting-notes", entity=Note)["title"] == "Original notes"
        assert ws.get("meeting-notes", entity=Draft)["title"] == "Draft notes"
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("action", ["delete", "delete_many", "mcp-delete", "mcp-delete-many", "set", "mcp-set"])
def test_inference_refuses_a_miss_on_an_entity_stored_only_on_pinecone_that_no_entity_matched(notes, action):
    """No entity matched, and the only possible holder is stored only on Pinecone, whose fetch missed."""
    ws, indexes, Note, Memo = notes
    indexes[Note.identifier].stale.add("meeting-notes")
    calls = {
        "delete": lambda: ws.delete("meeting-notes"),
        "delete_many": lambda: ws.delete_many(["meeting-notes"]),
        "mcp-delete": lambda: _tool(ws, "delete", Note, Memo)("meeting-notes"),
        "mcp-delete-many": lambda: _tool(ws, "delete_many", Note, Memo)(["meeting-notes"]),
        "set": lambda: ws.set({"object_id": "meeting-notes", "title": "Revised"}),
        "mcp-set": lambda: _tool(ws, "set", Note, Memo)({"object_id": "meeting-notes", "title": "Revised"}),
    }
    with pytest.raises((ValueError, RuntimeError), match="Pinecone backend 'vec'.*specify the target entity explicitly"):
        calls[action]()
    assert indexes[Note.identifier].records["meeting-notes"]["metadata"]["title"] == "Original notes"
    assert ws.delete((Note, "meeting-notes")) is True
    assert indexes[Note.identifier].records == {}


def test_inferred_delete_of_an_absent_id_reports_false_when_a_route_other_than_pinecone_settles_it(newsroom):
    ws, _index, _Article, _Draft = newsroom
    assert ws.delete("never-written") is False
    assert ws.delete_many(["never-written", "working-draft"]) == [False, True]
