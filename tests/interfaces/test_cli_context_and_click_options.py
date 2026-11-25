"""Regressions for CLI context copying and Click compilation."""

from dataclasses import replace

import click
import pytest

import heavenbase as hb
from heavenbase.cli.app import create_cli
from heavenbase.cli.backends.argparse import run_argparse
from heavenbase.cli.registry import CLIRegistry
from heavenbase.cli.spec import ArgumentSpec, CommandSpec, OptionSpec
from heavenbase.context import runtime, set_default_context

pytestmark = pytest.mark.fast


def test_default_cli_context_can_be_replaced():
    def callback(context):
        updated = replace(context, version="custom")
        assert updated is not context
        assert updated.cm is context.cm
        assert updated.runtime_context() is context.runtime_context()
        return updated.version

    registry = CLIRegistry()
    registry.add_root(CommandSpec("test", callback))
    app = create_cli("argparse", registry=registry)

    assert run_argparse(app, ["test"]) == "custom"


def test_copied_cli_context_uses_later_installed_default(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_DEFAULT_CONTEXT", None)
    registry = CLIRegistry()
    registry.add_root(CommandSpec("report", lambda context: replace(context, version="reports")))
    copied = run_argparse(create_cli("argparse", registry=registry), ["report"])
    fallback = copied.cm
    explicit = replace(copied, cm=fallback)
    installed = hb.Context.load(home_path=tmp_path / "bootstrap.yaml")
    try:
        set_default_context(installed)
        assert copied.cm is installed.config
        assert copied.runtime_context() is installed
        assert explicit.cm is fallback
        assert replace(explicit, version="other").cm is fallback
        assert replace(explicit, cm=None).cm is installed.config
    finally:
        installed.close()


def _argument_registry():
    registry = CLIRegistry()
    registry.add_root(
        CommandSpec(
            "test",
            lambda context, project, count: (project, count),
            args=[ArgumentSpec("project", required=False, default="all")],
            options=[OptionSpec("count", ("--count",), type=int)],
        )
    )
    return registry


def test_click_uses_the_declared_positional_default():
    app = create_cli("click", registry=_argument_registry())

    assert app.main(["test"], standalone_mode=False) == ("all", None)


def test_click_converts_and_validates_typed_options():
    app = create_cli("click", registry=_argument_registry())

    assert app.main(["test", "one", "--count", "12"], standalone_mode=False) == ("one", 12)
    with pytest.raises(click.BadParameter):
        app.main(["test", "one", "--count", "many"], standalone_mode=False)


def test_click_infers_option_type_only_when_omitted():
    registry = CLIRegistry()
    registry.add_root(
        CommandSpec(
            "reports",
            lambda context, limit, code: (limit, code),
            options=[OptionSpec("limit", ("--limit",), default=10), OptionSpec("code", ("--code",), default=10, type=str)],
        )
    )
    app = create_cli("click", registry=registry)
    assert app.main(["reports"], standalone_mode=False) == (10, "10")
    assert app.main(["reports", "--limit", "12", "--code", "001"], standalone_mode=False) == (12, "001")
