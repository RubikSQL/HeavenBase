import heavenbase as hb
import pytest

pytestmark = pytest.mark.fast


class StoredNote(hb.Entity):
    identifier = "r9ws-catalog"

    object_id = hb.field(hb.Identifier(max_chars=300)).pk()
    name = hb.field(hb.ShortText)


class ProposedNote(hb.Entity):
    identifier = StoredNote.identifier

    object_id = hb.field(hb.Identifier(max_chars=300)).pk()
    name = hb.field(hb.ShortText)
    extra = hb.field(hb.ShortText).optional()


@pytest.mark.slow
def test_reopened_catalog_preflight_leaves_stored_schema_intact(tmp_path):
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "db.sqlite")}}
    original = hb.HeavenBase("r9ws-catalog", detached=True, backends=backends)
    try:
        original.register(StoredNote, catalog="none")
        original.upsert(StoredNote, {"object_id": "a" * 256, "name": "Note"})
        previous = original.get("entity:r9ws-catalog", entity=hb.MetaSchema)
    finally:
        original.release()

    reopened = hb.HeavenBase("r9ws-catalog", detached=True, backends=backends)
    try:
        with pytest.raises(ValueError, match="256 characters"):
            reopened.register(ProposedNote, catalog="full", allow_replace=True)
        assert reopened.get("entity:r9ws-catalog", entity=hb.MetaSchema) == previous
        assert reopened.get("field:r9ws-catalog.extra", entity=hb.MetaSchema) is None
        assert reopened.entities.get(StoredNote.identifier) is not ProposedNote
    finally:
        reopened.release()


@pytest.mark.slow
def test_reopened_catalog_preflight_allows_an_additive_definition(tmp_path):
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "db.sqlite")}}
    original = hb.HeavenBase("r9ws-catalog", detached=True, backends=backends)
    try:
        original.register(StoredNote, catalog="none")
        original.upsert(StoredNote, {"object_id": "n1", "name": "Note"})
    finally:
        original.release()

    reopened = hb.HeavenBase("r9ws-catalog", detached=True, backends=backends)
    try:
        assert reopened.register(ProposedNote, catalog="full", allow_replace=True) is ProposedNote
        assert reopened.get("n1", entity=ProposedNote)["name"] == "Note"
        assert reopened.get("field:r9ws-catalog.extra", entity=hb.MetaSchema) is not None
        assert [row["target_id"] for row in reopened.query(hb.Catalog).execute().rows()] == ["n1"]
    finally:
        reopened.release()
