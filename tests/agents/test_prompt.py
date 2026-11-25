import inspect
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import heavenbase as hb
from heavenbase.extensions.prompt.name import check_prompt_name, prompt_row_id, translation_row_id
from heavenbase.extensions.prompt.prompt import ensure_prompt_entities
from heavenbase.utils.files import pj
from heavenbase.utils import sha256hash
from heavenbase.utils.config import ConfigManager


@contextmanager
def _default_extensions(value):
    old_value = hb.CM_HVNB.get("heavenbase.extensions.default", default=None)
    try:
        hb.CM_HVNB.set("heavenbase.extensions.default", value)
        yield
    finally:
        if old_value is None:
            hb.CM_HVNB.unset("heavenbase.extensions.default")
        else:
            hb.CM_HVNB.set("heavenbase.extensions.default", old_value)


def prompt_greet(name: str, *, tr=str) -> str:
    return f"{tr('Hello')}, {name}!"


def prompt_salute(name: str, *, tr=str) -> str:
    return f"{tr('Hi')}, {name}!"


def _prompt_context(tmp_path, name, config):
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path / name),
            "workspace": "default",
            "backend": {
                "type": "inmem",
                "module": "heavenbase.backends.inmem.backend",
                "name": "system",
            },
            "registry": {},
        },
        config=config,
    )
    context._setup_modules(force=True)
    return context


def test_prompt_public_export_and_config_default():
    assert hb.Prompt is not None
    assert issubclass(hb.Prompt, hb.Entity)
    assert issubclass(hb.Translation, hb.Entity)
    assert hb.CM_HVNB.get("heavenbase.prompt.main_lang") == "en"


def test_prompt_defaults_follow_bound_workspace_config(tmp_path):
    left = ConfigManager(default={}, setup=False)
    right = ConfigManager(default={}, setup=False)
    left.set("heavenbase.prompt.main_lang", "fr")
    left.set("heavenbase.prompt.lang", "fr-ca")
    left.set("heavenbase.prompt.elicit", "llm")
    right.set("heavenbase.prompt.main_lang", "de")
    right.set("heavenbase.prompt.lang", "de-ch")
    left_context = _prompt_context(tmp_path, "left", left)
    right_context = _prompt_context(tmp_path, "right", right)
    try:
        left_ws = SimpleNamespace(context=left_context, module_resolver=left_context.modules())
        right_ws = SimpleNamespace(context=right_context, module_resolver=right_context.modules())

        left_prompt = hb.Prompt("Hello", name="config.left", ws=left_ws)
        right_prompt = hb.Prompt("Hello", name="config.right", ws=right_ws)

        assert (left_prompt.main_lang, left_prompt.elicit, left_prompt._resolve_lang(None)) == ("fr", "llm", "fr-ca")
        assert (right_prompt.main_lang, right_prompt.elicit, right_prompt._resolve_lang(None)) == ("de", "none", "de-ch")
    finally:
        left_context.close()
        right_context.close()


def test_prompt_late_workspace_binding_replaces_only_omitted_defaults(tmp_path):
    config = ConfigManager(default={}, setup=False)
    config.set("heavenbase.prompt.main_lang", "ja")
    config.set("heavenbase.prompt.elicit", "llm")
    context = _prompt_context(tmp_path, "late", config)
    workspace = SimpleNamespace(context=context, module_resolver=context.modules())
    defaulted = hb.Prompt("Hello", name="config.defaulted")
    explicit = hb.Prompt("Hello", name="config.explicit", main_lang="en", elicit="none")
    try:
        defaulted._bind_workspace(workspace)
        explicit._bind_workspace(workspace)

        assert (defaulted.main_lang, defaulted.elicit) == ("ja", "llm")
        assert (explicit.main_lang, explicit.elicit) == ("en", "none")
    finally:
        context.close()


@pytest.mark.parametrize("name", ["demo.greet", "agent.autofunc.base", "toolkit_qa.prompt-1"])
def test_prompt_name_accepts_dotted_safe_segments(name):
    assert check_prompt_name(name) == name


@pytest.mark.parametrize("name", [".greet", "Demo.Greet", "demo..greet", "demo/greet", "1demo.greet"])
def test_prompt_name_rejects_unsafe_segments(name):
    with pytest.raises(ValueError):
        check_prompt_name(name)


def test_prompt_row_ids_are_stable_for_name_version():
    row_id = prompt_row_id("demo.greet", "2")
    assert row_id == prompt_row_id("demo.greet", "2")
    assert row_id != prompt_row_id("demo.greet", "1")


def test_prompt_extension_entities_register_and_roundtrip():
    ws = hb.HeavenBase("prompt-entity-test")
    ensure_prompt_entities(ws)
    assert {"sys-prompt", "sys-translation"} <= set(ws.entities)

    prompt_id = prompt_row_id("demo.greet", "1")
    tr_id = translation_row_id("demo.greet", "Hello", "zh")
    ws.upsert(hb.Prompt, {"object_id": prompt_id, "name": "demo.greet", "version": "1"})
    ws.upsert(hb.Translation, {"object_id": tr_id, "dict_name": "demo.greet", "source_key": "Hello", "lang": "zh", "target_value": "HELLO"})

    assert ws.get(prompt_id, entity=hb.Prompt)["name"] == "demo.greet"
    assert ws.get(tr_id, entity=hb.Translation)["target_value"] == "HELLO"


def test_ensure_prompt_entities_enables_prompt_when_defaults_disabled():
    with _default_extensions([]):
        ws = hb.HeavenBase("prompt-no-default-test", detached=True, backends={"main": {"type": "inmem"}})

        assert sorted(ws.extensions()) == ["capsule"]
        ensure_prompt_entities(ws)
        assert sorted(ws.extensions()) == ["capsule", "prompt"]
        assert {"sys-prompt", "sys-translation"} <= set(ws.entities)


def test_translation_helper_enables_prompt_extension_when_defaults_disabled():
    with _default_extensions([]):
        ws = hb.HeavenBase("translation-no-default-test", detached=True, backends={"main": {"type": "inmem"}})

        assert sorted(ws.extensions()) == ["capsule"]
        row_id = hb.Translation(ws=ws, dicts=["demo.translate"]).set("Hello", "zz", "HEY")

        assert sorted(ws.extensions()) == ["capsule", "prompt"]
        assert {"sys-prompt", "sys-translation"} <= set(ws.entities)
        assert ws.get(row_id, entity=hb.Translation)["target_value"] == "HEY"


def test_system_catalog_descriptions_use_medium_text():
    ws = hb.HeavenBase("prompt-long-desc-test")
    ensure_prompt_entities(ws)
    rows = ws.query(hb.MetaSchema).where({"kind": "entity"}).where({"subject_id": "sys-prompt"}).execute().rows()

    assert hb.MetaSchema.schema().get("desc").dtype.identifier == "medium-text"
    assert len(rows[0]["desc"]) > 255


def test_prompt_function_construction_renders_without_persistence():
    def greet(name: str, *, tr=str) -> str:
        return f"{tr('Hello')}, {name}!"

    prompt = hb.Prompt(greet, name="demo.greet")

    assert prompt("Ada") == "Hello, Ada!"
    assert prompt.__name__ == "greet"
    assert callable(prompt)
    assert list(inspect.signature(prompt).parameters) == ["name", "tr"]


def test_prompt_decorator_binds_function_and_returns_prompt():
    @hb.Prompt(name="demo.decorated")
    def greet(name: str, *, tr=str) -> str:
        """Decorated greeting."""
        return f"{tr('Hello')}, {name}!"

    assert isinstance(greet, hb.Prompt)
    assert greet("Ada") == "Hello, Ada!"
    assert greet.__name__ == "greet"
    assert greet.__doc__ == "Decorated greeting."


def test_unbound_prompt_rejects_render_attempts():
    prompt = hb.Prompt(name="demo.waiting")

    with pytest.raises(RuntimeError, match="not bound"):
        prompt("Ada")


def test_prompt_register_writes_sys_prompt_row_and_loads():
    ws = hb.HeavenBase("prompt-register-test")
    prompt = hb.Prompt(prompt_greet, name="demo.greet")

    prompt_id = prompt.register(ws=ws)
    row = ws.get(prompt_id, entity=hb.Prompt)
    loaded = hb.Prompt.load("demo.greet", ws=ws)

    assert row["name"] == "demo.greet"
    assert row["capsule"]["name"] == "demo.greet"
    assert loaded("Ada") == "Hello, Ada!"
    assert list(inspect.signature(loaded).parameters) == ["name", "tr"]


def test_prompt_from_dict_restores_capsule_manifest():
    ws = hb.HeavenBase("prompt-from-dict-test")
    prompt_id = hb.Prompt(prompt_greet, name="demo.from-dict").register(ws=ws)
    restored = hb.Prompt.from_dict(ws.get(prompt_id, entity=hb.Prompt))

    assert restored.to_dict()["capsule"]["name"] == "demo.from-dict"
    assert restored("Ada") == "Hello, Ada!"


def test_prompt_versions_and_latest_active_selection():
    ws = hb.HeavenBase("prompt-version-test")
    hb.Prompt(prompt_greet, name="demo.versioned", version="1").register(ws=ws)
    hb.Prompt(prompt_salute, name="demo.versioned", version="2").register(ws=ws)

    loaded = hb.Prompt.load("demo.versioned", ws=ws)

    assert hb.Prompt.versions("demo.versioned", ws=ws) == ["1", "2"]
    assert loaded.version == "2"
    assert loaded("Ada") == "Hi, Ada!"


def test_prompt_delete_tombstones_rows_for_load_and_list():
    ws = hb.HeavenBase("prompt-delete-test")
    hb.Prompt(prompt_greet, name="demo.delete", version="1").register(ws=ws)
    hb.Prompt(prompt_salute, name="demo.delete", version="2").register(ws=ws)

    assert hb.Prompt.delete("demo.delete:2", ws=ws)

    assert hb.Prompt.load("demo.delete", ws=ws).version == "1"
    assert [row["version"] for row in hb.Prompt.list(prefix="demo.delete", ws=ws)] == ["1"]
    with pytest.raises(KeyError):
        hb.Prompt.load("demo.delete:2", ws=ws)


def test_prompt_versions_delete_and_translations_are_workspace_isolated():
    ws_left = hb.HeavenBase("prompt-isolated-left", detached=True, backends={"main": {"type": "inmem"}})
    ws_right = hb.HeavenBase("prompt-isolated-right", detached=True, backends={"main": {"type": "inmem"}})
    hb.Prompt(prompt_greet, name="demo.isolated", version="1").register(ws=ws_left)
    hb.Prompt(prompt_salute, name="demo.isolated", version="2").register(ws=ws_left)
    hb.Prompt(prompt_greet, name="demo.isolated", version="1").register(ws=ws_right)
    hb.Prompt.load("demo.isolated", ws=ws_left).tr.set("Hello", "zz", "LEFT")
    hb.Prompt.load("demo.isolated", ws=ws_right).tr.set("Hello", "zz", "RIGHT")

    assert hb.Prompt.versions("demo.isolated", ws=ws_left) == ["1", "2"]
    assert hb.Prompt.versions("demo.isolated", ws=ws_right) == ["1"]
    assert hb.Prompt.load("demo.isolated", ws=ws_left, lang="zz")("Ada") == "Hi, Ada!"
    assert hb.Prompt.load("demo.isolated:1", ws=ws_left, lang="zz")("Ada") == "LEFT, Ada!"
    assert hb.Prompt.load("demo.isolated", ws=ws_right, lang="zz")("Ada") == "RIGHT, Ada!"

    assert hb.Prompt.delete("demo.isolated:2", ws=ws_left)
    assert hb.Prompt.load("demo.isolated", ws=ws_left, lang="zz")("Ada") == "LEFT, Ada!"
    assert hb.Prompt.load("demo.isolated", ws=ws_right, lang="zz")("Ada") == "RIGHT, Ada!"


def test_prompt_string_renders_template():
    prompt = hb.Prompt("Hello, {name}", name="demo.hello")

    assert prompt(name="Ada") == "Hello, Ada"


def test_prompt_template_persists_and_loads_as_capsule():
    ws = hb.HeavenBase("prompt-template-test")
    prompt = hb.Prompt("Hello, {name}", name="demo.template")

    prompt_id = prompt.register(ws=ws)
    row = ws.get(prompt_id, entity=hb.Prompt)
    loaded = hb.Prompt.load("demo.template", ws=ws)

    assert row["capsule"]["namespace"] == "prompt"
    assert loaded(name="Ada") == "Hello, Ada"


def test_prompt_string_missing_placeholder_adds_context():
    prompt = hb.Prompt("Hello, {name}", name="demo.missing")

    with pytest.raises(KeyError, match="demo.missing"):
        prompt()


def test_prompt_translation_exact_lookup_injected_at_render_time():
    ws = hb.HeavenBase("prompt-tr-exact-test")
    prompt = hb.Prompt(prompt_greet, name="demo.tr", ws=ws)

    assert isinstance(prompt.tr, hb.Translation)
    prompt.tr.set("Hello", "zz", "HEY")

    assert prompt.tr.get("Hello", "zz") == "HEY"
    assert prompt("Ada", lang="zz") == "HEY, Ada!"


def test_prompt_uses_config_scope_language():
    ws = hb.HeavenBase("prompt-config-lang-test")
    prompt = hb.Prompt(prompt_greet, name="demo.config-lang", ws=ws)
    prompt.tr.set("Hello", "zz", "HEY")
    hb.CM_HVNB.set("heavenbase.prompt.lang", "zz", scope="heavenbase.prompt-test")

    with hb.CM_HVNB.scoped("prompt-test"):
        assert prompt("Ada") == "HEY, Ada!"

    assert prompt("Ada") == "Hello, Ada!"


def test_prompt_language_precedence_is_call_bound_config_then_main():
    ws = hb.HeavenBase("prompt-lang-precedence-test")
    prompt_id = hb.Prompt(prompt_greet, name="demo.lang-order", ws=ws, lang="aa").register()
    prompt = hb.Prompt.load("demo.lang-order", ws=ws, lang="aa")
    prompt.tr.set_many({"aa": {"Hello": "AA"}, "bb": {"Hello": "BB"}, "cc": {"Hello": "CC"}})
    hb.CM_HVNB.set("heavenbase.prompt.lang", "cc", scope="heavenbase.prompt-lang-precedence")

    with hb.CM_HVNB.scoped("prompt-lang-precedence"):
        assert prompt("Ada") == "AA, Ada!"
        assert prompt("Ada", lang="bb") == "BB, Ada!"
        assert hb.Prompt.load(prompt_id, ws=ws)("Ada") == "CC, Ada!"

    assert hb.Prompt.load(prompt_id, ws=ws)("Ada") == "Hello, Ada!"


def test_translation_lookup_sees_rows_written_by_other_prompt_helpers():
    ws = hb.HeavenBase("prompt-translation-freshness-test")
    prompt = hb.Prompt(prompt_greet, name="demo.fresh", ws=ws)
    writer = hb.Prompt(prompt_greet, name="demo.fresh", ws=ws)

    assert prompt("Ada", lang="zz") == "Hello, Ada!"
    writer.tr.set("Hello", "zz", "HEY")

    assert prompt("Ada", lang="zz") == "HEY, Ada!"


def test_prompt_translation_pattern_lookup():
    ws = hb.HeavenBase("prompt-tr-pattern-test")
    prompt = hb.Prompt(name="demo.pattern", ws=ws)

    prompt.tr.set("Calculate F({n}).", "zz", "CALC F({n}).")

    assert prompt.tr.get("Calculate F(10).", "zz") == "CALC F(10)."


def test_prompt_translation_pattern_supports_repeated_placeholders():
    ws = hb.HeavenBase("prompt-tr-repeat-pattern-test")
    prompt = hb.Prompt(name="demo.repeat-pattern", ws=ws)

    prompt.tr.set("Pair {name} with {name}.", "zz", "PAIR {name}.")

    assert prompt.tr.get("Pair Ada with Ada.", "zz") == "PAIR Ada."
    assert prompt.tr.get("Pair Ada with Bob.", "zz") == "Pair Ada with Bob."


def test_prompt_translation_multiple_refs_follow_order():
    ws = hb.HeavenBase("prompt-tr-refs-test")
    prompt = hb.Prompt(prompt_greet, name="demo.refs", ws=ws)

    prompt.tr.bind("shared.agent-ui")
    prompt.tr.set("Hello", "zz", "SHARED", dict_name="shared.agent-ui")
    assert prompt("Ada", lang="zz") == "SHARED, Ada!"

    prompt.tr.set("Hello", "zz", "OWN")
    assert prompt("Ada", lang="zz") == "OWN, Ada!"


def test_prompt_translation_bind_persists_fresh_manifest_fingerprint():
    ws = hb.HeavenBase("prompt-tr-fingerprint-test")
    prompt_id = hb.Prompt(prompt_greet, name="demo.fingerprint", ws=ws).register()
    prompt = hb.Prompt.load("demo.fingerprint", ws=ws)

    prompt.tr.bind("shared.agent-ui")
    row = ws.get(prompt_id, entity=hb.Prompt)

    assert row["manifest"]["dicts"] == ["demo.fingerprint", "shared.agent-ui"]
    assert row["fingerprints"]["manifest"] == sha256hash(row["manifest"])


def test_prompt_translation_delete_and_llm_elicit():
    ws = hb.HeavenBase("prompt-tr-delete-test")
    prompt = hb.Prompt(prompt_greet, name="demo.delete-tr", ws=ws)
    prompt.tr.set("Hello", "zz", "HEY")

    assert prompt.tr.delete("Hello", "zz")
    assert prompt.tr.get("Hello", "zz") == "Hello"

    llm_prompt = hb.Prompt(prompt_greet, name="demo.llm-tr", ws=ws, elicit="llm")
    with pytest.raises(NotImplementedError, match="elicit='llm'"):
        llm_prompt("Ada", lang="zz")


def test_prompt_string_translates_template_and_selected_values():
    ws = hb.HeavenBase("prompt-template-tr-test")
    prompt = hb.Prompt("Hello, {name}! Welcome to {place}", name="demo.welcome", tr_keys=["place"], ws=ws)

    prompt.tr.set("Hello, {name}! Welcome to {place}", "zz", "HEY, {name}! GO {place}")
    prompt.tr.set("Tokyo", "zz", "TOKIO")

    assert prompt(name="Ada", place="Tokyo", lang="zz") == "HEY, Ada! GO TOKIO"


def test_prompt_capsule_restore_runs_after_changing_cwd(tmp_path, monkeypatch):
    ws = hb.HeavenBase("prompt-cwd-test")
    hb.Prompt(prompt_greet, name="demo.cwd").register(ws=ws)

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.chdir(pj(str(other_dir)))
    loaded = hb.Prompt.load("demo.cwd", ws=ws)

    assert loaded._resolver is ws.module_resolver
    assert loaded._config is ws.context.config
    assert loaded("Ada") == "Hello, Ada!"


def test_prompt_capsule_restore_surfaces_checksum_mismatch():
    ws = hb.HeavenBase("prompt-checksum-test")
    prompt_id = hb.Prompt(prompt_greet, name="demo.checksum").register(ws=ws)
    row = ws.get(prompt_id, entity=hb.Prompt)
    layer = next(layer for layer in row["capsule"]["restore_layers"] if layer["type"] == "source")
    layer["source"] = layer["source"].replace("Hello", "Hola")
    row["object_id"] = prompt_id
    ws.set(row, entity=hb.Prompt)

    with pytest.raises(Exception, match="source checksum mismatch"):
        hb.Prompt.load("demo.checksum", ws=ws)("Ada")


def test_prompt_workspace_resolution_surfaces_registry_errors(monkeypatch):
    def fail_load(*args, **kwargs):
        raise ValueError("registry boom")

    monkeypatch.setattr(hb.HeavenBase, "load", fail_load)

    with pytest.raises(ValueError, match="registry boom"):
        hb.Prompt.list()


def test_translation_workspace_resolution_surfaces_registry_errors(monkeypatch):
    def fail_load(*args, **kwargs):
        raise ValueError("registry boom")

    monkeypatch.setattr(hb.HeavenBase, "load", fail_load)

    with pytest.raises(ValueError, match="registry boom"):
        hb.Prompt(name="demo.registry-error").tr.get("Hello", "zz")
