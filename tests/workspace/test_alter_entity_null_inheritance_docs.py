import heavenbase as hb
import pytest

pytestmark = pytest.mark.fast


def test_alter_entity_documents_null_inheritance():
    workspace = hb.HeavenBase("r9ws-alter", detached=True, backends={"main": {"type": "inmem"}})
    definition = {"entity_id": "r9ws-alter-note", "fields": {"name": {"type": "short-text"}}}
    try:
        workspace.register(definition, gram=False, catalog="identity")
        workspace.alter_entity(dict(definition, gram=None, catalog=None), confirm="r9ws-alter-note")
        assert workspace.entity_options["r9ws-alter-note"].gram is False
        assert workspace.entity_options["r9ws-alter-note"].catalog == "identity"
        with pytest.raises(TypeError, match="gram must be a boolean"):
            workspace.alter_entity(dict(definition, gram="false"), confirm="r9ws-alter-note")

        assert "JSON null" in (hb.HeavenBase.alter_entity.__doc__ or "")
    finally:
        workspace.release()
