import re
import sqlite3
import sys

import pytest

from heavenbase.cli.app import build_registry, create_cli, main
from heavenbase.cli.backends.argparse import build_argparse, run_argparse
from heavenbase.cli.backends.click import build_click
from heavenbase.cli.backends.typer import build_typer
from heavenbase.cli.base import CLIContext
from heavenbase.cli.context import PackageCLIContext
from heavenbase.cli.output import CLIOutput
from heavenbase.cli.registry import CLIRegistry
from heavenbase.cli.spec import ArgumentSpec, CommandSpec, OptionSpec
from heavenbase.workspace import WorkspaceSpec
from heavenbase.llm import LLMSession
from heavenbase.utils import get_file_dir, pj, touch_dir

import heavenbase as hb

CAPSULE_REGISTRY_ENTITIES = {"sys-capsule"}


def test_cli_context_is_public_runtime_contract():
    import heavenbase.cli as cli_api
    import heavenbase.cli.spec as spec_api

    assert cli_api.CLIContext is CLIContext
    assert cli_api.PackageCLIContext is PackageCLIContext
    assert "CLIContext" in cli_api.__all__
    assert "PackageCLIContext" in cli_api.__all__
    assert "raise_cli_error" in cli_api.__all__
    assert "PackageCLIContext" not in spec_api.__all__


def test_cli_specs_and_backends_accept_a_minimal_context_contract():
    class MinimalContext:
        package = "portable"
        version = "1.0"

        def fail(self, error, *, code=1, msg=None):
            raise SystemExit(code) from error

    context = MinimalContext()
    registry = CLIRegistry()
    registry.add_root(CommandSpec("ping", lambda context: context.package))

    assert isinstance(context, CLIContext)
    assert run_argparse(build_argparse(registry, context), ["ping"]) == "portable"

    from click.testing import CliRunner as ClickRunner
    from typer.testing import CliRunner as TyperRunner

    assert ClickRunner().invoke(build_click(registry, context), ["ping"]).exit_code == 0
    assert TyperRunner().invoke(build_typer(registry, context), ["ping"]).exit_code == 0


def _sample_registry():
    def hello(context, name, shout=False):
        msg = name.upper() if shout else name
        context.out.echo(f"hello {msg}")

    registry = CLIRegistry()
    registry.add_root(
        CommandSpec(
            "hello",
            hello,
            aliases=["hi"],
            args=[ArgumentSpec("name", help="Name")],
            options=[OptionSpec.from_flags("shout", "--shout", help="Uppercase output.", default=False, is_flag=True, type=bool)],
        )
    )
    return registry


def test_registry_rejects_alias_collisions():
    def noop(context):
        return None

    registry = CLIRegistry()
    registry.add_root(CommandSpec("alpha", noop, aliases=["a"]))
    with pytest.raises(ValueError):
        registry.add_root(CommandSpec("beta", noop, aliases=["a"]))


def test_registry_reads_aliases_from_specs():
    registry = build_registry()
    assert registry.aliases_for((), "config") == ["cfg"]
    assert registry.aliases_for(("config",), "list") == ["ls", "show"]
    assert registry.aliases_for(("llm",), "imagen") == ["image"]


def test_argparse_backend_invokes_command(capsys):
    context = PackageCLIContext(package="demo", out=CLIOutput())
    parser = build_argparse(_sample_registry(), context)
    run_argparse(parser, ["hi", "ada", "--shout"])
    assert "hello ADA" in capsys.readouterr().out


def test_argparse_version_alias(capsys):
    context = PackageCLIContext(package="demo", version="1.2.3", out=CLIOutput())
    parser = build_argparse(_sample_registry(), context)
    with pytest.raises(SystemExit) as exc:
        run_argparse(parser, ["-v"])
    assert exc.value.code == 0
    assert "1.2.3" in capsys.readouterr().out


def test_click_backend_invokes_command():
    from click.testing import CliRunner

    context = PackageCLIContext(package="demo", out=CLIOutput())
    runner = CliRunner()
    result = runner.invoke(build_click(_sample_registry(), context), ["hi", "ada", "--shout"])
    assert result.exit_code == 0
    assert "hello ADA" in result.output


def test_click_version_alias():
    from click.testing import CliRunner

    context = PackageCLIContext(package="demo", version="1.2.3", out=CLIOutput())
    result = CliRunner().invoke(build_click(_sample_registry(), context), ["-v"])
    assert result.exit_code == 0
    assert "1.2.3" in result.output


def test_typer_backend_invokes_command():
    from typer.testing import CliRunner

    context = PackageCLIContext(package="demo", out=CLIOutput())
    runner = CliRunner()
    result = runner.invoke(build_typer(_sample_registry(), context), ["hi", "ada", "--shout"])
    assert result.exit_code == 0
    assert "hello ADA" in result.output


def test_backends_render_cli_errors():
    def fail(context):
        raise ValueError("boom")

    registry = CLIRegistry()
    registry.add_root(CommandSpec("fail", fail))
    context = PackageCLIContext(package="demo", out=CLIOutput())

    parser = build_argparse(registry, context)
    with pytest.raises(SystemExit) as exc:
        run_argparse(parser, ["fail"])
    assert exc.value.code == 1

    from click.testing import CliRunner as ClickRunner
    from typer.testing import CliRunner as TyperRunner

    assert ClickRunner().invoke(build_click(registry, context), ["fail"]).exit_code == 1
    result = TyperRunner().invoke(build_typer(registry, context), ["fail"])
    assert result.exit_code == 1
    assert "ERR boom" in result.output


def test_negatable_options_work_across_backends(capsys):
    def show(context, enabled=True):
        context.out.echo(str(enabled))

    registry = CLIRegistry()
    registry.add_root(
        CommandSpec(
            "show",
            show,
            options=[OptionSpec.from_flags("enabled", "--enabled", "--no-enabled", default=True, is_flag=True, type=bool, negatable=True)],
        )
    )
    context = PackageCLIContext(package="demo", out=CLIOutput())

    run_argparse(build_argparse(registry, context), ["show", "--no-enabled"])
    assert "False" in capsys.readouterr().out

    from click.testing import CliRunner as ClickRunner
    from typer.testing import CliRunner as TyperRunner

    assert "False" in ClickRunner().invoke(build_click(registry, context), ["show", "--no-enabled"]).output
    assert "False" in TyperRunner().invoke(build_typer(registry, context), ["show", "--no-enabled"]).output


def test_typer_help_displays_aliases_once():
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(create_cli("typer"), ["--help"])
    assert result.exit_code == 0
    assert "config|cfg" in result.output
    assert " cfg " not in result.output
    pytest.importorskip("rich")
    assert any(char in result.output for char in "╭┌│╰└")
    assert runner.invoke(create_cli("typer"), ["-v"]).exit_code == 0


def test_typer_group_help_displays_command_aliases():
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(create_cli("typer"), ["config", "--help"])
    assert result.exit_code == 0
    assert "list|ls|show" in result.output
    assert "unset|remove|rm|del|delete" in result.output
    ws_help = runner.invoke(create_cli("typer"), ["ws", "--help"]).output
    assert "manifest" in ws_help
    assert "import" in ws_help
    assert "skill" in ws_help
    create_help = runner.invoke(create_cli("typer"), ["ws", "create", "--help"]).output
    set_help = runner.invoke(create_cli("typer"), ["ws", "set", "--help"]).output
    assert "--active" not in create_help
    assert "--scope" not in create_help
    assert "--scope" not in set_help
    assert "--active" not in runner.invoke(create_cli("typer"), ["ws", "import", "--help"]).output
    assert "show|browse|get" in runner.invoke(create_cli("typer"), ["mcp", "--help"]).output
    assert "workspace" in runner.invoke(create_cli("typer"), ["mcp", "--help"]).output
    assert "--wait" not in runner.invoke(create_cli("typer"), ["mcp", "serve", "--help"]).output


def test_llm_help_exposes_verbose_and_provider_gateway_aliases():
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(create_cli("typer"), ["llm", "chat", "--help"])
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert result.exit_code == 0
    assert "--verbose" in output
    assert "-v" in output
    assert "--provider" in output
    assert "-b" in output
    assert "--gateway" in output
    assert "-g" in output
    assert "--mcp" in output
    assert "--image" in output
    assert "--copy" in output
    assert "-cp" in output
    embed_help = re.sub(r"\x1b\[[0-9;]*m", "", runner.invoke(create_cli("typer"), ["llm", "embed", "--help"]).output)
    assert "--preview" in embed_help
    assert "--copy" in embed_help
    assert "-cp" in embed_help
    session_help = re.sub(r"\x1b\[[0-9;]*m", "", runner.invoke(create_cli("typer"), ["llm", "session", "--help"]).output)
    assert "--mcp" in session_help
    assert "--image" in session_help
    assert "--mcp-url" not in session_help
    assert "--mcp-toolkit" not in session_help


def test_default_cli_registry_exposes_required_groups():
    registry = build_registry()
    assert {command.name for command in registry.root_commands} >= {"setup", "pj"}
    assert {group.name for group in registry.groups} >= {"ws", "config", "llm", "mcp", "prompt"}
    assert create_cli("argparse") is not None


def test_create_cli_reads_backend_from_config():
    class DemoConfig:
        default = {}

        def get(self, key, default=None):
            return "argparse" if key == "heavenbase.cli.backend" else default

    app = create_cli(context=PackageCLIContext(package="demo", cm=DemoConfig(), out=CLIOutput()))
    assert app.prog == "demo"


def test_main_argv_does_not_mutate_sys_argv():
    original = list(sys.argv)
    with pytest.raises(SystemExit) as exc:
        main(["-v"])
    assert exc.value.code == 0
    assert sys.argv == original


def test_setup_registers_default_workspace(isolated_context):
    spec = WorkspaceSpec.setup(reset=True, context=isolated_context)
    assert spec.id == "default"
    assert WorkspaceSpec.load(context=isolated_context).id == "default"
    assert [workspace.id for workspace in WorkspaceSpec.list(context=isolated_context)] == ["default"]
    modules = isolated_context.modules()
    assert modules.catalog.get(("module_set", "heavenbase")) is not None
    assert modules.inspect("operation", "eq")
    registry = hb.capsule_registry(resolver=modules, config=isolated_context.config)
    assert CAPSULE_REGISTRY_ENTITIES <= set(registry.store.owner.workspace.entities)


def test_workspace_setup_indexes_modules_without_initializing_capsule_registry(monkeypatch, isolated_context):
    import heavenbase.capsule as capsule

    events = []

    def setup_modules(force=False):
        events.append(("modules", force))
        return False

    def setup_capsules(reset=False, *, resolver=None, config=None):
        events.append(("capsule", reset, resolver, config))
        return {}

    monkeypatch.setattr(isolated_context, "_setup_modules", setup_modules)
    monkeypatch.setattr(capsule, "setup_capsule_registry", setup_capsules)

    WorkspaceSpec.setup(context=isolated_context)

    assert events == [("modules", False)]


def test_setup_force_reindexes_without_destructive_reset(monkeypatch, isolated_context):
    import heavenbase.workspace.registry as workspace_registry

    events = []

    def setup_modules(force=False):
        events.append(("modules", force))
        return True

    monkeypatch.setattr(isolated_context, "_setup_modules", setup_modules)
    monkeypatch.setattr(workspace_registry, "reset_default_local_state", lambda context=None: events.append(("reset",)))
    context = PackageCLIContext(cm=isolated_context.config, context=isolated_context, out=CLIOutput())
    parser = build_argparse(build_registry(), context)

    run_argparse(parser, ["setup", "--force"])
    # The safe repair forces the module re-index and never touches local state.
    assert events == [("modules", True)]

    events.clear()
    run_argparse(parser, ["setup"])
    assert events == [("modules", False)]

    events.clear()
    run_argparse(parser, ["setup", "--reset", "--force"])
    # --reset keeps its destructive semantics and already implies the re-index.
    assert events == [("reset",), ("modules", True)]


@pytest.mark.slow
def test_setup_reset_removes_broken_default_local_state(tmp_path):
    from heavenbase.context import load_bootstrap
    from heavenbase.capsule.registry import reset_capsule_registry
    from heavenbase.llm.cache import LLMCache

    database_override = load_bootstrap(home_path=tmp_path / "missing-bootstrap.yaml").backend["_override"]
    bootstrap = load_bootstrap(
        {
            "version": 1,
            "root": str(tmp_path),
            "workspace": "default",
            "backend": {
                "type": "sqlite",
                "module": "heavenbase.backends.sql.sqlite",
                "name": "system",
                "database": "file://%/system.db",
                "_override": database_override,
            },
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        }
    )
    manager = hb.ConfigManager(root=str(tmp_path), setup=False)
    context = hb.Context(bootstrap, config=manager)
    default_service = hb.DEFAULT_CONTEXT.modules()

    def write_old_schema(path: str) -> None:
        touch_dir(get_file_dir(path, abs=True))
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE sys_metaschema (kind VARCHAR(255) NOT NULL, subject_id VARCHAR(255) NOT NULL, name VARCHAR(255))")
            conn.execute("INSERT INTO sys_metaschema (kind, subject_id, name) VALUES ('entity', 'old', 'old')")
            conn.commit()
        finally:
            conn.close()

    try:
        write_old_schema(str(tmp_path / "system.db"))
        write_old_schema(manager.pj("%/capsule/registry.db", abs=True))
        write_old_schema(manager.pj("%/llm/cache.db", abs=True))

        assert WorkspaceSpec.setup(reset=True, context=context).id == "default"
        registry = hb.capsule_registry(resolver=context.modules(), config=context.config)
        assert CAPSULE_REGISTRY_ENTITIES <= set(registry.store.owner.workspace.entities)
        assert hb.LLM(preset="mock", context=context).chat("reset cache smoke") == "mock response"
        assert hb.DEFAULT_CONTEXT.modules() is default_service
    finally:
        modules = context._modules
        if modules is not None:
            reset_capsule_registry(drop=True, resolver=modules, config=context.config)
        LLMCache.reset(drop=True, config_manager=context.config)
        context.close()


@pytest.mark.slow
def test_mcp_cli_lists_calls_and_removes_toolkit(tmp_path):
    from typer.testing import CliRunner

    def add_cli(left: int, right: int) -> int:
        return left + right

    workspace_id = f"mcp-cli-{hb.utils.sha256hash(str(tmp_path))[:12]}"
    workspace = hb.HeavenBase(
        workspace_id,
        backends={"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(pj(str(tmp_path), "mcp-cli.db"))}},
    )
    hb.Toolkit("cli-tools", {"add": add_cli}, namespace="demo", version="1").register(ws=workspace)

    runner = CliRunner()
    app = create_cli("typer")
    listed = runner.invoke(app, ["mcp", "list", "--namespace", "demo", "--workspace", workspace_id, "--json"])
    called = runner.invoke(app, ["mcp", "call", "cli-tools", "add", "--namespace", "demo", "--workspace", workspace_id, "--args", '{"left":2,"right":3}'])
    mcp_json = runner.invoke(app, ["mcp", "mcp-json", "demo/cli-tools:1", "--workspace", workspace_id, "--transport", "stdio"])
    sse_json = runner.invoke(app, ["mcp", "mcp-json", "demo/cli-tools:1", "--workspace", workspace_id, "--transport", "sse"])
    http_json = runner.invoke(app, ["mcp", "mcp-json", "demo/cli-tools:1", "--workspace", workspace_id, "--transport", "streamable-http"])
    removed = runner.invoke(app, ["mcp", "remove", "cli-tools", "--namespace", "demo", "--workspace", workspace_id])
    listed_after = runner.invoke(app, ["mcp", "list", "--namespace", "demo", "--workspace", workspace_id, "--json"])

    assert listed.exit_code == 0
    assert "cli-tools" in listed.output
    assert called.exit_code == 0
    assert "5" in called.output
    assert mcp_json.exit_code == 0
    server = hb.loads_json(mcp_json.output)["mcpServers"]["cli-tools"]
    assert server["command"] == "hb"
    assert server["transport"] == "stdio"
    assert workspace_id in server["args"]
    assert sse_json.exit_code == 0
    sse_server = hb.loads_json(sse_json.output)["mcpServers"]["cli-tools"]
    assert sse_server == {"transport": "sse", "url": "http://127.0.0.1:7001/sse"}
    assert http_json.exit_code == 0
    http_server = hb.loads_json(http_json.output)["mcpServers"]["cli-tools"]
    assert http_server == {"transport": "streamable-http", "url": "http://127.0.0.1:7001/mcp"}
    assert removed.exit_code == 0
    assert "removed toolkit" in removed.output
    assert listed_after.exit_code == 0
    assert "cli-tools" not in listed_after.output


@pytest.mark.slow
def test_mcp_cli_supports_unqualified_negative_version_refs(tmp_path):
    from typer.testing import CliRunner

    def add_cli(left: int, right: int) -> int:
        return left + right

    workspace_id = f"mcp-cli-negative-{hb.utils.sha256hash(str(tmp_path))[:12]}"
    workspace = hb.HeavenBase(
        workspace_id,
        backends={"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(pj(str(tmp_path), "mcp-cli-negative.db"))}},
    )
    hb.Toolkit("cli-tools", {"add": add_cli}, namespace="demo", version="1").register(ws=workspace)
    hb.Toolkit("cli-tools", {"add": add_cli}, namespace="demo", version="2").register(ws=workspace)

    called_latest = CliRunner().invoke(
        create_cli("typer"), ["mcp", "call", "cli-tools:-1", "add", "--workspace", workspace_id, "--args", '{"left":2,"right":3}']
    )

    assert called_latest.exit_code == 0
    assert "5" in called_latest.output


def test_mcp_workspace_command_serves_registered_profile(monkeypatch):
    from heavenbase.cli.groups.mcp.commands import serve_workspace

    calls = {}

    class FakeWorkspace:
        def serve(self, **kwargs):
            calls.update(kwargs)

    class FakeSpec:
        def open(self, *, context=None):
            calls["context"] = context
            return FakeWorkspace()

    machine = hb.DEFAULT_CONTEXT
    context = PackageCLIContext(package="demo", context=machine, out=CLIOutput())
    monkeypatch.setattr(WorkspaceSpec, "load", classmethod(lambda cls, ws_id=None, context=None: FakeSpec()))

    serve_workspace(context, "registered", profile="database", transport="http", host="127.0.0.1", port=17013)

    assert calls == {
        "context": machine,
        "profile": "database",
        "transport": "http",
        "host": "127.0.0.1",
        "port": 17013,
    }


def test_prompt_cli_template_translation_roundtrip(isolated_context):
    from typer.testing import CliRunner

    WorkspaceSpec.setup(reset=True, context=isolated_context)
    WorkspaceSpec.register("promptcli", context=isolated_context)
    isolated_context.activate_workspace("promptcli")
    runner = CliRunner()
    app = create_cli("typer", context=PackageCLIContext(cm=isolated_context.config, context=isolated_context))

    created = runner.invoke(app, ["prompt", "create", "demo.cli", "--template", "Hello, {name}", "--tr-key", "name"])
    listed = runner.invoke(app, ["prompt", "list", "--prefix", "demo.", "--json"])
    rendered = runner.invoke(app, ["prompt", "render", "demo.cli", "--args", '{"name":"Ada"}'])
    tr_set = runner.invoke(app, ["prompt", "tr-set", "demo.cli", "Hello, {name}", "zz", "HEY, {name}"])
    rendered_tr = runner.invoke(app, ["prompt", "render", "demo.cli", "--args", '{"name":"Ada"}', "--lang", "zz"])
    tr_list = runner.invoke(app, ["prompt", "tr-list", "demo.cli", "--lang", "zz", "--json"])
    removed = runner.invoke(app, ["prompt", "remove", "demo.cli"])

    assert created.exit_code == 0
    assert listed.exit_code == 0 and "demo.cli" in listed.output
    assert rendered.exit_code == 0 and "Hello, Ada" in rendered.output
    assert tr_set.exit_code == 0
    assert rendered_tr.exit_code == 0 and "HEY, Ada" in rendered_tr.output
    assert tr_list.exit_code == 0 and "HEY, {name}" in tr_list.output
    assert removed.exit_code == 0


def test_workspace_context_activation_marks_active(isolated_context):
    WorkspaceSpec.setup(reset=True, context=isolated_context)
    WorkspaceSpec.register("scratch", context=isolated_context)
    assert isolated_context.activate_workspace("scratch") == "scratch"
    assert WorkspaceSpec.load("scratch", context=isolated_context).active is True
    assert WorkspaceSpec.load(context=isolated_context).id == "scratch"
    assert isolated_context.deactivate_workspace() == "scratch"
    assert all(not workspace.active for workspace in WorkspaceSpec.list(context=isolated_context))


@pytest.mark.slow
def test_workspace_skill_cli_installs_selected_profile(tmp_path):
    from typer.testing import CliRunner

    workspace_id = f"skill-cli-{hb.utils.sha256hash(str(tmp_path))[:12]}"
    hb.HeavenBase(workspace_id, backends={"main": {"type": "inmem", "name": "main"}})

    result = CliRunner().invoke(
        create_cli("typer"),
        ["ws", "skill", workspace_id, "--profile", "full", "--root", str(tmp_path), "--port", "17012"],
    )

    target = tmp_path / workspace_id
    assert result.exit_code == 0, result.output
    assert target.joinpath("SKILL.md").is_file()
    assert "profile: full" in result.output
    assert "http://127.0.0.1:17012/mcp" in target.joinpath("agents", "openai.yaml").read_text(encoding="utf-8")


@pytest.mark.slow
def test_workspace_preset_cli_lists_shows_and_creates(isolated_context):
    from typer.testing import CliRunner

    WorkspaceSpec.setup(reset=True, context=isolated_context)
    runner = CliRunner()
    app = create_cli("typer", context=PackageCLIContext(cm=isolated_context.config, context=isolated_context))

    listed = runner.invoke(app, ["ws", "presets", "list"])
    shown = runner.invoke(app, ["ws", "presets", "show", "debug"])
    created = runner.invoke(app, ["ws", "create", "preset-cli", "--preset", "debug"])
    selected_after_create = WorkspaceSpec.load(context=isolated_context).id
    registered = runner.invoke(app, ["ws", "set", "preset-cli"])
    activated = runner.invoke(app, ["ws", "activate", "preset-cli"])
    selected_after_activate = WorkspaceSpec.load(context=isolated_context).id
    deactivated = runner.invoke(app, ["ws", "deactivate"])

    assert listed.exit_code == 0 and "local-lts" in listed.output
    assert shown.exit_code == 0 and "workspace-preset" in shown.output
    assert created.exit_code == 0 and "preset-cli" in created.output
    spec = WorkspaceSpec.load("preset-cli", context=isolated_context)
    assert spec.config == {"preset": "debug"}
    assert selected_after_create == "default"
    assert registered.exit_code == 0
    assert activated.exit_code == 0 and "active workspace is preset-cli" in activated.output
    assert selected_after_activate == "preset-cli"
    assert deactivated.exit_code == 0 and "deactivated workspace preset-cli" in deactivated.output
    assert WorkspaceSpec.load(context=isolated_context).id == "default"


@pytest.mark.slow
def test_workspace_create_uses_id_scoped_default_unless_preset_is_explicit(isolated_context):
    from typer.testing import CliRunner

    WorkspaceSpec.setup(reset=True, context=isolated_context)
    config = isolated_context.config
    runner = CliRunner()
    app = create_cli("typer", context=PackageCLIContext(cm=config, context=isolated_context))
    implicit_id = "cli-scoped-default"
    explicit_id = "cli-explicit-preset"
    scoped_preset = "cli-scoped-inmem"
    preset_config = {
        "desc": "Dependency-light CLI scope test.",
        "backends": {"main": {"type": "inmem", "name": "main"}},
    }
    for ws_id in (implicit_id, explicit_id):
        scope = f"{config.base_scope}.{ws_id}"
        config.set("heavenbase.workspace.default_preset", scoped_preset, scope=scope)
        config.set(f"heavenbase.workspace.presets.{scoped_preset}", preset_config, scope=scope)

    implicit = runner.invoke(app, ["ws", "create", implicit_id])
    explicit = runner.invoke(app, ["ws", "create", explicit_id, "--preset", "debug"])

    assert implicit.exit_code == 0, implicit.output
    assert explicit.exit_code == 0, explicit.output
    assert WorkspaceSpec.load(implicit_id, context=isolated_context).config == {"preset": scoped_preset}
    assert WorkspaceSpec.load(explicit_id, context=isolated_context).config == {"preset": "debug"}


@pytest.mark.slow
def test_workspace_create_reads_implicit_preset_from_machine_context_config(tmp_path):
    from heavenbase.cli.groups.ws.runtime import create_workspace

    machine_config = hb.ConfigManager(root=str(tmp_path / "machine"), setup=False)
    cli_config = hb.ConfigManager(root=str(tmp_path / "cli"), setup=False)
    machine = hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path / "machine"),
            "workspace": "default",
            "backend": {
                "type": "inmem",
                "module": "heavenbase.backends.inmem.backend",
                "name": "system",
            },
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=machine_config,
    )
    ws_id = "cli-machine-config"
    machine_preset = "machine-scoped-inmem"
    cli_preset = "cli-scoped-inmem"
    preset_config = {
        "desc": "Dependency-light CLI config authority test.",
        "backends": {"main": {"type": "inmem", "name": "main"}},
    }
    scope = f"{machine_config.base_scope}.{ws_id}"

    try:
        machine_config.set("heavenbase.workspace.default_preset", machine_preset, scope=scope)
        machine_config.set(f"heavenbase.workspace.presets.{machine_preset}", preset_config, scope=scope)
        machine_config.set(f"heavenbase.workspace.presets.{cli_preset}", preset_config, scope=scope)
        cli_config.set("heavenbase.workspace.default_preset", cli_preset, scope=scope)
        cli_config.set(f"heavenbase.workspace.presets.{cli_preset}", preset_config, scope=scope)

        cli_context = PackageCLIContext(package="demo", cm=cli_config, context=machine, out=CLIOutput())
        create_workspace(cli_context, ws_id)

        assert WorkspaceSpec.load(ws_id, context=machine).config == {"preset": machine_preset}
    finally:
        machine.close()


def test_llm_session_mock_roundtrip():
    class FakeLLM:
        def chat(self, messages, include=None, **kwargs):
            assert include == "delta"
            return [{"role": "assistant", "content": "hello back"}]

    session = LLMSession(FakeLLM())
    message = session.send("hello")
    assert message["role"] == "assistant"
    assert session.undo() is True
    assert session.messages == []


def test_llm_session_turn_appends_returned_delta(capsys):
    from heavenbase.cli.groups.llm.session import _session_turn

    class FakeLLM:
        def stream(self, messages, include=None, reduce=False, **kwargs):
            assert "delta" in include
            yield {
                "text": "do not print this aggregate text first",
                "think": "do not print this aggregate think first",
                "message": {"role": "assistant", "content": "reconstructed"},
                "delta": [
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "thinking before tool",
                        "tool_calls": [{"id": "call_add", "type": "function", "function": {"name": "add", "arguments": '{"a":1}'}}],
                    },
                ],
            }
            yield {
                "text": "do not print this aggregate text first",
                "think": "do not print this aggregate think first",
                "message": {"role": "assistant", "content": "reconstructed"},
                "delta": [
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "thinking before tool",
                        "tool_calls": [{"id": "call_add", "type": "function", "function": {"name": "add", "arguments": '{"a":1}'}}],
                    },
                    {"role": "tool", "tool_call_id": "call_add", "name": "add", "content": "3"},
                    {"role": "assistant", "content": "final answer"},
                ],
            }

    context = PackageCLIContext(package="demo", out=CLIOutput())
    session = LLMSession(FakeLLM(), messages=[{"role": "user", "content": "call"}])

    _session_turn(context, session, stream=True)

    assert [message["role"] for message in session.messages[-3:]] == ["assistant", "tool", "assistant"]
    output = capsys.readouterr().out
    assert output.startswith("STEPS: 001 / 020")
    assert "do not print this aggregate" not in output
    assert "<think>\nthinking before tool\n</think>" in output
    assert "[Tool: add(a=1)]" in output
    assert "[Response: add(a=1)]\n3" in output
    assert "STEPS: 002 / 020" in output
    assert output.index("thinking before tool") < output.index("[Tool: add(a=1)]") < output.index("[Response: add(a=1)]") < output.index("final answer")


def test_llm_cli_wraps_streamed_thinking(capsys):
    from heavenbase.cli.groups.llm.render import _emit_stream

    context = PackageCLIContext(package="demo", out=CLIOutput())
    _emit_stream(context, iter([{"think": "brief thought"}, {"text": "answer"}]))

    assert "<think>\nbrief thought\n</think>\nanswer" in capsys.readouterr().out


def test_llm_chat_passes_mcp_tools(monkeypatch, capsys):
    import heavenbase.cli.groups.llm.chat as llm_cli

    toolkit = object()

    class FakeLLM:
        def chat(self, messages, system=None, include=None, reduce=False, stream=False, **kwargs):
            assert kwargs["tools"] == [toolkit]
            assert kwargs["max_tool_turns"] == 2
            return {"text": "done", "think": "", "delta": [{"role": "assistant", "content": "done"}]}

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: FakeLLM())
    monkeypatch.setattr(llm_cli, "_load_mcp_tools", lambda context, sources: [toolkit])
    monkeypatch.setattr(llm_cli, "_log_chat_messages", lambda *args, **kwargs: None)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._chat(context, "hello", stream=False, mcps=["demo.chat-tools:-1"], max_steps=3)

    assert "done" in capsys.readouterr().out


def test_llm_chat_passes_images_and_logs_multimodal_message(monkeypatch, tmp_path, capsys):
    import heavenbase.cli.groups.llm.chat as llm_cli

    image = tmp_path / "page.png"
    image.write_bytes(b"page")
    logged = []

    class FakeLLM:
        def chat(self, messages, system=None, include=None, reduce=False, stream=False, **kwargs):
            assert kwargs["images"] == [str(image)]
            return {"text": "recognized", "think": "", "delta": [{"role": "assistant", "content": "recognized"}]}

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: FakeLLM())
    monkeypatch.setattr(llm_cli, "_log_chat_messages", lambda context, messages, **kwargs: logged.extend(messages))

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._chat(context, "Text Recognition: ", image_files=[str(image)], stream=False)

    assert "recognized" in capsys.readouterr().out
    assert logged[0]["role"] == "user"
    assert [part["type"] for part in logged[0]["content"]] == ["text", "image_url"]


def test_llm_session_initial_images_are_attached_once(monkeypatch, tmp_path):
    import heavenbase.cli.groups.llm.session as llm_cli

    image = tmp_path / "page.png"
    image.write_bytes(b"page")
    turns = []

    class SessionEntity:
        def __init__(self, **kwargs):
            self.ws = type("Workspace", (), {"upsert": lambda *args, **kwargs: None})()

        def to_dict(self):
            return {}

        def append_messages(self, *args, **kwargs):
            return None

    class Console:
        prompts = iter(["follow up", "/exit"])

        def prompt(self, text):
            return next(self.prompts)

    def capture_turn(context, session, stream=True, kwargs=None):
        turns.append(list(session.messages))
        session.messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: object())
    monkeypatch.setattr(llm_cli, "_active_workspace", lambda context: object())
    monkeypatch.setattr(llm_cli, "_session_entity", lambda workspace: SessionEntity)
    monkeypatch.setattr(llm_cli, "_session_prompt", lambda: Console())
    monkeypatch.setattr(llm_cli, "_session_turn", capture_turn)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._session(context, "Text Recognition: ", image_files=[str(image)], stream=False)

    assert [part["type"] for part in turns[0][-1]["content"]] == ["text", "image_url"]
    assert turns[1][-1] == {"role": "user", "content": "follow up"}


def test_llm_cli_constructor_uses_the_invocation_context(monkeypatch):
    """LLM commands and workspace persistence must share one machine authority."""
    import heavenbase.cli.groups.llm.runtime as llm_runtime

    owner = object()
    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    cli = type("CLIContext", (), {"runtime_context": lambda self: owner})()
    monkeypatch.setattr(llm_runtime, "LLM", FakeLLM)

    assert isinstance(llm_runtime._llm(context=cli, preset="chat", cache=False), FakeLLM)
    assert captured["context"] is owner
    assert captured["preset"] == "chat"
    assert captured["cache"] is False


def test_llm_chat_copy_copies_final_text(monkeypatch, capsys):
    import heavenbase.cli.groups.llm.chat as llm_cli
    import heavenbase.cli.groups.llm.runtime as llm_runtime

    copied = []

    class FakeLLM:
        def chat(self, messages, system=None, include=None, reduce=False, stream=False, **kwargs):
            return {"text": "done", "think": "", "delta": [{"role": "assistant", "content": "done"}]}

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: FakeLLM())
    monkeypatch.setattr(llm_runtime, "clipboard", lambda text: copied.append(text) or True)
    monkeypatch.setattr(llm_cli, "_log_chat_messages", lambda *args, **kwargs: None)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._chat(context, "hello", stream=False, copy=True)

    captured = capsys.readouterr()
    assert copied == ["done"]
    assert "done" in captured.out
    assert "copied to clipboard" in captured.err


def test_llm_chat_persists_completed_run(monkeypatch, capsys):
    import heavenbase.cli.groups.llm.chat as llm_cli

    ws = hb.HeavenBase("llm-chat-log-test", backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")

    class FakeLLM:
        def chat(self, messages, system=None, include=None, reduce=False, stream=False, **kwargs):
            return {"text": "done", "think": "", "usage": {"total_tokens": 3}, "delta": [{"role": "assistant", "content": "done"}]}

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: FakeLLM())
    monkeypatch.setattr(llm_cli, "_active_workspace", lambda context: ws)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._chat(context, "hello", system="be brief", stream=False)

    assert "done" in capsys.readouterr().out
    sessions = ws.rows(hb.Session)
    messages = ws.rows(hb.Message)
    assert len(sessions) == 1
    assert [message["payload"]["role"] for message in sorted(messages, key=lambda item: item["idx"])] == ["system", "user", "assistant"]
    assert sessions[0]["usage"] == {"total_tokens": 3}


def test_llm_chat_streaming_persists_the_usage_it_measured(monkeypatch, capsys):
    """A streamed run must store real counters, not the empty aggregate it started with."""
    import heavenbase.cli.groups.llm.chat as llm_cli

    ws = hb.HeavenBase("llm-chat-stream-usage-test", backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")

    class FakeLLM:
        def stream(self, messages, system=None, include=None, reduce=False, **kwargs):
            assert "usage" in include and "truncated" in include
            yield {"text": "half ", "think": "", "message": {}, "delta": [], "usage": {"prompt_tokens": 5}, "elapsed": 0.1, "truncated": False}
            yield {"text": "done", "think": "", "message": {}, "delta": [], "usage": {"completion_tokens": 7}, "elapsed": 0.2, "truncated": False}
            # The closing aggregate reports no usage on purpose, to avoid double counting.
            yield {
                "text": "",
                "think": "",
                "message": {"role": "assistant", "content": "half done"},
                "delta": [{"role": "assistant", "content": "half done"}],
                "usage": {},
                "elapsed": 0.4,
                "truncated": False,
            }

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: FakeLLM())
    monkeypatch.setattr(llm_cli, "_active_workspace", lambda context: ws)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._chat(context, "hello", stream=True)

    assert "half done" in capsys.readouterr().out
    session = ws.rows(hb.Session)[0]
    assert session["usage"] == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
    assert session["state"]["usage_recorded"] is True
    assert session["state"]["elapsed"] == 0.4
    assert session["state"]["truncated"] is False


def test_llm_chat_streaming_marks_unmeasured_usage_and_tool_cutoff(monkeypatch, capsys):
    """An empty `usage` must be readable as unmeasured, and a cut-off run as incomplete."""
    import heavenbase.cli.groups.llm.chat as llm_cli

    ws = hb.HeavenBase("llm-chat-stream-unmeasured-test", backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")
    answer = "Let me check the remaining entities.\n\n[incomplete: the tool-call budget of 1 round(s) ran out]"

    class FakeLLM:
        def stream(self, messages, system=None, include=None, reduce=False, **kwargs):
            yield {"text": "Let me check the remaining entities.", "think": "", "message": {}, "delta": [], "usage": {}, "truncated": False}
            yield {
                "text": "",
                "think": "",
                "message": {"role": "assistant", "content": answer},
                "delta": [{"role": "assistant", "content": answer}],
                "usage": {},
                "truncated": True,
            }

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: FakeLLM())
    monkeypatch.setattr(llm_cli, "_active_workspace", lambda context: ws)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._chat(context, "count every entity", stream=True, max_steps=2)

    capsys.readouterr()
    session = ws.rows(hb.Session)[0]
    # The stored aggregate stays empty because nothing was reported; the row says so
    # rather than letting a reader sum it as a measured zero.
    assert session["usage"] == {}
    assert session["state"]["usage_recorded"] is False
    assert session["state"]["truncated"] is True
    assert "elapsed" not in session["state"]
    assert session["turn_count"] == 1


def test_llm_chat_streaming_shows_and_records_a_tool_budget_cutoff(monkeypatch, capsys):
    """A run cut off by its tool budget must say so on screen and in the stored row."""
    import heavenbase.cli.groups.llm.chat as llm_cli

    ws = hb.HeavenBase("llm-chat-stream-cutoff-test", backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")

    def count(step: int) -> int:
        return step

    def mock_chat(spec, messages, stream=False, **kwargs):
        step = sum(message.get("role") == "tool" for message in messages) + 1
        call = {"id": f"c{step}", "type": "function", "function": {"name": "count", "arguments": f'{{"step":{step}}}'}}
        payload = {"role": "assistant", "content": "Let me check the remaining entities.", "tool_calls": [call]}
        if stream:
            return iter([{"choices": [{"delta": {**payload, "tool_calls": [{"index": 0, **call}]}}], "usage": {"total_tokens": 2}}])
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 2}}

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False))
    monkeypatch.setattr(llm_cli, "_load_mcp_tools", lambda context, sources: [count])
    monkeypatch.setattr(llm_cli, "_active_workspace", lambda context: ws)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._chat(context, "count every entity", stream=True, mcps=["demo.count-tools:-1"], max_steps=3)

    output = capsys.readouterr().out
    assert "STEPS: 003 / 003" in output
    assert "[incomplete: the tool-call budget of 2 round(s) ran out" in output
    session = ws.rows(hb.Session)[0]
    assert session["state"]["truncated"] is True
    assert session["state"]["usage_recorded"] is True
    # Three model rounds ran, and every round's counters survived the stream.
    assert session["usage"]["total_tokens"] == 6


def test_llm_embed_full_preview_json_and_copy(monkeypatch, capsys):
    import heavenbase.cli.groups.llm.embed as llm_cli
    import heavenbase.cli.groups.llm.runtime as llm_runtime

    copied = []
    vector = [0.123456789, 1.0, 2.0, 3.0, 4.0, 5.0, -6.25, 7.125]

    class FakeLLM:
        def embed(self, text, include=None, reduce=False):
            assert include == ["embeddings", "dim", "usage"]
            assert reduce is False
            return {"embeddings": vector, "dim": len(vector), "usage": {"total_tokens": 1}}

    monkeypatch.setattr(llm_cli, "_llm", lambda **kwargs: FakeLLM())
    monkeypatch.setattr(llm_runtime, "clipboard", lambda text: copied.append(text) or True)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_cli._embed(context, "hello")
    full_output = capsys.readouterr().out.strip()
    assert hb.loads_json(full_output) == vector
    assert "..." not in full_output

    llm_cli._embed(context, "hello", preview=True)
    preview_output = capsys.readouterr().out.strip()
    assert preview_output == "[0.123457, 1.000000, 2.000000, 3.000000, ..., -6.250000, 7.125000]"

    llm_cli._embed(context, "hello", preview=True, as_json=True, copy=True)
    captured = capsys.readouterr()
    data = hb.loads_json(captured.out)
    assert data == {
        "embeddings": "[0.123457, 1.000000, 2.000000, 3.000000, ..., -6.250000, 7.125000]",
        "dim": 8,
        "usage": {"total_tokens": 1},
    }
    assert copied == [captured.out.strip()]
    assert "copied to clipboard" in captured.err


def test_llm_session_mcp_command_adds_tools(monkeypatch, capsys):
    import heavenbase.cli.groups.llm.mcp as llm_mcp
    import heavenbase.cli.groups.llm.session as llm_cli

    def add(left: int, right: int) -> int:
        return left + right

    toolkit = hb.Toolkit("session-mcp-command", {"add": add}, namespace="demo")
    monkeypatch.setattr(llm_mcp, "_load_mcp_source", lambda context, source: toolkit)

    context = PackageCLIContext(package="demo", out=CLIOutput())
    session = LLMSession(object())
    handled, should_exit, regen, kwargs = llm_cli._handle_session_command(context, session, "/mcp demo.session-mcp-command:-1")

    assert (handled, should_exit, regen, kwargs) == (True, False, False, {})
    assert session.tools == [toolkit]

    llm_cli._session_help(context)
    assert "/tools" in capsys.readouterr().out
    assert "/toolkits" not in llm_cli._SESSION_COMMANDS


def test_llm_session_turn_passes_session_tools():
    from heavenbase.cli.groups.llm.session import _session_turn

    def add(left: int, right: int) -> int:
        return left + right

    toolkit = hb.Toolkit("session-tools", {"add": add}, namespace="demo")

    class FakeLLM:
        def stream(self, messages, include=None, reduce=False, **kwargs):
            assert kwargs["tools"] == [toolkit]
            assert kwargs["max_tool_turns"] == 19
            yield {"text": "done", "think": "", "message": {"role": "assistant", "content": "done"}, "delta": [{"role": "assistant", "content": "done"}]}

    context = PackageCLIContext(package="demo", out=CLIOutput())
    session = LLMSession(FakeLLM(), messages=[{"role": "user", "content": "call"}])
    session.add_tool(toolkit)

    _session_turn(context, session, stream=True)

    assert session.messages[-1] == {"role": "assistant", "content": "done"}


def test_llm_session_turn_persists_completed_iteration():
    from heavenbase.cli.groups.llm.session import _session_turn

    ws = hb.HeavenBase("llm-session-log-test", backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")
    persistent = hb.Session(title="CLI", ws=ws)
    ws.upsert(hb.Session, persistent.to_dict())

    class FakeLLM:
        def stream(self, messages, include=None, reduce=False, **kwargs):
            yield {"text": "done", "think": "", "message": {"role": "assistant", "content": "done"}, "delta": [{"role": "assistant", "content": "done"}]}

    context = PackageCLIContext(package="demo", out=CLIOutput())
    session = LLMSession(FakeLLM(), messages=[{"role": "system", "content": "sys"}])
    session.persistent = persistent
    session._log_start_idx = len(session.messages)
    session.messages.append({"role": "user", "content": "hello"})

    _session_turn(context, session, stream=True)

    loaded = hb.Session.load(persistent.object_id, ws=ws)
    assert [message["role"] for message in loaded.messages()] == ["user", "assistant"]


def test_llm_session_back_syncs_persistent_history(capsys):
    import heavenbase.cli.groups.llm.session as llm_cli

    ws = hb.HeavenBase("llm-session-back-sync-test", backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")
    persistent = hb.Session(title="CLI", ws=ws)
    ws.upsert(hb.Session, persistent.to_dict())
    kept = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "one"}]
    removed = [{"role": "user", "content": "second"}, {"role": "assistant", "content": "two"}]
    persistent.append_messages([*kept, *removed])
    session = LLMSession(object(), messages=[*kept, *removed])
    session.persistent = persistent

    handled, should_exit, regen, kwargs = llm_cli._handle_session_command(PackageCLIContext(package="demo", out=CLIOutput()), session, "/back")

    assert (handled, should_exit, regen, kwargs) == (True, False, False, {})
    assert session.messages == kept
    assert hb.Session.load(persistent.object_id, ws=ws).messages() == kept
    assert "back one step" in capsys.readouterr().out


def test_llm_session_regen_removes_old_persistent_response_before_append():
    import heavenbase.cli.groups.llm.session as llm_cli

    ws = hb.HeavenBase("llm-session-regen-sync-test", backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")
    persistent = hb.Session(title="CLI", ws=ws)
    ws.upsert(hb.Session, persistent.to_dict())
    prompt = {"role": "user", "content": "hello"}
    old = {"role": "assistant", "content": "old"}
    persistent.append_messages([prompt, old])

    class FakeLLM:
        def stream(self, messages, include=None, reduce=False, **kwargs):
            assert messages == [prompt]
            assert "seed" in kwargs
            yield {"text": "new", "think": "", "message": {"role": "assistant", "content": "new"}, "delta": [{"role": "assistant", "content": "new"}]}

    context = PackageCLIContext(package="demo", out=CLIOutput())
    session = LLMSession(FakeLLM(), messages=[prompt, old])
    session.persistent = persistent

    handled, should_exit, regen, kwargs = llm_cli._handle_session_command(context, session, "/regen")
    session._log_start_idx = len(session.messages)
    llm_cli._session_turn(context, session, stream=True, kwargs=kwargs)

    assert (handled, should_exit, regen) == (True, False, True)
    assert session.messages == [prompt, {"role": "assistant", "content": "new"}]
    assert hb.Session.load(persistent.object_id, ws=ws).messages() == session.messages


@pytest.mark.fast
def test_llm_list_providers_serializes_and_redacts_secrets(monkeypatch, capsys):
    """LLM catalog listing must project immutable catalogs and never print secrets."""
    from types import MappingProxyType

    from heavenbase.cli.groups.llm import catalog as llm_catalog
    from heavenbase.cli.context import raise_cli_error

    sentinel = "sk-test-sentinel-credential-dx15"
    frozen = MappingProxyType(
        {
            "probe": MappingProxyType(
                {
                    "api_key": sentinel,
                    "base_url": "https://example.test/v1",
                    "backend": "openai",
                }
            )
        }
    )

    class FakeEngine:
        def root(self):
            return MappingProxyType(
                {
                    "providers": frozen,
                    "presets": MappingProxyType({}),
                    "models": MappingProxyType({}),
                    "gateways": MappingProxyType({}),
                    "endpoints": MappingProxyType({}),
                }
            )

    monkeypatch.setattr(llm_catalog, "LLMEngine", lambda context=None: FakeEngine())
    context = PackageCLIContext(package="demo", out=CLIOutput())
    llm_catalog._list(context, "providers")
    listed = capsys.readouterr().out
    assert "probe" in listed
    assert "https://example.test/v1" in listed
    assert "<redacted>" in listed
    assert sentinel not in listed

    with pytest.raises(SystemExit):
        raise_cli_error(context, RuntimeError(f"cannot represent mappingproxy(api_key={sentinel})"))
    err = capsys.readouterr().err
    assert sentinel not in err
    assert "<redacted>" in err


@pytest.mark.fast
def test_cli_output_redacts_structured_secrets(capsys):
    """CLI JSON/YAML helpers should redact sensitive keys before printing."""
    out = CLIOutput()
    out.yaml({"token": "super-secret", "name": "ok"})
    printed = capsys.readouterr().out
    assert "super-secret" not in printed
    assert "<redacted>" in printed
    assert "ok" in printed


@pytest.mark.fast
def test_cli_output_redacts_flattened_config_table_secrets(capsys):
    """CLI tables should retain field identity while redacting flattened values."""
    out = CLIOutput()
    out.table(
        ["key", "value"],
        [
            ["api_key", "sk-table-secret"],
            ["database", "postgresql://user:db-password@example.test/app"],
            ["name", "visible"],
        ],
    )

    printed = capsys.readouterr().out

    assert "sk-table-secret" not in printed
    assert "db-password" not in printed
    assert printed.count("<redacted>") == 2
    assert "visible" in printed


@pytest.mark.fast
def test_typer_honors_is_flag_without_bool_type():
    """OptionSpec.is_flag must compile as a bare switch in Typer even when type is str."""
    from typer.testing import CliRunner

    def show(context, quiet=False):
        context.out.echo("quiet" if quiet else "loud")

    registry = CLIRegistry()
    registry.add_root(
        CommandSpec(
            "show",
            show,
            options=[OptionSpec.from_flags("quiet", "--quiet", "-q", help="Quiet mode.", default=False, is_flag=True)],
        )
    )
    context = PackageCLIContext(package="demo", out=CLIOutput())
    runner = CliRunner()
    bare = runner.invoke(build_typer(registry, context), ["show", "--quiet"])
    assert bare.exit_code == 0
    assert "quiet" in bare.output
    help_text = runner.invoke(build_typer(registry, context), ["show", "--help"]).output
    assert "<str>" not in help_text


@pytest.mark.fast
def test_click_honors_option_spec_destination_name(capsys):
    """Click must pass OptionSpec.name even when it differs from the long flag."""
    from click.testing import CliRunner

    def show(context, workspace_id=None):
        context.out.echo(f"ws={workspace_id}")

    registry = CLIRegistry()
    registry.add_root(
        CommandSpec(
            "show",
            show,
            options=[OptionSpec.from_flags("workspace_id", "--workspace", "-w", help="Workspace id.")],
        )
    )
    context = PackageCLIContext(package="demo", out=CLIOutput())
    result = CliRunner().invoke(build_click(registry, context), ["show", "--workspace", "alpha"])
    assert result.exit_code == 0
    assert "ws=alpha" in result.output


@pytest.mark.fast
def test_create_cli_accepts_caller_registry():
    """External packages must compile their own CLIRegistry through create_cli."""
    from typer.testing import CliRunner

    def ping(context):
        context.out.echo("pong")

    registry = CLIRegistry()
    registry.add_root(CommandSpec("ping", ping))
    context = PackageCLIContext(package="external", out=CLIOutput())
    app = create_cli("typer", context=context, registry=registry)
    result = CliRunner().invoke(app, ["ping"])
    assert result.exit_code == 0
    assert "pong" in result.output


@pytest.mark.fast
def test_hidden_options_parse_without_appearing_in_backend_help(capsys):
    """One OptionSpec.hidden contract should compile consistently."""
    from click.testing import CliRunner as ClickRunner
    from typer.testing import CliRunner as TyperRunner

    def show(context, profile=None):
        context.out.echo(f"profile={profile}")

    registry = CLIRegistry()
    registry.add_root(
        CommandSpec(
            "show",
            show,
            options=[OptionSpec.from_flags("profile", "--profile", help="Advanced profile.", hidden=True)],
        )
    )
    context = PackageCLIContext(package="demo", out=CLIOutput())

    parser = build_argparse(registry, context)
    with pytest.raises(SystemExit):
        run_argparse(parser, ["show", "--help"])
    assert "--profile" not in capsys.readouterr().out
    run_argparse(parser, ["show", "--profile", "internal"])
    assert "profile=internal" in capsys.readouterr().out

    click_app = build_click(registry, context)
    click_runner = ClickRunner()
    assert "--profile" not in click_runner.invoke(click_app, ["show", "--help"]).output
    assert "profile=internal" in click_runner.invoke(click_app, ["show", "--profile", "internal"]).output

    typer_app = build_typer(registry, context)
    typer_runner = TyperRunner()
    assert "--profile" not in typer_runner.invoke(typer_app, ["show", "--help"]).output
    assert "profile=internal" in typer_runner.invoke(typer_app, ["show", "--profile", "internal"]).output
