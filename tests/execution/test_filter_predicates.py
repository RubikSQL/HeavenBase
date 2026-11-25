"""Portable execution predicate protocol tests."""

import pytest

import heavenbase as hb
from heavenbase.execution.handlers.filters import filter_handler
from heavenbase.modules import ModuleService
from heavenbase.schema import FieldSchema
from heavenbase.utils.registry import Registry


class _Config:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


def _same_initial(left, value):
    """Match values whose text begins with the same letter."""
    return str(left or "")[:1].lower() == str(value)[:1].lower()


def _longer_than(left, limit):
    """Match values whose text length exceeds one prepared limit."""
    return len(str(left or "")) > limit


def _equal(left, value):
    """Match equal values for isolated resolver tests."""
    return left == value


def _not_equal(left, value):
    """Match unequal values for isolated resolver tests."""
    return left != value


def _service():
    """Return one isolated process-local module authority."""
    return ModuleService(Registry("system-modules"))


@pytest.fixture(autouse=True)
def _default_context_registrations_are_undone():
    """Remove the operations and predicates a test registers in the default Context after it.

    They are process-wide: a filter operation left behind would be advertised, and compiled, by every later
    workspace in the same test process.
    """
    modules = hb.DEFAULT_CONTEXT.modules()
    before = {(row["kind"], row["identifier"]) for row in modules.inspect(include_inactive=True)}
    yield
    added = [(row["kind"], row["identifier"]) for row in modules.inspect(include_inactive=True) if (row["kind"], row["identifier"]) not in before]
    # Predicates require their operation, so they go first.
    for kind, identifier in sorted(added, key=lambda item: item[0] != "predicate"):
        modules.unregister(kind, identifier)


class _ReadResolver:
    """Expose a ModuleService through only its read-side resolver contract."""

    def __init__(self, service):
        self._service = service
        self.catalog = service.catalog

    def inspect(self, kind=None, identifier=None, *, include_inactive=False):
        return self._service.inspect(kind, identifier, include_inactive=include_inactive)

    def resolve(self, kind, identifier):
        return self._service.resolve(kind, identifier)

    def resolve_executable(self, reference, *, owner=None):
        return self._service.resolve_executable(reference, owner=owner)


def _ctx() -> hb.ext.HandlerContext:
    return hb.ext.HandlerContext("main", "inmem", hb.InlineColumn.identifier)


def test_builtin_predicates_prepare_and_match_values():
    """Built-ins should expose one reusable compile-then-match contract."""
    regex = hb.ext.Predicate.load("regex")
    pattern = regex.compile("^alpha")

    assert regex.match("alphabet", pattern)
    assert not regex.match("beta", pattern)
    assert "array_contains" in hb.ext.Predicate.identifiers()
    assert "get" in hb.ext.Predicate.identifiers(hb.Json)
    assert hb.ext.Predicate.load("eq", hb.Json).match(True, 1) is False
    with pytest.raises(TypeError, match="list, tuple, or set"):
        hb.ext.Predicate.load("in").compile("alpha")


def test_predicate_policy_is_captured_from_explicit_handler_config():
    config = _Config(
        {
            "heavenbase.query.regex.max_chars": 3,
            "heavenbase.query.regex.max_text_chars": 4,
        }
    )
    ctx = hb.ext.HandlerContext(
        "main",
        "inmem",
        hb.InlineColumn.identifier,
        resolver=hb.DEFAULT_CONTEXT.modules(),
        config=config,
    )
    field = FieldSchema("name", hb.ShortText())

    with pytest.raises(ValueError, match="exceeds 3"):
        filter_handler("regex")(field, "abcd", ctx)

    predicate = hb.ext.Predicate.load("regex", resolver=ctx.resolver)
    prepared = predicate.compile("^a", resolver=ctx.resolver, config=config)
    config.values["heavenbase.query.regex.max_text_chars"] = 100
    with pytest.raises(ValueError, match="exceeds 4"):
        predicate.match("alphabet", prepared)


def test_custom_predicate_extends_portable_filter_handler():
    """A registered predicate should add scan behavior without compiler edits."""
    ws = hb.HeavenBase("custom-predicate-test", backends={"main": hb.backends.InMemBackend("main")})
    op = "test_same_initial"
    hb.ext.register_op(op, aliases=("$test_same_initial",), tags={"filter": True, "filter-domains": ["text"]})
    predicate = hb.ext.Predicate(op, _same_initial).register()

    fragment = filter_handler(op)(FieldSchema("name", hb.ShortText()), "Alpha", _ctx())

    assert hb.ext.Predicate.load("$test_same_initial") is predicate
    assert fragment.payload({"name": "Atom"})
    assert not fragment.payload({"name": "Beta"})
    assert fragment.notes["mode"] == "py_scan"

    class PredicateDoc(hb.Entity):
        identifier = "custom-predicate-doc"

        name = hb.field(hb.ShortText)

    hb.ext.Handler(hb.ShortText, op, "inmem", hb.InlineColumn, filter_handler(op)).register(ws.handlers)
    ws.register(PredicateDoc)
    ws.upsert_many(PredicateDoc, [{"object_id": "p1", "name": "Atom"}, {"object_id": "p2", "name": "Beta"}])

    assert ws.query(PredicateDoc).where(hb.filters.Filter("name", op, "Alpha")).execute().ids == ["p1"]


def test_custom_predicate_composes_inside_hyperg_scan():
    """HyperG scans should resolve nested operations through the same registry."""
    op = "test_longer_than"
    hb.ext.register_op(op)
    hb.ext.Predicate(op, _longer_than, int).register()
    dtype = hb.HyperG["label" : hb.ShortText]
    field = FieldSchema("relations", dtype)
    value = {"label": {op: "4"}}

    fragment = filter_handler("exists")(field, value, _ctx())

    assert fragment.payload({"relations": [{"label": "short"}]})
    assert not fragment.payload({"relations": [{"label": "tiny"}]})


def test_register_op_extends_existing_aliases_and_tags():
    """Repeated registration should add metadata without dropping prior values."""
    op = "test_extend_operation_metadata"
    hb.ext.register_op(op, aliases=("$test_extend_old",), tags={"filter": True, "filter-domains": ["text"]})
    hb.ext.register_op(op, aliases=("$test_extend_new",), tags={"nested-filter": True})

    assert hb.ext.normalize_op("$test_extend_old") == op
    assert hb.ext.normalize_op("$test_extend_new") == op
    assert set(hb.ext.op_aliases(op)) == {"$test_extend_old", "$test_extend_new"}
    assert hb.DEFAULT_CONTEXT.modules().tags("operation", op).raw == {
        "filter": True,
        "filter-domains": ("text",),
        "nested-filter": True,
    }


def test_predicate_registration_validates_contract_and_duplicates():
    """Registration should reject malformed and duplicate definitions."""
    with pytest.raises(ValueError, match="filter predicate identifier"):
        hb.ext.Predicate("$", lambda left, value: left == value)
    with pytest.raises(TypeError, match="matcher"):
        hb.ext.Predicate("test_invalid_predicate", None)
    read_service = _service()
    hb.ext.register_op("test_read_only_predicate", resolver=read_service)
    with pytest.raises(TypeError, match="mutable ModuleService"):
        hb.ext.Predicate("test_read_only_predicate", _equal).register(resolver=_ReadResolver(read_service))
    hb.ext.register_op("test_missing_predicate")
    with pytest.raises(ValueError, match="no portable predicate"):
        hb.ext.Predicate.load("test_missing_predicate")

    hb.ext.register_op("test_replace_predicate")
    first = hb.ext.Predicate("test_replace_predicate", _equal).register()
    with pytest.raises(ValueError, match="already registered"):
        hb.ext.Predicate("test_replace_predicate", _not_equal).register()
    replacement = hb.ext.Predicate("test_replace_predicate", _not_equal).register(replace=True)

    assert hb.ext.Predicate.load("test_replace_predicate") is replacement
    assert hb.ext.Predicate.load("test_replace_predicate") is not first


def test_generic_predicates_are_isolated_by_module_resolver_identity():
    """The same operation may resolve to different portable behavior per Context."""
    first = _service()
    second = _service()
    operation = "resolver_predicate"
    hb.ext.register_op(operation, resolver=first)
    hb.ext.register_op(operation, resolver=second)
    hb.ext.Predicate(operation, _equal).register(resolver=first)
    hb.ext.Predicate(operation, _not_equal).register(resolver=second)

    first_predicate = hb.ext.Predicate.load(operation, resolver=first)
    second_predicate = hb.ext.Predicate.load(operation, resolver=second)

    assert first_predicate.matcher is _equal
    assert second_predicate.matcher is _not_equal
    assert first_predicate.match("same", "same")
    assert not second_predicate.match("same", "same")
    assert hb.ext.Predicate.identifiers(resolver=first) == (operation,)
    assert hb.ext.Predicate.identifiers(resolver=second) == (operation,)


def test_typed_predicate_overrides_are_isolated_without_generic_cross_talk():
    """Type-scoped caches must retain the resolver that owns each override."""
    first = _service()
    second = _service()
    operation = "resolver_typed_predicate"
    for service in (first, second):
        hb.ext.register_op(operation, resolver=service)
        hb.ext.Predicate(operation, _equal).register(resolver=service)
    hb.ext.Predicate(operation, _equal).register(dtype="json", resolver=first)
    hb.ext.Predicate(operation, _not_equal).register(dtype="json", resolver=second)

    assert hb.ext.Predicate.load(operation, "json", resolver=first).match(1, 1)
    assert not hb.ext.Predicate.load(operation, "json", resolver=second).match(1, 1)
    assert hb.ext.Predicate.load(operation, resolver=first).match(1, 1)
    assert hb.ext.Predicate.load(operation, resolver=second).match(1, 1)
    assert hb.ext.Predicate.identifiers("json", resolver=first) == (operation,)
    assert hb.ext.Predicate.identifiers("json", resolver=second) == (operation,)
