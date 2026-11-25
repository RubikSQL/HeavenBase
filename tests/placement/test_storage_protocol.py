"""Contract tests for authored and resolved placement objects."""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.placement import StorageBinding, StoragePlan, StorageProfile, StorageRule


class ProtocolStorageType(hb.ShortText):
    """Logical type used only by storage protocol registration tests."""

    identifier = "protocol-storage-type"


def test_storage_rule_normalizes_strategy_values_and_params():
    strategy = hb.GraphEdge(target="protocol-target")
    rule = StorageRule(backend="graph", strategy=strategy, params={"source": "protocol-source"})

    assert rule.strategy == hb.GraphEdge.identifier
    assert rule.params == {"target": "protocol-target", "source": "protocol-source"}
    assert rule.strategy_spec() == hb.GraphEdge.identifier

    with pytest.raises(ValueError, match="backend cannot be empty"):
        StorageRule(backend="  ")


def test_storage_profile_owns_logical_type_registration():
    hb.ext.register_logical_type(ProtocolStorageType, tags={"filter": True, "filter-domains": ["text"]})
    profile = StorageProfile(
        ProtocolStorageType,
        ({"tag": "storage-roles", "contains": "row"},),
        hb.InlineColumn,
        "protocol-default",
        (
            {
                "when": {
                    "all": [
                        {"tag": "sql", "equals": True},
                        {"tag": "storage-roles", "contains": "row"},
                    ]
                },
                "strategy": hb.SideTable,
            },
        ),
    )

    assert profile.register() == profile
    assert hb.ext.registered_storage_profiles()[ProtocolStorageType.identifier] == profile
    assert profile.strategy_for({"sql": True, "storage-roles": ["row"]}) == hb.SideTable.identifier
    assert profile.to_dict()["dtype"] == ProtocolStorageType.identifier

    with pytest.raises(ValueError, match="already registered"):
        StorageProfile(
            ProtocolStorageType,
            ({"tag": "storage-roles", "contains": "row"},),
            hb.InlineColumn,
            "duplicate",
        ).register()


def test_storage_profile_mapping_and_specific_array_keys():
    profile = StorageProfile.from_mapping(
        hb.Array[hb.ShortText],
        {
            "backend_preferences": [{"tag": "sql", "equals": True}],
            "default_strategy": hb.SparseGramIndex.identifier,
            "reason": "protocol-mapping",
        },
    )

    assert StorageProfile.keys_for(hb.Array[hb.ShortText]) == ("array[short-text]", "array")
    assert profile.identifier == "array[short-text]"
    assert profile.default_strategy == hb.SparseGramIndex.identifier


def test_storage_binding_and_plan_own_resolved_route_queries():
    options = {"normalizer": "simple"}
    binding = StorageBinding("protocol-entity", "terms", "sql", hb.SparseGramIndex, "protocol", options)
    options["normalizer"] = "changed"
    plan = StoragePlan("protocol-entity", [binding])

    assert binding.strategy_options == {"normalizer": "simple"}
    assert binding.matches(backend="sql", strategy=hb.SparseGramIndex)
    assert binding.route_notes() == {"strategy": hb.SparseGramIndex.identifier, "strategy_options": {"normalizer": "simple"}}
    assert plan.first("terms") is binding
    assert plan.has_binding("terms", backend="sql", strategy=hb.SparseGramIndex)
    assert not plan.has_binding("terms", backend="main")

    serialized = binding.to_dict()
    serialized["strategy_options"]["normalizer"] = "mutated"
    assert binding.strategy_options == {"normalizer": "simple"}

    with pytest.raises(TypeError, match="must be StorageBinding"):
        StoragePlan("protocol-entity", [object()])
