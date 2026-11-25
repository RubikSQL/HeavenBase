"""Exercise Pinecone read misses during row and graph mutations."""

import pytest

import heavenbase as hb
from heavenbase.entity.registry import load_entity_artifact_export
from test_pinecone_records import _workspace

pytestmark = pytest.mark.fast


@pytest.mark.slow
@pytest.mark.parametrize("method", ["set", "set_many", "delete"])
@pytest.mark.parametrize("scoped", [False, True], ids=["workspace", "mcp"])
def test_inferred_mutation_does_not_select_another_entity_on_a_fetch_miss(monkeypatch, method, scoped):
    ws, indexes = _workspace(monkeypatch, "article-drafts")

    class Article(hb.Entity):
        identifier = "published-article"

        title = hb.field(hb.ShortText).store(to="vec")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Draft(hb.Entity):
        identifier = "article-draft"

        title = hb.field(hb.ShortText)

    try:
        ws.register(Article)
        ws.register(Draft)
        [index] = indexes.values()
        ws.upsert(Article, {"object_id": "spring-news", "title": "Spring news", "embedding": [1, 0]})
        ws.upsert(Draft, {"object_id": "spring-news", "title": "Working draft"})
        patch = {"object_id": "spring-news", "title": "Revised title"}

        if scoped:
            profile_type = load_entity_artifact_export("sys-toolkit", "profile", "ProfileSpec")
            context_type = load_entity_artifact_export("sys-toolkit", "families.workspace.context", "WorkspaceToolContext")
            build_tool = load_entity_artifact_export("sys-toolkit", f"families.workspace.tools.{method}", "build_tool")
            profile = profile_type(name="articles", tools=(method,), entities=(Article.identifier, Draft.identifier))
            action = build_tool(context_type(ws, profile, (method,)))
            error_type = RuntimeError
        else:
            action = getattr(ws, method)
            error_type = ValueError

        def mutate():
            if method == "set":
                return action(patch)
            if method == "set_many":
                return action([patch])
            return action("spring-news")

        with pytest.raises(error_type, match="multiple (MCP profile )?entities"):
            mutate()
        index.stale.add("spring-news")
        with pytest.raises(error_type, match="Pinecone.*entity"):
            mutate()
        assert ws.get("spring-news", entity=Draft)["title"] == "Working draft"
        assert ws.get("spring-news", entity=Article)["title"] == "Spring news"
        ws.set(patch, entity=Draft)
        assert ws.get("spring-news", entity=Draft)["title"] == "Revised title"
    finally:
        ws.release()


@pytest.fixture
def bookmarks(monkeypatch):
    ws, indexes = _workspace(monkeypatch, "article-bookmarks")

    class Article(hb.Entity):
        identifier = "bookmarked-article"

        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Bookmark(hb.Entity):
        identifier = "reader-bookmark"

        links = hb.field(hb.HyperG["article" : hb.Identifier["bookmarked-article"]]).store(strategy=hb.GraphEdge)

    try:
        ws.register(Article)
        ws.register(Bookmark)
        [index] = indexes.values()
        ws.upsert(Article, {"object_id": "spring-news", "embedding": [1, 0]})
        ws.upsert(Bookmark, {"object_id": "reading-list", "links": [{"article": "spring-news"}]})
        yield ws, index, Article, Bookmark
    finally:
        ws.release()


def test_graph_delete_cannot_bypass_restriction_after_target_fetch_misses(bookmarks):
    ws, index, Article, Bookmark = bookmarks
    with pytest.raises(hb.GraphDeleteRestrictedError):
        ws.delete((Article, "spring-news"))
    index.stale.add("spring-news")
    with pytest.raises(hb.GraphDeleteRestrictedError):
        ws.delete((Article, "spring-news"))
    assert ws.exists("spring-news", entity=Article)
    assert ws.get("reading-list", entity=Bookmark)["links"] == [{"article": "spring-news"}]
    with pytest.raises(hb.GraphDeleteRestrictedError):
        ws.delete((Article, "spring-news"))


@pytest.mark.slow
def test_graph_delete_of_a_missed_unreferenced_target_deletes_its_record(bookmarks):
    """A target that may exist and that no edge references is deleted; the call reports that no route returned it."""
    ws, index, Article, _Bookmark = bookmarks
    ws.upsert(Article, {"object_id": "summer-news", "embedding": [0, 1]})
    index.stale.add("summer-news")
    assert ws.delete((Article, "summer-news")) is False
    assert "summer-news" not in index.records
    assert ws.delete((Article, "never-written")) is False


@pytest.mark.slow
def test_graph_delete_detaches_references_to_a_missed_target(monkeypatch):
    """Detach rewrites the references of a target that may exist, then deletes its record."""
    ws, indexes = _workspace(monkeypatch, "article-reading")

    class Article(hb.Entity):
        identifier = "reading-article"

        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Bookmark(hb.Entity):
        identifier = "reading-bookmark"

        links = hb.field(hb.HyperG["article" : hb.Identifier["reading-article"]]).store(strategy=hb.GraphEdge(on_delete="detach"))

    try:
        ws.register(Article)
        ws.register(Bookmark)
        [index] = indexes.values()
        ws.upsert(Article, {"object_id": "spring-news", "embedding": [1, 0]})
        ws.upsert(Bookmark, {"object_id": "reading-list", "links": [{"article": "spring-news"}]})
        index.stale.add("spring-news")
        assert ws.delete((Article, "spring-news")) is False
        assert "spring-news" not in index.records
        assert ws.get("reading-list", entity=Bookmark)["links"] == []
        assert ws.check_graph() == []
    finally:
        ws.release()


def test_confirmed_graph_repair_cannot_remove_edge_after_target_fetch_misses(bookmarks):
    ws, index, Article, Bookmark = bookmarks
    assert ws.check_graph() == []
    index.stale.add("spring-news")
    with pytest.raises(ValueError, match="Pinecone.*retry"):
        ws.scrub_dangling(confirm=True)
    assert ws.exists("spring-news", entity=Article)
    assert ws.get("reading-list", entity=Bookmark)["links"] == [{"article": "spring-news"}]
    assert ws.scrub_dangling(confirm=True) == {"dangling": [], "removed": 0}


def test_unused_detach_policy_does_not_preempt_restriction(monkeypatch):
    ws, _ = _workspace(monkeypatch, "article-citations")

    class Article(hb.Entity):
        identifier = "cited-article"

        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["article" : hb.Identifier["cited-article"]]).store(strategy=hb.GraphEdge).optional()
        related = hb.field(hb.HyperG["article" : hb.Identifier["cited-article"]]).store(strategy=hb.GraphEdge(on_delete="detach")).optional()
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Article)
        ws.upsert(Article, {"object_id": "spring-news", "title": "Spring news"})
        ws.upsert(Article, {"object_id": "summer-news", "title": "Summer news", "cites": [{"article": "spring-news"}]})
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((Article, "spring-news"), edges="restrict")
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((Article, "spring-news"))
        assert ws.exists("spring-news", entity=Article)
        ws.upsert(
            Article,
            {
                "object_id": "summer-news",
                "title": "Summer news",
                "cites": [{"article": "spring-news"}],
                "related": [{"article": "spring-news"}],
            },
        )
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((Article, "spring-news"))
        assert ws.get("summer-news", entity=Article)["related"] == [{"article": "spring-news"}]
    finally:
        ws.release()


def test_vectorless_row_with_a_defaulted_field_on_a_row_keeping_route(monkeypatch):
    ws, indexes = _workspace(monkeypatch, "article-replicated-notes")

    class Article(hb.Entity):
        identifier = "replicated-article"

        title = hb.field(hb.ShortText)
        note = hb.field(hb.ShortText).store(to="main").store(to="vec").optional().default("No note")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Article)
        [index] = indexes.values()
        ws.upsert(Article, {"object_id": "spring-news", "title": "Spring news", "embedding": [1, 0]})
        ws.upsert(Article, {"object_id": "spring-news", "title": "Revised title", "note": None, "embedding": None})
        assert ws.get("spring-news", entity=Article)["note"] is None
        assert "spring-news" not in index.records
    finally:
        ws.release()


def test_vectorless_full_upsert_rejects_clearing_defaulted_pinecone_metadata(monkeypatch):
    ws, _ = _workspace(monkeypatch, "article-notes")

    class Article(hb.Entity):
        identifier = "annotated-article"

        title = hb.field(hb.ShortText)
        note = hb.field(hb.ShortText).store(to="vec").optional().default("No note")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Article)
        ws.upsert(Article, {"object_id": "spring-news", "title": "Spring news", "embedding": [1, 0]})
        with pytest.raises(ValueError, match="Pinecone cannot store.*without"):
            ws.upsert(Article, {"object_id": "spring-news", "title": "Revised title", "note": None, "embedding": None})
        assert ws.get("spring-news", entity=Article)["title"] == "Spring news"
        assert ws.get("spring-news", entity=Article)["note"] == "No note"
        ws.upsert(Article, {"object_id": "spring-news", "title": "Revised title", "note": None, "embedding": [1, 0]})
        assert ws.get("spring-news", entity=Article, columns=["note"])["note"] is None
    finally:
        ws.release()
