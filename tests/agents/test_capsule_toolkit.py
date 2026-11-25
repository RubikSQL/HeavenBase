from __future__ import annotations

import gc
import inspect
import math
import weakref
from collections.abc import Callable as ABCCallable
from dataclasses import FrozenInstanceError

import pytest

import heavenbase as hb
from heavenbase.capsule import CapsuleRegistry, RestorePolicy, capsule_registry, reset_capsule_registry
from heavenbase.capsule.manifest import CapsuleManifest
from heavenbase.capsule.serialize import CallableSerialization
from heavenbase.utils import pj, touch_dir
from heavenbase.utils.config import ConfigManager


def add(left: int, right: int) -> int:
    """Add two integers.

    Args:
        left: First integer.
        right: Second integer.

    Returns:
        Sum of both integers.
    """
    return left + right


def _build_unit_capsule_ref(ctx: hb.ext.CapsuleLayerBuildContext) -> dict:
    """Build the test Capsule reference layer."""
    del ctx
    return {"type": "unit_capsule_ref", "target": "add"}


def _restore_unit_capsule_ref(manifest, layer, policy):
    """Restore the test Capsule reference layer."""
    del manifest, policy
    if layer["target"] != "add":
        raise ValueError("unexpected custom layer target")
    return add


def mul(left: int, right: int) -> int:
    """Multiply two integers."""
    return left * right


def sub(left: int, right: int) -> int:
    """Subtract right from left."""
    return left - right


def div(left: int, right: int) -> float:
    """Divide left by right."""
    if right == 0:
        raise ValueError("division by zero")
    return left / right


def mod(left: int, right: int) -> int:
    """Return left modulo right."""
    if right == 0:
        raise ValueError("modulo by zero")
    return left % right


def fibonacci(number: int) -> int:
    """Return the Fibonacci number for a non-negative index."""
    if number < 0:
        raise ValueError("fibonacci needs a non-negative number")
    previous = 0
    current = 1
    for _ in range(number):
        previous, current = current, previous + current
    return previous


def ceil_half(value: int) -> int:
    """Ceil value divided by two."""
    return math.ceil(value / 2)


def query_rows() -> list[dict[str, object]]:
    """Return table-shaped rows."""
    return [{"name": "Desk", "stock": 7}, {"name": "Lamp", "stock": 2}]


def rows_markdown(rows: list[dict[str, object]]) -> str:
    """Return table-shaped rows as markdown."""
    lines = ["| name | stock |", "| --- | --- |"]
    lines.extend(f"| {row['name']} | {row['stock']} |" for row in rows)
    return "\n".join(lines)


def capsule_text_serializer(output, *, config) -> str:
    """Serialize a value through a registered Capsule."""
    del config
    return f"capsule:{output}"


def offset_tool():
    offset = 7

    def add_offset(value: int) -> int:
        return value + offset

    return add_offset


def unresolved_default_tool():
    helper = object()

    def use_helper(value: int, selected=helper) -> int:
        return value

    return use_helper


def math_toolkit() -> hb.Toolkit:
    toolkit = hb.Toolkit("math-tools", description="Basic arithmetic Toolkit", namespace="demo", version="1")
    for func in (add, mul, sub, div, mod, fibonacci):
        toolkit.add(func, name=func.__name__, namespace="demo", version="1")
    return toolkit


def registry_config(tmp_path) -> dict:
    return {
        "workspace": "math-capsule-test",
        "backend": {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(pj(str(tmp_path), "registry.db"))}},
    }


def toolkit_workspace(tmp_path, prefix: str = "toolkit-test") -> hb.HeavenBase:
    workspace_id = f"{prefix}-{hb.utils.sha256hash(str(tmp_path))[:12]}"
    return hb.HeavenBase(
        workspace_id,
        backends={
            "main": {
                "type": "sqlite",
                "name": "main",
                "database": hb.utils.path_to_file_uri(pj(str(tmp_path), "workspace.db")),
            }
        },
        detached=True,
    )


def _isolated_context(root) -> hb.Context:
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {
                "type": "inmem",
                "module": "heavenbase.backends.inmem.backend",
                "name": "system",
            },
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )


def test_root_runtime_exports_remain_user_facing():
    assert hb.Prompt.__name__ == "Prompt"
    assert issubclass(hb.Prompt, hb.Capsule)
    assert hb.Capsule.schema().entity_id == "sys-capsule"
    assert RestorePolicy.__module__ == "heavenbase.capsule.restore"
    assert not hasattr(hb.Capsule, "decorator")
    assert callable(hb.Capsule.from_func)
    assert callable(hb.Capsule.from_code)
    assert callable(hb.Capsule.from_manifest)
    assert callable(hb.Capsule.from_row)
    assert callable(hb.Capsule.from_str)
    assert hb.Tool.__name__ == "Tool"
    assert hb.Toolkit.schema().entity_id == "sys-toolkit"


def test_restore_policy_normalizes_layers_and_is_immutable():
    policy = RestorePolicy(layers=["source", "import"])

    assert policy.layers == ("source", "import")
    with pytest.raises(FrozenInstanceError):
        policy.allow_source = False


@pytest.mark.slow
def test_capsule_registry_loads_source_from_another_cwd(tmp_path, monkeypatch):
    config = registry_config(tmp_path)
    capsule = hb.Capsule(add, name="add", namespace="demo", version="1", include_cloudpickle=False)
    capsule_id = capsule.register(registry_config=config)

    other_dir = touch_dir(pj(str(tmp_path), "other"))
    monkeypatch.chdir(other_dir)

    loaded = hb.Capsule.load(capsule_id, registry_config=config)
    assert loaded.run(left=2, right=3) == 5
    assert pj(".", abs=True) == other_dir


def test_capsule_defaults_to_source_only_capture():
    capsule = hb.Capsule(add, name="source_only_add", namespace="demo", version="1")
    toolkit = hb.Toolkit("source-only-tools", namespace="demo")
    toolkit.add(add, name="add")

    assert "cloudpickle" not in {layer.get("type") for layer in capsule.manifest.restore_layers}
    assert "cloudpickle" not in {layer.get("type") for layer in toolkit.tools["add"].capsule.manifest.restore_layers}
    assert capsule.manifest.input_schema["properties"] == {
        "left": {"type": "integer"},
        "right": {"type": "integer"},
    }
    assert capsule.manifest.output_schema == {"type": "integer"}


def test_capsule_explicit_layers_replace_capture_flags():
    capsule = hb.Capsule(add, name="cloudpickle_add", namespace="demo", version="1", layers=("cloudpickle",))

    assert [layer.get("type") for layer in capsule.manifest.restore_layers] == ["cloudpickle"]
    assert capsule.manifest.trust["allow_binary_payload"] is True
    assert hb.Capsule.from_manifest(capsule.to_dict()).to_func(policy=RestorePolicy(allow_binary=True))(left=2, right=3) == 5


def test_capsule_restore_policy_selects_layer_order():
    capsule = hb.Capsule(add, name="layered_add", namespace="demo", version="1", layers=("source", "cloudpickle"))
    source_layer = next(layer for layer in capsule.manifest.restore_layers if layer["type"] == "source")
    source_layer["source"] = source_layer["source"].replace("left + right", "left - right")
    restored = hb.Capsule.from_manifest(capsule.to_dict()).to_func(policy=RestorePolicy(layers=["cloudpickle"], allow_binary=True))

    assert restored(left=2, right=3) == 5


def test_capsule_unknown_capture_layer_raises():
    with pytest.raises(ValueError, match="Capsule capture layer"):
        hb.Capsule(add, name="bad_layer_add", namespace="demo", version="1", layers=("missing-layer",))


def test_capsule_custom_layer_registration_round_trip():
    layer_name = "unit_capsule_ref"

    hb.ext.register_capsule_layer(layer_name, builder=_build_unit_capsule_ref, restorer=_restore_unit_capsule_ref, replace=True)
    capsule = hb.Capsule(add, name="custom_layer_add", namespace="demo", version="1", layers=(layer_name,))
    restored = hb.Capsule.from_manifest(capsule.to_dict()).to_func()
    definition = hb.DEFAULT_CONTEXT.modules().inspect("capsule_layer", layer_name)[0]["record"]["meta"]["definition"]

    assert layer_name in hb.ext.registered_capsule_layers()
    assert definition["builder"]["source"] == "python-import"
    assert definition["restorer"]["kind"] == "heavenbase.executable"
    assert capsule.manifest.restore_layers == [{"type": layer_name, "target": "add", "priority": 100}]
    assert restored(left=2, right=3) == 5


def test_registered_capsule_can_implement_serializer_without_implicit_persistence():
    serializer_name = "unit-capsule-serializer"
    capsule = hb.Capsule(capsule_text_serializer, name=serializer_name, namespace="tests", version="1")
    service = hb.DEFAULT_CONTEXT.modules()

    with pytest.raises(ValueError, match="must be explicitly registered"):
        hb.ext.ToolSerializer(serializer_name, capsule).register(resolver=service)
    assert not service.inspect("serializer", serializer_name)

    capsule_id = capsule.register(reason="serializer executable reference")
    try:
        registered = hb.ext.ToolSerializer(serializer_name, capsule).register(resolver=service)
        reference = service.inspect("serializer", serializer_name)[0]["record"]["meta"]["definition"]["serialize"]

        assert reference == {
            "kind": "heavenbase.executable",
            "version": 1,
            "source": "capsule",
            "target": capsule_id,
        }
        assert isinstance(registered.serialize, hb.Capsule)
        assert registered("value") == "capsule:value"
    finally:
        # Leave the worker's default Context without this serializer, so a later drop of its Capsule cannot leave it dangling.
        if service.inspect("serializer", serializer_name):
            service.unregister("serializer", serializer_name)
        capsule_registry().delete(capsule_id, tombstone=False)


def test_capsule_layer_contract_lives_in_layer_module():
    from heavenbase.capsule import CapsuleLayerBuildContext as PackageContext
    from heavenbase.capsule import CapsuleLayer as PackageLayer
    from heavenbase.capsule.layer import CapsuleLayer, CapsuleLayerBuildContext

    assert hb.ext.CapsuleLayer is CapsuleLayer
    assert hb.ext.CapsuleLayerBuildContext is CapsuleLayerBuildContext
    assert PackageLayer is CapsuleLayer
    assert PackageContext is CapsuleLayerBuildContext
    assert CapsuleLayer.__module__ == "heavenbase.capsule.layer"
    assert CapsuleLayerBuildContext.__module__ == "heavenbase.capsule.layer"


def test_capsule_load_rejects_source_checksum_mismatch(tmp_path):
    config = registry_config(tmp_path)
    registry = hb.capsule_registry(config)
    capsule = hb.Capsule(add, name="checksum_add", namespace="demo", version="1", include_cloudpickle=False)
    capsule_id = capsule.register(registry=registry)
    row = registry.store.get(capsule_id)
    layer = next(layer for layer in row["manifest"]["restore_layers"] if layer["type"] == "source")
    layer["source"] = layer["source"].replace("left + right", "left - right")
    registry.store.set(row)

    with pytest.raises(ValueError, match="source checksum mismatch"):
        hb.Capsule.load(capsule_id, registry=registry)


def test_capsule_verification_cache_is_scoped_to_payload_content(tmp_path):
    context = _isolated_context(tmp_path / "payload-cache")
    try:
        context._setup_modules()
        capsule = hb.Capsule(
            add,
            name="payload_cache_add",
            namespace="demo",
            resolver=context.modules(),
            config=context.config,
        )
        capsule.verify()
        tampered = capsule.to_dict()
        layer = next(layer for layer in tampered["restore_layers"] if layer["type"] == "source")
        layer["source"] = layer["source"].replace("left + right", "left - right")

        with pytest.raises(ValueError, match="source checksum mismatch"):
            hb.Capsule.from_manifest(tampered, resolver=context.modules(), config=context.config).verify()
    finally:
        context.close()


def test_capsule_verification_cache_does_not_retain_runtime_authority(tmp_path):
    context = _isolated_context(tmp_path / "authority-lifetime")
    context._setup_modules()
    resolver = context.modules()
    config = context.config
    manifest = CapsuleManifest.from_dict(hb.Capsule(add, name="authority_lifetime_add", namespace="demo", resolver=resolver, config=config).to_dict())
    manifest.verify(resolver=resolver, config=config)
    resolver_ref = weakref.ref(resolver)
    config_ref = weakref.ref(config)

    context.close()
    del resolver, config, context
    gc.collect()

    assert resolver_ref() is None
    assert config_ref() is None


def test_capsule_registry_reset_is_context_scoped(tmp_path):
    first = _isolated_context(tmp_path / "first-registry")
    second = _isolated_context(tmp_path / "second-registry")
    first._setup_modules()
    second._setup_modules()
    first_registry = capsule_registry(resolver=first.modules(), config=first.config)
    second_registry = capsule_registry(resolver=second.modules(), config=second.config)
    try:
        reset_capsule_registry(resolver=first.modules(), config=first.config)

        assert capsule_registry(resolver=first.modules(), config=first.config) is not first_registry
        assert capsule_registry(resolver=second.modules(), config=second.config) is second_registry
    finally:
        reset_capsule_registry(resolver=first.modules(), config=first.config)
        reset_capsule_registry(resolver=second.modules(), config=second.config)
        first.close()
        second.close()


@pytest.mark.fast
def test_capsule_registry_is_capsule_only_and_accepts_an_injected_store(tmp_path):
    context = _isolated_context(tmp_path / "injected-capsule-store")

    class Rows:
        def __init__(self):
            self.data = {}

        def get(self, object_id):
            return self.data.get(object_id)

        def rows(self):
            return list(self.data.values())

        def upsert(self, row):
            self.data[row["object_id"]] = dict(row)

        def set(self, row):
            self.data[row["object_id"]] = dict(row)

        def delete(self, object_id):
            return self.data.pop(object_id, None) is not None

    try:
        store = Rows()
        registry = CapsuleRegistry(store, resolver=context.modules(), config=context.config)

        assert registry.store is store
        assert not hasattr(registry, "workspace")
        assert not hasattr(registry, "register_toolkit")
        assert not hasattr(registry, "load_toolkit")
        assert not hasattr(registry, "list_toolkits")
    finally:
        context.close()


@pytest.mark.fast
def test_toolkit_registry_persists_capsules_in_explicit_workspace(tmp_path):
    context = _isolated_context(tmp_path / "workspace-executable-store")
    context._setup_modules()
    from heavenbase.toolkit import toolkit_registry

    try:
        workspace = hb.HeavenBase(
            "workspace-executable-store",
            context=context,
            backends={"main": {"type": "inmem", "name": "main"}},
        )
        toolkit_id = math_toolkit().register(ws=workspace)
        registry = toolkit_registry(workspace)
        toolkit_row = workspace.get(toolkit_id, entity=registry.toolkit_type)
        capsule_ids = [ref["capsule_id"] for ref in toolkit_row["capsule_refs"]]

        assert registry.store.owner is workspace
        assert registry.capsules.store.owner is workspace
        assert all(workspace.get(capsule_id, entity=registry.capsules.store._entity) for capsule_id in capsule_ids)
    finally:
        context.close()


@pytest.mark.fast
def test_toolkit_rows_are_isolated_between_same_context_workspaces(tmp_path):
    context = _isolated_context(tmp_path / "workspace-isolation")
    context._setup_modules()

    try:
        first = hb.HeavenBase("first-tools", context=context, backends={"main": {"type": "inmem", "name": "main"}})
        second = hb.HeavenBase("second-tools", context=context, backends={"main": {"type": "inmem", "name": "main"}})
        toolkit_id = math_toolkit().register(ws=first)

        assert hb.Toolkit.load(toolkit_id, ws=first).run("add", left=2, right=3) == 5
        with pytest.raises(KeyError):
            hb.Toolkit.load(toolkit_id, ws=second)
    finally:
        context.close()


def test_source_only_restore_failure_reports_missing_binary_fallback():
    capsule = hb.Capsule(unresolved_default_tool(), name="needs_helper", namespace="demo", version="1", include_cloudpickle=False)
    restored = hb.Capsule.from_manifest(capsule.to_dict())

    with pytest.raises(Exception, match="source-only capsules cannot use trusted-local binary loading"):
        restored.run(value=1)


@pytest.mark.slow
def test_capsule_source_restore_includes_module_globals(tmp_path, monkeypatch):
    config = registry_config(tmp_path)
    capsule = hb.Capsule(ceil_half, name="ceil_half", namespace="demo", version="1", include_cloudpickle=False, prefer_import=False)
    capsule_id = capsule.register(registry_config=config)
    layer = next(layer for layer in capsule.manifest.restore_layers if layer["type"] == "source")

    monkeypatch.chdir(touch_dir(pj(str(tmp_path), "module-consumer")))
    loaded = hb.Capsule.load(capsule_id, registry_config=config)

    assert layer["global_refs"]["math"] == {"kind": "module", "module": "math"}
    assert loaded.run(value=5) == 3


@pytest.mark.slow
def test_capsule_source_restore_includes_json_safe_closure_values(tmp_path, monkeypatch):
    config = registry_config(tmp_path)
    capsule = hb.Capsule(offset_tool(), name="add_offset", namespace="demo", version="1", include_cloudpickle=False, prefer_import=False)
    capsule_id = capsule.register(registry_config=config)
    layer = next(layer for layer in capsule.manifest.restore_layers if layer["type"] == "source")

    monkeypatch.chdir(touch_dir(pj(str(tmp_path), "closure-consumer")))
    loaded = hb.Capsule.load(capsule_id, registry_config=config)

    assert layer["global_refs"]["offset"] == {"kind": "value", "value": 7}
    assert loaded.run(value=5) == 12


def test_capsule_behaves_like_python_callable():
    capsule = hb.Capsule(add, name="add", namespace="demo", version="1", include_cloudpickle=False)

    assert callable(capsule)
    assert isinstance(capsule, ABCCallable)
    assert capsule(left=2, right=3) == 5
    assert capsule.__name__ == "add"
    assert capsule.__doc__ == "Add two integers."
    assert list(inspect.signature(capsule).parameters) == ["left", "right"]


def test_tool_accepts_callable_capsule_and_decorator_forms():
    direct = hb.Tool(add, namespace="demo")
    wrapped = hb.Tool(hb.Capsule(mul, namespace="demo"), name="times")

    @hb.Tool
    def decorated(left: int, right: int) -> int:
        """Add through a bare Tool decorator."""
        return left + right

    @hb.Tool(name="aliased", namespace="demo")
    def configured(left: int, right: int) -> int:
        """Multiply through a configured Tool decorator."""
        return left * right

    assert direct(2, 3) == 5
    assert wrapped(left=2, right=3) == 6
    assert decorated(left=2, right=3) == 5
    assert configured(2, 3) == 6
    assert direct.name == "add"
    assert wrapped.name == "times"
    assert decorated.__name__ == "decorated"
    assert decorated.__doc__ == "Add through a bare Tool decorator."
    assert list(inspect.signature(configured).parameters) == ["left", "right"]


def test_unbound_tool_reports_missing_function():
    tool = hb.Tool(name="waiting")

    with pytest.raises(RuntimeError, match="Tool 'waiting' is not bound to a function"):
        tool()


def test_tool_keeps_manifest_capsule_restore_lazy():
    captured = hb.Capsule(add, namespace="demo")
    lazy = hb.Capsule.from_manifest(captured.to_dict())

    tool = hb.Tool(lazy)

    assert lazy._callable is None
    assert tool(left=2, right=3) == 5
    assert lazy._callable is not None


@pytest.mark.slow
def test_capsule_decorator_can_register(tmp_path):
    config = registry_config(tmp_path)

    @hb.Capsule(name="decorated_add", namespace="demo", version="1", include_cloudpickle=False, register=True, registry_config=config)
    def decorated(left: int, right: int) -> int:
        return left + right

    assert decorated(left=2, right=3) == 5
    assert hb.Capsule.load(name="decorated_add", namespace="demo", version="1", registry_config=config).run(left=2, right=3) == 5


def test_toolkit_accepts_list_dict_pairs_and_decorator():
    list_toolkit = hb.Toolkit("list-tools", [add, ("times", mul)], namespace="demo")
    dict_toolkit = hb.Toolkit("dict-tools", {"plus": add, "times": mul}, namespace="demo")
    decorated_toolkit = hb.Toolkit("decorated-tools", namespace="demo")

    @decorated_toolkit.tool(name="plus")
    def plus(left: int, right: int) -> int:
        return left + right

    assert list_toolkit.list_tools() == ["add", "times"]
    assert dict_toolkit.list_tools() == ["plus", "times"]
    assert decorated_toolkit.run("plus", left=2, right=3) == 5


def test_toolkit_default_serializer_is_none_and_resolves_to_jstr():
    toolkit = math_toolkit()
    tool = next(iter(toolkit.tools.values()))
    artifact_root = hb.Toolkit.__module__.partition(".")[0]

    assert tool.serializer is None
    assert artifact_root.startswith("_heavenbase_artifact_")
    assert tool.__class__.__module__ == f"{artifact_root}.tool"
    assert hb.Tool is tool.__class__
    assert tool._serializer_capsule().manifest.name == "jstr_serializer"
    assert len({tool._serializer_capsule().manifest.id for tool in toolkit.tools.values()}) == 1
    assert toolkit.run_to_str("add", left=2, right=3) == "5"


def test_jstr_serializer_json_encodes_structured_tool_output():
    toolkit = hb.Toolkit("structured", {"rows": lambda: [{"a": 1}, {"b": [2, 3]}]}, namespace="demo")

    assert toolkit.tools["rows"].serializer is None
    assert toolkit.run_to_str("rows") == '[{"a": 1}, {"b": [2, 3]}]'


def test_tool_set_serializer_none_restores_jstr_default():
    toolkit = hb.Toolkit("reset", {"value": lambda: {"x": 1}}, namespace="demo")
    toolkit.set_serializer("value", lambda output: f"custom:{output}")

    assert toolkit.run_to_str("value") == "custom:{'x': 1}"

    toolkit.set_serializer("value", None)

    assert toolkit.tools["value"].serializer is None
    assert toolkit.run_to_str("value") == '{"x": 1}'


def test_tool_serializer_must_return_str():
    toolkit = hb.Toolkit("bad-serializer", {"value": lambda: {"x": 1}}, namespace="demo")
    toolkit.set_serializer("value", lambda output: output)

    with pytest.raises(TypeError, match="must return str, got dict"):
        toolkit.run_to_str("value")


def test_default_namespace_is_configurable(monkeypatch):
    real_get = hb.CM_HVNB.get

    def fake_get(key, default=None, **kwargs):
        if key in {"heavenbase.capsule.namespace", "heavenbase.toolkit.namespace"}:
            return "configured"
        return real_get(key, default=default, **kwargs)

    monkeypatch.setitem(vars(hb.CM_HVNB), "get", fake_get)

    assert hb.Capsule(add, name="add", version="1", include_cloudpickle=False).manifest.namespace == "configured"
    assert hb.Toolkit("configured-tools").namespace == "configured"


@pytest.mark.slow
def test_math_toolkit_persists_capsules_and_loads_without_original_callables(tmp_path, monkeypatch):
    from heavenbase.toolkit import toolkit_registry

    workspace = toolkit_workspace(tmp_path, "math-tools")
    toolkit_id = math_toolkit().register(ws=workspace, reason="math-demo")
    registry = toolkit_registry(workspace)
    capsule_rows = workspace.rows(registry.capsules.store._entity)
    capsule_names = sorted(row["name"] for row in capsule_rows)
    toolkit_row = registry.store.get(toolkit_id)
    tool_refs = sorted(toolkit_row["capsule_refs"], key=lambda row: row["alias"])

    other_dir = touch_dir(pj(str(tmp_path), "consumer"))
    monkeypatch.chdir(other_dir)

    loaded = hb.Toolkit.load(toolkit_id, ws=workspace)

    assert capsule_names == ["add", "div", "fibonacci", "jstr_serializer", "mod", "mul", "sub"]
    assert [row["alias"] for row in tool_refs] == ["add", "div", "fibonacci", "mod", "mul", "sub"]
    assert all(row["capsule_id"] and row["serializer_capsule_id"] for row in tool_refs)
    assert loaded.list_tools() == ["add", "mul", "sub", "div", "mod", "fibonacci"]
    assert len({tool._serializer_capsule().manifest.id for tool in loaded.tools.values()}) == 1
    assert loaded.run("add", left=2, right=3) == 5
    assert loaded.run("mul", left=6, right=7) == 42
    assert loaded.run("sub", left=9, right=4) == 5
    assert loaded.run("div", left=9, right=2) == 4.5
    assert loaded.run("mod", left=17, right=5) == 2
    assert loaded.run("fibonacci", number=10) == 55


@pytest.mark.slow
def test_toolkit_persists_serializer_capsules(tmp_path, monkeypatch):
    workspace = toolkit_workspace(tmp_path, "serializer-tools")
    toolkit = hb.Toolkit("table-tools", namespace="demo", version="1")
    toolkit.add(query_rows, name="exec_sql", serializer=rows_markdown)
    toolkit_id = toolkit.register(ws=workspace, reason="table-serializer")
    manifest = hb.Toolkit.describe(toolkit_id, ws=workspace)
    ref = manifest["capsule_refs"][0]

    other_dir = touch_dir(pj(str(tmp_path), "table-consumer"))
    monkeypatch.chdir(other_dir)
    loaded = hb.Toolkit.load(toolkit_id, ws=workspace)

    assert ref["serializer_capsule_id"]
    assert ref["serializer_name"] == "exec_sql_serializer"
    assert manifest["capabilities"]["exec_sql"]["serializer"]["python_callable"]["ok"] is True
    assert loaded.run("exec_sql") == [{"name": "Desk", "stock": 7}, {"name": "Lamp", "stock": 2}]
    assert loaded.run_to_str("exec_sql") == "| name | stock |\n| --- | --- |\n| Desk | 7 |\n| Lamp | 2 |"


@pytest.mark.slow
def test_toolkit_persists_tool_schemas_and_metadata(tmp_path):
    workspace = toolkit_workspace(tmp_path, "schema-tools")
    input_schema = {
        "type": "object",
        "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
        "required": ["left", "right"],
        "additionalProperties": False,
    }
    output_schema = {"type": "integer", "minimum": 0}
    annotations = {"title": "Sum integers", "readOnlyHint": True, "openWorldHint": False}
    metadata = {"audience": "agent", "safe": True}
    toolkit = hb.Toolkit(
        "schema-tools",
        [
            hb.Tool(
                add,
                name="sum",
                input_schema=input_schema,
                output_schema=output_schema,
                annotations=annotations,
                metadata=metadata,
            )
        ],
        namespace="demo",
    )

    toolkit_id = toolkit.register(ws=workspace, reason="schema-roundtrip")
    manifest = hb.Toolkit.describe(toolkit_id, ws=workspace)
    loaded = hb.Toolkit.load(toolkit_id, ws=workspace)

    assert manifest["capsule_refs"][0]["input_schema"] == input_schema
    assert manifest["capsule_refs"][0]["output_schema"] == output_schema
    assert manifest["capsule_refs"][0]["annotations"] == annotations
    assert manifest["capsule_refs"][0]["metadata"] == metadata
    assert loaded.tools["sum"].input_schema == input_schema
    assert loaded.tools["sum"].output_schema == output_schema
    assert loaded.tools["sum"].annotations == annotations
    assert loaded.tools["sum"].metadata == metadata
    assert loaded.run("sum", left=2, right=3) == 5


@pytest.mark.slow
def test_toolkit_load_reports_missing_serializer_capsule_id(tmp_path):
    from heavenbase.toolkit import toolkit_registry

    workspace = toolkit_workspace(tmp_path, "missing-serializer")
    toolkit_id = math_toolkit().register(ws=workspace, reason="missing-serializer-check")
    registry = toolkit_registry(workspace)
    row = registry.store.get(toolkit_id)
    row["capsule_refs"][0].pop("serializer_capsule_id")
    registry.store.set(row)

    with pytest.raises(ValueError, match="missing serializer_capsule_id.*add.*re-register toolkit"):
        hb.Toolkit.load(toolkit_id, ws=workspace)


@pytest.mark.anyio
@pytest.mark.slow
async def test_math_toolkit_serves_healthy_mcp(tmp_path):
    Client = pytest.importorskip("fastmcp").Client
    workspace = toolkit_workspace(tmp_path, "math-mcp")
    toolkit_id = math_toolkit().register(ws=workspace, reason="math-mcp")
    toolkit = hb.Toolkit.load(toolkit_id, ws=workspace)
    server = toolkit.to_fastmcp()

    async with Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        add_result = await client.call_tool("add", {"left": 2, "right": 3})
        fib_result = await client.call_tool("fibonacci", {"number": 10})

    assert {"add", "mul", "sub", "div", "mod", "fibonacci"} <= names
    assert add_result.content[0].text == "5"
    assert add_result.data == 5
    assert fib_result.content[0].text == "55"
    assert fib_result.data == 55


@pytest.mark.fast
def test_fastmcp_server_imports_as_toolkit_client():
    pytest.importorskip("fastmcp")
    server_toolkit = math_toolkit()
    server_toolkit.tools["add"].annotations = {
        "title": "Add integers",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    client_toolkit = hb.Toolkit.from_fastmcp(server_toolkit.to_fastmcp(), name="math-client")

    assert {"add", "fibonacci"} <= set(client_toolkit.list_tools())
    assert set(client_toolkit.tools["add"].input_schema["required"]) == {"left", "right"}
    assert client_toolkit.tools["add"].output_schema == {"type": "integer"}
    assert client_toolkit.tools["add"].annotations == server_toolkit.tools["add"].annotations
    assert client_toolkit.run("add", left=2, right=3) == 5
    assert client_toolkit.run_to_str("add", left=2, right=3) == "5"


@pytest.mark.fast
def test_fastmcp_server_imports_as_runtime_callables():
    pytest.importorskip("fastmcp")
    from heavenbase.toolkit.mcp import callables_from_fastmcp

    server_toolkit = math_toolkit()
    server_toolkit.tools["add"].annotations = {"readOnlyHint": True}

    tools = {tool.__name__: tool for tool in callables_from_fastmcp(server_toolkit.to_fastmcp())}

    assert tools["add"](left=2, right=3) == 5
    assert set(tools["add"].__tool_schema__["required"]) == {"left", "right"}
    assert tools["add"].annotations == {"readOnlyHint": True}


@pytest.mark.fast
def test_toolkit_rejects_invalid_mcp_annotations():
    toolkit = math_toolkit()
    toolkit.tools["add"].annotations = {"readOnlyHint": "not-a-bool"}

    with pytest.raises(ValueError, match="readOnlyHint"):
        toolkit.to_fastmcp()


def test_callable_serialization_strategy_feeds_json_roundtrip():
    payload = CallableSerialization.from_func(add, name="add", namespace="demo", version="1", include_cloudpickle=False).to_dict()
    capsule = CallableSerialization.from_dict(payload).to_capsule()
    restored_ref = hb.loads_json(hb.dumps_json({"callable": add}))
    restored_payload = CallableSerialization.from_dict(hb.loads_json(hb.dumps_json(payload))).to_capsule()

    assert payload["format"] == "heavenbase-callable"
    assert capsule.run(left=2, right=3) == 5
    assert restored_payload.run(left=2, right=3) == 5
    assert restored_ref["callable"] == f"{add.__module__}.{add.__qualname__}"


def test_workspace_mcp_does_not_import_external_tool(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "legacy_pkg" or name.startswith("legacy_pkg."):
            raise AssertionError("HeavenBase MCP path imported legacy package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    workspace = hb.HeavenBase("native-mcp-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="native-mcp-test")

    assert "list_entities" in toolkit.list_tools()


def test_workspace_mcp_toolkit_is_runtime_only(tmp_path):
    workspace = hb.HeavenBase("runtime-only-mcp-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="runtime-only-mcp-test")

    with pytest.raises(ValueError, match="cannot be registered as a Capsule"):
        toolkit.register(ws=workspace)
