"""Context service ownership and Registry memoization tests."""

from copy import deepcopy
import gc
import os
from pathlib import Path
import sys
import threading
import time
import weakref

import pytest

import heavenbase as hb
from heavenbase.context import load_bootstrap
from heavenbase.workspace import WorkspaceSpec
from heavenbase.utils import cmd, dumps_json, loads_json
from heavenbase.utils.config import ConfigManager


class _ResetLeaseEntity(hb.Entity):
    """Entity used only to probe stale workspace registration."""

    identifier = "reset-lease-entity"

    name = hb.field(hb.ShortText)


def _bootstrap(tmp_path):
    return {
        "version": 1,
        "root": str(tmp_path),
        "workspace": "default",
        "backend": {
            "type": "inmem",
            "module": "heavenbase.backends.inmem.backend",
            "name": "system",
        },
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }


def _sqlite_bootstrap(tmp_path):
    bootstrap = _bootstrap(tmp_path)
    bootstrap["backend"] = load_bootstrap(home_path=tmp_path / "missing-bootstrap.yaml").backend
    return bootstrap


def _workspace_config(root):
    return ConfigManager(
        root=str(root),
        default={
            "heavenbase": {
                "registry": {
                    "default_workspace": "default",
                    "active": {"workspace": None},
                }
            }
        },
        setup=True,
    )


def test_default_context_owns_the_public_config_manager():
    assert hb.DEFAULT_CONTEXT.config is hb.CM_HVNB


def test_context_constructs_backend_and_registries_lazily(tmp_path):
    config = ConfigManager(default={}, setup=False)
    context = hb.Context.load(_bootstrap(tmp_path), config=config)

    assert context._backend is None
    first = context.modules()
    second = context.modules()

    assert context._backend is not None
    assert context._registry_store.backend is context._backend
    assert first is second
    assert first.catalog.persistent
    assert context.backend().ws_id == "default"
    assert context.backend().module_resolver is first


def test_context_control_backend_resolves_a_lazy_package_facade(tmp_path):
    # A fresh interpreter, so the facade has not cached its export yet.
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import json
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.utils.config import ConfigManager

bootstrap = json.loads(sys.argv[2])
context = hb.Context.load(bootstrap, config=ConfigManager(default={}, setup=False))
print(type(context.backend()).__name__)
"""
    bootstrap = _bootstrap(tmp_path)
    bootstrap["backend"]["module"] = "heavenbase.backends.inmem"

    out = cmd([sys.executable, "-I", "-c", script, str(source), dumps_json(bootstrap)], include="out", check=True)

    assert out == "InMemBackend"


def test_context_control_backend_skips_an_export_its_bootstrap_module_does_not_define(tmp_path, monkeypatch):
    # The lazy-facade scan read every name in __all__ without a default, so a
    # bootstrap module listing a name it does not define failed with
    # AttributeError; 0.1.2.2 scanned only the names the module bound.
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    (module_dir / "hb_bootstrap_missing_export.py").write_text(
        'from heavenbase.backends.inmem.backend import InMemBackend\n\n__all__ = ["InMemBackend", "NotDefinedHere"]\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(module_dir))
    bootstrap = _bootstrap(tmp_path)
    bootstrap["backend"]["module"] = "hb_bootstrap_missing_export"
    context = hb.Context.load(bootstrap, config=ConfigManager(default={}, setup=False))
    try:
        assert type(context.backend()).__name__ == "InMemBackend"
    finally:
        context.close()
        sys.modules.pop("hb_bootstrap_missing_export", None)


def test_context_control_backend_first_open_uses_base_config_scope(tmp_path, monkeypatch):
    from heavenbase.backends.inmem.backend import InMemBackend

    config = ConfigManager(default={}, setup=False)
    context = hb.Context.load(_bootstrap(tmp_path), config=config)
    base_scope = config.base_scope
    workspace_scope = f"{base_scope}.workspace-probe"
    observed_scopes = []
    original = InMemBackend.from_config.__func__

    def record_scope(cls, name, data, *, resolver=None, config=None):
        observed_scopes.append(config.scope)
        return original(cls, name, data, resolver=resolver, config=config)

    monkeypatch.setattr(InMemBackend, "from_config", classmethod(record_scope))

    try:
        assert context._backend is None
        with config.scoped(workspace_scope):
            assert config.scope == workspace_scope
            registry = context.registry("first-open-scope-probe")
            assert registry.persistent
            assert config.scope == workspace_scope

        assert observed_scopes == [base_scope]
        assert config.scope == base_scope
    finally:
        context.close()


def test_context_backend_first_exposes_one_complete_authority_pair(tmp_path):
    config = ConfigManager(default={}, setup=False)
    context = hb.Context.load(_bootstrap(tmp_path), config=config)

    control_backend = context._control_backend()
    assert context._modules is None
    with pytest.raises(RuntimeError, match="not yet bound"):
        _ = control_backend.module_resolver

    backend = context.backend()

    assert backend is control_backend
    assert context._modules is not None
    assert backend._module_resolver is context.modules()
    assert backend.module_resolver is context.modules()
    assert backend.config is config


def test_partial_runtime_authorities_derive_only_from_their_owner(tmp_path):
    first_root = tmp_path / "first-authority"
    second_root = tmp_path / "second-authority"
    first = hb.Context.load(_bootstrap(first_root), config=ConfigManager(root=str(first_root), setup=False))
    second = hb.Context.load(_bootstrap(second_root), config=ConfigManager(root=str(second_root), setup=False))
    try:
        first._setup_modules()
        second._setup_modules()

        from_resolver = hb.LLMEngine(resolver=first.modules())
        from_config = hb.LLMEngine(manager=first.config)
        toolkit = hb.Toolkit("authority-probe", resolver=first.modules())
        database = hb.Database(provider="sqlite", database=":memory:", resolver=first.modules())

        assert from_resolver.manager is first.config
        assert from_config.resolver is first.modules()
        assert toolkit._config is first.config
        assert database._config is first.config
        with pytest.raises(ValueError, match="different Contexts"):
            hb.LLMEngine(manager=first.config, resolver=second.modules())
        with pytest.raises(ValueError, match="different Contexts"):
            hb.Toolkit("mismatched-authority", resolver=first.modules(), config=second.config)
        with pytest.raises(ValueError, match="different Contexts"):
            hb.Database(provider="sqlite", database=":memory:", resolver=first.modules(), config=second.config)
        with pytest.raises(ValueError, match="different Contexts"):
            hb.ext.HandlerContext(
                "main",
                "inmem",
                hb.InlineColumn.identifier,
                resolver=first.modules(),
                config=second.config,
            )

        from heavenbase.database.dialect.registry import ResolverDialectCatalog

        mismatched_engine = hb.DBEngine(
            manager=first.config,
            dialects=ResolverDialectCatalog(second.modules()),
        )
        with pytest.raises(ValueError, match="different Contexts"):
            hb.Database(provider="sqlite", database=":memory:", config_engine=mismatched_engine)
    finally:
        first.close()
        second.close()


def test_context_setup_modules_delegates_force_to_its_service(tmp_path, monkeypatch):
    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    service = context.modules()
    calls = []

    def setup(force=False):
        calls.append(force)
        return True

    monkeypatch.setattr(service, "setup", setup)

    assert context._setup_modules(force=True)
    assert calls == [True]
    context.close()


def test_workspace_creation_sets_up_modules_before_backend_resolution(monkeypatch):
    import heavenbase.workspace.setup.bootstrap as workspace_bootstrap

    events = []

    class BackendResolutionReached(RuntimeError):
        pass

    def setup_modules(self, force=False):
        assert self is hb.DEFAULT_CONTEXT
        events.append(("modules", force))
        return False

    def stop_backend_resolution(*args, **kwargs):
        events.append(("backend", False))
        raise BackendResolutionReached

    monkeypatch.setattr(hb.Context, "_setup_modules", setup_modules)
    monkeypatch.setattr(workspace_bootstrap, "resolve_workspace_backend_setup", stop_backend_resolution)

    with pytest.raises(BackendResolutionReached):
        hb.HeavenBase("module-setup-order", detached=True)

    assert events == [("modules", False), ("backend", False)]


def test_context_config_uses_its_context_owned_registry(tmp_path):
    config = ConfigManager(default={"enabled": False})
    context = hb.Context.load(_bootstrap(tmp_path), config=config)

    config.set("enabled", True)

    assert config._scope_registry is context.registry("system-config")
    assert config.get("enabled") is True


def test_context_rebinds_an_unused_process_config_registry(tmp_path):
    config = ConfigManager(default={}, setup=False)
    assert not config._scope_registry.persistent

    context = hb.Context.load(_bootstrap(tmp_path), config=config)
    config.set("enabled", True)

    assert config._scope_registry is context.registry("system-config")
    assert config._scope_registry.persistent


def test_context_child_inherits_backend_and_selection_is_isolated(tmp_path):
    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    child = context.child("project")
    parent_registry = context.registry("child-registry-probe")
    child_registry = child.registry("child-registry-probe")

    assert child.parent is context
    assert child.active_workspace_id == "project"
    assert child.backend() is context.backend()
    assert child.config is context.config
    assert child.modules() is context.modules()
    assert child.modules().catalog is context.modules().catalog
    assert parent_registry is not child_registry
    assert child._registry_store is not context._registry_store
    assert child._registry_store.backend is context._registry_store.backend


def test_context_workspace_activation_persists_without_mutating_running_contexts(tmp_path):
    bootstrap = _sqlite_bootstrap(tmp_path)
    contexts = []
    try:
        owner = hb.Context.load(bootstrap, config=_workspace_config(tmp_path))
        observer = hb.Context.load(bootstrap, config=_workspace_config(tmp_path))
        contexts.extend((owner, observer))
        WorkspaceSpec.register(
            "persisted-project",
            config={"backends": {"main": {"type": "inmem"}}},
            context=owner,
        )

        assert owner.active_workspace_id is None
        assert observer.active_workspace_id is None
        assert len(owner._workspace_registry) == 0
        with pytest.raises(KeyError):
            observer.activate_workspace("not-registered")

        owner.activate_workspace("persisted-project")

        assert owner.active_workspace_id == "persisted-project"
        assert owner.config.get("heavenbase.registry.active.workspace") == "persisted-project"
        assert observer.active_workspace_id is None
        assert len(owner._workspace_registry) == 0

        reopened = hb.Context.load(bootstrap, config=_workspace_config(tmp_path))
        contexts.append(reopened)
        assert reopened.active_workspace_id == "persisted-project"

        owner.deactivate_workspace()

        assert owner.active_workspace_id is None
        assert owner.config.get("heavenbase.registry.active.workspace") is None
        assert observer.active_workspace_id is None
        assert reopened.active_workspace_id == "persisted-project"

        after_deactivation = hb.Context.load(bootstrap, config=_workspace_config(tmp_path))
        contexts.append(after_deactivation)
        assert after_deactivation.active_workspace_id is None
    finally:
        for context in reversed(contexts):
            context.close()


def test_context_close_is_idempotent_and_blocks_future_service_access(tmp_path):
    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    context.backend()

    context.close()
    context.close()

    with pytest.raises(RuntimeError, match="closed"):
        context.registry("system-modules")


def test_context_reset_reconstructs_the_backend_module_authority_pair(tmp_path):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    first_service = context.modules()
    first_backend = context.backend()

    reset_default_local_state(context)
    second_service = context.modules()
    second_backend = context.backend()

    assert second_service is not first_service
    assert second_backend is not first_backend
    assert second_backend.module_resolver is second_service
    context.close()


def test_context_reset_retires_children_services_and_every_workspace_surface(tmp_path):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    child = context.child("child")
    service = context.modules()
    workspace = hb.HeavenBase("reset-lease", context=context, backends={"main": {"type": "inmem"}})

    reset_default_local_state(context)

    for operation in (
        lambda: child.modules(),
        lambda: child.backend(),
        lambda: service.inspect(),
        lambda: service.catalog.publication,
        lambda: workspace.to_manifest(),
        lambda: workspace.register(_ResetLeaseEntity),
        lambda: workspace.to_mcp(),
        lambda: workspace.to_mcp_json(),
        lambda: workspace.serve(),
        lambda: workspace.release(),
        lambda: workspace.drop(),
    ):
        with pytest.raises(RuntimeError, match="retired"):
            operation()

    replacement = hb.HeavenBase("reset-lease", context=context, backends={"main": {"type": "inmem"}})
    assert replacement is not workspace
    assert context.modules() is not service
    replacement.release()
    child.close()
    context.close()


def test_config_manager_has_one_context_owner_even_after_close(tmp_path):
    manager = ConfigManager(default={}, setup=False)
    first = hb.Context.load(_bootstrap(tmp_path / "first"), config=manager)

    with pytest.raises(ValueError, match="already bound"):
        hb.Context.load(_bootstrap(tmp_path / "second"), config=manager)

    first.close()
    with pytest.raises(ValueError, match="already bound"):
        hb.Context.load(_bootstrap(tmp_path / "third"), config=manager)


def test_reset_rejects_construction_during_the_atomic_transition(tmp_path, monkeypatch):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    service = context.modules()
    entered = threading.Event()
    resume = threading.Event()
    original_retire = service._retire_authority
    failures = []

    def block_retirement():
        original_retire()
        entered.set()
        assert resume.wait(timeout=10)

    def run_reset():
        try:
            reset_default_local_state(context)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(service, "_retire_authority", block_retirement)
    reset = threading.Thread(target=run_reset)
    reset.start()
    assert entered.wait(timeout=10)
    try:
        with pytest.raises(RuntimeError, match="reset is in progress"):
            context.modules()
        with pytest.raises(RuntimeError, match="reset is in progress"):
            hb.HeavenBase("during-reset", context=context)
    finally:
        resume.set()
        reset.join(timeout=10)

    assert not reset.is_alive()
    assert failures == []
    assert context.modules() is not service
    context.close()


def test_close_cannot_publish_a_workspace_after_retirement_starts(tmp_path, monkeypatch):
    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    service = context.modules()
    entered = threading.Event()
    resume = threading.Event()
    original_retire = service._retire_authority
    failures = []

    def block_retirement():
        original_retire()
        entered.set()
        assert resume.wait(timeout=10)

    def construct():
        try:
            hb.HeavenBase("during-close", context=context)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(service, "_retire_authority", block_retirement)
    closer = threading.Thread(target=context.close)
    closer.start()
    assert entered.wait(timeout=10)
    constructor = threading.Thread(target=construct)
    constructor.start()
    resume.set()
    closer.join(timeout=10)
    constructor.join(timeout=10)

    assert not closer.is_alive()
    assert not constructor.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "close" in str(failures[0])
    assert not len(context._workspace_registry)


def test_context_lifecycle_rejects_self_retirement_without_partial_close(tmp_path):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    service = context.modules()

    with service._authority_guard():
        with pytest.raises(RuntimeError, match="active resolver operations"):
            context.close()
        with pytest.raises(RuntimeError, match="active resolver operations"):
            reset_default_local_state(context)

    assert not context._closed
    assert not context._closing
    assert not context._resetting
    assert context.modules() is service
    context.close()


def test_context_disposes_an_owned_sql_backend_on_close(tmp_path):
    context = hb.Context.load(_sqlite_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    backend = context.backend()
    database = backend.db

    assert database.spec.database == hb.utils.path_to_file_uri(tmp_path / "system.db")
    assert backend._database_config_engine.bootstrap_backend_tag("workspace-database-namespace") is False
    assert database._module_resolver is context.modules()

    context.close()

    assert database.disposed


def test_sql_engine_lifecycle_isolated_for_contexts_with_the_same_spec(tmp_path):
    shared_database = hb.utils.path_to_file_uri(tmp_path / "shared-system.db")
    first_bootstrap = _sqlite_bootstrap(tmp_path / "first")
    second_bootstrap = _sqlite_bootstrap(tmp_path / "second")
    first_bootstrap["backend"]["database"] = shared_database
    second_bootstrap["backend"]["database"] = shared_database
    first = hb.Context.load(first_bootstrap, config=ConfigManager(default={}, setup=False))
    second = hb.Context.load(second_bootstrap, config=ConfigManager(default={}, setup=False))
    first_database = first.backend().db
    second_database = second.backend().db
    try:
        assert first_database.spec.database == second_database.spec.database == shared_database
        assert first_database.engine is not second_database.engine
        first_database.execute("CREATE TABLE IF NOT EXISTS context_probe (value INTEGER)")
        assert "context_probe" in second_database.tables()

        first.close()

        assert first_database.disposed
        assert not second_database.disposed
        assert second_database.execute("SELECT count(*) AS n FROM context_probe", readonly=True).scalar() == 0
    finally:
        first.close()
        second.close()


def test_retired_database_authority_is_weak_and_generation_scoped(tmp_path):
    from heavenbase.database.registry import DatabaseEngineRegistry

    context = hb.Context.load(_sqlite_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    database = context.backend().db
    service = context.modules()
    _ = database.engine
    authority = service.authority_token
    service_ref = weakref.ref(service)
    context_ref = weakref.ref(context)

    assert DatabaseEngineRegistry._authorities[authority] is service
    key = DatabaseEngineRegistry.key(
        database.spec,
        authority_id=database._authority_id,
        resource_id=database._resource_id,
    )
    database.dispose()
    assert key in DatabaseEngineRegistry._disposed
    context.close()
    assert database.disposed
    assert key not in DatabaseEngineRegistry._disposed

    del database, service, context
    gc.collect()

    assert service_ref() is None
    assert context_ref() is None
    assert authority not in DatabaseEngineRegistry._authorities


def test_context_rejects_incompatible_options_for_an_open_namespace(tmp_path):
    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    context.registry("option-contract", normalize=str.lower)

    with pytest.raises(ValueError, match="already open with different"):
        context.registry("option-contract")


def test_context_rejects_unknown_backend_name(tmp_path):
    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))

    with pytest.raises(KeyError, match="Unknown Context backend"):
        context.backend("other")


def test_context_bootstrap_imports_only_the_selected_backend_provider(tmp_path):
    script = f"""
import json
import sys

import heavenbase as hb

context = hb.Context.load({repr(_sqlite_bootstrap(tmp_path))})
context.backend()
forbidden_roots = {{
    "heavenbase.backends.elasticsearch",
    "heavenbase.backends.file",
    "heavenbase.backends.inmem",
    "heavenbase.backends.neo4j",
    "heavenbase.backends.redis",
    "heavenbase.backends.surreal",
    "heavenbase.backends.vector",
    "heavenbase.backends.sql.duckdb",
    "heavenbase.backends.sql.mysql",
    "heavenbase.backends.sql.postgres",
}}
prefixes = tuple(f"{{name}}." for name in forbidden_roots)
loaded = sorted(name for name in sys.modules if name in forbidden_roots or name.startswith(prefixes))
context.close()
print(json.dumps(loaded))
"""

    assert loads_json(cmd([sys.executable, "-c", script], include="out", check=True)) == []


def test_context_injects_bootstrap_database_authority_for_external_control_backend(tmp_path, monkeypatch):
    from heavenbase.database import DBEngine
    from heavenbase.database.dialect.registry import BootstrapDialectCatalog

    module_name = "hb_m0_external_control"
    (tmp_path / f"{module_name}.py").write_text(
        """from heavenbase.backends.inmem.backend import InMemBackend


class ExternalControlBackend(InMemBackend):
    identifier = "external-control"

    @classmethod
    def from_config(cls, name, data, *, resolver=None, config=None):
        values = dict(data)
        return cls(name, ws_id=values.pop("ws_id", None), resolver=resolver, config=config, **values)

    def __init__(self, name="system", ws_id=None, *, resolver=None, config=None, **kwargs):
        self.bootstrap_kwargs = kwargs
        super().__init__(name, ws_id=ws_id, resolver=resolver, config=config)
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(tmp_path)
    override = {
        "default_provider": "sqlite",
        "default_args": {"http_proxy": None},
        "dialects": {"sqlite": {}},
        "providers": {"sqlite": {"dialect": "sqlite"}},
    }
    bootstrap = _bootstrap(tmp_path)
    bootstrap["backend"] = {
        "type": "external-control",
        "module": module_name,
        "name": "system",
        "_override": deepcopy(override),
    }
    context = hb.Context.load(bootstrap, config=ConfigManager(default={}, setup=False))

    backend = context.backend()
    registry = context.registry("external-control-contract")
    registry.create("value", 1)

    service = hb.DEFAULT_CONTEXT.modules()
    receipt = service.install(
        {
            "manifest_version": 2,
            "coordinate": "test/external-control-backend",
            "version": hb.__version__,
            "compatibility": {},
            "bundle": "external-control-backend",
            "items": [
                {
                    "kind": "backend_type",
                    "identifier": "external-control",
                    "aliases": ["external-control-alias"],
                    "name": "External control Backend",
                    "desc": "Out-of-package Backend used by both construction modes.",
                    "source": "python-import",
                    "target": f"{module_name}:ExternalControlBackend",
                    "active": True,
                    "meta": {
                        "schema_version": 1,
                        "factory": None,
                        "tags": {"portable": True, "storage-roles": ["row"]},
                    },
                }
            ],
        },
        publisher="test-suite",
    )
    try:
        ordinary = hb.BackendType.build(
            "ordinary",
            {"type": "external-control-alias"},
            ws_id="external-ordinary",
        )
        ordinary_tags = service.tags("backend_type", "external-control").raw
    finally:
        service.uninstall(receipt)

    assert type(backend).__module__ == module_name
    assert isinstance(backend.bootstrap_kwargs["config_engine"], DBEngine)
    assert isinstance(backend.bootstrap_kwargs["config_engine"].dialects, BootstrapDialectCatalog)
    assert type(ordinary) is type(backend)
    assert ordinary_tags["portable"] is True
    assert ordinary_tags["storage-roles"] == ("row",)
    assert ordinary.ws_id == "external-ordinary"
    assert ordinary.bootstrap_kwargs == {}
    assert context.bootstrap.backend["_override"] == override
    assert registry["value"] == 1
    with pytest.raises(KeyError, match="external-control"):
        hb.BackendType.build("ordinary", {"type": "external-control"})
    context.close()


def test_direct_and_registered_workspace_envelopes_construct_equivalent_backends(tmp_path):
    config = ConfigManager(default={}, setup=False)
    context = hb.Context.load(_bootstrap(tmp_path), config=config)
    envelope = {"backends": {"main": {"type": "inmem", "name": "primary"}}}
    direct = hb.HeavenBase("config-source-parity", detached=True, context=context, **envelope)
    registered = WorkspaceSpec.register("config-source-parity", config=envelope, context=context)
    restored = registered.open_detached(context=context)

    def contract(workspace):
        backend = workspace.backends.get("primary")
        return type(backend), backend.name, backend.type, backend.ws_id

    try:
        assert registered.config == envelope
        assert direct._construction.kwargs == envelope
        assert restored._construction.kwargs == envelope
        assert contract(direct) == contract(restored)
    finally:
        direct.release()
        restored.release()
        context.close()


def _personal_home_with_invalid_bootstrap(tmp_path, home_env) -> dict[str, str]:
    """Return a subprocess environment whose personal bootstrap must never be read."""
    home = tmp_path / "home"
    (home / ".heavenbase").mkdir(parents=True)
    (home / ".heavenbase" / "bootstrap.yaml").write_text("version: 999\n", encoding="utf-8")
    return home_env(home)


def test_importing_heavenbase_never_constructs_the_default_context(tmp_path, home_env):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
import heavenbase.workspace
import heavenbase.entity
import heavenbase.llm
from heavenbase.context.runtime import _default_context_instance

assert _default_context_instance() is None
try:
    hb.DEFAULT_CONTEXT
except ValueError as error:
    print("rejected: " + str(error))
else:
    print("constructed")
"""
    result = cmd([sys.executable, "-I", "-c", script, str(source)], include="out", check=True, env=_personal_home_with_invalid_bootstrap(tmp_path, home_env))

    assert result.startswith("rejected: HeavenBase bootstrap version must be 1")


@pytest.mark.slow
def test_set_default_context_installs_an_explicit_root_before_first_use(tmp_path, home_env):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import default_context, set_default_context
from heavenbase.utils.config import ConfigManager

bootstrap = {
    "version": 1,
    "root": sys.argv[2],
    "workspace": "default",
    "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
    "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
}
context = hb.Context.load(bootstrap, config=ConfigManager(default={}, setup=False))
try:
    set_default_context(context.child("default"))
except ValueError:
    pass
else:
    raise AssertionError("a child view was installed as the process default")
assert set_default_context(context) is context
assert set_default_context(context) is context
assert default_context() is context
assert hb.DEFAULT_CONTEXT is context
try:
    set_default_context(hb.Context.load(bootstrap, config=ConfigManager(default={}, setup=False)))
except RuntimeError:
    pass
else:
    raise AssertionError("the process default was replaced after installation")
ws = hb.HeavenBase("pinned", backends={"main": {"type": "inmem"}})
assert ws.context is context
assert hb.CM_HVNB.get("heavenbase.registry.default_workspace") == "default"
assert not hb.CM_HVNB._scope_registry.persistent
context.close()
print("ok")
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="out",
        check=True,
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result == "ok"


_INSTALLED_DEFAULT_CONFIG_SCRIPT = """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import set_default_context
from heavenbase.execution.contracts import HandlerContext
from heavenbase.extensions.database.toolkit.policy import database_allow_write
from heavenbase.extensions.memory.toolkit._utils import list_limit
from heavenbase.query.aggregate import _require_code_reduce
from heavenbase.toolkit.logging import mcp_call_logging_enabled
from heavenbase.toolkit.profiles import available_profiles, resolve_profile
from heavenbase.toolkit.server import mcp_transport
from heavenbase.utils.config import ConfigManager

bootstrap = {
    "version": 1,
    "root": sys.argv[2],
    "workspace": "default",
    "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
    "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
}
values = {
    "heavenbase": {
        "query": {"allow_code_reduce": False, "near": {"default_top_k": 7}},
        "database": {"allow_write": True},
        "mcp": {
            "transport": "sse",
            "log_calls": True,
            "memory": {"list_limit": 3},
            "profiles": {"installed-only": {"tools": []}, "shared": {"tools": [], "desc": "from-installed"}},
        },
    }
}
context = hb.Context.load(bootstrap, config=ConfigManager(default=values, setup=False))
set_default_context(context)
hb.HeavenBase("pinned", backends={"main": {"type": "inmem"}})
assert hb.CM_HVNB is not context.config

if sys.argv[3] == "handlers":
    # Neither authority given: the installed default Context supplies both.
    prepared = hb.ext.Predicate.load("like").compile("a%")
    assert hb.ext.Predicate.load("like").match("abc", prepared)
    handler = HandlerContext("b", "inmem", "inline")
    assert handler.config is context.config and handler.resolver is context.modules()
    assert handler.top_k == 7
elif sys.argv[3] == "profiles":
    # resolve_profile given only resolver= reads that resolver's Context config.
    # It read the default config first, so a profile only the other Context
    # configures raised AttributeError and a same-named installed one shadowed it.
    other_values = {"heavenbase": {"mcp": {"profiles": {"other-only": {"tools": []}, "shared": {"tools": [], "desc": "from-other"}}}}}
    other = hb.Context.load({**bootstrap, "root": sys.argv[2] + "-other"}, config=ConfigManager(default=other_values, setup=False))
    modules = other.modules()
    assert resolve_profile("other-only", resolver=modules, serializer_names=["jstr"]).name == "other-only"
    assert resolve_profile("shared", resolver=modules, serializer_names=["jstr"]).desc == "from-other"
    assert available_profiles(resolver=modules)["shared"].desc == "from-other"
    assert resolve_profile("shared", serializer_names=["jstr"]).desc == "from-installed"
    try:
        resolve_profile("installed-only", resolver=modules, serializer_names=["jstr"])
    except ValueError as error:
        assert "MCP workspace profile" in str(error)
    else:
        raise AssertionError("the other Context resolved a profile only the installed Context configures")
    other.close()
else:
    # Config-reading helpers given no config read the installed Context's values, not CM_HVNB's.
    assert database_allow_write() is True
    assert list_limit() == 3
    assert mcp_call_logging_enabled() is True
    assert mcp_transport() == "sse"
    assert "installed-only" in available_profiles(resolver=context.modules())
    assert "installed-only" in available_profiles()
    try:
        _require_code_reduce()
    except ValueError:
        pass
    else:
        raise AssertionError("code_reduce ran although the installed Context forbids it")
context.close()
print("ok")
"""


@pytest.mark.slow
@pytest.mark.parametrize("surface", ["handlers", "helpers", "profiles"])
def test_omitted_authorities_read_the_installed_default_context_config(tmp_path, surface, home_env):
    # After set_default_context installed a Context with its own config,
    # Predicate.compile() and HandlerContext() paired the installed modules
    # with CM_HVNB and raised "belong to different Contexts", and the toolkit
    # and query config helpers read CM_HVNB instead of the installed config.
    # resolve_profile given only resolver= must read that resolver's Context.
    source = Path(__file__).resolve().parents[2] / "src"
    result = cmd(
        [sys.executable, "-I", "-c", _INSTALLED_DEFAULT_CONFIG_SCRIPT, str(source), str(tmp_path / "root"), surface],
        include="result",
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result.ok, result.err
    assert result.out == "ok"


def test_explicit_context_mode_refuses_the_process_default(tmp_path, home_env):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import default_context, set_default_context
from heavenbase.context.runtime import _default_context_instance
from heavenbase.utils.config import ConfigManager

import heavenbase.context as context_package

assert context_package.configure_explicit_context is hb.configure_explicit_context
hb.configure_explicit_context()
context_package.configure_explicit_context()
# DEFAULT_CONTEXT is an attribute, not a star export: the context package star import builds nothing in explicit mode
assert "DEFAULT_CONTEXT" not in hb.__all__ and "DEFAULT_CONTEXT" not in context_package.__all__
exec("from heavenbase.context import *", {})
assert _default_context_instance() is None
for attempt in (
    default_context,
    lambda: hb.DEFAULT_CONTEXT,
    lambda: context_package.DEFAULT_CONTEXT,
    lambda: hb.CM_HVNB.get("heavenbase.registry.default_workspace"),
):
    try:
        attempt()
    except RuntimeError as error:
        assert "Explicit Context mode" in str(error)
        assert "context=" in str(error) and "CM_HVNB" in str(error)
    else:
        raise AssertionError("explicit mode built the process default")
bootstrap = {
    "version": 1,
    "root": sys.argv[2],
    "workspace": "default",
    "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
    "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
}
context = hb.Context.load(bootstrap, config=ConfigManager(default={}, setup=False))
try:
    set_default_context(context)
except RuntimeError as error:
    assert "Explicit Context mode" in str(error)
    assert "set_default_context() cannot install" in str(error)
else:
    raise AssertionError("explicit mode installed a process default")
from unittest import mock

import heavenbase.context.runtime as runtime

# An assignment is refused too, and the error names the assignment, not a set_default_context() call it never made.
for assign in (
    lambda: setattr(runtime, "DEFAULT_CONTEXT", context),
    lambda: mock.patch.object(runtime, "DEFAULT_CONTEXT", context).start(),
):
    try:
        assign()
    except RuntimeError as error:
        assert "Explicit Context mode" in str(error)
        assert "assigning heavenbase.context.runtime.DEFAULT_CONTEXT cannot install" in str(error), error
        assert "set_default_context" not in str(error), error
    else:
        raise AssertionError("explicit mode let an assignment install a process default")
assert _default_context_instance() is None
context.close()
print("ok")
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="out",
        check=True,
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result == "ok"


_EXPLICIT_MODE_WORKSPACE_ROUTES = (
    'ws.entities["agent-agent"]',
    'ws.entities["sys-toolkit"]',
    'ws.entities["agent-trace"]',
)
_EXPLICIT_MODE_CONTEXT_ROUTES = (
    'load_entity_type("agent-agent", resolver=context.modules())',
    'load_entity_artifact_export("sys-toolkit", "tool", "Tool", resolver=context.modules())',
)


@pytest.mark.slow
def test_explicit_context_mode_uses_registry_backed_classes_the_way_the_docs_say(tmp_path, home_env):
    """registry-context.md names how an explicit-mode application gets a Registry-backed class; each way works there.

    The facade exports load their class through the default Context, so a named import of one raises like the star
    import. Without a workspace the Context routes need `context.modules().setup()` first, and then return the class
    a later workspace holds. The class in the defining module is not the Registry-restored one, and a workspace
    refuses it.
    """
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "resources" / "architecture" / "registry-context.md").read_text(encoding="utf-8")
    for route in (*_EXPLICIT_MODE_WORKSPACE_ROUTES, *_EXPLICIT_MODE_CONTEXT_ROUTES, "context.modules().setup()"):
        assert f"`{route}`" in doc, route
    script = (
        """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context.runtime import _default_context_instance
from heavenbase.entity.registry import load_entity_artifact_export, load_entity_type

hb.configure_explicit_context()
for statement in (
    "from heavenbase import Agent",
    "from heavenbase import Toolkit",
    "from heavenbase import Session",
    "from heavenbase import Tool",
    "from heavenbase.extensions.agent_log import AgentTrace",
):
    try:
        exec(statement, {})
    except RuntimeError as error:
        assert "Explicit Context mode" in str(error), (statement, error)
    else:
        raise AssertionError(f"{statement} did not need the default Context")
"""
        + _INMEM_BOOTSTRAP_SCRIPT
        + """
context = hb.Context.load(bootstrap)
context_routes = sys.argv[4].split("|")
for route in context_routes:
    try:
        eval(route)
    except KeyError as error:
        assert "Unknown entity registration" in str(error), (route, error)
    else:
        raise AssertionError(f"{route} resolved before the Context's modules were set up")
assert context.modules().setup() is True
found = {route: eval(route) for route in context_routes}
ws = hb.HeavenBase("explicit-classes", context=context, detached=True, backends={"main": {"type": "inmem"}})
ws.enable_extension("agent-log")
found.update({route: eval(route) for route in sys.argv[3].split("|")})
Agent = found['ws.entities["agent-agent"]']
assert found['load_entity_type("agent-agent", resolver=context.modules())'] is Agent
for entity_id in ("agent-agent", "sys-toolkit", "agent-trace"):
    cls = found[f'ws.entities["{entity_id}"]']
    assert cls.schema().entity_id == entity_id
    assert len(ws.query(cls).execute()) == ws.count(cls)
before = ws.count(Agent)
ws.upsert(Agent, {"object_id": "explicit-agent", "name": "explicit"})
assert ws.count(Agent) == before + 1
assert found[context_routes[-1]].__name__ == "Tool"

from heavenbase.extensions.agent.agent import Agent as DefiningAgent
from heavenbase.extensions.agent.session import Session as DefiningSession
from heavenbase.extensions.agent_log.trace import AgentTrace as DefiningTrace

for cls in (DefiningAgent, DefiningSession, DefiningTrace):
    try:
        ws.count(cls)
    except ValueError as error:
        assert "registered with a different structure" in str(error), (cls, error)
    else:
        raise AssertionError(f"{cls.__name__} from its defining module was accepted")
assert _default_context_instance() is None
ws.release()
context.close()
print("ok")
"""
    )
    result = cmd(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(root / "src"),
            str(tmp_path / "root"),
            "|".join(_EXPLICIT_MODE_WORKSPACE_ROUTES),
            "|".join(_EXPLICIT_MODE_CONTEXT_ROUTES),
        ],
        include="out",
        check=True,
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result == "ok"


def test_explicit_context_mode_is_refused_once_the_default_is_in_use():
    from heavenbase._initialization import configure_explicit_context

    assert hb.DEFAULT_CONTEXT is not None
    with pytest.raises(RuntimeError, match="before using a default Context"):
        configure_explicit_context()


def test_set_default_context_rejects_a_replacement_once_the_default_is_in_use(tmp_path):
    from heavenbase.context import default_context, set_default_context

    assert default_context() is hb.DEFAULT_CONTEXT
    assert set_default_context(hb.DEFAULT_CONTEXT) is hb.DEFAULT_CONTEXT
    other = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            set_default_context(other)
    finally:
        other.close()


_INMEM_BOOTSTRAP_SCRIPT = """
bootstrap = {
    "version": 1,
    "root": sys.argv[2],
    "workspace": "default",
    "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
    "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
}
"""


@pytest.mark.slow
def test_explicit_context_mode_runs_a_fully_context_bound_llm_chat_and_mcp_toolkit(tmp_path, home_env):
    # hash_id without length= (LLM cache keys, toolkit and capsule ids) used to read CM_HVNB,
    # whose first read selects the process default and so raised in explicit mode.
    source = Path(__file__).resolve().parents[2] / "src"
    script = (
        """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
import heavenbase.llm.base as llm_base
from heavenbase.context.runtime import _default_context_instance
from heavenbase.utils.hash import md5hash

hb.configure_explicit_context()
"""
        + _INMEM_BOOTSTRAP_SCRIPT
        + """
context = hb.Context.load(bootstrap)
assert context.config is not hb.CM_HVNB
ws = hb.HeavenBase("bound", context=context)
assert hb.utils.hash_id("x") == md5hash("x", length=42)


def fake_chat(spec, messages, stream=False, **kwargs):
    yield {"choices": [{"delta": {"role": "assistant", "content": "hi"}}]}


llm_base.chat_complete = fake_chat
llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", context=context, api_key="unused")
assert llm.chat("hello") == "hi"
assert ws.to_mcp(profile="full") is not None
assert _default_context_instance() is None
try:
    hb.CM_HVNB.get("heavenbase.entity.id.hash_length")
except RuntimeError as error:
    assert "Explicit Context mode" in str(error)
else:
    raise AssertionError("explicit mode let CM_HVNB select the process default")
context.close()
print("ok")
"""
    )
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="result",
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result.ok, result.err
    assert result.out == "ok"


@pytest.mark.slow
def test_standalone_backends_follow_an_installed_default_with_its_own_config(tmp_path, home_env):
    # A fully unbound standalone Backend used to fall back to CM_HVNB, which an
    # installed default with its own ConfigManager leaves unowned.
    source = Path(__file__).resolve().parents[2] / "src"
    script = (
        """
import os
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.backends.registry import BackendRegistry, BackendType
from heavenbase.context import set_default_context
"""
        + _INMEM_BOOTSTRAP_SCRIPT
        + """
context = set_default_context(hb.Context.load(bootstrap))
assert context.config is not hb.CM_HVNB
context._setup_modules()
backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(os.path.join(sys.argv[2], "main.db")))
assert backend.config is context.config
ws = hb.HeavenBase("standalone", detached=True, backends={"main": backend})
assert backend.module_resolver is context.modules() and backend.config is context.config
registry = BackendRegistry()
assert registry._config is context.config
adopted = hb.backends.InMemBackend("aux")
registry.register(adopted)
assert adopted.module_resolver is context.modules() and adopted.config is context.config
assert BackendType.build("aux", {"type": "inmem"}).config is context.config
ws.release()
from heavenbase.capsule.authority import CapsuleAuthority
from heavenbase.llm.config import LLMEngine

modules = context.modules()
with hb.Database(provider="sqlite", database=":memory:", config=hb.CM_HVNB) as db:
    assert db._config is context.config and db._module_resolver is modules
    assert db.execute("select 1 as value").to_list() == [{"value": 1}]
engine = LLMEngine(manager=hb.CM_HVNB)
assert engine.manager is context.config and engine.resolver is modules
capsule = CapsuleAuthority.resolve(config=hb.CM_HVNB)
assert capsule.config is context.config and capsule.resolver is modules
try:
    CapsuleAuthority.resolve(config=hb.CM_HVNB, resolver=modules)
except ValueError as error:
    assert "different Contexts" in str(error), error
else:
    raise AssertionError("an explicit pair accepted configuration from outside its Context")
backend = hb.backends.SQLiteBackend("cm", hb.utils.path_to_file_uri(os.path.join(sys.argv[2], "cm.db")), config=hb.CM_HVNB)
assert backend.db._config is context.config and backend.db._module_resolver is modules
assert backend.config is context.config and backend.module_resolver is modules, "SQLiteBackend retained its bootstrap-only pair"
print("ok")
"""
    )
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="result",
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result.ok, result.err
    assert result.out == "ok"


@pytest.mark.slow
@pytest.mark.parametrize("closed", [False, True])
def test_the_first_default_selection_adopts_a_root_context_built_over_cm_hvnb(tmp_path, home_env, closed):
    # Selecting the default used to build a second Context over CM_HVNB and fail
    # with "ConfigManager is already bound to another Context".
    source = Path(__file__).resolve().parents[2] / "src"
    script = (
        """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import default_context, set_default_context
"""
        + _INMEM_BOOTSTRAP_SCRIPT
        + """
context = hb.Context.load(bootstrap, config=hb.CM_HVNB)
if sys.argv[3] == "closed":
    context.close()
    try:
        default_context()
    except RuntimeError as error:
        assert "closed Context" in str(error) and "set_default_context" in str(error), error
    else:
        raise AssertionError("a closed Context was adopted as the process default")
else:
    assert default_context() is context and hb.DEFAULT_CONTEXT is context
    assert set_default_context(context) is context
    ws = hb.HeavenBase("adopted")
    assert ws.context is context
    ws.release()
    context.close()
print("ok")
"""
    )
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root"), "closed" if closed else "open"],
        include="result",
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result.ok, result.err
    assert result.out == "ok"


def test_a_context_built_over_cm_hvnb_cannot_overtake_a_first_default_selection(tmp_path, home_env):
    # Context.__init__ used to claim CM_HVNB without its lock, so a concurrent
    # Context.load(config=CM_HVNB) could bind it after default_context() had seen
    # no owner, and that default selection failed with "already bound".
    source = Path(__file__).resolve().parents[2] / "src"
    script = (
        """
import sys
import threading

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
import heavenbase.context.runtime as runtime
from heavenbase.context import default_context
"""
        + _INMEM_BOOTSTRAP_SCRIPT
        + """
real_load_bootstrap = runtime.load_bootstrap
selecting_thread = threading.get_ident()
outcome = {}


def build_over_cm_hvnb():
    try:
        outcome["context"] = hb.Context.load(bootstrap, config=hb.CM_HVNB)
    except ValueError as error:
        outcome["error"] = str(error)


racer = threading.Thread(target=build_over_cm_hvnb)


def load_bootstrap(value=None, *, home_path=None):
    if threading.get_ident() != selecting_thread:
        return real_load_bootstrap(value, home_path=home_path)
    # default_context() has seen no CM_HVNB owner; give the racer every chance to claim it.
    racer.start()
    racer.join(1.0)
    return real_load_bootstrap(bootstrap)


runtime.load_bootstrap = load_bootstrap
context = default_context()
racer.join(30)
assert not racer.is_alive()
assert hb.CM_HVNB._context is context and hb.DEFAULT_CONTEXT is context
assert "already bound to another Context" in outcome.get("error", ""), outcome
context.close()
print("ok")
"""
    )
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="result",
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result.ok, result.err
    assert result.out == "ok"


def test_first_default_context_use_races_a_first_cm_hvnb_read(tmp_path, home_env):
    # One thread builds the default Context while another reads CM_HVNB for the
    # first time, whose registry factory needs that default: both must finish.
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import runtime

original = runtime.Context.load.__func__


def slow_load(cls, *args, **kwargs):
    time.sleep(0.5)  # holds the default-Context lock while the reader starts
    return original(cls, *args, **kwargs)


runtime.Context.load = classmethod(slow_load)
errors = []


def build():
    try:
        runtime.default_context()
    except BaseException as error:
        errors.append(error)


def read():
    try:
        hb.CM_HVNB.get("heavenbase.registry.default_workspace")
    except BaseException as error:
        errors.append(error)


builder = threading.Thread(target=build, daemon=True)
builder.start()
time.sleep(0.1)
reader = threading.Thread(target=read, daemon=True)
reader.start()
builder.join(60)
reader.join(60)
assert not builder.is_alive() and not reader.is_alive(), "deadlock"
assert not errors, errors
assert hb.CM_HVNB._scope_registry is runtime.default_context().registry("system-config")
print("ok")
"""
    home = tmp_path / "home"
    home.mkdir()
    result = cmd([sys.executable, "-I", "-c", script, str(source)], include="out", check=True, env=home_env(home))

    assert result == "ok"


# Each entry point reaches CM_HVNB as the fallback for an omitted config, which
# only the default Context owns. They run in a fresh interpreter because this
# suite's conftest builds the default Context on import, which hides the case
# where nothing has built it yet.
_CM_HVNB_FALLBACK_ENTRY_POINTS = {
    "sqlite-backend": """
class Doc(hb.Entity):
    identifier = "unbound-doc"
    title = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[3]).optional()


backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(os.path.join(root, "main.db")))
ws = hb.HeavenBase("unbound", detached=True, backends={"main": backend})
ws.register(Doc)
ws.upsert(Doc, {"object_id": "a", "title": "a", "embedding": [0.1, 0.2, 0.3]})
assert ws.get("a", entity=Doc)["title"] == "a"
ws.release()
""",
    "database-config": """
db = hb.Database(provider="sqlite", database=":memory:", config=hb.CM_HVNB)
assert db.execute("select 1 as x").to_list() == [{"x": 1}]
""",
    "database-db-engine": """
db = hb.Database(provider="sqlite", database=":memory:", config_engine=hb.DB_ENGINE)
assert db.execute("select 1 as x").to_list() == [{"x": 1}]
""",
    "llm-engine": """
engine = hb.LLMEngine(manager=hb.CM_HVNB)
assert engine.resolver is default_context().modules()
""",
    "llm-cache": """
cache = hb.LLMCache.current(config_manager=hb.CM_HVNB)
assert cache is hb.LLMCache.current()
assert hb.LLMCache.from_dict({}, config_manager=hb.CM_HVNB).context is default_context()
""",
    "capsule-authority": """
from heavenbase.capsule.authority import CapsuleAuthority

assert CapsuleAuthority.resolve(config=hb.CM_HVNB).resolver is default_context().modules()
""",
    "handler-context": """
from heavenbase.execution.contracts import HandlerContext

assert HandlerContext("main", "inmem", "default", config=hb.CM_HVNB).resolver is default_context().modules()
""",
}


@pytest.fixture(scope="module")
def set_up_home(tmp_path_factory, home_env):
    """Return a HOME whose packaged modules are indexed, as after ``hb setup``.

    The setup runs in its own interpreter, so later fresh interpreters still
    start without a default Context.
    """
    source = Path(__file__).resolve().parents[2] / "src"
    home = tmp_path_factory.mktemp("set-up-home")
    script = "import sys; sys.path.insert(0, sys.argv[1]); from heavenbase.workspace import WorkspaceSpec; WorkspaceSpec.setup()"
    cmd([sys.executable, "-I", "-c", script, str(source)], check=True, env=home_env(home))
    return home


_CM_HVNB_FALLBACK_SCRIPT = """
import os
import sys

sys.path.insert(0, sys.argv[1])
root = sys.argv[2]
explicit = sys.argv[3] == "explicit"
entry = sys.stdin.read()
import heavenbase as hb

# Importing heavenbase.context.runtime first would change the order in which
# CM_HVNB and the default Context meet, so the entry point runs before it.
if explicit:
    hb.configure_explicit_context()
runtime = sys.modules.get("heavenbase.context.runtime")
assert runtime is None or runtime._default_context_instance() is None


def default_context():
    from heavenbase.context import default_context

    return default_context()


try:
    exec(entry)
except RuntimeError as error:
    assert explicit and "Explicit Context mode" in str(error), error
    assert sys.modules["heavenbase.context.runtime"]._default_context_instance() is None
    print("explicit")
else:
    assert not explicit, "explicit mode built the process default"
    assert hb.CM_HVNB._context is sys.modules["heavenbase.context.runtime"]._default_context_instance() is default_context()
    print("ok")
"""


@pytest.mark.slow
@pytest.mark.parametrize("entry", sorted(_CM_HVNB_FALLBACK_ENTRY_POINTS))
@pytest.mark.parametrize("mode", ["default", "explicit"])
def test_cm_hvnb_fallback_selects_the_lazy_default_context(tmp_path, set_up_home, home_env, entry, mode):
    source = Path(__file__).resolve().parents[2] / "src"
    result = cmd(
        [sys.executable, "-I", "-c", _CM_HVNB_FALLBACK_SCRIPT, str(source), str(tmp_path), mode],
        include="result",
        env=home_env(set_up_home),
        input=_CM_HVNB_FALLBACK_ENTRY_POINTS[entry],
    )

    assert result.ok, result.err
    assert result.out == mode.replace("default", "ok")


@pytest.mark.slow
def test_concurrent_first_cm_hvnb_fallbacks_build_one_default_context(tmp_path, set_up_home, home_env):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import os
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
root = sys.argv[2]
import heavenbase as hb
from heavenbase.context import runtime

original = runtime.Context.load.__func__
loads = []


def slow_load(cls, *args, **kwargs):
    loads.append(kwargs.get("config"))
    time.sleep(0.5)  # every other thread arrives while the default is being built
    return original(cls, *args, **kwargs)


runtime.Context.load = classmethod(slow_load)
entries = (
    lambda i: hb.backends.SQLiteBackend(f"main{i}", hb.utils.path_to_file_uri(os.path.join(root, f"{i}.db"))).db,
    lambda i: hb.Database(provider="sqlite", database=":memory:", config=hb.CM_HVNB)._module_resolver,
    lambda i: hb.LLMEngine(manager=hb.CM_HVNB).resolver,
    lambda i: hb.LLMCache.current(config_manager=hb.CM_HVNB),
)
count = 16
barrier = threading.Barrier(count)
errors = []
results = [None] * count


def run(i):
    try:
        barrier.wait(30)
        results[i] = entries[i % len(entries)](i)
    except BaseException as error:
        errors.append(error)


threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(count)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(120)
assert not any(thread.is_alive() for thread in threads), "deadlock"
assert not errors, errors
assert len(loads) == 1 and loads[0] is hb.CM_HVNB, loads
default = runtime.default_context()
assert hb.CM_HVNB._context is default
assert all(results[i] is default.modules() for i in range(1, count, len(entries)))
assert all(results[i] is default.modules() for i in range(2, count, len(entries)))
assert len({id(results[i]) for i in range(3, count, len(entries))}) == 1
print("ok")
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path)],
        include="result",
        env=home_env(set_up_home),
    )

    assert result.ok, result.err
    assert result.out == "ok"


# A value saved with ``hb config set`` must be read whatever import path first
# reaches CM_HVNB. Each entry obtains CM_HVNB (as ``config``) and a ``read``
# that performs the process's first config read; none of them imports
# heavenbase.context.runtime before that read.
_SAVED_KEY = "heavenbase.backend_defaults.sql.id_lookup.batch_size"
_CM_HVNB_FIRST_READ_ENTRY_POINTS = {
    "root-attribute": """
import heavenbase as hb

config = hb.CM_HVNB
read = lambda: config.get(KEY)
""",
    "root-import": """
from heavenbase import CM_HVNB as config

read = lambda: config.get(KEY)
""",
    "utils": """
from heavenbase.utils import CM_HVNB as config

read = lambda: config.get(KEY)
""",
    "utils-config": """
from heavenbase.utils.config import CM_HVNB as config

read = lambda: config.get(KEY)
""",
    "config-manager-module": """
from heavenbase.utils.config.manager import CM_HVNB as config

read = lambda: config.get(KEY)
""",
    "sqlite-backend": """
from heavenbase.backends import SQLiteBackend
from heavenbase.utils import CM_HVNB as config, path_to_file_uri

read = lambda: SQLiteBackend("main", path_to_file_uri(os.path.join(root, "main.db")))._sql_id_lookup_batch_size
""",
    "cli": """
import contextlib
import io

from heavenbase.cli.app import main
from heavenbase.utils import CM_HVNB as config


def read():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            main(["config", "get", KEY])
        except SystemExit as exit:
            assert exit.code == 0, exit.code
    return int(out.getvalue().splitlines()[0])
""",
}

_CM_HVNB_FIRST_READ_SCRIPT = """
import os
import sys

sys.path.insert(0, sys.argv[1])
root = sys.argv[2]
explicit = sys.argv[3] == "explicit"
KEY = sys.argv[4]
exec(sys.stdin.read())

# Importing CM_HVNB neither opens its scopes nor builds the default Context.
runtime = sys.modules.get("heavenbase.context.runtime")
assert runtime is None or runtime._default_context_instance() is None
assert config._registry is None
if explicit:
    import heavenbase as hb

    hb.configure_explicit_context()
try:
    value = read()
except RuntimeError as error:
    assert explicit and "Explicit Context mode" in str(error), error
    assert sys.modules["heavenbase.context.runtime"]._default_context_instance() is None
    print("explicit")
else:
    assert not explicit, "explicit mode built the process default"
    assert config._scope_registry.persistent
    assert config._context is sys.modules["heavenbase.context.runtime"]._default_context_instance() is not None
    print(value)
"""


@pytest.fixture(scope="module")
def saved_config_home(tmp_path_factory, home_env):
    """Return a set-up HOME where ``hb config set`` saved ``_SAVED_KEY`` as 77."""
    source = Path(__file__).resolve().parents[2] / "src"
    home = tmp_path_factory.mktemp("saved-config-home")
    script = """
import sys

sys.path.insert(0, sys.argv[1])
from heavenbase.cli.app import main
from heavenbase.workspace import WorkspaceSpec

WorkspaceSpec.setup()
main(["config", "set", sys.argv[2], "77"])
"""
    cmd([sys.executable, "-I", "-c", script, str(source), _SAVED_KEY], check=True, env=home_env(home))
    return home


@pytest.mark.slow
@pytest.mark.parametrize(
    ("entry", "mode"),
    # The CLI is an application of the process default Context, so it has no explicit mode.
    [(entry, mode) for entry in sorted(_CM_HVNB_FIRST_READ_ENTRY_POINTS) for mode in ("default", "explicit") if (entry, mode) != ("cli", "explicit")],
)
def test_a_first_cm_hvnb_read_sees_config_saved_through_the_default_context(tmp_path, saved_config_home, home_env, entry, mode):
    # A first read before anything imported heavenbase.context.runtime used to
    # open a process-only scope Registry and ignore the saved value (500).
    source = Path(__file__).resolve().parents[2] / "src"
    result = cmd(
        [sys.executable, "-I", "-c", _CM_HVNB_FIRST_READ_SCRIPT, str(source), str(tmp_path), mode, _SAVED_KEY],
        include="result",
        env=home_env(saved_config_home),
        input=_CM_HVNB_FIRST_READ_ENTRY_POINTS[entry],
    )

    assert result.ok, result.err
    assert result.out == ("77" if mode == "default" else "explicit")


def test_a_cm_hvnb_fallback_racing_the_default_build_gets_the_default_context(tmp_path, home_env):
    # ``_config_context`` read CM_HVNB's owner before it checked for an
    # installed default. When another thread built the default between the two
    # reads, it returned the stale ``None`` and the caller raised "config-only
    # authority is unbound". The trace runs the racing build exactly there.
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys
import threading

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import runtime
from heavenbase.context.authority import resolve_runtime_authority

raced = []


def trace(frame, event, arg):
    if frame.f_code is not runtime._config_context.__code__:
        return None

    def line(frame, event, arg):
        if event == "line" and "owner" in frame.f_locals and not raced:
            raced.append(True)
            builder = threading.Thread(target=runtime.default_context)
            builder.start()
            builder.join(60)
        return line

    return line


sys.settrace(trace)
try:
    owner = runtime._config_context(hb.CM_HVNB)
finally:
    sys.settrace(None)
assert raced
assert owner is runtime._default_context_instance() is not None, owner
assert resolve_runtime_authority(config=hb.CM_HVNB) is not None
print("ok")
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source)],
        include="result",
        env=home_env(tmp_path),
    )

    assert result.ok, result.err
    assert result.out == "ok"


def test_importing_heavenbase_modules_never_opens_cm_hvnb_or_builds_the_default_context(tmp_path, home_env):
    # CM_HVNB's scopes open through the default Context, so a module that read
    # config on import would build that Context eagerly.
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import importlib
import pkgutil
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase

for info in pkgutil.walk_packages(heavenbase.__path__, "heavenbase."):
    if info.name.rsplit(".", 1)[-1] == "__main__":
        continue
    try:
        importlib.import_module(info.name)
    except ImportError:
        continue  # an optional dependency is missing
    runtime = sys.modules.get("heavenbase.context.runtime")
    assert runtime is None or runtime._default_context_instance() is None, info.name
    manager = sys.modules.get("heavenbase.utils.config.manager")
    assert manager is None or manager.CM_HVNB._registry is None, info.name
assert runtime is not None and manager is not None
print("ok")
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source)],
        include="result",
        env=home_env(tmp_path),
    )

    assert result.ok, result.err
    assert result.out == "ok"


def test_config_registry_opens_while_another_thread_holds_the_context_lock(tmp_path):
    # The first config read opens its Registry under the Context lock; a thread
    # that holds that lock and reads config at the same moment must not deadlock.
    context = hb.Context.load(_bootstrap(tmp_path), config=_workspace_config(tmp_path / "config"))
    manager = context.config
    original = manager._registry_factory
    opening = threading.Event()

    def factory():
        opening.set()
        return original()

    manager._registry_factory = factory
    errors = []

    def first_read():
        try:
            manager.get("heavenbase.registry.default_workspace")
        except BaseException as error:
            errors.append(error)

    holder_done = threading.Event()

    def hold_context_lock_and_read():
        try:
            with context._lock:
                reader = threading.Thread(target=first_read, daemon=True)
                reader.start()
                assert opening.wait(10)
                time.sleep(0.1)  # the reader now waits for the Context lock inside the factory
                assert manager.get("heavenbase.registry.default_workspace") == "default"
            reader.join(30)
            assert not reader.is_alive()
            holder_done.set()
        except BaseException as error:
            errors.append(error)

    holder = threading.Thread(target=hold_context_lock_and_read, daemon=True)
    holder.start()
    holder.join(60)
    try:
        assert holder_done.is_set(), "deadlock"
        assert not errors, errors
        assert manager._scope_registry is context.registry("system-config")
    finally:
        context.close()


class _InFlightNote(hb.Entity):
    """Entity used to hold a workspace read in flight across close or reset."""

    identifier = "in-flight-note"

    name = hb.field(hb.ShortText)


def _read_in_flight(context, monkeypatch):
    """Start one workspace read that stays inside its backend until released."""
    ws = hb.HeavenBase("in-flight", context=context, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(_InFlightNote)
    ws.upsert(_InFlightNote, {"object_id": "n1", "name": "one"})
    backend = ws.backends.get("main")
    original_get_many = backend.get_many
    entered = threading.Event()
    release = threading.Event()

    def gated_get_many(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        return original_get_many(*args, **kwargs)

    monkeypatch.setattr(backend, "get_many", gated_get_many)
    result = {}

    def read():
        try:
            result["row"] = ws.get("n1", entity=_InFlightNote)
        except BaseException as error:
            result["error"] = error

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert entered.wait(10)
    monkeypatch.setattr(backend, "get_many", original_get_many)
    return ws, reader, release, result


@pytest.mark.slow
def test_close_completes_during_an_admitted_read_and_refuses_new_ones(tmp_path, monkeypatch):
    context = hb.Context.load(_bootstrap(tmp_path), config=_workspace_config(tmp_path / "config"))
    ws, reader, release, result = _read_in_flight(context, monkeypatch)
    closed = threading.Event()
    closer = threading.Thread(target=lambda: (context.close(), closed.set()), daemon=True)
    closer.start()
    try:
        assert closed.wait(5)
        with pytest.raises(RuntimeError, match="Context is closed"):
            ws.get("n1", entity=_InFlightNote)
    finally:
        release.set()
    reader.join(10)
    closer.join(10)

    assert closed.is_set()
    assert result.get("row", {}).get("name") == "one"  # the read in flight finished normally
    with pytest.raises(RuntimeError, match="closed"):
        ws.get("n1", entity=_InFlightNote)


def test_close_with_a_writer_waiting_behind_a_read_in_flight(tmp_path, monkeypatch):
    context = hb.Context.load(_bootstrap(tmp_path), config=_workspace_config(tmp_path / "config"))
    ws, reader, release, result = _read_in_flight(context, monkeypatch)
    outcome = {}

    def write():
        try:
            ws.upsert(_InFlightNote, {"object_id": "n2", "name": "two"})
            outcome["written"] = True
        except RuntimeError as error:
            outcome["error"] = error

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    deadline = time.monotonic() + 10
    while len(ws._lock._writers_waiting) != 1:  # the writer waits for the read without the Context lock
        assert time.monotonic() < deadline
        time.sleep(0.005)
    closed = threading.Event()
    closer = threading.Thread(target=lambda: (context.close(), closed.set()), daemon=True)
    closer.start()
    try:
        assert closed.wait(5)
    finally:
        release.set()
    for thread in (reader, closer, writer):
        thread.join(30)
        assert not thread.is_alive()

    assert closed.is_set()
    assert result.get("row", {}).get("name") == "one"
    assert "closed" in str(outcome.get("error", "")) or "close is in progress" in str(outcome.get("error", ""))
    lock = ws._lock
    assert lock._writer is None and not lock._writers_waiting and lock._head is None and not lock._queue


@pytest.mark.slow
def test_reset_completes_during_an_admitted_read(tmp_path, monkeypatch):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=_workspace_config(tmp_path / "config"))
    ws, reader, release, result = _read_in_flight(context, monkeypatch)
    done = threading.Event()
    resetter = threading.Thread(target=lambda: (reset_default_local_state(context), done.set()), daemon=True)
    resetter.start()
    try:
        assert done.wait(5)
        with pytest.raises(RuntimeError, match="retired"):
            ws.get("n1", entity=_InFlightNote)
    finally:
        release.set()
    reader.join(10)
    resetter.join(10)
    try:
        assert done.is_set()
        assert result.get("row", {}).get("name") == "one"
        with pytest.raises(RuntimeError, match="retired"):
            ws.get("n1", entity=_InFlightNote)
    finally:
        context.close()


def test_default_context_assignment_rebinds_the_default_in_use(tmp_path):
    """After first use, assigning the module attribute replaces the default as in 0.1.2.2; ``del`` restores it."""
    from heavenbase.context import runtime

    default = runtime.default_context()
    assert runtime.DEFAULT_CONTEXT is default
    runtime.DEFAULT_CONTEXT = default  # the default in use: nothing changes
    other = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    try:
        with pytest.raises(TypeError):
            runtime.DEFAULT_CONTEXT = None
        runtime.DEFAULT_CONTEXT = other
        assert runtime.DEFAULT_CONTEXT is other
        assert runtime.default_context() is other and runtime.context_or_default() is other
        with pytest.raises(RuntimeError, match="already in use"):
            runtime.set_default_context(default)  # the function stays strict
        del runtime.DEFAULT_CONTEXT
        assert runtime.default_context() is default
        assert "DEFAULT_CONTEXT" not in dir(runtime)  # listing the module never selects the default
    finally:
        del runtime.DEFAULT_CONTEXT
        other.close()
    assert runtime.default_context() is default


def test_patch_helpers_swap_and_restore_the_default_context(tmp_path, home_env):
    """``mock.patch.object`` and ``monkeypatch`` swap the module default and restore it, as in 0.1.2.2.

    ``mock.patch.object`` saves the namespace entry, so patching a process with
    no default never selects one: the invalid personal bootstrap is never read.
    """
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import inspect
import sys
from unittest import mock

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import runtime
from heavenbase.utils.config import ConfigManager
from _pytest.monkeypatch import MonkeyPatch


def load(name):
    bootstrap = {
        "version": 1,
        "root": sys.argv[2] + "/" + name,
        "workspace": "default",
        "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }
    return hb.Context.load(bootstrap, config=ConfigManager(default={}, setup=False))


first, second = load("first"), load("second")
inspect.getmembers(runtime)
assert runtime._default_context_instance() is None
with mock.patch.object(runtime, "DEFAULT_CONTEXT", first):
    assert runtime.context_or_default() is first
    with mock.patch.object(runtime, "DEFAULT_CONTEXT", second):
        assert runtime.DEFAULT_CONTEXT is runtime.default_context() is second
    assert runtime.default_context() is first
assert runtime._default_context_instance() is None

runtime.DEFAULT_CONTEXT = first
patch = MonkeyPatch()
patch.setattr(runtime, "DEFAULT_CONTEXT", second)
assert runtime.context_or_default() is second
with mock.patch.object(runtime, "DEFAULT_CONTEXT", first):
    assert runtime.default_context() is first
assert runtime.default_context() is second
patch.undo()
assert runtime.default_context() is first
del runtime.DEFAULT_CONTEXT
assert runtime._default_context_instance() is None
first.close()
second.close()
print("ok")
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="result",
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result.ok, result.err
    assert result.out == "ok"


def test_default_context_assignment_installs_before_first_use(tmp_path, home_env):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context import runtime
from heavenbase.utils.config import ConfigManager

bootstrap = {
    "version": 1,
    "root": sys.argv[2],
    "workspace": "default",
    "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
    "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
}
context = hb.Context.load(bootstrap, config=ConfigManager(default={}, setup=False))
runtime.DEFAULT_CONTEXT = context
assert runtime.default_context() is context
assert hb.DEFAULT_CONTEXT is context
context.close()
print("ok")
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="out",
        check=True,
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result == "ok"


@pytest.mark.slow
def test_explicit_context_mode_runs_context_scoped_ids_rng_and_a_cached_llm_chat(tmp_path, home_env):
    # hash_id without length= and StableRNG read CM_HVNB, whose first read selects
    # the process default, so a cached LLM call (cache keys), the mock LLM (its
    # RNG) and prompt row ids raised in explicit mode although every call passed
    # context=. They now use the packaged values when the default is refused.
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.context.runtime import _default_context_instance
from heavenbase.extensions.prompt.name import prompt_row_id
from heavenbase.utils import StableRNG, hash_id
from heavenbase.utils.config import ConfigManager
from heavenbase.utils.hash import md5hash
from heavenbase.workspace import WorkspaceSpec

hb.configure_explicit_context()
bootstrap = {
    "version": 1,
    "root": sys.argv[2],
    "workspace": "default",
    "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
    "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
}
context = hb.Context.load(bootstrap, config=ConfigManager(root=sys.argv[2], setup=True))
WorkspaceSpec.setup(context=context)
assert hash_id("x") == md5hash("x", length=42)
assert len(prompt_row_id("demo.prompt")) == 42
assert StableRNG(seed=1).rnd_int(0, 10) == StableRNG(seed=1).rnd_int(0, 10)
assert hb.LLM(preset="mock", context=context).chat("hello")
assert _default_context_instance() is None
try:
    hb.CM_HVNB.get("heavenbase.entity.id.hash_length")
except RuntimeError as error:
    assert "Explicit Context mode" in str(error)
else:
    raise AssertionError("explicit mode let a direct CM_HVNB read select the process default")
context.close()
print("ok")
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), str(tmp_path / "root")],
        include="result",
        env=_personal_home_with_invalid_bootstrap(tmp_path, home_env),
    )

    assert result.ok, result.err
    assert result.out == "ok"


def _packaged_override_without_its_dialect():
    """Return the packaged bootstrap's database override with its ``sqlite`` dialect renamed away."""
    import yaml

    packaged = Path(__file__).resolve().parents[2] / "src" / "heavenbase" / "resources" / "configs" / "bootstrap.yaml"
    override = yaml.safe_load(packaged.read_text(encoding="utf-8"))["backend"]["_override"]
    override["dialects"]["sqlite-renamed"] = override["dialects"].pop("sqlite")
    return {"_override": override}


_UNBUILDABLE_BOOTSTRAPS = {
    "unimportable-module": lambda: {"module": "heavenbase_tests_no_such_backend_module"},
    "rejected-settings": lambda: {"database": "postgresql://user:secret@127.0.0.1:1/x"},
    # The bootstrap catalog raises a plain LookupError for a dialect it lacks.
    "dialect-missing-from-catalog": _packaged_override_without_its_dialect,
    # A module that is installed but whose import fails, as a driver missing
    # its shared library does, raises a plain ImportError.
    "module-whose-import-fails": lambda: {"module": "heavenbase_tests_broken_backend_module"},
}


@pytest.mark.parametrize("case", sorted(_UNBUILDABLE_BOOTSTRAPS))
def test_ids_and_the_rng_use_packaged_values_when_the_bootstrap_backend_cannot_be_built(tmp_path, home_env, case):
    # Only a ValueError mentioning the bootstrap counted as an invalid
    # bootstrap file, so a bootstrap naming a backend module this interpreter
    # cannot import, or settings its backend rejects, made hash_id and
    # StableRNG raise where 0.1.2.2 used the packaged values.
    import yaml

    data = {
        "version": 1,
        "root": "~/.heavenbase",
        "workspace": "default",
        "backend": {
            "type": "sqlite",
            "module": "heavenbase.backends.sql.sqlite",
            "name": "system",
            "database": "file://%/system.db",
            **_UNBUILDABLE_BOOTSTRAPS[case](),
        },
    }
    env = _personal_home_with_invalid_bootstrap(tmp_path, home_env)
    (tmp_path / "home" / ".heavenbase" / "bootstrap.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "heavenbase_tests_broken_backend_module.py").write_text(
        'raise ImportError("libheavenbase_tests.so.1: cannot open shared object file: No such file or directory")\n', encoding="utf-8"
    )
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys

sys.path.insert(0, sys.argv[1])
sys.path.append(sys.argv[2])
import heavenbase as hb
from heavenbase.utils import StableRNG, get_logger, hash_id
from heavenbase.utils.hash import md5hash

assert hash_id("x") == md5hash("x", length=42)
assert StableRNG(seed=1).rnd_int(0, 10) == StableRNG(seed=1).rnd_int(0, 10)
get_logger("heavenbase.tests.bootstrap").warning("logged")
assert hb.CM_HVNB.get("heavenbase.entity.id.hash_length") == 42
assert hb.CM_HVNB.set("heavenbase.entity.id.hash_length", 31)
assert hb.CM_HVNB.last_write_persisted is False
assert hb.CM_HVNB.get("heavenbase.entity.id.hash_length") == 31
try:
    hb.DEFAULT_CONTEXT._config_registry()
except ValueError as error:
    assert "bootstrap" in str(error) and error.__cause__ is not None, error
else:
    raise AssertionError("the Context opened a bootstrap whose backend cannot be built")
print("ok")
"""
    result = cmd([sys.executable, "-I", "-c", script, str(source), str(modules)], include="result", env=env)

    assert result.ok, result.err
    assert result.out == "ok"


def test_a_failure_to_reach_the_database_is_not_a_bootstrap_fault():
    # Only the bootstrap's own faults may make the tolerant readers fall back
    # for good; a locked or unreachable database may pass and must be raised.
    from heavenbase.context.runtime import _bootstrap_fault

    class OperationalError(Exception):
        pass

    _DeadlockErrorStandIn = type("_DeadlockError", (RuntimeError,), {})

    class DBAPIError(Exception):
        pass

    class SQLAlchemyOperationalError(DBAPIError):
        pass

    def chained(error, cause):
        error.__cause__ = cause
        return error

    assert _bootstrap_fault(ModuleNotFoundError("No module named 'x'", name="x"))
    assert _bootstrap_fault(RuntimeError("Direct SQL control bootstrap requires an explicit database config engine"))
    assert _bootstrap_fault(TypeError("unexpected keyword argument"))
    assert not _bootstrap_fault(OperationalError("database is locked"))
    assert not _bootstrap_fault(SQLAlchemyOperationalError("database is locked"))
    assert not _bootstrap_fault(chained(ValueError("bad"), OperationalError("database is locked")))
    assert not _bootstrap_fault(ConnectionRefusedError(111, "Connection refused"))
    assert not _bootstrap_fault(ImportError("cannot import name 'x' from partially initialized module 'y'"))
    # An installed module whose import fails lasts until the bootstrap or the
    # environment changes, as a missing one does; an import that failed only
    # because another thread held its import lock does not.
    assert _bootstrap_fault(ImportError("libpq.so.5: cannot open shared object file: No such file or directory"))
    assert _bootstrap_fault(ImportError("DLL load failed while importing _psycopg: The specified module could not be found."))
    assert not _bootstrap_fault(chained(ImportError("import of x halted"), _DeadlockErrorStandIn("deadlock detected by _ModuleLock('x')")))
    assert _bootstrap_fault(LookupError("Database dialect 'sqlite' is absent from the bootstrap catalog"))
    # A KeyError or IndexError comes from a bug in the code building the
    # backend, not from the bootstrap, and must not make the readers fall back.
    assert not _bootstrap_fault(KeyError("name"))
    assert not _bootstrap_fault(IndexError("list index out of range"))

    class _DeadlockError(RuntimeError):
        pass

    assert not _bootstrap_fault(_DeadlockError("deadlock detected by _ModuleLock('asyncio')"))


_UNSAVED_LOG_SETTINGS_SCRIPT = """
import io
import os
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.utils import get_logger, redirect_logs, restore_logs, set_log_level, suppress_logs

if sys.argv[2] == "explicit":
    hb.configure_explicit_context()
name = "heavenbase.tests.unsaved"
logger = get_logger(name)
stream = io.StringIO()
redirect_logs(stream, persist=True)
set_log_level("ERROR", persist=True)
logger.warning("hidden-warning")
set_log_level("DEBUG", persist=True)
logger.debug("shown-debug")
suppress_logs(persist=True)
logger.error("hidden-suppressed")
restore_logs(persist=True)
redirect_logs(stream, persist=True)
logger.error("shown-restored")
path = os.path.join(sys.argv[3], "redirected.log")
redirect_logs(path, persist=True)
logger.error("shown-in-file")
restore_logs(persist=True)
lines = [line.split(":", 2)[2] for line in stream.getvalue().splitlines() if line.count(":") >= 2 and line.split(":", 2)[1] == name]
assert lines == ["shown-debug", "shown-restored"], lines
with open(path, encoding="utf-8") as handle:
    assert "shown-in-file" in handle.read()
if sys.argv[2] == "explicit":
    assert sys.modules["heavenbase.context.runtime"]._default_context_instance() is None
print("ok")
"""


_STALE_LOG_HELPER_SCRIPT = """
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.utils import suppress_logs

def other_process(code):
    prefix = "import sys; sys.path.insert(0, sys.argv[1]); import heavenbase as hb; from heavenbase.utils import restore_logs; "
    return subprocess.run([sys.executable, "-I", "-c", prefix + code, sys.argv[1]], check=True, capture_output=True, text=True).stdout.strip()

suppress_logs(persist=True)
other_process("restore_logs(persist=True)")
suppress_logs(persist=True)
saved = other_process("print(hb.CM_HVNB.get('heavenbase.log.sink'), hb.CM_HVNB.get('heavenbase.log.suppress'))")
assert saved == "suppress True", saved
print("ok")
"""


@pytest.mark.slow
def test_a_global_log_helper_is_saved_after_another_process_changed_the_setting(tmp_path, home_env):
    # suppress_logs() after another process ran restore_logs() matched this
    # process's stale in-memory settings, so it saved nothing and the home
    # stayed restored.
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).resolve().parents[2] / "src"
    result = cmd([sys.executable, "-I", "-c", _STALE_LOG_HELPER_SCRIPT, str(source)], include="result", env=home_env(home))

    assert result.ok, result.err
    assert result.out == "ok"


_RELATIVE_LOG_FILE_SCRIPT = """
import os
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb
from heavenbase.utils import configure_logs, get_logger, redirect_logs, restore_logs

base = sys.argv[2]
first, second = os.path.join(base, "first"), os.path.join(base, "second")
os.makedirs(first)
os.makedirs(second)
os.chdir(first)
redirect_logs("relative.log", persist=True)
saved = hb.CM_HVNB.get("heavenbase.log.file")
assert os.path.isabs(saved) and os.path.samefile(os.path.dirname(saved), first), saved
code = "import sys; sys.path.insert(0, sys.argv[1]); from heavenbase.utils import get_logger; get_logger('heavenbase.tests.relative').warning('from-second')"
subprocess.run([sys.executable, "-I", "-c", code, sys.argv[1]], cwd=second, check=True)
assert not os.path.exists(os.path.join(second, "relative.log"))
with open(os.path.join(first, "relative.log"), encoding="utf-8") as handle:
    assert "from-second" in handle.read()
# A path through a symbolic link is saved unresolved, so records follow the
# link's current target.
target_a, target_b, link = os.path.join(base, "A"), os.path.join(base, "B"), os.path.join(base, "current")
os.makedirs(target_a)
os.makedirs(target_b)
try:
    os.symlink(target_a, link, target_is_directory=True)
except (OSError, NotImplementedError):
    link = None
if link is not None:
    redirect_logs(os.path.join(link, "linked.log"), persist=True)
    assert hb.CM_HVNB.get("heavenbase.log.file") == os.path.join(link, "linked.log"), hb.CM_HVNB.get("heavenbase.log.file")
    (os.rmdir if os.name == "nt" else os.unlink)(link)
    os.symlink(target_b, link, target_is_directory=True)
    linked = code.replace("relative", "linked").replace("from-second", "via-link")
    subprocess.run([sys.executable, "-I", "-c", linked, sys.argv[1]], cwd=second, check=True)
    with open(os.path.join(target_b, "linked.log"), encoding="utf-8") as handle:
        assert "via-link" in handle.read()
    if os.path.exists(os.path.join(target_a, "linked.log")):
        with open(os.path.join(target_a, "linked.log"), encoding="utf-8") as handle:
            assert "via-link" not in handle.read()
# A relative path is kept as written, without raising, when the working
# directory no longer exists (POSIX only: Windows cannot remove it).
if os.name != "nt":
    gone = os.path.join(base, "gone")
    os.makedirs(gone)
    os.chdir(gone)
    os.rmdir(gone)
    configure_logs(file="gone.log", persist=True)
    assert hb.CM_HVNB.get("heavenbase.log.file") == "gone.log", hb.CM_HVNB.get("heavenbase.log.file")
    os.chdir(base)
configure_logs(file="%/logs/home.log", persist=True)
assert hb.CM_HVNB.get("heavenbase.log.file") == "%/logs/home.log"
configure_logs(file=" %/logs/padded.log", persist=True)
assert hb.CM_HVNB.get("heavenbase.log.file") == " %/logs/padded.log"
restore_logs(persist=True)
print("ok")
"""


@pytest.mark.slow
def test_a_relative_global_log_file_is_saved_resolved_for_every_process(tmp_path, home_env):
    # redirect_logs("relative.log") saved the path as written, so another
    # process on the same home wrote relative.log in its own working directory.
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).resolve().parents[2] / "src"
    result = cmd(
        [sys.executable, "-I", "-c", _RELATIVE_LOG_FILE_SCRIPT, str(source), str(tmp_path / "work")],
        include="result",
        env=home_env(home),
    )

    assert result.ok, result.err
    assert result.out == "ok"


@pytest.mark.parametrize("case", ["explicit", "invalid-bootstrap", "unwritable-home"])
def test_global_log_helpers_apply_to_the_process_when_their_settings_cannot_be_saved(tmp_path, home_env, case):
    # The helpers' CM_HVNB writes raised there and were swallowed, so
    # set_log_level, suppress_logs and redirect_logs(path) did nothing; 0.1.2.2
    # applied them to the process.
    source = Path(__file__).resolve().parents[2] / "src"
    out = tmp_path / "out"
    out.mkdir()
    if case == "unwritable-home":
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root can write a read-only directory")
        if os.name == "nt":
            pytest.skip("a read-only mode does not stop writes into a Windows directory")
        home = tmp_path / "home"
        home.mkdir()
        home.chmod(0o555)
        env = home_env(home)
    else:
        env = _personal_home_with_invalid_bootstrap(tmp_path, home_env)
    try:
        result = cmd(
            [sys.executable, "-I", "-c", _UNSAVED_LOG_SETTINGS_SCRIPT, str(source), "explicit" if case == "explicit" else "default", str(out)],
            include="result",
            env=env,
        )
    finally:
        if case == "unwritable-home":
            home.chmod(0o755)

    assert result.ok, result.err
    assert result.out == "ok"
    if case == "unwritable-home":
        assert not (home / ".heavenbase").exists()


@pytest.mark.slow
@pytest.mark.parametrize("shutdown", ["close", "reset"])
def test_in_flight_reader_cannot_wait_for_ongoing_shutdown(tmp_path, monkeypatch, shutdown):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=_workspace_config(tmp_path / "config"))
    ws = hb.HeavenBase("in-flight", context=context, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(_InFlightNote)
    ws.upsert(_InFlightNote, {"object_id": "n1", "name": "one"})
    backend = ws.backends.get("main")
    original_get_many = backend.get_many
    entered = threading.Event()
    close_from_reader = threading.Event()
    disposing = threading.Event()
    finish_disposal = threading.Event()
    outcome = {}
    service = context.modules()
    retire_service = service._retire_authority

    def hold_disposal():
        disposing.set()
        assert finish_disposal.wait(10)
        retire_service()

    monkeypatch.setattr(service, "_retire_authority", hold_disposal)

    def get_many(*args, **kwargs):
        entered.set()
        assert close_from_reader.wait(10)
        if shutdown == "reset":
            with pytest.raises(RuntimeError, match="Context reset is in progress"):
                context.close()
        else:
            context.close()
        return original_get_many(*args, **kwargs)

    def read():
        try:
            outcome["row"] = ws.get("n1", entity=_InFlightNote)
        except BaseException as error:
            outcome["error"] = error

    def retire():
        try:
            if shutdown == "close":
                context.close()
            else:
                reset_default_local_state(context)
            outcome["retired"] = True
        except BaseException as error:
            outcome["retire_error"] = error

    monkeypatch.setattr(backend, "get_many", get_many)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert entered.wait(10)
    retirement = threading.Thread(target=retire, daemon=True)
    retirement.start()
    try:
        assert disposing.wait(5)
        close_from_reader.set()
        reader.join(3)
        assert not reader.is_alive(), "the reader's close waited for shutdown"
    finally:
        close_from_reader.set()
        finish_disposal.set()
    retirement.join(3)

    assert not retirement.is_alive()
    assert outcome.get("row", {}).get("name") == "one", outcome
    assert outcome.get("retired"), outcome
    context.close()


@pytest.mark.slow
@pytest.mark.parametrize("shutdown", ["close", "reset"])
@pytest.mark.parametrize("operation", ["get", "upsert"])
def test_shutdown_refuses_workspace_calls_before_queueing(tmp_path, monkeypatch, shutdown, operation):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=_workspace_config(tmp_path / "config"))
    ws, reader, release, result = _read_in_flight(context, monkeypatch)
    errors = []
    disposing = threading.Event()
    finish_disposal = threading.Event()
    service = context.modules()
    retire_service = service._retire_authority

    def hold_disposal():
        disposing.set()
        assert finish_disposal.wait(10)
        retire_service()

    monkeypatch.setattr(service, "_retire_authority", hold_disposal)

    def upsert():
        ws.upsert(_InFlightNote, {"object_id": "n2", "name": "two"})

    def waiting_writer():
        try:
            upsert()
        except RuntimeError as error:
            errors.append(error)

    def shutdown_context():
        if shutdown == "close":
            context.close()
        else:
            reset_default_local_state(context)

    outcome = {}

    def call_during_shutdown():
        try:
            if operation == "get":
                ws.get("n1", entity=_InFlightNote)
            else:
                upsert()
            outcome["unexpected_success"] = True
        except BaseException as error:
            outcome["error"] = error

    writer = threading.Thread(target=waiting_writer, daemon=True)
    retirement = threading.Thread(target=shutdown_context, daemon=True)
    request = threading.Thread(target=call_during_shutdown, daemon=True)
    writer.start()
    deadline = time.monotonic() + 10
    try:
        while context._lock._drainer is not ws._lock:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        retirement.start()
        assert disposing.wait(5)
        request.start()
        request.join(3)
        assert not request.is_alive(), "a new workspace call queued behind the reader that shutdown is draining"
        assert isinstance(outcome.get("error"), RuntimeError), outcome
        assert str(outcome["error"]) == f"Context {shutdown} is in progress"
    finally:
        finish_disposal.set()
        release.set()
        for thread in (reader, retirement, writer, request):
            if thread.ident is not None:
                thread.join(10)

    assert all(not thread.is_alive() for thread in (reader, writer, retirement, request))
    assert result.get("row", {}).get("name") == "one", result
    assert len(errors) == 1
    context.close()


@pytest.mark.slow
@pytest.mark.parametrize("root_action", ["close", "reset"])
def test_workspace_reader_cannot_start_context_retirement(tmp_path, monkeypatch, root_action):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=_workspace_config(tmp_path / "config"))
    child = context.child("in-flight")
    ws = hb.HeavenBase("in-flight", context=child, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(_InFlightNote)
    ws.upsert(_InFlightNote, {"object_id": "n1", "name": "one"})
    backend = ws.backends.get("main")
    original_get_many = backend.get_many
    reading = threading.Barrier(2)
    checked = threading.Barrier(2)
    outcomes = {}

    def get_many(*args, **kwargs):
        target = threading.current_thread().name
        reading.wait(10)
        with pytest.raises(RuntimeError, match="active workspace read"):
            if target == "child":
                child.close()
            elif root_action == "close":
                context.close()
            else:
                reset_default_local_state(context)
        checked.wait(10)
        return original_get_many(*args, **kwargs)

    def read():
        name = threading.current_thread().name
        try:
            outcomes[name] = ws.get("n1", entity=_InFlightNote)
        except BaseException as error:
            outcomes[name] = error

    monkeypatch.setattr(backend, "get_many", get_many)
    root_reader = threading.Thread(target=read, name="root", daemon=True)
    child_reader = threading.Thread(target=read, name="child", daemon=True)
    root_reader.start()
    child_reader.start()
    for thread in (root_reader, child_reader):
        thread.join(3)

    assert not root_reader.is_alive() and not child_reader.is_alive(), "root and child retirement each wait for the other reader"
    assert {name: row.get("name") if isinstance(row, dict) else row for name, row in outcomes.items()} == {"root": "one", "child": "one"}
    child.close()
    context.close()


@pytest.mark.slow
@pytest.mark.parametrize("shutdown", ["close", "reset"])
def test_writer_cannot_retire_context_while_reader_waits_for_worker(tmp_path, monkeypatch, shutdown):
    from heavenbase.context.reset import reset_default_local_state

    context = hb.Context.load(_bootstrap(tmp_path), config=_workspace_config(tmp_path / "config"))
    outer = hb.HeavenBase("outer", context=context, detached=True, backends={"main": {"type": "inmem"}})
    inner = hb.HeavenBase("inner", context=context, detached=True, backends={"main": {"type": "inmem"}})
    for ws in (outer, inner):
        ws.register(_InFlightNote)
        ws.upsert(_InFlightNote, {"object_id": "n1", "name": "one"})
    outer_backend = outer.backends.get("main")
    inner_backend = inner.backends.get("main")
    original_get_many = outer_backend.get_many
    original_upsert = inner_backend.upsert
    writing = threading.Event()
    retire = threading.Event()
    errors = []

    def run(fn):
        try:
            fn()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run, args=(lambda: inner.get("n1", entity=_InFlightNote),), daemon=True)

    def get_many(*args, **kwargs):
        worker.start()
        worker.join()
        return original_get_many(*args, **kwargs)

    def upsert(*args, **kwargs):
        writing.set()
        assert retire.wait(10)
        with pytest.raises(RuntimeError, match="active Context lock"):
            if shutdown == "close":
                context.close()
            else:
                reset_default_local_state(context)
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(outer_backend, "get_many", get_many)
    monkeypatch.setattr(inner_backend, "upsert", upsert)
    writer = threading.Thread(target=run, args=(lambda: inner.upsert(_InFlightNote, {"object_id": "n2", "name": "two"}),), daemon=True)
    reader = threading.Thread(target=run, args=(lambda: outer.get("n1", entity=_InFlightNote),), daemon=True)
    writer.start()
    assert writing.wait(10)
    reader.start()
    deadline = time.monotonic() + 10
    try:
        while not inner._lock._held_back:
            assert time.monotonic() < deadline
            time.sleep(0.005)
    finally:
        retire.set()
    writer.join(3)
    reader.join(3)

    assert not writer.is_alive() and not reader.is_alive(), "shutdown waits for a reader whose worker waits for the shutdown caller"
    assert not worker.is_alive()
    assert not errors, errors
    context.close()
