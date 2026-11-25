from __future__ import annotations

from collections.abc import Mapping

import pytest

import heavenbase as hb
from heavenbase.utils import ConfigEngine, ConfigSpec, InterpolationPolicy, resolve_interpolations, to_mutable_data


class DemoConfig(hb.Entity):
    """Entity schema used to prove optional schema canonicalization."""

    identifier = "demo-config"

    preset = hb.field(hb.ShortText).optional()
    service = hb.field(hb.ShortText).required()
    retries = hb.field(hb.Integer).default(2)
    params = hb.field(hb.Json).default({})


class DemoEngine(ConfigEngine):
    domain = "demo-config"
    schema = DemoConfig
    extra = "collect"
    extras_field = "params"

    def _normalize(self, data: Mapping, *, context):
        return {key: value for key, value in data.items() if value is not None}

    def _validate(self, spec: ConfigSpec, *, context) -> None:
        if spec.get("retries") < 0:
            raise ValueError("demo-config.retries must be non-negative")

    def _default_profile(self, spec: ConfigSpec) -> str:
        return "runtime"


def _demo_engine() -> DemoEngine:
    engine = DemoEngine(
        manager=None,
        root={
            "default_preset": "worker",
            "presets": {"worker": {"service": "worker", "params": {"seed": 42}}},
        },
    )
    engine.register_profile("runtime", lambda spec: to_mutable_data(spec.data))
    return engine


def test_config_spec_is_recursively_immutable_and_behaviorally_equal():
    engine = _demo_engine()
    spec = engine.resolve({"timeout": 30})
    restored = ConfigSpec.from_dict(spec.to_dict())
    diagnostic_variant = ConfigSpec(spec.domain, spec.data, bindings=spec.bindings, raw={"different": True}, meta={"trace": "other"})

    assert spec.get("service") == "worker"
    assert spec.get("retries") == 2
    assert spec.get("params") == {"seed": 42, "timeout": 30}
    assert restored == spec == diagnostic_variant
    assert hash(restored) == hash(spec) == hash(diagnostic_variant)
    assert engine.resolve(restored) == spec

    with pytest.raises(TypeError):
        spec.data["service"] = "mutated"
    with pytest.raises(TypeError):
        spec.data["params"]["seed"] = 0

    exported = spec.to_dict()
    exported["data"]["params"]["seed"] = 0
    assert spec.get("params.seed") == 42


def test_schema_normalization_runs_after_domain_shorthand_and_before_bind():
    events = []

    class StageEngine(ConfigEngine):
        domain = "demo-config"
        schema = DemoConfig
        extra = "forbid"

        def _normalize(self, data, *, context):
            events.append("normalize")
            out = dict(data)
            out["retries"] = out.pop("attempts")
            return out

        def _bind(self, data, *, context):
            events.append(("bind", type(data["retries"]), data["retries"]))
            return {}

        def _validate(self, spec, *, context):
            events.append("validate")

    spec = StageEngine(manager=None).resolve({"service": "api", "attempts": "3"})

    assert spec.get("retries") == 3
    assert events == ["normalize", ("bind", int, 3), "validate"]


def test_schema_validation_is_strict_and_domain_validation_does_not_repair():
    engine = _demo_engine()

    with pytest.raises(ValueError, match="non-negative"):
        engine.resolve({"retries": -1})

    class RequiredEngine(ConfigEngine):
        domain = "demo-config"
        schema = DemoConfig
        extra = "forbid"

        def _normalize(self, data, *, context):
            return data

        def _validate(self, spec, *, context):
            raise AssertionError("domain validation must not run after schema failure")

    with pytest.raises(ValueError, match="service is required"):
        RequiredEngine(manager=None).resolve({})


def test_whole_template_presets_merge_and_explicit_null_disables_default():
    class GuiEngine(ConfigEngine):
        domain = "gui"

        def _normalize(self, data, *, context):
            return {"theme": "light", **dict(data)}

        def _validate(self, spec, *, context):
            if spec.get("theme") not in {"light", "dark"}:
                raise ValueError("gui.theme must be 'light' or 'dark'")

    engine = GuiEngine(
        manager=None,
        root={
            "default_preset": "desktop",
            "presets": {"desktop": {"theme": "dark", "nested": {"a": 1, "b": 2}, "items": [1, 2]}},
        },
    )

    defaulted = engine.resolve({"nested": {"b": 3}, "items": [9]})
    disabled = engine.resolve({"preset": None})

    assert defaulted.get("preset") == "desktop"
    assert defaulted.get("nested") == {"a": 1, "b": 3}
    assert defaulted.get("items") == (9,)
    assert disabled.get("preset") is None
    assert disabled.get("theme") == "light"
    assert engine.assert_idempotent(defaulted) == defaulted


def test_profiles_are_explicit_and_no_print_profile_is_implicit():
    class BareEngine(ConfigEngine):
        domain = "bare"

    engine = BareEngine(manager=None)

    assert engine.profiles() == {}
    with pytest.raises(ValueError, match="requires an explicit profile"):
        engine.apply({})
    with pytest.raises(ValueError, match="Unknown config profile"):
        engine.apply({}, profile="print")


def test_public_orchestration_cannot_be_overridden():
    with pytest.raises(TypeError, match="final ConfigEngine"):

        class InvalidEngine(ConfigEngine):
            domain = "invalid"

            def resolve(self, config=None):
                return config


def test_cmd_interpolation_obeys_policy():
    class CommandEngine(ConfigEngine):
        domain = "command"

    engine = CommandEngine(manager=None, policy=InterpolationPolicy(allow_cmd=False))

    with pytest.raises(Exception):
        engine.resolve({"value": "${cmd:echo should-not-run}"})


def test_resolve_interpolations_helper_is_mapping_based(monkeypatch):
    monkeypatch.setenv("HB_CONFIG_SPEC_HELPER", "ok")

    assert resolve_interpolations({"value": "${env:HB_CONFIG_SPEC_HELPER}"}) == {"value": "ok"}


def test_public_config_spec_exports_are_clean():
    assert hb.ConfigEngine is ConfigEngine
    assert hb.ConfigSpec is ConfigSpec
    assert callable(hb.resolve_interpolations)
    for name in ("Config" + tail for tail in ("Source", "Choice", "System", "Struct", "Field")):
        assert not hasattr(hb, name)


def test_spec_cache_identity_is_memoized_and_keys_engine_caches(monkeypatch):
    from heavenbase.utils.config import engine as engine_module

    engine = _demo_engine()
    spec = engine.resolve({"service": "svc", "retries": 3})
    seen = []
    fingerprint = engine_module._fingerprint

    def counting(value):
        if isinstance(value, tuple) and len(value) == 3 and value[0] == spec.domain:
            seen.append(value)
        return fingerprint(value)

    monkeypatch.setattr(engine_module, "_fingerprint", counting)
    identity = spec._cache_identity

    assert identity is spec._cache_identity
    assert identity == fingerprint((spec.domain, spec.data, spec.bindings))
    assert engine.resolve(spec) is spec
    assert engine.apply(spec, profile="runtime") == engine.apply(spec, profile="runtime")
    # one spec object fingerprints its behavioral identity exactly once
    assert seen == [(spec.domain, spec.data, spec.bindings)]

    uncacheable = ConfigSpec("demo-config", {"service": "svc"}, bindings={"client": object()})
    assert uncacheable._cache_identity is None
    assert engine._spec_cache_key(uncacheable) is engine_module._MISSING
