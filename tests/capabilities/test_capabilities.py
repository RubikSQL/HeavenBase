"""Contract tests for the registry-backed capability front door."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import sys

import pytest

import heavenbase as hb
from heavenbase.utils import cmd


@pytest.mark.parametrize("capabilities_first", (False, True))
def test_root_capabilities_is_module_front_door_in_both_import_orders(capabilities_first: bool):
    script = """
import importlib
import heavenbase as hb

capabilities = importlib.import_module("heavenbase.capabilities")
assert hb.capabilities is capabilities
"""
    if capabilities_first:
        script = """
import importlib

capabilities = importlib.import_module("heavenbase.capabilities")
import heavenbase as hb
assert hb.capabilities is capabilities
"""
    cmd([sys.executable, "-c", script], check=True)


def test_module_front_door_rebuilds_default_index_after_resolver_reset(monkeypatch):
    """The module facade must not retain handlers owned by a retired Context."""

    class Catalog:
        publication = 0

    class Resolver:
        catalog = Catalog()

        def inspect(self, kind=None, identifier=None, *, include_inactive=False):
            return ()

        def resolve(self, kind, identifier):
            raise KeyError((kind, identifier))

        def resolve_executable(self, reference, *, owner=None):
            raise KeyError(reference)

    first_resolver = Resolver()
    second_resolver = Resolver()

    # Retire the Context's memoized resolver instead of replacing the
    # `heavenbase.modules.module_resolver` selector. A selector patch is copied
    # into the import-time binding of every module first imported inside the
    # patched window, so it outlives the teardown that restores the facade.
    monkeypatch.setattr(hb.DEFAULT_CONTEXT, "_modules", first_resolver)
    first = hb.capabilities._default_index()
    monkeypatch.setattr(hb.DEFAULT_CONTEXT, "_modules", second_resolver)
    second = hb.capabilities._default_index()

    assert first is not second
    assert first.resolver is first_resolver
    assert second.resolver is second_resolver


def test_retired_resolver_never_survives_in_a_module_selector_binding():
    """Every module-level default-resolver selector stays the canonical one."""
    from heavenbase.modules.base import module_resolver

    leaked = sorted(
        name
        for name, module in tuple(sys.modules.items())
        if name.startswith("heavenbase.") and module is not None and getattr(module, "module_resolver", module_resolver) is not module_resolver
    )

    assert leaked == []


def test_capability_index_has_one_logical_type_picker():
    assert not hasattr(hb.capabilities, "types")
    assert not hasattr(hb.capabilities, "get_logical_type")
    assert not hasattr(hb.capabilities, "get_strategy")

    identifiers = {option.identifier for option in hb.capabilities.logical_types(backend="inmem", strategy=hb.InlineColumn, op="eq")}

    assert "short-text" in identifiers
    assert "vector" not in identifiers


def test_workspace_discovery_filters_iterables_by_backend_name():
    ws = hb.HeavenBase._construct(
        "discovery-protocol-test",
        context=hb.DEFAULT_CONTEXT,
        backends={"main": hb.backends.InMemBackend("main"), "vec": hb.backends.InMemBackend("vec")},
        detached=True,
        tracked=False,
        register_workspace=False,
        register_pending=False,
        track_live=False,
        publish=False,
    )

    rows = ws.capabilities.capabilities(
        dtype=[hb.ShortText, hb.Vector],
        strategy=[hb.InlineColumn, hb.VectorIndex],
        backend=["vec"],
        op=["eq", "$near"],
    )
    keys = {(row.dtype, row.op, row.strategy, row.backend_name) for row in rows}

    assert ("short-text", "eq", hb.InlineColumn.identifier, "vec") in keys
    assert ("vector", "near", hb.VectorIndex.identifier, "vec") in keys
    assert {row.backend_name for row in rows} == {"vec"}


def test_discovery_options_are_immutable_serializable_values():
    option = hb.capabilities.logical_types()[0]

    with pytest.raises(FrozenInstanceError):
        option.identifier = "changed"

    assert option.to_dict()["identifier"] == option.identifier

    capability = hb.capabilities.capabilities(dtype=hb.ShortText, backend="inmem", strategy=hb.InlineColumn, op="eq")[0]
    with pytest.raises(TypeError):
        capability.aliases["dtype"] = ()


def test_capability_option_never_reconstructs_metadata_from_an_implicit_context():
    values = ("short-text", "eq", "inmem", hb.InlineColumn.identifier)

    with pytest.raises(TypeError, match="alias snapshot or an explicit module resolver"):
        hb.capabilities.CapabilityOption(*values)

    option = hb.capabilities.CapabilityOption(*values, resolver=hb.DEFAULT_CONTEXT.modules())
    payload = option.to_dict()
    assert payload["dtype"] == "short-text"
    assert "dtype" in payload["aliases"]
    assert "families" not in payload
