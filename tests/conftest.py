"""Shared pytest bootstrap and explicit daily/full suite policy."""

from collections.abc import Mapping
from dataclasses import replace
import ntpath
import os
from pathlib import Path
import sys
import tempfile

import pytest

# The developer's HeavenBase home, read before HeavenBase is imported. The suite
# neither reads nor writes it, so a session that starts without this folder must
# not create it.
PERSONAL_HOME = Path.home() / ".heavenbase"
PERSONAL_HOME_EXISTED = PERSONAL_HOME.exists()

from heavenbase.backends.inmem.backend import InMemBackend
from heavenbase.context.bootstrap import load_bootstrap
from heavenbase.context.reset import reset_default_local_state
from heavenbase.context.runtime import Context, set_default_context
from heavenbase.utils import CM_HVNB, resolve_interpolations
from heavenbase.utils.files import exists_path
from heavenbase.utils.files import get_file_basename, get_file_dir, pj

# Install the process default Context before anything selects one, so this process
# never reads the developer's ~/.heavenbase/bootstrap.yaml: the default uses the
# packaged bootstrap under a temporary root. `_isolate_global_config` then moves it to
# the session's temporary root. A child process that may select its own default
# Context gets a temporary home from `home_env`.
_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="hvnb-test-root-", ignore_cleanup_errors=True)
DEFAULT_CONTEXT = set_default_context(
    Context(
        replace(load_bootstrap(home_path=Path(_IMPORT_ROOT.name) / "bootstrap.yaml"), root=_IMPORT_ROOT.name),
        config=CM_HVNB,
    )
)

ROOT = get_file_dir(get_file_dir(__file__), abs=True)
for path in (pj(ROOT, "src", abs=True),):
    if exists_path(path) and path not in sys.path:
        sys.path.insert(0, path)

PROVIDER_ENV = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "GEMINI_API_KEY",
    "GEMINI_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "MOONSHOT_API_KEY",
    "MOONSHOT_BASE_URL",
    "MOONSHOT_ZH_BASE_URL",
    "ZAI_API_KEY",
    "ZAI_BASE_URL",
    "ZAI_ZH_BASE_URL",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE_URL",
    "OLLAMA_API_KEY",
    "OLLAMA_BASE_URL",
    "LM_STUDIO_API_KEY",
    "LM_STUDIO_BASE_URL",
    "VLLM_API_KEY",
    "VLLM_BASE_URL",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "BIFROST_API_KEY",
    "BIFROST_BASE_URL",
    "COHERE_API_KEY",
    "COHERE_BASE_URL",
    "COHERE_LITELLM_BASE_URL",
    "VOYAGE_API_KEY",
    "VOYAGE_BASE_URL",
)

FULL_MARKERS = frozenset({"detail", "external", "llm", "slow"})
FAST_MODULES = frozenset(
    {
        "test_backend_conditional.py",
        "test_bootstrap.py",
        "test_config_resolution.py",
        "test_config_spec.py",
        "test_context.py",
        "test_docs_contract.py",
        "test_file_uri.py",
        "test_filter_predicates.py",
        "test_handler_protocol.py",
        "test_logical_types.py",
        "test_modules.py",
        "test_public_api.py",
        "test_query_value.py",
        "test_query_execution_protocol.py",
        "test_query_json.py",
        "test_query_spec.py",
        "test_registry.py",
        "test_registry_backend.py",
        "test_registry_kernel.py",
        "test_row_transport.py",
        "test_schema_ir.py",
        "test_sql_text_semantics.py",
        "test_storage_execution_protocol.py",
        "test_storage_protocol.py",
        "test_strategy_adapter.py",
        "test_utils_basic.py",
        "test_workspace_presets.py",
    }
)


def _env_flag(name: str) -> bool:
    value = resolve_interpolations({"value": f"${{env:{name},false}}"})["value"]
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    marker_expr = config.getoption("markexpr") or ""
    run_external = ("external" in marker_expr and "not external" not in marker_expr) or _env_flag("HB_TEST_EXTERNAL")
    for item in items:
        if item.get_closest_marker("external") is not None and not run_external:
            item.add_marker(pytest.mark.skip(reason="external tests require -m external or HB_TEST_EXTERNAL=1"))
        if any(item.get_closest_marker(name) for name in FULL_MARKERS):
            if item.get_closest_marker("full") is None:
                item.add_marker(pytest.mark.full)
            continue
        if item.get_closest_marker("full") is not None:
            continue
        if item.get_closest_marker("fast") is not None:
            continue
        if get_file_basename(str(item.path)) in FAST_MODULES:
            item.add_marker(pytest.mark.fast)
        else:
            item.add_marker(pytest.mark.full)


def pytest_sessionfinish(session, exitstatus):
    """Fail a session that created the developer's HeavenBase home; xdist workers leave the check to the controller."""
    if hasattr(session.config, "workerinput") or PERSONAL_HOME_EXISTED or not PERSONAL_HOME.exists():
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("!", f"a test created {PERSONAL_HOME}; tests must use a temporary home", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(scope="session", autouse=True)
def _isolate_global_config(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("hvnb-config"))
    CM_HVNB.root = root
    DEFAULT_CONTEXT.root = root
    DEFAULT_CONTEXT.bootstrap = replace(DEFAULT_CONTEXT.bootstrap, root=root)
    reset_default_local_state(DEFAULT_CONTEXT)
    DEFAULT_CONTEXT._backend = InMemBackend("system", ws_id="default", config=CM_HVNB)
    DEFAULT_CONTEXT._owns_backend = False
    CM_HVNB._bind_registry_factory(lambda: DEFAULT_CONTEXT.registry("system-config"))
    CM_HVNB._clear_caches()
    CM_HVNB.setup(reset=True)
    DEFAULT_CONTEXT._setup_modules(force=True)
    CM_HVNB.set("heavenbase.llm.default_preset", "mock")
    yield
    reset_default_local_state(DEFAULT_CONTEXT)
    CM_HVNB._clear_caches()


@pytest.fixture(autouse=True)
def _restore_global_config(_isolate_global_config):
    """Restore the worker's raw config records, including scope history."""
    registry = CM_HVNB._scope_registry
    publication = registry.publication
    records = registry.snapshot()
    yield
    if registry.publication == publication:
        return

    def restore(draft):
        for scope in draft.list():
            if scope not in records:
                draft.delete(scope)
        for scope, record in records.items():
            if draft.get(scope) != record:
                draft.update(scope, record)

    registry.mutate(restore)
    CM_HVNB.refresh()


@pytest.fixture(autouse=True)
def _isolate_provider_env(_restore_global_config, monkeypatch, request):
    if request.node.get_closest_marker("external") or request.node.get_closest_marker("llm"):
        yield
        CM_HVNB.refresh()
        return
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    CM_HVNB.refresh()
    yield
    CM_HVNB.refresh()


@pytest.fixture()
def isolated_context(tmp_path):
    """Give tests that change machine state their own configuration and stores."""
    bootstrap = replace(DEFAULT_CONTEXT.bootstrap, root=str(tmp_path))
    context = Context(bootstrap)
    try:
        yield context
    finally:
        context.close()


@pytest.fixture()
def default_extensions_without_agent():
    """Temporarily keep tests that require an empty user schema off the default Agent root."""
    old_value = CM_HVNB.get("heavenbase.extensions.default", default=None)
    CM_HVNB.set("heavenbase.extensions.default", ["toolkit", "prompt"])
    try:
        yield
    finally:
        if old_value is None:
            CM_HVNB.unset("heavenbase.extensions.default")
        else:
            CM_HVNB.set("heavenbase.extensions.default", old_value)


def isolated_home_env(home: str | os.PathLike[str], base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``base`` (default ``os.environ``) whose home directory is ``home`` on every platform.

    POSIX resolves ``~`` from ``HOME``. Windows ignores ``HOME`` and resolves ``~``,
    ``Path.home()`` and therefore ``~/.heavenbase`` from ``USERPROFILE``, then from
    ``HOMEDRIVE`` plus ``HOMEPATH``. A child process given only ``HOME`` would read and
    write the developer's own HeavenBase home on Windows.
    """
    env = dict(os.environ if base is None else base)
    path = os.fspath(home)
    env["HOME"] = path
    env["USERPROFILE"] = path
    drive, rest = ntpath.splitdrive(path)
    if drive:
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = rest or "\\"
    else:
        env.pop("HOMEDRIVE", None)
        env.pop("HOMEPATH", None)
    return env


@pytest.fixture(scope="session")
def home_env():
    """Build a subprocess environment for a temporary home; see `isolated_home_env`."""
    return isolated_home_env
