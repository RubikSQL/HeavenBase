"""Backend strategy-adapter placement contracts."""

import pytest

import heavenbase as hb
from heavenbase.backends.strategy import StrategyAdapter
from heavenbase.backends.sql.strategies import SQLSparseGramAdapter


class AdapterEntity(hb.Entity):
    identifier = "strategy-adapter-entity"

    value = hb.field(hb.ShortText)


class Binding:
    backend = "main"
    strategy = hb.InlineColumn.identifier


class DemoAdapter(StrategyAdapter):
    identifier = "demo-adapter"
    strategy = hb.InlineColumn.identifier

    def __init__(self, backend):
        super().__init__(backend)
        self.ensured = []
        self.rows = []

    def ensure(self, schema, binding):
        self.ensured.append((schema.entity_id, binding.strategy))

    def upsert(self, rows):
        self.rows.extend(rows)


def test_strategy_adapter_minimal_protocol_and_defaults():
    backend = hb.backends.InMemBackend("main")
    adapter = DemoAdapter(backend)
    schema = AdapterEntity.schema()
    binding = Binding()
    row = hb.ext.RowOp(schema.entity_id, "a1", {"value": "alpha"})

    assert adapter.register() is adapter
    assert adapter.register() is adapter
    assert backend.strategy_adapter(hb.InlineColumn) is adapter
    assert adapter.supports(schema, binding) is True
    assert backend.supports_strategy(schema, binding) is True
    adapter.ensure(schema, binding)
    adapter.upsert([row])

    fragment = hb.ext.QueryFragment("main", "eq", "value", "alpha")
    assert adapter.can_execute(fragment) is False
    assert backend.strategy_adapter_for_fragment(fragment) is None
    with pytest.raises(TypeError, match="does not execute"):
        adapter.execute(fragment)
    assert adapter.reset() is None
    envelope = {"identifier": "demo-adapter", "strategy": "inline", "metadata": {}}
    live_adapters = backend.tag("strategy-adapters")
    assert adapter.to_dict() == envelope
    assert live_adapters is not None
    assert live_adapters.value == (envelope,)
    assert live_adapters.status == "resolved"
    assert live_adapters.source == "live"
    assert live_adapters.instance is backend
    assert adapter.ensured == [(schema.entity_id, "inline")]
    assert adapter.rows == [row]
    assert adapter.prune(schema, [binding]) is None


def test_row_op_strategies_flag_defaults_on_and_must_be_boolean():
    """``RowOp.strategies`` is a typed hint that defaults to full adapter maintenance."""
    schema = AdapterEntity.schema()

    assert hb.ext.RowOp(schema.entity_id, "a1", {"value": "alpha"}).strategies is True
    assert hb.ext.RowOp(schema.entity_id, "a1", {"value": "alpha"}, strategies=False).strategies is False
    assert hb.ext.RowOp(schema.entity_id, "a1", op="delete", strategies=False).is_delete() is True
    with pytest.raises(TypeError, match="RowOp strategies must be a boolean"):
        hb.ext.RowOp(schema.entity_id, "a1", {"value": "alpha"}, strategies="no")


def test_strategy_adapter_registration_rejects_conflicts_and_wrong_owner():
    class ReplacementAdapter(DemoAdapter):
        identifier = "replacement-adapter"

    backend = hb.backends.InMemBackend("main")
    other = hb.backends.InMemBackend("other")
    first = DemoAdapter(backend).register()
    replacement = ReplacementAdapter(backend)

    with pytest.raises(ValueError, match="already has an adapter"):
        replacement.register()
    assert replacement.register(replace=True) is replacement
    assert backend.strategy_adapter(hb.InlineColumn) is replacement
    with pytest.raises(ValueError, match="different backend"):
        other.register_strategy_adapter(first)
    with pytest.raises(TypeError, match="Expected StrategyAdapter"):
        backend.register_strategy_adapter(object())

    class DuplicateIdentifierAdapter(DemoAdapter):
        identifier = "replacement-adapter"
        strategy = hb.JsonField.identifier

    with pytest.raises(ValueError, match="adapter identifier"):
        DuplicateIdentifierAdapter(backend).register()


def test_strategy_adapter_validates_identity_and_requires_write_hooks():
    class MissingIdentifierAdapter(StrategyAdapter):
        strategy = hb.InlineColumn.identifier

    class UnknownStrategyAdapter(StrategyAdapter):
        identifier = "unknown-strategy-adapter"
        strategy = "missing-strategy"

    class IncompleteAdapter(StrategyAdapter):
        identifier = "incomplete-adapter"
        strategy = hb.InlineColumn.identifier

    backend = hb.backends.InMemBackend("main")
    with pytest.raises(ValueError, match="strategy adapter identifier"):
        MissingIdentifierAdapter(backend)
    with pytest.raises(ValueError, match="Unknown strategy"):
        UnknownStrategyAdapter(backend)

    adapter = IncompleteAdapter(backend)
    with pytest.raises(TypeError, match="must implement ensure, upsert"):
        adapter.register()
    with pytest.raises(NotImplementedError, match="ensure"):
        adapter.ensure(AdapterEntity.schema(), Binding())
    with pytest.raises(NotImplementedError, match="upsert"):
        adapter.upsert([])


def test_sql_sparse_gram_uses_the_shared_strategy_adapter_base():
    assert issubclass(SQLSparseGramAdapter, StrategyAdapter)
    assert hb.ext.StrategyAdapter is StrategyAdapter
    assert StrategyAdapter.__module__ == "heavenbase.backends.strategy"
