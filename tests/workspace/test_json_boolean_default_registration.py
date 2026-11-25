import heavenbase as hb
import pytest

pytestmark = pytest.mark.fast


def article_type(visible):
    class Article(hb.Entity):
        identifier = "r9ws-json"

        name = hb.field(hb.ShortText)
        settings = hb.field(hb.Json).default({"nested": [{"visible": visible}]})
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))

    return Article


@pytest.mark.slow
def test_rejected_gram_change_keeps_json_boolean_default(tmp_path):
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "db.sqlite")}}
    stored = article_type(False)
    proposed = article_type(0)
    row = {"object_id": "n1", "name": "Note", "keywords": ["search query"]}

    original = hb.HeavenBase("r9ws-json", detached=True, backends=backends)
    try:
        original.register(stored)
        original.upsert(stored, row)
        assert original.get("n1", entity=stored)["settings"]["nested"][0]["visible"] is False
    finally:
        original.release()

    reopened = hb.HeavenBase("r9ws-json", detached=True, backends=backends)
    try:
        with pytest.warns(RuntimeWarning, match="differs from the stored schema"), pytest.raises(ValueError, match="gram=False conflicts"):
            reopened.register(proposed, gram=False, allow_replace=True)
        assert stored.identifier not in reopened.entities
        field = reopened.get("field:r9ws-json.settings", entity=hb.MetaSchema)
        assert field["meta"]["default"]["nested"][0]["visible"] is False
        reopened.register(stored)
        reopened.upsert(stored.identifier, dict(row, object_id="n2"))
        assert reopened.get("n2", entity=stored.identifier)["settings"]["nested"][0]["visible"] is False
    finally:
        reopened.release()


def test_failed_publication_restores_exact_metaschema_rows(monkeypatch):
    workspace = hb.HeavenBase("r9ws-restore", detached=True, backends={"main": {"type": "inmem"}})
    entity_id = "r9ws-restore-note"
    previous_definition = {"entity_id": entity_id, "fields": {"settings": {"type": "json", "default": {"visible": False}}}}
    proposed_definition = {
        "entity_id": entity_id,
        "fields": {
            "settings": {"type": "json", "default": {"visible": 0}},
            "extra": {"type": "short-text", "required": False},
        },
    }
    try:
        workspace.register(previous_definition)
        entity_row_id = f"entity:{entity_id}"
        field_row_id = f"field:{entity_id}.settings"
        extra_row_id = f"field:{entity_id}.extra"
        entity_row = workspace.get(entity_row_id, entity=hb.MetaSchema)
        workspace.upsert(hb.MetaSchema, dict(entity_row, desc="Manually curated description"))
        before = workspace.read_rows(hb.MetaSchema, [entity_row_id, field_row_id, extra_row_id])
        publish = workspace._metaschema.publish_schema_rows

        def fail_after_write(entity):
            publish(entity)
            raise OSError("publication failed")

        monkeypatch.setattr(workspace._metaschema, "publish_schema_rows", fail_after_write)
        with pytest.raises(OSError, match="publication failed"):
            workspace.register(proposed_definition, gram=False, allow_replace=True)
        after = workspace.read_rows(hb.MetaSchema, [entity_row_id, field_row_id, extra_row_id])
        assert after == before
        assert after[1]["meta"]["default"]["visible"] is False
        assert workspace.entity_options[entity_id].gram is True
    finally:
        workspace.release()
