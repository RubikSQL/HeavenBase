from datetime import date, datetime, timedelta, timezone

import pytest

import heavenbase as hb
from heavenbase.execution.handlers.filters import normalize_filter_value
from heavenbase.types import logical_type_from_any

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry


class Slug(hb.LogicalType):
    """Durable custom logical type used by registry contract tests."""

    identifier = "test-slug"

    def validate(self, value):
        """Normalize a value as a lower-hyphen slug."""
        return str(value).strip().lower().replace(" ", "-")


class LimitedText(hb.LogicalType):
    """Durable parameterized logical type used by descriptor tests."""

    identifier = "test-limited-text"

    def __init__(self, limit=8):
        self.limit = int(limit)

    @classmethod
    def from_dict(cls, spec, *, resolver=None):
        """Restore a limited-text descriptor."""
        return cls(spec.get("limit", spec.get("value", 8)))

    def validate(self, value):
        """Validate a value against the configured character limit."""
        text = str(value)
        if len(text) > self.limit:
            raise ValueError(f"Text exceeds {self.limit} characters")
        return text

    def to_dict(self):
        """Return the configured logical-type descriptor."""
        return {"type": self.identifier, "limit": self.limit}

    def clone(self):
        """Return an independent value with the same limit."""
        return type(self)(self.limit)


class JsonOnlyBackend(hb.ext.Backend):
    """Importable Backend fixture with an explicit JSON indexability claim."""

    identifier = "json-only-backend-test"


class Base64InMemBackend(hb.backends.InMemBackend):
    """Importable InMem fixture with an explicit base64 storage claim."""

    identifier = "base64-inmem-backend-test"


def _db(path, name: str) -> str:
    return hb.utils.path_to_file_uri(path / name)


@pytest.fixture(autouse=True)
def _isolate_workspaces():
    before = set(WORKSPACES.keys())
    yield
    for wid in set(WORKSPACES.keys()) - before:
        WORKSPACES.pop(wid, None)


def test_logical_type_tags_replace_family_selectors():
    modules = hb.DEFAULT_CONTEXT.modules()
    assert hb.LogicalType.load("short-text") is hb.ShortText
    assert modules.tags("logical_type", "vector").raw == {"filter": True, "near": True, "vector": True}
    assert modules.tags("logical_type", "short-text").raw["filter-domains"] == ("text",)
    assert not hasattr(hb.LogicalType, "types_for")
    assert not hasattr(hb.LogicalType, "identifiers_for")
    assert not hasattr(hb.LogicalType, "families")
    assert not hasattr(hb.LogicalType, "in_family")
    assert not hasattr(hb.ShortText, "is_indexable")
    assert hb.backends.InMemBackend.indexable(hb.ShortText)
    assert hb.backends.InMemBackend.indexable(hb.Integer())
    assert not hb.backends.InMemBackend.indexable(hb.Json)
    assert not hb.backends.InMemBackend.indexable(hb.Vector)


def test_custom_logical_type_registers_through_class_contract():
    """A custom type should own values while the registry owns discovery."""

    assert Slug.register(tags={"test-text": True}, aliases=("slug",)) is Slug
    assert hb.LogicalType.load("slug") is Slug
    assert hb.LogicalType.from_any("slug").validate("Hello World") == "hello-world"
    assert hb.LogicalType.spec_of(Slug) == {"type": "test-slug"}
    assert hb.DEFAULT_CONTEXT.modules().tags("logical_type", "slug").raw == {"test-text": True}


def test_custom_parameterized_logical_type_owns_descriptor_fields():
    """Parameterized types should parse their own descriptor parameters."""

    LimitedText.register()

    dtype = hb.LogicalType.from_any({"type": "test-limited-text", "limit": 4})
    shorthand = hb.LogicalType.from_any("test-limited-text[3]")

    assert dtype.to_dict() == {"type": "test-limited-text", "limit": 4}
    assert dtype.clone().to_dict() == dtype.to_dict()
    assert shorthand.to_dict() == {"type": "test-limited-text", "limit": 3}
    with pytest.raises(ValueError, match="exceeds"):
        dtype.validate("12345")


@pytest.mark.parametrize(
    "dtype",
    [
        hb.Vector[3],
        hb.Categorical(["todo", "done"]),
    ],
)
def test_mapping_selector_preserves_configured_logical_type_instance(dtype):
    """Instance selectors should retain their configured type parameters."""
    restored = logical_type_from_any({"type": dtype})

    assert restored is dtype
    assert restored.to_dict() == dtype.to_dict()


def test_mapping_class_selector_does_not_consult_the_default_module_catalog(monkeypatch):
    """An explicit implementation class needs no implicit resolver lookup."""

    def fail(*args, **kwargs):
        raise AssertionError("explicit class selector consulted the module catalog")

    monkeypatch.setattr(hb.Float, "load", fail)

    assert isinstance(logical_type_from_any({"type": hb.Float}), hb.Float)


def test_plain_logical_type_rejects_unowned_descriptor_fields():
    """The base parser should reject parameters a scalar type does not own."""
    with pytest.raises(ValueError, match="descriptor key"):
        hb.LogicalType.from_any({"type": "float", "dimension": 3})


def test_handler_context_indexable_uses_backend_policy():
    modules = hb.DEFAULT_CONTEXT.modules()
    JsonOnlyBackend.register(tags={"indexable-types": ["json"]}, resolver=modules)
    try:
        ctx = hb.ext.HandlerContext("custom", "builder-only", hb.InlineColumn.identifier, backend_instance=JsonOnlyBackend("custom"))
        unresolved = hb.ext.HandlerContext("custom", "builder-only", hb.InlineColumn.identifier)

        assert ctx.indexable(hb.Json)
        assert not ctx.indexable(hb.ShortText)
        with pytest.raises(KeyError, match="builder-only"):
            unresolved.indexable(hb.ShortText)
    finally:
        modules.unregister("backend_type", JsonOnlyBackend.identifier)


def test_filter_value_normalization_uses_logical_type_contract():
    assert normalize_filter_value(hb.Integer, "eq", "3") == 3
    assert normalize_filter_value(hb.Integer(), "in", ["1", 2]) == [1, 2]
    with pytest.raises(TypeError, match="Expected LogicalType"):
        normalize_filter_value(object(), "eq", "x")


@pytest.mark.parametrize("dtype", [hb.ShortText(), hb.LongText(), hb.Identifier(), hb.Date()], ids=["short-text", "long-text", "identifier", "date"])
def test_text_order_filters_reject_none(dtype):
    """An order filter given None raises instead of comparing with the empty string; eq None still tests for a missing value."""
    for op in ("lt", "lte", "gt", "gte"):
        with pytest.raises(ValueError, match=f"'{op}' filter needs a value; None only matches a missing value through eq, ne or in"):
            normalize_filter_value(dtype, op, None)
    assert normalize_filter_value(dtype, "eq", None) is None
    assert normalize_filter_value(dtype, "in", [None]) == [None]


@pytest.mark.parametrize(
    ("dtype", "sample", "canonical"),
    [
        (hb.Integer(), "3", 3),
        (hb.Duration(), "3", 3),
        (hb.Float(), "1.5", 1.5),
        (hb.Boolean(), "yes", True),
        (hb.Categorical([1, 2], base="integer"), "2", 2),
        (hb.Categorical(["todo", "done"]), "todo", "todo"),
        (hb.Date(), date(2026, 5, 14), "2026-05-14"),
        (hb.ShortText(), 42, "42"),
        (hb.Timestamp(), 2500, 2500),
        (hb.Json(), '{"k": 1}', {"k": 1}),
    ],
    ids=["integer", "duration", "float", "boolean", "categorical-integer", "categorical-string", "date", "short-text", "timestamp", "json"],
)
def test_none_exact_and_membership_filters_test_for_a_missing_value(dtype, sample, canonical):
    """`eq`/`ne`/`in` keep None as a missing-value test on every logical type; order filters reject it."""
    assert normalize_filter_value(dtype, "eq", None) is None
    assert normalize_filter_value(dtype, "ne", None) is None
    assert normalize_filter_value(dtype, "in", [None, sample]) == [None, canonical]
    assert normalize_filter_value(dtype, "eq", sample) == canonical
    for op in ("lt", "lte", "gt", "gte"):
        if isinstance(dtype, hb.Json) or (isinstance(dtype, hb.Categorical) and dtype.base == "string"):
            continue  # no order filters on these types
        with pytest.raises(ValueError, match=f"'{op}' filter needs a value; None only matches a missing"):
            normalize_filter_value(dtype, op, None)


@pytest.mark.parametrize(
    ("dtype", "sample", "canonical"),
    [
        (hb.Integer(), "3", 3),
        (hb.Float(), "1.5", 1.5),
        (hb.Boolean(), "yes", True),
        (hb.Categorical([1, 2], base="integer"), "2", 2),
        (hb.Timestamp(), 2500, 2500),
    ],
    ids=["integer", "float", "boolean", "categorical-integer", "timestamp"],
)
def test_scalar_in_filters_need_a_collection(dtype, sample, canonical):
    """`in` takes a collection; a scalar or a string raises TypeError instead of failing or splitting into characters."""
    for operand in (sample, canonical, "13", b"13"):
        with pytest.raises(TypeError, match="in filter expects a collection of values"):
            normalize_filter_value(dtype, "in", operand)
    for operand in ([sample], (sample,), {sample}, (item for item in [sample])):
        assert normalize_filter_value(dtype, "in", operand) == [canonical]


def test_hyperg_column_none_filters_test_for_a_missing_value():
    """A HyperG column's exact/membership filters keep None, whichever dtype path normalizes them."""
    relations = hb.HyperG["slot" : hb.ShortText, "weight" : hb.Integer, "blob" : {"type": hb.Artifact, "filterable": True}]
    for name in ("slot", "weight", "blob"):
        column = relations.get(name)
        assert column.filter_value("eq", None) is None
        assert column.filter_value("in", [None]) == [None]
    with pytest.raises(ValueError, match="'gt' filter needs a value"):
        relations.get("weight").filter_value("gt", None)


def test_categorical_validate_and_roundtrip():
    """Categorical should enforce allowed values and serialize its params."""
    status = hb.Categorical(["todo", "doing", "done"])
    priority = hb.Categorical([1, 2, 3], base="integer")
    status_shorthand = hb.Categorical["todo", "doing", "done"]
    priority_shorthand = hb.Categorical[1, 2, 3]

    assert status.validate("todo") == "todo"
    assert status.validate(None) is None
    assert priority.validate("2") == 2
    assert priority.validate(2.0) == 2
    assert status_shorthand.to_dict() == status.to_dict()
    assert priority_shorthand.to_dict() == priority.to_dict()
    assert status.clone().to_dict() == {
        "type": "categorical",
        "values": ["todo", "doing", "done"],
        "base": "string",
    }
    assert logical_type_from_any(status.to_dict()).to_dict() == status.to_dict()

    with pytest.raises(ValueError, match="not one of"):
        status.validate("blocked")
    with pytest.raises(TypeError, match="ints"):
        hb.Categorical(["1"], base="integer")
    with pytest.raises(ValueError, match="not an int"):
        priority.validate(1.9)
    with pytest.raises(ValueError, match="String Categorical"):
        status.filter_value("gt", "todo")
    with pytest.raises(ValueError, match="Integer Categorical"):
        priority.filter_value("contains", "2")


def test_hyperg_schema_validate_and_roundtrip():
    """HyperG should carry a compact mini-row schema."""
    relations = hb.HyperG[
        "subject" : hb.Identifier,
        "relation" : hb.Categorical["cites", "mentions"],
        "object" : hb.Identifier,
        "weight" : hb.Float,
        "meta" : {"type": hb.Json, "filterable": False, "required": False},
    ]

    tag_dtype = hb.HyperG["slot", "value", "topic"]
    assert tag_dtype.field_names == ("slot", "value", "topic")
    assert tag_dtype.validate([("intent", "refund", "support")]) == [{"slot": "intent", "value": "refund", "topic": "support"}]
    assert relations.field_names == ("subject", "relation", "object", "weight", "meta")
    assert relations.validate([{"subject": "doc:1", "relation": "cites", "object": "doc:2", "weight": "0.7"}]) == [
        {"subject": "doc:1", "relation": "cites", "object": "doc:2", "weight": 0.7}
    ]
    assert relations.filter_value("exists", {"relation": {"$in": ["cites"]}, "weight": {"$gte": "0.5"}}) == {
        "relation": {"in": ["cites"]},
        "weight": {"gte": 0.5},
    }
    assert logical_type_from_any(relations.to_dict()).to_dict() == relations.to_dict()

    with pytest.raises(ValueError, match="unknown fields"):
        relations.validate([{"subject": "doc:1", "relation": "cites", "object": "doc:2", "unknown": "x"}])
    with pytest.raises(ValueError, match="not filterable"):
        relations.filter_value("exists", {"meta": {"eq": {"source": "demo"}}})


def test_identifier_entity_scope_roundtrip():
    """Identifier[Entity] carries a target-entity scope as metadata; the value stays a bare id."""
    plain = hb.Identifier()
    scoped = hb.Identifier["author-entity"]  # a target entity_id string

    assert plain.target_entity is None
    assert scoped.target_entity == "author-entity"
    assert scoped.validate("a1") == "a1"  # validate is unchanged: a bare id string
    assert plain.to_dict() == {"type": "identifier", "max_chars": 63}
    assert scoped.to_dict() == {"type": "identifier", "max_chars": 63, "target_entity": "author-entity"}
    assert logical_type_from_any(scoped.to_dict()).target_entity == "author-entity"  # round-trips through the registry
    assert logical_type_from_any(plain.to_dict()).target_entity is None
    assert scoped.clone().target_entity == "author-entity"
    with pytest.raises(AttributeError):
        scoped.target_entity = "other-entity"

    edge = hb.HyperG["cited" : hb.Identifier["doc-entity"]]  # usable as a HyperG endpoint column
    assert edge.field_names == ("cited",)
    assert edge.to_dict()["fields"][0]["dtype"] == {"type": "identifier", "max_chars": 63, "target_entity": "doc-entity"}


@pytest.mark.parametrize(
    ("dtype_cls", "default_max_chars"),
    [
        (hb.Identifier, 63),
        (hb.ShortText, 255),
        (hb.MediumText, 4095),
        (hb.LongText, 65535),
    ],
)
def test_bounded_text_descriptor_state_is_validated_immutable_and_serializable(dtype_cls, default_max_chars):
    """Text limits should be durable logical-type state, not ambient policy."""
    default = dtype_cls()
    bounded = dtype_cls(max_chars=7)

    assert default.max_chars == default_max_chars
    assert default.to_dict() == {"type": dtype_cls.identifier, "max_chars": default_max_chars}
    assert bounded.to_dict() == {"type": dtype_cls.identifier, "max_chars": 7}
    assert logical_type_from_any(bounded.to_dict()).to_dict() == bounded.to_dict()
    assert bounded.clone().to_dict() == bounded.to_dict()
    assert bounded.validate("1234567") == "1234567"
    with pytest.raises(ValueError, match="exceeds 7"):
        bounded.validate("12345678")
    with pytest.raises(AttributeError):
        bounded.max_chars = 8


@pytest.mark.parametrize("invalid", [True, 1.5, "7"])
def test_bounded_text_rejects_non_integer_limits(invalid):
    """Text descriptor limits should reject coercive or boolean inputs."""
    with pytest.raises(TypeError, match="must be an integer"):
        hb.ShortText(max_chars=invalid)


@pytest.mark.parametrize("invalid", [0, -1])
def test_bounded_text_rejects_non_positive_limits(invalid):
    """Text descriptor limits should be strictly positive."""
    with pytest.raises(ValueError, match="must be positive"):
        hb.ShortText(max_chars=invalid)


def test_bounded_text_constraints_roundtrip_inside_parameterized_types():
    """Nested logical descriptors should retain every bounded-text constraint."""
    tags = hb.Array(hb.ShortText(max_chars=12))
    relation = hb.HyperG[
        "subject" : hb.Identifier("document", max_chars=17),
        "summary" : hb.MediumText(max_chars=31),
    ]

    restored_tags = logical_type_from_any(tags.to_dict())
    restored_relation = logical_type_from_any(relation.to_dict())

    assert restored_tags.to_dict() == tags.to_dict()
    assert restored_tags.item_type.max_chars == 12
    assert restored_relation.to_dict() == relation.to_dict()
    assert restored_relation.fields[0].dtype.max_chars == 17
    assert restored_relation.fields[0].dtype.target_entity == "document"
    assert restored_relation.fields[1].dtype.max_chars == 31


def test_bounded_text_defaults_do_not_follow_config_authorities(tmp_path):
    """Independent config values should not reinterpret logical schemas."""
    from heavenbase.utils.config import ConfigManager

    first = ConfigManager(default={"heavenbase": {"types": {"short-text": {"max_chars": 5}}}}, root=str(tmp_path / "first"), setup=False)
    second = ConfigManager(default={"heavenbase": {"types": {"short-text": {"max_chars": 9}}}}, root=str(tmp_path / "second"), setup=False)

    assert first.get("heavenbase.types.short-text.max_chars") == 5
    assert second.get("heavenbase.types.short-text.max_chars") == 9
    assert hb.ShortText().to_dict() == {"type": "short-text", "max_chars": 255}
    assert hb.Timestamp.default_min == 0
    assert hb.Timestamp.default_max == 253_402_300_799_000


def test_timestamp_and_date_validate_and_roundtrip():
    """Temporal logical types should normalize user-facing values to backend scalars."""
    instant = datetime(1970, 1, 1, 0, 0, 2, 500000, tzinfo=timezone.utc)
    utc_midnight_crossing = datetime(2026, 5, 14, 0, 30, tzinfo=timezone(timedelta(hours=2)))

    assert hb.Timestamp().validate(instant) == 2500
    assert hb.Timestamp(unit="s").validate(instant) == 2
    assert hb.Timestamp(unit="us").validate("1970-01-01T00:00:00.000003+00:00") == 3
    assert hb.Timestamp().validate(utc_midnight_crossing) == int(utc_midnight_crossing.astimezone(timezone.utc).timestamp() * 1000)
    assert hb.Timestamp().validate(1.9) == 1
    assert hb.Timestamp().validate(date(2026, 5, 14)) == hb.Timestamp().validate(datetime(2026, 5, 14))
    assert hb.Timestamp().validate("2026-05-14") == hb.Timestamp().validate(date(2026, 5, 14))
    with pytest.raises(ValueError, match="Timestamp requires a value"):
        hb.Timestamp().validate(None)
    assert hb.Timestamp().filter_value("eq", None) is None  # a query for a missing value, as for a text field
    assert hb.Timestamp().filter_value("ne", None) is None
    assert hb.Timestamp().filter_value("in", [None, 2500]) == [None, 2500]
    with pytest.raises(ValueError, match="'gt' filter needs a value"):
        hb.Timestamp().filter_value("gt", None)
    with pytest.raises(ValueError, match=r"^Timestamp expects int, float, datetime, date, or ISO string, got list$"):
        hb.Timestamp().validate([1])
    assert logical_type_from_any(hb.Timestamp(unit="s").to_dict()).to_dict() == {
        "type": "timestamp",
        "unit": "s",
    }
    with pytest.raises(AttributeError):
        hb.Timestamp().unit = "s"

    assert hb.Date().validate("2026-05-14") == "2026-05-14"
    assert hb.Date().validate(date(2026, 5, 14)) == "2026-05-14"
    assert hb.Date().validate(datetime(2026, 5, 14, 12, 30)) == "2026-05-14"
    assert hb.Date().validate(utc_midnight_crossing) == "2026-05-13"
    assert logical_type_from_any(hb.Date().to_dict()).to_dict() == {"type": "date"}

    with pytest.raises(ValueError, match="Invalid Timestamp"):
        hb.Timestamp().validate("not-a-time")
    with pytest.raises(ValueError, match="epoch"):
        hb.Date().validate(1778720690)


def test_timestamp_bounds_clip_and_roundtrip():
    """Timestamp should clip values to configured or instance min/max bounds."""
    assert hb.Timestamp.default_max == 253_402_300_799_000
    assert hb.Timestamp.default_min == 0
    assert hb.Timestamp().validate(0) == 0
    bounded = hb.Timestamp(max=100, min=10)
    assert bounded.validate(0) == 10
    assert bounded.validate(50) == 50
    assert bounded.validate(200) == 100
    assert bounded.filter_value("gt", 500) == 100
    assert logical_type_from_any(hb.Timestamp(max=100, min=10).to_dict()).to_dict() == {
        "type": "timestamp",
        "unit": "ms",
        "max": 100,
        "min": 10,
    }
    string_bounds = hb.Timestamp(max="100", min="10")
    assert string_bounds.validate(50) == 50
    with pytest.raises(ValueError, match="cannot exceed max"):
        hb.Timestamp(max=10, min=100)


def test_duration_validate_and_roundtrip():
    """Duration should coerce non-negative millisecond intervals."""
    from heavenbase.types.storage import stores_as_integer

    assert hb.Duration().validate(500) == 500
    assert hb.Duration().validate("12") == 12
    assert hb.Duration().filter_value("gt", "100") == 100
    assert logical_type_from_any(hb.Duration().to_dict()).to_dict() == {"type": "duration"}
    assert logical_type_from_any({"type": "interval"}).to_dict() == {"type": "duration"}
    assert stores_as_integer(hb.Duration()) is True

    with pytest.raises(ValueError, match="negative"):
        hb.Duration().validate(-1)
    with pytest.raises(ValueError, match="bool"):
        hb.Duration().validate(True)


def test_text_storage_affinity_and_filter_operands_are_canonical():
    """Text affinity and query operands should follow concrete logical types."""
    from heavenbase.execution.handlers.filters import normalize_filter_value
    from heavenbase.types.storage import stores_as_text

    assert stores_as_text(hb.Identifier) is True
    assert stores_as_text(hb.ShortText()) is True
    assert stores_as_text(hb.MediumText) is True
    assert stores_as_text(hb.LongText()) is True
    assert stores_as_text(hb.Date) is True
    assert stores_as_text(hb.Categorical) is False
    assert stores_as_text(hb.Categorical(["todo", "done"])) is True
    assert stores_as_text(hb.Categorical([1, 2], base="integer")) is False
    assert stores_as_text(hb.Integer) is False

    assert normalize_filter_value(hb.ShortText, "eq", 42) == "42"
    assert normalize_filter_value(hb.ShortText, "eq", None) is None
    assert normalize_filter_value(hb.ShortText, "in", [42, None]) == ["42", None]
    assert normalize_filter_value(hb.ShortText, "contains", 42) == "42"
    assert normalize_filter_value(hb.Array[hb.ShortText], "eq", (42, None)) == ["42", ""]
    assert normalize_filter_value(hb.Array[hb.ShortText], "array_contains", 42) == "42"

    with pytest.raises(TypeError, match="in filter"):
        normalize_filter_value(hb.ShortText, "in", "42")
    with pytest.raises(ValueError, match="text filter"):
        hb.Categorical([1, 2], base="integer").filter_value("starts_with", "1")


def test_json_storage_and_filter_contracts_are_separate():
    """Json storage stays tagged while query operands use strict JSON values."""
    dtype = hb.Json()
    extended = {
        "a": 1,
        "items": (2, 3),
        "tags": {"x", "y"},
        "large": 2**54,
        "type": str,
        "callable": test_json_storage_and_filter_contracts_are_separate,
    }
    normalised = dtype.validate(extended)
    storage = dtype.encode(normalised)
    assert isinstance(storage, str)
    restored = dtype.decode(storage)
    assert normalised is extended
    assert dtype.validate(dtype.decode(dtype.encode(dtype.validate('{"z": 1}')))) == {"z": 1}
    assert restored["items"] == (2, 3)
    assert restored["tags"] == {"x", "y"}
    assert dtype.decode(dtype.encode({"large": 2**54})) == {"large": 2**54}
    assert restored["type"] == "builtins.str"
    assert restored["callable"].endswith(".test_json_storage_and_filter_contracts_are_separate")
    assert dtype.decode(None) is None

    assert dtype.filter_value("eq", {"nested": (1, 2)}) == {"nested": [1, 2]}
    assert dtype.filter_value("eq", '{"nested": [1, 2]}') == {"nested": [1, 2]}
    tagged_shape = {"__obj_type__": "tuple", "__obj_data__": [1, 2]}
    assert dtype.decode(dtype.encode(tagged_shape)) == (1, 2)
    assert dtype.filter_value("eq", tagged_shape) == tagged_shape

    with pytest.raises(TypeError, match="keys must be strings"):
        dtype.filter_value("eq", {1: "value"})
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError, match="finite"):
            dtype.filter_value("get", {"path": "score", "eq": value})
    with pytest.raises(TypeError, match="Json values"):
        dtype.filter_value("eq", {"items": {1, 2}})
    with pytest.raises(TypeError, match="objects or arrays"):
        dtype.filter_value("eq", '"scalar"')


def test_artifact_bytes_roundtrip():
    """Artifact should normalise to bytes and roundtrip through encode/decode."""
    from heavenbase.utils.serialize import dumps_b64

    dtype = hb.Artifact()
    assert dtype.validate(None) == b""
    assert dtype.validate("hello") == b"hello"
    payload = b"\x00\xff"
    assert dtype.encode(payload) == payload
    assert dtype.decode(payload) == payload
    storage = dumps_b64(payload)
    assert dtype.decode(storage) == payload
    bytearray_payload = bytearray(b"\x00\xff")
    decoded = dtype.decode(bytearray_payload)
    assert decoded == b"\x00\xff"
    assert type(decoded) is bytes


def test_artifact_inmem_roundtrip():
    """In-memory backends should store Artifact payloads as raw bytes."""
    ws = hb.HeavenBase("artifact-inmem-test", backends={"main": hb.backends.InMemBackend("main")})

    class BlobDoc(hb.Entity):
        identifier = "artifact-inmem-doc"

        payload = hb.field(hb.Artifact)

    ws.register(BlobDoc)
    payload = b"\x00abc\xff"
    ws.upsert(BlobDoc, {"object_id": "b1", "payload": payload})
    assert ws.get("b1", BlobDoc)["payload"] == payload
    backend_row = ws.backends.get("main").scan("artifact-inmem-doc")[0]
    assert backend_row["payload"] == payload


def test_artifact_text_storage_encoding():
    """Backends claiming binary-storage=base64 should store base64 text, not raw bytes."""
    from heavenbase.utils.serialize import dumps_b64

    modules = hb.DEFAULT_CONTEXT.modules()
    inmem = modules.inspect("backend_type", "inmem")[0]["record"]["meta"]["tags"]
    Base64InMemBackend.register(tags={**inmem, "binary-storage": "base64"}, resolver=modules)
    ws = hb.HeavenBase("artifact-text-encode", backends={"main": Base64InMemBackend("main")})
    backend = ws.backends.get("main")

    class BlobDoc(hb.Entity):
        identifier = "artifact-text-doc"

        payload = hb.field(hb.Artifact)

    try:
        ws.register(BlobDoc)
        payload = b"\x00abc\xff"
        ws.upsert(BlobDoc, {"object_id": "b1", "payload": payload})
        assert ws.get("b1", BlobDoc)["payload"] == payload
        backend_row = backend.scan("artifact-text-doc")[0]
        assert backend_row["payload"] == dumps_b64(payload)
    finally:
        ws.drop()
        modules.unregister("backend_type", Base64InMemBackend.identifier)


def test_query_does_not_double_decode_json(monkeypatch):
    """Query results must decode storage once; a second decode on canonical values must fail."""
    ws = hb.HeavenBase("query-json-decode-test", backends={"main": hb.backends.InMemBackend("main")})

    class JsonDoc(hb.Entity):
        identifier = "query-json-decode-doc"

        meta = hb.field(hb.Json)

    ws.register(JsonDoc)
    original_decode = hb.Json.decode

    def strict_decode(self, value):
        if isinstance(value, dict):
            raise RuntimeError("decode called on canonical Json dict")
        return original_decode(self, value)

    monkeypatch.setattr(hb.Json, "decode", strict_decode)

    ws.upsert(JsonDoc, {"object_id": "d1", "meta": {"a": 1, "b": [2]}})
    row = ws.query(JsonDoc).execute().rows()[0]
    assert row["meta"] == {"a": 1, "b": [2]}
    assert isinstance(row["meta"], dict)
    assert ws.get("d1", JsonDoc)["meta"] == {"a": 1, "b": [2]}


def test_boolean_string_and_null_parsing():
    """Boolean should parse common string and null aliases."""
    dtype = hb.Boolean()
    assert dtype.validate("false") is False
    assert dtype.validate("TRUE") is True
    assert dtype.validate("null") is None
    assert dtype.validate(0) is False
    assert dtype.validate(1.0) is True
    with pytest.raises(ValueError, match="Cannot coerce"):
        dtype.validate("maybe")


def test_long_text_enforces_max_chars():
    """LongText should reject payloads above its descriptor-owned limit."""
    limit = hb.LongText().max_chars
    hb.LongText().validate("x" * limit)
    with pytest.raises(ValueError, match="exceeds"):
        hb.LongText().validate("x" * (limit + 1))


def test_logical_type_identifier_registry_aliases():
    """Registered logical types should resolve by stable identifier aliases."""
    from heavenbase.types import get_logical_type

    assert get_logical_type("short-text") is hb.ShortText
    assert get_logical_type("long-text") is hb.LongText
    assert hb.Json.identifier == "json"


def test_hyperg_requires_explicit_fields():
    """HyperG should reject construction without a field schema."""
    with pytest.raises(TypeError):
        hb.HyperG()


def test_entity_from_schema_reconstructs_new_logical_types():
    """JSON entity definitions should preserve parametric logical type metadata."""
    Task = hb.Entity.from_schema(
        {
            "entity_id": "logical-json-task",
            "fields": {
                "status": {"type": {"type": "categorical", "values": ["todo", "done"]}},
                "priority": {"type": {"type": "categorical", "values": [1, 2, 3], "base": "integer"}},
                "created_at": {"type": {"type": "timestamp", "unit": "us"}},
                "due_date": {"type": {"type": "date"}},
                "relations": {
                    "type": {
                        "type": "hyper-g",
                        "fields": [
                            {"name": "subject", "dtype": "identifier"},
                            {"name": "relation", "dtype": {"type": "categorical", "values": ["cites", "mentions"]}},
                            {"name": "object", "dtype": "identifier"},
                        ],
                    }
                },
            },
        }
    )
    schema = Task.schema()

    assert schema.get("status").dtype.values == ("todo", "done")
    assert schema.get("priority").dtype.base == "integer"
    assert schema.get("created_at").dtype.unit == "us"
    assert type(schema.get("due_date").dtype).identifier == "date"
    assert schema.get("relations").dtype.field_names == ("subject", "relation", "object")


def test_workspace_schema_signature_includes_bounded_text_constraints():
    """Changing a text limit should be a structural schema change."""
    workspace = hb.HeavenBase("bounded-text-signature", detached=True, backends={"main": {"type": "inmem"}})

    class NarrowNote(hb.Entity):
        identifier = "bounded-text-signature-note"

        title = hb.field(hb.ShortText(max_chars=17))

    class WideNote(hb.Entity):
        identifier = "bounded-text-signature-note"

        title = hb.field(hb.ShortText(max_chars=29))

    try:
        workspace.register(NarrowNote)
        with pytest.raises(ValueError, match="different structure"):
            workspace.register(WideNote)
    finally:
        workspace.release()


def test_field_choices_shortcut_sets_categorical_dtype():
    """FieldSpec.choices should provide a compact Categorical declaration."""

    class ChoiceTask(hb.Entity):
        identifier = "choice-task"

        status = hb.field(hb.ShortText).choices("todo", "doing", "done")
        priority = hb.field(hb.Integer).choices(1, 2, 3)

    assert ChoiceTask.schema().get("status").dtype.to_dict() == {
        "type": "categorical",
        "values": ["todo", "doing", "done"],
        "base": "string",
    }
    assert ChoiceTask.schema().get("priority").dtype.to_dict() == {
        "type": "categorical",
        "values": [1, 2, 3],
        "base": "integer",
    }


def test_array_constructor_coerces_item_type():
    """Array() should accept logical type classes the same way Array[...] does."""
    assert hb.Array(hb.Integer).validate(["1", 2]) == [1, 2]
    assert hb.Array[hb.Integer].validate(["1", 2]) == [1, 2]


def test_sql_column_mappings_for_new_logical_types():
    """SQL column mapping should use scalar storage for all new logical types."""
    from heavenbase.database.types import DatabaseBigIntegerType, DatabaseBlobType, DatabaseIntegerType, DatabaseTextType
    from heavenbase.backends.sql.base import _sa_col

    assert isinstance(_sa_col(hb.Categorical(["todo", "done"])), DatabaseTextType)
    assert getattr(_sa_col(hb.Categorical(["todo", "done"])), "length", None) == 4
    assert isinstance(_sa_col(hb.Categorical([1, 2], base="integer")), DatabaseIntegerType)
    assert isinstance(_sa_col(hb.Timestamp()), DatabaseBigIntegerType)
    assert isinstance(_sa_col(hb.Duration()), DatabaseBigIntegerType)
    assert isinstance(_sa_col(hb.Date()), DatabaseTextType)
    assert getattr(_sa_col(hb.Date()), "length", None) == 10
    assert getattr(_sa_col(hb.ShortText(max_chars=17)), "length", None) == 17
    assert isinstance(_sa_col(hb.Artifact()), DatabaseBlobType)


def test_database_blob_type_text_dialect_fallback():
    """Exotic SQL dialects without BLOB should persist standard base64 text."""
    from heavenbase.utils.serialize import dumps_b64
    from heavenbase.database.types import DatabaseBlobType

    blob = DatabaseBlobType()
    payload = b"\x00ab"
    storage = dumps_b64(payload)
    trino = type("Dialect", (), {"name": "trino"})()
    assert blob.process_bind_param(payload, trino) == storage
    assert blob.process_result_value(storage, trino) == storage


def test_sqlite_artifact_roundtrip(tmp_path):
    """SQLite inline Artifact fields should persist bytes without JSON encoding."""
    ws = hb.HeavenBase("artifact-sqlite-test", backends={"sql": hb.backends.SQLiteBackend("sql", _db(tmp_path, "artifact.db"))})

    class BlobDoc(hb.Entity):
        identifier = "artifact-sqlite-doc"

        payload = hb.field(hb.Artifact)

    ws.register(BlobDoc)
    payload = b"\x00abc\xff"
    ws.upsert(BlobDoc, {"object_id": "b1", "payload": payload})
    assert ws.get("b1", BlobDoc)["payload"] == payload
    assert ws.query(BlobDoc).execute().rows()[0]["payload"] == payload


def test_inmem_filters_and_schema_catalog_preserve_new_type_payloads():
    """Portable scan filters and catalog rows should understand new logical types."""
    ws = hb.HeavenBase("logical-type-inmem-test", backends={"main": hb.backends.InMemBackend("main")})

    class TypedTask(hb.Entity):
        identifier = "logical-type-task"

        status = hb.field(hb.Categorical(["todo", "doing", "done"]))
        priority = hb.field(hb.Categorical([1, 2, 3], base="integer"))
        created_at = hb.field(hb.Timestamp())
        due_date = hb.field(hb.Date())

    ws.register(TypedTask)
    ws.upsert_many(
        TypedTask,
        [
            {"object_id": "t1", "status": "todo", "priority": 1, "created_at": datetime(2026, 5, 13, tzinfo=timezone.utc), "due_date": date(2026, 5, 20)},
            {"object_id": "t2", "status": "doing", "priority": "2", "created_at": "2026-05-14T00:00:00+00:00", "due_date": "2026-05-21"},
            {"object_id": "t3", "status": "done", "priority": 3, "created_at": "2026-05-15T00:00:00+00:00", "due_date": "2026-05-22"},
        ],
    )

    assert ws.query(TypedTask).where(TypedTask.status.in_(["doing", "done"])).execute().ids == ["t2", "t3"]
    assert ws.query(TypedTask).where(TypedTask.priority > 1).execute().ids == ["t2", "t3"]
    assert ws.query(TypedTask).where(TypedTask.created_at >= datetime(2026, 5, 14, tzinfo=timezone.utc)).execute().ids == ["t2", "t3"]
    assert ws.query(TypedTask).where(TypedTask.due_date < date(2026, 5, 22)).execute().ids == ["t1", "t2"]

    row = (
        ws.query(hb.MetaSchema)
        .where(hb.MetaSchema.kind == "field")
        .where(hb.MetaSchema.subject_id == "logical-type-task")
        .where(hb.MetaSchema.name == "status")
        .execute()
        .rows()[0]
    )
    assert row["meta"]["dtype"] == {
        "type": "categorical",
        "values": ["todo", "doing", "done"],
        "base": "string",
    }

    with pytest.raises(ValueError, match="not one of"):
        ws.upsert(TypedTask, {"object_id": "bad", "status": "blocked", "priority": 1, "created_at": 0, "due_date": "2026-05-20"})


def test_hyperg_exists_python_scan_and_negation():
    """Portable backends should scan HyperG exists predicates correctly."""
    ws = hb.HeavenBase("hyperg-exists-inmem-test", backends={"main": hb.backends.InMemBackend("main")})

    class RelationDoc(hb.Entity):
        identifier = "hyperg-exists-inmem-doc"

        relations = hb.field(
            hb.HyperG[
                "subject" : hb.Identifier,
                "relation" : hb.Categorical["cites", "mentions"],
                "object" : hb.Identifier,
                "weight" : {"type": hb.Float, "required": False},
            ]
        )

    ws.register(RelationDoc)
    ws.upsert_many(
        RelationDoc,
        [
            {"object_id": "r1", "relations": [{"subject": "doc:1", "relation": "cites", "object": "doc:2", "weight": 0.8}]},
            {"object_id": "r2", "relations": [{"subject": "doc:3", "relation": "mentions", "object": "doc:4"}]},
            {"object_id": "r3", "relations": [{"subject": "doc:5", "relation": "cites", "object": "doc:5"}]},
        ],
    )

    query = ws.query(RelationDoc).where(RelationDoc.relations.any(relation="cites", object="doc:2"))
    step = query.explain()["steps"][0]
    all_query = ws.query(RelationDoc).where(
        RelationDoc.relations.exists(relation="cites") & RelationDoc.relations.all(where={"relation": "cites"}, object={"$in": ["doc:2"]})
    )
    json_all = ws.query(RelationDoc).where({"relations": {"$all": {"where": {"relation": "cites"}, "then": {"object": {"$in": ["doc:2"]}}}}})

    assert query.execute().ids == ["r1"]
    assert all_query.execute().ids == ["r1"]
    assert json_all.execute().ids == ["r1", "r2"]
    assert ws.query(RelationDoc).where(RelationDoc.relations.all(where={"relation": "cites"}, weight={"$gte": 0.5})).execute().ids == ["r1", "r2"]
    assert ws.query(RelationDoc).where(~RelationDoc.relations.exists(relation="cites")).execute().ids == ["r2"]
    assert step["handler_mode"] == "py_scan"
    assert step["fallback_reason"] is not None


def test_hyperg_exists_mapping_form_matches_kwargs():
    """exists/any accept a positional mapping equivalently to the kwargs form."""
    ws = hb.HeavenBase("hyperg-exists-form-test", backends={"main": hb.backends.InMemBackend("main")})

    class RelDoc(hb.Entity):
        identifier = "hyperg-exists-form-doc"

        relations = hb.field(hb.HyperG["relation" : hb.ShortText, "object" : hb.Identifier])

    ws.register(RelDoc)
    ws.upsert_many(
        RelDoc,
        [
            {"object_id": "r1", "relations": [{"relation": "cites", "object": "doc:2"}]},
            {"object_id": "r2", "relations": [{"relation": "mentions", "object": "doc:4"}]},
        ],
    )

    kwargs_ids = ws.query(RelDoc).where(RelDoc.relations.exists(relation="cites")).execute().ids
    mapping_ids = ws.query(RelDoc).where(RelDoc.relations.exists({"relation": "cites"})).execute().ids
    merged_ids = ws.query(RelDoc).where(RelDoc.relations.exists({"relation": "cites"}, object="doc:2")).execute().ids
    any_ids = ws.query(RelDoc).where(RelDoc.relations.any({"relation": "cites"})).execute().ids

    assert kwargs_ids == mapping_ids == merged_ids == any_ids == ["r1"]


def test_sql_filters_for_new_logical_types_preserve_portable_semantics(tmp_path):
    """SQLite filters should normalize new logical types before native or scan routing."""
    ws = hb.HeavenBase("logical-type-sql-test", backends={"sql": hb.backends.SQLiteBackend("sql", _db(tmp_path, "logical-types.db"))})

    class SQLTypedTask(hb.Entity):
        identifier = "sql-logical-type-task"

        status = hb.field(hb.Categorical(["todo", "doing", "done"]))
        priority = hb.field(hb.Categorical([1, 2, 3], base="integer"))
        created_at = hb.field(hb.Timestamp())
        due_date = hb.field(hb.Date())

    ws.register(SQLTypedTask)
    ws.upsert_many(
        SQLTypedTask,
        [
            {"object_id": "s1", "status": "todo", "priority": 1, "created_at": "2026-05-13T00:00:00+00:00", "due_date": "2026-05-20"},
            {"object_id": "s2", "status": "doing", "priority": 2, "created_at": "2026-05-14T00:00:00+00:00", "due_date": "2026-05-21"},
            {"object_id": "s3", "status": "done", "priority": 3, "created_at": "2026-05-15T00:00:00+00:00", "due_date": "2026-05-22"},
        ],
    )

    query = ws.query(SQLTypedTask).where((SQLTypedTask.status == "done") | (SQLTypedTask.created_at > "2026-05-14T12:00:00+00:00"))
    assert query.execute().ids == ["s3"]
    assert ws.query(SQLTypedTask).where(SQLTypedTask.priority >= "2").execute().ids == ["s2", "s3"]
    assert ws.query(SQLTypedTask).where(SQLTypedTask.due_date <= date(2026, 5, 21)).execute().ids == ["s1", "s2"]

    steps = [step for step in query.explain()["steps"] if step.get("node") == "filter"]
    assert steps
    by_field = {step["field"]: step for step in steps}
    assert by_field["status"]["handler_mode"] == "native"
    assert by_field["status"]["portable_proof"] is True
    assert by_field["created_at"]["handler_mode"] == "native"


@pytest.mark.detail
def test_elasticsearch_native_filter_payloads_normalize_new_logical_types():
    """Elasticsearch native filters should receive backend-ready scalar values."""
    from heavenbase.backends.elasticsearch.handler import elasticsearch_filter_handler

    class ESTypedTask(hb.Entity):
        identifier = "es-logical-type-task"

        priority = hb.field(hb.Categorical([1, 2, 3], base="integer"))
        created_at = hb.field(hb.Timestamp())
        due_date = hb.field(hb.Date())

    schema = ESTypedTask.schema()
    ctx = hb.ext.HandlerContext("search", "elasticsearch", hb.InlineColumn.identifier, scalar_indexed=True)
    aware_dt = datetime(2026, 5, 14, 0, 30, tzinfo=timezone(timedelta(hours=2)))

    ts_fragment = elasticsearch_filter_handler("gte")(schema.get("created_at"), aware_dt, ctx)
    date_fragment = elasticsearch_filter_handler("eq")(schema.get("due_date"), aware_dt, ctx)
    cat_fragment = elasticsearch_filter_handler("eq")(schema.get("priority"), "2", ctx)

    assert ts_fragment.payload == {"range": {"created_at": {"gte": int(aware_dt.astimezone(timezone.utc).timestamp() * 1000)}}}
    assert date_fragment.payload == {"term": {"due_date": "2026-05-13"}}
    assert cat_fragment.payload == {"term": {"priority": 2}}


def test_all_registered_logical_types_serialize_roundtrip():
    """Every registered logical type should roundtrip through JSON descriptors."""
    samples = [
        hb.ShortText(),
        hb.LongText(),
        hb.MediumText(),
        hb.Identifier(),
        hb.Integer(),
        hb.Float(),
        hb.Boolean(),
        hb.Json(),
        hb.Artifact(),
        hb.Timestamp(),
        hb.Timestamp(unit="s"),
        hb.Date(),
        hb.Vector(768),
        hb.Array(hb.ShortText),
        hb.Categorical(["todo", "done"]),
        hb.Categorical([1, 2], base="integer"),
        hb.HyperG["subject" : hb.Identifier, "relation" : hb.ShortText],
    ]
    for dtype in samples:
        restored = logical_type_from_any(dtype.to_dict())
        assert restored.to_dict() == dtype.to_dict(), type(dtype).identifier


def test_entity_from_schema_json_spec_uses_serializable_types():
    """Dynamic entity specs should store JSON-safe field type descriptors."""
    Customer = hb.Entity.from_schema(
        {
            "entity_id": "json-vector-customer",
            "fields": {
                "object_id": {"type": "identifier", "pk": True},
                "embedding": {"type": hb.Vector[768]},
            },
        }
    )
    assert Customer._hb_json_spec["fields"]["embedding"]["type"] == {"type": "vector", "dim": 768}


def test_json_decode_restores_tagged_values_a_backend_already_parsed():
    """A backend that parses JSON itself (Milvus) hands back tagged objects; decoding restores them as it does JSON text."""
    dtype = hb.Json()
    assert dtype.decode({"__obj_type__": "tuple", "__obj_data__": [1, 2]}) == (1, 2)
    assert dtype.decode([{"__obj_type__": "bigint", "__obj_data__": str(2**70)}, {"k": {"__obj_type__": "set", "__obj_data__": [1]}}]) == [
        2**70,
        {"k": {1}},
    ]
    assert dtype.decode({"k": 1}) == {"k": 1}
