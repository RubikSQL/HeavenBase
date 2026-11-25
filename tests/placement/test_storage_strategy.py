"""Placement strategy value and registry contracts."""

import pytest

import heavenbase as hb


class StrategyRegistryProbe(hb.StorageStrategy):
    """Durable strategy used by Registry discovery tests."""

    option_fields = ("level",)


class StrategyValueProbe(hb.StorageStrategy):
    """Durable strategy used by value-protocol tests."""

    option_fields = ("level", "mode")


def test_storage_strategy_registry_owns_lookup_and_tag_claims() -> None:
    registered = hb.ext.register_strategy(
        StrategyRegistryProbe,
        tags={"strategy-probe": True},
        aliases=("strategy-probe-alias",),
    )

    assert registered is StrategyRegistryProbe
    assert hb.StorageStrategy.__module__ == "heavenbase.strategies.base"
    assert hb.StorageStrategy.identifier_of(StrategyRegistryProbe) == "strategy-registry-probe"
    assert hb.StorageStrategy.load("strategy-probe-alias") is StrategyRegistryProbe
    assert "strategy-probe-alias" in hb.ext.strategy_aliases("strategy-registry-probe")
    view = hb.DEFAULT_CONTEXT.modules().tags("strategy", "strategy-probe-alias")
    claim = view["strategy-probe"]
    assert dict(view.raw) == {"strategy-probe": True}
    assert claim.value is True
    assert claim.status == "unresolved"
    assert claim.source == "record"
    assert claim.subject == ("strategy", "strategy-registry-probe")


def test_storage_strategy_value_protocol_normalizes_specs() -> None:
    assert StrategyValueProbe.register(aliases=("strategy-value-alias",)) is StrategyValueProbe

    value = hb.StorageStrategy.value_of({"type": "strategy-value-alias", "level": 2, "mode": "strict", "empty": None})

    assert isinstance(value, StrategyValueProbe)
    assert value.options() == {"level": 2, "mode": "strict"}
    assert value.params == {}
    assert value.to_dict() == {"type": "strategy-value-probe", "level": 2, "mode": "strict"}
    assert hb.StorageStrategy.spec_of("strategy-value-alias", {"mode": "loose"}) == {
        "type": "strategy-value-probe",
        "mode": "loose",
    }
    assert hb.StorageStrategy.spec_of(StrategyValueProbe, {"level": 3}) == {
        "type": "strategy-value-probe",
        "level": 3,
    }
    assert hb.StorageStrategy.value_of(value) is value

    with pytest.raises(ValueError, match="strategy-value-probe strategy option"):
        hb.StorageStrategy.value_of({"type": "strategy-value-probe", "unknown": True})


def test_strategy_options_and_placement_params_stay_separate() -> None:
    sparse = hb.SparseGramIndex(normalizer="simple", gram_unit="token")
    edge = hb.GraphEdge(target="cited")
    detaching_edge = hb.GraphEdge(target="cited", on_delete="DETACH")
    edge_rule = hb.ext.StorageRule(strategy=edge)
    detaching_edge_rule = hb.ext.StorageRule(strategy=detaching_edge)

    assert sparse.options() == {"normalizer": "simple", "gram_unit": "token"}
    assert sparse.params == {}
    assert hb.StorageStrategy.spec_of(sparse) == {
        "type": "sparse-gram",
        "normalizer": "simple",
        "gram_unit": "token",
    }
    assert edge.on_delete == "restrict"
    assert edge.options() == {}
    assert edge_rule.strategy == hb.GraphEdge.identifier
    assert edge_rule.strategy_options == {}
    assert edge_rule.params == {"target": "cited"}
    assert edge_rule.strategy_spec() == hb.GraphEdge.identifier
    assert detaching_edge.on_delete == "detach"
    assert detaching_edge.options() == {"on_delete": "detach"}
    assert detaching_edge_rule.strategy == hb.GraphEdge.identifier
    assert detaching_edge_rule.strategy_options == {"on_delete": "detach"}
    assert detaching_edge_rule.params == {"target": "cited"}
    assert detaching_edge_rule.strategy_spec() == {"type": "graph", "on_delete": "detach"}


def test_graph_edge_delete_policy_normalizes_serialized_values() -> None:
    restricted = hb.StorageStrategy.value_of({"type": "graph", "on_delete": "RESTRICT"})
    detaching = hb.StorageStrategy.value_of({"type": "graph", "on_delete": "DETACH"})

    assert isinstance(restricted, hb.GraphEdge)
    assert restricted.on_delete == "restrict"
    assert restricted.options() == {}
    assert restricted.to_dict() == {"type": "graph"}
    assert isinstance(detaching, hb.GraphEdge)
    assert detaching.on_delete == "detach"
    assert detaching.options() == {"on_delete": "detach"}
    assert detaching.to_dict() == {"type": "graph", "on_delete": "detach"}

    with pytest.raises(ValueError, match="GraphEdge on_delete"):
        hb.GraphEdge(on_delete="cascade")

    with pytest.raises(ValueError, match="GraphEdge on_delete"):
        hb.StorageStrategy.value_of({"type": "graph", "on_delete": "preserve"})

    with pytest.raises(ValueError, match="GraphEdge on_delete"):
        hb.ext.StorageRule(strategy=hb.GraphEdge, strategy_options={"on_delete": "cascade"})


def test_register_strategy_rejects_non_strategy_classes() -> None:
    with pytest.raises(TypeError, match="StorageStrategy subclass"):
        hb.ext.register_strategy(object)
