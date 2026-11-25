from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
import warnings

import pytest

from heavenbase.utils import ConfigEngine, ConfigSpec, resolve_interpolations, to_mutable_data


class SpyEngine(ConfigEngine):
    domain = "spy"
    binding_contracts = ("spy-route/v1",)

    def __init__(self, **kwargs):
        self.calls = {"catalog": 0, "normalize": 0, "bind": 0, "validate": 0, "apply": 0}
        self.catalog_revision = 1
        super().__init__(**kwargs)

    def _capture_catalog(self, root):
        self.calls["catalog"] += 1
        return self.catalog_revision, {"token": "secret", "route": "stable"}

    def _normalize(self, data, *, context):
        self.calls["normalize"] += 1
        out = dict(data)
        out.pop("desc", None)
        if "url" in out:
            out["endpoint"] = out.pop("url")
        out.setdefault("endpoint", "local")
        return out

    def _bind(self, data, *, context):
        self.calls["bind"] += 1
        return {"contract": "spy-route/v1", "route": context.catalog["route"], "token": context.catalog["token"]}

    def _validate(self, spec, *, context):
        self.calls["validate"] += 1
        if not spec.get("endpoint"):
            raise ValueError("spy.endpoint is required")

    def _default_profile(self, spec):
        return "runtime"


def _engine(**root):
    config = {
        "default_preset": "default",
        "presets": {"default": {"endpoint": "local", "nested": {"value": 1}}},
        **root,
    }
    engine = SpyEngine(manager=None, root=config)

    def apply_runtime(spec, **kwargs):
        engine.calls["apply"] += 1
        return {"data": to_mutable_data(spec.data), "context": dict(kwargs)}

    engine.register_profile("runtime", apply_runtime, aliases=("run",))
    return engine


def test_raw_and_applied_cache_hits_skip_expensive_hooks_and_isolate_mutation():
    engine = _engine()

    first = engine.resolve({"endpoint": "custom"})
    second = engine.resolve({"endpoint": "custom"})
    output = engine.apply({"endpoint": "custom"}, context="one")
    output["data"]["nested"]["value"] = 99
    cached_output = engine.apply({"endpoint": "custom"}, context="one")

    assert first is second
    assert cached_output["data"]["nested"]["value"] == 1
    assert engine.calls["normalize"] == 1
    assert engine.calls["bind"] == 1
    assert engine.calls["validate"] == 1
    assert engine.calls["apply"] == 1


def test_compatible_restored_spec_skips_source_normalize_and_bind():
    source_engine = _engine()
    spec = source_engine.resolve({"endpoint": "custom"})
    restored = ConfigSpec.from_dict(spec.to_dict())
    engine = _engine()

    first = engine.resolve(restored)
    second = engine.resolve(restored)

    assert first == second == spec
    assert engine.calls == {"catalog": 0, "normalize": 0, "bind": 0, "validate": 1, "apply": 0}

    bad = ConfigSpec("spy", spec.data, bindings={"contract": "unknown/v1"})
    with pytest.raises(ValueError, match="Unknown binding contract"):
        engine.resolve(bad)


def test_assert_idempotent_is_uncached_source_stable_and_stage_limited():
    engine = _engine()
    before = (len(engine._resolved_cache), len(engine._applied_cache))

    spec = engine.assert_idempotent({"url": "custom"})

    assert spec.get("endpoint") == "custom"
    assert engine.calls == {"catalog": 1, "normalize": 2, "bind": 2, "validate": 0, "apply": 0}
    assert (len(engine._resolved_cache), len(engine._applied_cache)) == before


def test_assert_idempotent_reports_binding_path_without_secret_values():
    class UnstableEngine(SpyEngine):
        domain = "unstable"

        def _bind(self, data, *, context):
            self.calls["bind"] += 1
            return {"contract": "spy-route/v1", "secret": f"credential-{self.calls['bind']}"}

    engine = UnstableEngine(manager=None)

    with pytest.raises(RuntimeError, match=r"bindings.secret \([0-9a-f]{12} != [0-9a-f]{12}\)") as exc:
        engine.assert_idempotent({})

    assert "credential" not in str(exc.value)


def test_profile_registry_replacement_updates_aliases_and_only_invalidates_applied_results():
    engine = _engine()
    spec = engine.resolve({})
    first = engine.apply(spec)
    counts = dict(engine.calls)

    with pytest.raises(ValueError, match="identifier collision"):
        engine.register_profile("run", lambda resolved: {"version": 0})
    engine.register_profile("runtime", lambda resolved: {"version": 2}, aliases=("execute",), replace=True)
    second = engine.apply(spec, profile="execute")

    assert "data" in first
    assert second == {"version": 2}
    assert engine.profiles()["runtime"]["aliases"] == ("execute",)
    with pytest.raises(ValueError, match="Unknown config profile"):
        engine.apply(spec, profile="run")
    assert engine.calls["normalize"] == counts["normalize"]
    assert engine.calls["bind"] == counts["bind"]
    assert engine.calls["validate"] == counts["validate"]


def test_catalog_revision_invalidates_raw_resolution_but_not_restored_specs():
    engine = _engine()
    spec = engine.resolve({})
    engine.catalog_revision += 1

    engine.resolve({})
    raw_counts = dict(engine.calls)
    engine.resolve(ConfigSpec.from_dict(spec.to_dict()))
    engine.resolve(ConfigSpec.from_dict(spec.to_dict()))

    assert raw_counts["normalize"] == 2
    assert raw_counts["bind"] == 2
    assert engine.calls["normalize"] == raw_counts["normalize"]
    assert engine.calls["bind"] == raw_counts["bind"]
    assert engine.calls["validate"] == raw_counts["validate"] + 1


def test_direct_environment_interpolation_invalidates_resolved_cache(monkeypatch):
    engine = _engine()
    monkeypatch.setenv("HB_SPY_ENDPOINT", "first")

    first = engine.resolve({"endpoint": "${env:HB_SPY_ENDPOINT}"})
    monkeypatch.setenv("HB_SPY_ENDPOINT", "second")
    second = engine.resolve({"endpoint": "${env:HB_SPY_ENDPOINT}"})

    assert first.get("endpoint") == "first"
    assert second.get("endpoint") == "second"
    assert second is not first
    assert engine.calls["normalize"] == 2


def test_environment_interpolation_keeps_literal_defaults_without_warnings(monkeypatch):
    """Every supported OmegaConf resolves `${env:NAME,5432}` silently and keeps the int default."""
    monkeypatch.delenv("HB_SPY_UNSET_PORT", raising=False)
    monkeypatch.setenv("HB_SPY_SET_PORT", "7")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolved = resolve_interpolations({"unset": "${env:HB_SPY_UNSET_PORT,5432}", "set": "${env:HB_SPY_SET_PORT,5432}", "empty": "${env:HB_SPY_UNSET_PORT}"})

    assert resolved == {"unset": 5432, "set": "7", "empty": ""}
    assert type(resolved["unset"]) is int


def test_concurrent_identical_calls_execute_each_expensive_hook_once():
    engine = _engine()

    with ThreadPoolExecutor(max_workers=8) as pool:
        specs = list(pool.map(lambda _: engine.resolve({"endpoint": "shared"}), range(32)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(lambda _: engine.apply(specs[0]), range(32)))

    assert all(spec == specs[0] for spec in specs)
    assert all(output == outputs[0] for output in outputs)
    assert engine.calls["normalize"] == 1
    assert engine.calls["bind"] == 1
    assert engine.calls["validate"] == 2
    assert engine.calls["apply"] == 1


def test_unregistered_non_json_profile_values_bypass_applied_cache():
    engine = _engine()
    values = []

    def live_value(spec):
        value = object()
        values.append(value)
        return value

    engine.register_profile("live", live_value)
    first = engine.apply({}, profile="live")
    second = engine.apply({}, profile="live")

    assert first is values[0]
    assert second is values[1]
    assert first is not second
    assert engine.profiles()["live"]["cacheable"] is True


def test_cache_bounds_and_zero_disable_are_enforced():
    bounded = _engine(cache={"max_resolved": 1, "max_applied": 1})
    bounded.resolve({"endpoint": "one"})
    bounded.resolve({"endpoint": "two"})
    bounded.apply({"endpoint": "one"})
    bounded.apply({"endpoint": "two"})

    assert len(bounded._resolved_cache) <= 1
    assert len(bounded._applied_cache) <= 1

    disabled = _engine(cache={"max_resolved": 0, "max_applied": 0})
    disabled.apply({})
    disabled.apply({})
    assert not disabled._resolved_cache
    assert not disabled._applied_cache
    assert disabled.calls["normalize"] == 2
    assert disabled.calls["apply"] == 2

    with pytest.raises(ValueError, match="max_resolved"):
        _engine(cache={"max_resolved": -1})
