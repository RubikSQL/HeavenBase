import pytest

import heavenbase as hb
from heavenbase.schema import EntitySchema, FieldSchema
from heavenbase.types import Identifier, logical_type_from_any


def _entity_id_schema_text(entity_schema):
    """Render only a schema's identifier for registration tests."""
    return entity_schema.entity_id


def _entity_name_schema_text(entity_schema):
    """Render only a schema's name for duplicate tests."""
    return entity_schema.name


def _sample_entity_schema_dict():
    return {
        "entity_id": "note",
        "name": "Note",
        "desc": "A note",
        "tags": ["demo"],
        "fields": {
            "object_id": {
                "name": "object_id",
                "dtype": Identifier().to_dict(),
                "pk": True,
                "required": True,
            },
            "title": {
                "name": "title",
                "dtype": hb.ShortText().to_dict(),
                "required": True,
                "desc": "Title text",
                "tags": ["search"],
                "examples": ["Hello"],
            },
            "score": {
                "name": "score",
                "dtype": hb.Float().to_dict(),
                "has_default": True,
                "default": 0.0,
            },
        },
    }


def test_field_schema_roundtrip():
    raw = {
        "name": "title",
        "dtype": hb.Vector[3].to_dict(),
        "required": True,
        "desc": "Embedding",
        "tags": ["vec"],
        "examples": [[0.1, 0.2, 0.3]],
    }
    field_schema = FieldSchema.from_dict(raw)
    assert field_schema.name == "title"
    assert field_schema.dtype.to_dict() == hb.Vector[3].to_dict()
    assert field_schema.required is True
    assert field_schema.to_dict() == {
        "name": "title",
        "dtype": hb.Vector[3].to_dict(),
        "pk": False,
        "required": True,
        "default": None,
        "has_default": False,
        "desc": "Embedding",
        "tags": ["vec"],
        "examples": [[0.1, 0.2, 0.3]],
    }


def test_field_schema_none_default_requires_has_default():
    field_schema = FieldSchema.from_dict(
        {
            "name": "payload",
            "dtype": hb.Json().to_dict(),
            "has_default": True,
            "default": None,
        }
    )
    assert field_schema.has_default is True
    assert field_schema.default is None
    roundtrip = FieldSchema.from_dict(field_schema.to_dict())
    assert roundtrip.has_default is True
    assert roundtrip.default is None


def test_field_spec_compiles_an_isolated_schema_snapshot():
    spec = hb.field(hb.ShortText, desc="Title text", tags=["search"], examples=["Desk"]).required().default("Untitled")

    field_schema = spec.to_schema("title")
    spec.tags("later").example("Chair")

    assert field_schema.dtype is not spec.dtype
    assert field_schema.required is True
    assert field_schema.default == "Untitled"
    assert field_schema.tags == ["search"]
    assert field_schema.examples == ["Desk"]


def test_reused_field_declarations_are_isolated_per_entity():
    shared = hb.field(hb.Vector[2], tags=["vector"]).default([0.0, 0.0]).store(to="vec", strategy=hb.VectorIndex).query_compute(lambda value: value)

    class FirstVector(hb.Entity):
        identifier = "first-vector-protocol"

        emb = shared

    class SecondVector(hb.Entity):
        identifier = "second-vector-protocol"

        emb = shared

    FirstVector._hb_specs["emb"].tags("first")
    FirstVector._hb_specs["emb"].default_value[0] = 1.0
    FirstVector._hb_specs["emb"].stores[0].params["metric"] = "cosine"

    assert FirstVector._hb_specs["emb"] is not SecondVector._hb_specs["emb"]
    assert FirstVector.schema().get("emb").dtype is not FirstVector._hb_specs["emb"].dtype
    assert SecondVector._hb_specs["emb"].tag_values == ["vector"]
    assert SecondVector._hb_specs["emb"].default_value == [0.0, 0.0]
    assert SecondVector._hb_specs["emb"].stores[0].params == {}


def test_entity_schema_roundtrip():
    raw = _sample_entity_schema_dict()
    schema = EntitySchema.from_dict(raw)
    schema.validate()
    assert schema.entity_id == "note"
    assert schema.name == "Note"
    assert set(schema.fields) == {"object_id", "title", "score"}
    assert schema.pk_name == "object_id"
    assert EntitySchema.from_dict(schema.to_dict()).to_dict() == schema.to_dict()


def test_entity_schema_to_str_defaults_to_llm_text():
    schema = EntitySchema.from_dict(_sample_entity_schema_dict())

    assert schema.to_str() == str(schema)
    assert schema.to_str() == "\n".join(
        [
            "Entity schema: Note (note)",
            "Description: A note",
            "Tags: demo",
            "Use field names exactly.",
            "",
            "Fields:",
            "- object_id: identifier(max_chars=63) (primary key; required)",
            "- title: short-text(max_chars=255) (required)",
            "  Description: Title text",
            "  Tags: search",
            '  Examples: ["Hello"]',
            "- score: float (optional; default=0.0)",
        ]
    )


def test_schema_text_registry_is_extension_author_surface():
    schema = EntitySchema.from_dict(_sample_entity_schema_dict())

    assert "llm" in hb.ext.registered_schema_texts()
    assert hb.ext.resolve_schema_text("llm") is hb.ext.llm_schema_text
    hb.ext.register_schema_text("test-entity-id-only", _entity_id_schema_text, replace=True)
    assert schema.to_str(format="test-entity-id-only") == "note"
    with pytest.raises(ValueError, match="already registered"):
        hb.ext.register_schema_text("test-entity-id-only", _entity_name_schema_text)
    with pytest.raises(ValueError, match="schema text format"):
        schema.to_str(format="missing-schema-text")


def test_entity_schema_from_entity_class():
    class Note(hb.Entity):
        title = hb.field(hb.ShortText).required()

    compiled = Note.schema()
    compiled.validate()
    restored = EntitySchema.from_dict(compiled.to_dict())
    restored.validate()
    assert restored.to_dict() == compiled.to_dict()


def test_entity_schema_validate_rejects_bad_pk():
    schema = EntitySchema(
        entity_id="bad",
        name="Bad",
        fields={
            "object_id": FieldSchema("object_id", Identifier(), pk=True, required=True),
            "alt_id": FieldSchema("alt_id", hb.ShortText(), pk=True),
        },
    )
    with pytest.raises(ValueError, match="multiple primary keys"):
        schema.validate()


def test_entity_schema_validate_requires_object_id():
    schema = EntitySchema(
        entity_id="bad",
        name="Bad",
        fields={"title": FieldSchema("title", hb.ShortText(), required=True)},
    )
    with pytest.raises(ValueError, match="must define object_id"):
        schema.validate()


def test_entity_schema_from_dict_rejects_key_name_mismatch():
    raw = _sample_entity_schema_dict()
    raw["fields"]["title"]["name"] = "other"
    with pytest.raises(ValueError, match="does not match field name"):
        EntitySchema.from_dict(raw)


def test_field_schema_from_dict_accepts_type_shorthand():
    field_schema = FieldSchema.from_dict({"name": "title", "dtype": "ShortText"})
    assert field_schema.dtype.identifier == "short-text"
    assert logical_type_from_any(field_schema.to_dict()["dtype"]).identifier == "short-text"
