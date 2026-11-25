from concurrent.futures import ThreadPoolExecutor
from math import copysign
import threading

import pytest

from heavenbase.utils.config import ConfigManager, engine as engine_module, manager
from heavenbase.utils.config.engine import ConfigEngine

pytestmark = pytest.mark.fast


def _config(default):
    return ConfigManager(package="heavenbase-test", default=default)


@pytest.mark.parametrize(("index", "position"), [(-1, 2), (-3, 0), (-8, 0), (8, 3)])
def test_inserting_through_a_list_preserves_existing_items(index, position):
    items = [{"name": name} for name in ("a", "b", "c")]
    config = _config({"items": items})
    assert config.set(f"items[{index}+].name", "inserted")
    assert config.snapshot().to_dict()["items"] == [*items[:position], {"name": "inserted"}, *items[position:]]


@pytest.mark.parametrize("key", [r"pkg\.v1.items", r"pkg.items\.v1"])
def test_appending_through_an_escaped_dot_saves_the_literal_path(key):
    parent, child = ("pkg.v1", "items") if key.startswith(r"pkg\.") else ("pkg", "items.v1")
    config = _config({parent: {child: [1]}})

    assert config.set(f"{key}[]", 2)
    assert config.layer() == {parent: {child: [1, 2]}}
    assert config.snapshot().to_dict() == {parent: {child: [1, 2]}}

    assert config.unset(f"{key}[0]")
    assert config.layer() == {parent: {child: [2]}}


def test_config_saves_the_sign_of_zero():
    config = _config({})
    assert config.set("value", {"items": [0.0]})
    assert config.set("value", {"items": [-0.0]})
    assert copysign(1, config.snapshot(refresh=True).get("value.items[0]")) == -1
    assert [copysign(1, row["data"]["value"]["items"][0]) for row in config.history(limit=2)] == [-1, 1]


class _ExampleEngine(ConfigEngine):
    domain = "example"


def test_engine_cache_distinguishes_signed_zero():
    engine = _ExampleEngine(manager=None)
    assert copysign(1, engine.resolve({"value": 0.0}).data["value"]) == 1
    assert copysign(1, engine.resolve({"value": -0.0}).data["value"]) == -1


@pytest.mark.parametrize("read", ["snapshot", "_memory_snapshot"])
def test_environment_change_during_resolution_does_not_pin_the_old_value(monkeypatch, read):
    monkeypatch.setenv("HB_CFG_RACING_ENV", "old")
    config = _config({"value": "${env:HB_CFG_RACING_ENV}"})
    resolved = threading.Event()
    changed = threading.Event()
    interpolate = manager._resolve_interpolation

    def pause_after_resolution(*args, **kwargs):
        result = interpolate(*args, **kwargs)
        if not resolved.is_set():
            resolved.set()
            assert changed.wait(10)
        return result

    monkeypatch.setattr(manager, "_resolve_interpolation", pause_after_resolution)
    with ThreadPoolExecutor(max_workers=1) as pool:
        if read == "snapshot":
            pending = pool.submit(config.snapshot, refresh=True)
        else:
            pending = pool.submit(config._memory_snapshot, tuple(config.scope_chain))
        try:
            assert resolved.wait(10)
            monkeypatch.setenv("HB_CFG_RACING_ENV", "new")
        finally:
            changed.set()
        pending.result(timeout=10)

    assert config.snapshot(refresh=True).get("value") == "new"


def test_engine_does_not_cache_a_value_resolved_after_its_environment_key(monkeypatch):
    engine = _ExampleEngine(manager=None)
    raw = {"value": "${env:HB_CFG_ENGINE_RACE}"}
    monkeypatch.setenv("HB_CFG_ENGINE_RACE", "old")
    interpolate = engine_module._resolve_interpolation
    change_pending = True

    def rotate_before_resolution(data, policy, environment):
        nonlocal change_pending
        if data == raw and change_pending:
            change_pending = False
            monkeypatch.setenv("HB_CFG_ENGINE_RACE", "new")
        return interpolate(data, policy, environment)

    monkeypatch.setattr(engine_module, "_resolve_interpolation", rotate_before_resolution)
    engine.resolve(raw)
    monkeypatch.setenv("HB_CFG_ENGINE_RACE", "old")
    assert engine.resolve(raw).get("value") == "old"


def test_builtin_environment_interpolation_refreshes(monkeypatch):
    config = _config({"value": "${oc.env:HB_CFG_BUILTIN_ENV,missing}"})
    monkeypatch.setenv("HB_CFG_BUILTIN_ENV", "old")
    first = config.snapshot()
    monkeypatch.setenv("HB_CFG_BUILTIN_ENV", "new")
    assert config.snapshot(refresh=True).get("value") == "new"
    monkeypatch.delenv("HB_CFG_BUILTIN_ENV")
    assert config.snapshot().get("value") == "missing"
    assert first.get("value") == "old"


def test_engine_resolves_current_builtin_environment_in_direct_config(monkeypatch):
    engine = _ExampleEngine(manager=None)
    raw = {"value": "${oc.env:HB_CFG_BUILTIN_ENV}"}
    monkeypatch.setenv("HB_CFG_BUILTIN_ENV", "old")
    assert engine.resolve(raw).data["value"] == "old"
    monkeypatch.setenv("HB_CFG_BUILTIN_ENV", "new")
    assert engine.resolve(raw).data["value"] == "new"


@pytest.mark.parametrize("expression", ["${env:${name}}", "${oc.env:${name}}", '${oc.env:"HB_CFG_REF_ENV"}'])
def test_snapshot_tracks_nonbare_environment_names(monkeypatch, expression):
    config = _config({"name": "HB_CFG_REF_ENV", "value": expression})
    monkeypatch.setenv("HB_CFG_REF_ENV", "old")
    assert config.snapshot().get("value") == "old"
    monkeypatch.setenv("HB_CFG_REF_ENV", "new")
    assert config.snapshot().get("value") == "new"


@pytest.mark.parametrize("read", ["snapshot", "setdef_memory", "engine"])
@pytest.mark.parametrize("resolver", ["env", "oc.env"])
def test_transient_environment_value_does_not_poison_cache(monkeypatch, read, resolver):
    name = "HB_CFG_TRANSIENT_ENV"
    raw = {"value": f"${{{resolver}:{name}}}"}
    monkeypatch.setenv(name, "original")
    config = _config(raw)
    engine = _ExampleEngine(manager=None)
    if read == "engine":
        target, attr = engine_module, "_resolve_interpolation"

        def read_value():
            return engine.resolve(raw).get("value")

    else:
        target, attr = manager, "_resolve_interpolation"

        def read_value():
            if read == "snapshot":
                return config.snapshot(refresh=True).get("value")
            return config._memory_snapshot(tuple(config.scope_chain)).snapshot.get("value")

    interpolate = getattr(target, attr)
    changed = False

    def resolve_during_transient_value(data, policy, environment):
        nonlocal changed
        if data != raw or changed:
            return interpolate(data, policy, environment)
        changed = True
        with monkeypatch.context() as transient:
            transient.setenv(name, "transient")
            return interpolate(data, policy, environment)

    monkeypatch.setattr(target, attr, resolve_during_transient_value)
    assert read_value() == ("original" if resolver == "env" else "transient")
    assert changed
    assert read_value() == "original"


@pytest.mark.parametrize("expression", ["${${resolver}:HB_CFG_DYNAMIC_RESOLVER}", "${oc.${short}:HB_CFG_DYNAMIC_RESOLVER}"])
@pytest.mark.parametrize("read", ["snapshot", "engine"])
def test_computed_resolver_names_read_the_current_environment(monkeypatch, expression, read):
    raw = {"resolver": "oc.env", "short": "env", "value": expression}
    config = _config(raw)
    engine = _ExampleEngine(manager=None)
    monkeypatch.setenv("HB_CFG_DYNAMIC_RESOLVER", "first")
    first = config.snapshot(refresh=True) if read == "snapshot" else engine.resolve(raw)
    monkeypatch.setenv("HB_CFG_DYNAMIC_RESOLVER", "second")
    second = config.snapshot(refresh=True) if read == "snapshot" else engine.resolve(raw)
    assert first.get("value") == "first"
    assert second.get("value") == "second"


@pytest.mark.parametrize("placement", ["tuple_in_list", "mapping_key"])
def test_config_saves_signed_zero_in_tuples_and_keys(placement):
    config = _config({})
    if placement == "tuple_in_list":
        initial, updated = [(0.0,)], [(-0.0,)]

        def zero(value):
            return value[0][0]

    else:
        initial, updated = {0.0: "value"}, {-0.0: "value"}

        def zero(value):
            return next(iter(value))

    assert config.set("value", initial)
    assert config.set("value", updated)
    assert copysign(1, zero(config.layer()["value"])) == -1
    versions = [row["data"]["value"] for row in config.history(limit=2) if "value" in row["data"]]
    assert [copysign(1, zero(value)) for value in versions] == [-1, 1]


def test_opaque_environment_names_only_disable_caching_in_their_own_scope(monkeypatch):
    monkeypatch.setenv("HB_CFG_SCOPE_ENV", "first")
    config = _config({"value": "base"})
    original = config.snapshot()
    assert config.snapshot() is original

    with config.scoped("temporary"):
        assert config.set("name", "HB_CFG_SCOPE_ENV")
        assert config.set("value", "${oc.env:${name}}")
        assert config.snapshot().get("value") == "first"
        monkeypatch.setenv("HB_CFG_SCOPE_ENV", "second")
        assert config.snapshot().get("value") == "second"

    assert config.snapshot() is original
    assert config.remove(scope=f"{config.base_scope}.temporary")
    config.refresh()
    rebuilt = config.snapshot()
    assert rebuilt.get("value") == "base"
    assert config.snapshot() is rebuilt


def test_old_opaque_interpolation_does_not_disable_caching_in_current_layer(monkeypatch):
    monkeypatch.setenv("HB_CFG_SCOPE_ENV", "first")
    config = _config({"name": "HB_CFG_SCOPE_ENV"})
    assert config.set("value", "${oc.env:${name}}")
    assert config.snapshot().get("value") == "first"

    assert config.set("value", "${env:HB_CFG_SCOPE_ENV}")
    assert config.history(limit=2)[1]["data"]["value"] == "${oc.env:${name}}"
    first = config.snapshot()
    assert config.snapshot(refresh=True) is first
    monkeypatch.setenv("HB_CFG_SCOPE_ENV", "second")
    second = config.snapshot()
    assert second.get("value") == "second"
    assert config.snapshot() is second


def test_engine_reuses_a_spec_built_with_the_same_captured_environment(monkeypatch):
    engine = _ExampleEngine(manager=None)
    raw = {"value": "${env:HB_CFG_CAPTURED_ENV,42}"}
    monkeypatch.delenv("HB_CFG_CAPTURED_ENV", raising=False)
    missing = engine.resolve(raw)
    assert missing.get("value") == 42
    assert engine.resolve(raw) is missing

    monkeypatch.setenv("HB_CFG_CAPTURED_ENV", "")
    empty = engine.resolve(raw)
    assert empty.get("value") == ""
    assert engine.resolve(raw) is empty
