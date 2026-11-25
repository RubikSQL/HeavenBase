"""Regression coverage for the CLI's process default Context."""

from argparse import ArgumentParser

import pytest
from typer import Typer

import heavenbase as hb
from heavenbase.cli.app import create_cli, main
from heavenbase.cli.backends.argparse import run_argparse
from heavenbase.context import runtime, set_default_context
from heavenbase.utils.config import ConfigManager
from heavenbase.workspace import WorkspaceSpec

pytestmark = pytest.mark.fast


@pytest.fixture
def installed_context(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_DEFAULT_CONTEXT", None)
    bootstrap = {
        "version": 1,
        "root": str(tmp_path),
        "workspace": "default",
        "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }
    config = ConfigManager(default={}, setup=False)
    context = hb.Context.load(bootstrap, config=config)
    try:
        WorkspaceSpec.register("installed-only", config={"backends": {"main": {"type": "inmem"}}}, context=context)
        yield context
    finally:
        context.close()


@pytest.mark.parametrize(("backend", "app_type"), [(None, Typer), ("argparse", ArgumentParser)])
def test_default_cli_uses_installed_context(installed_context, capsys, backend, app_type):
    if backend is not None:
        installed_context.config.set("heavenbase.cli.backend", backend)
    assert set_default_context(installed_context) is installed_context
    assert isinstance(create_cli(), app_type)
    try:
        main(["ws", "list"])
    except SystemExit as error:
        assert error.code == 0
    assert "installed-only" in capsys.readouterr().out


def test_cli_created_before_install_uses_installed_context(installed_context, capsys):
    parser = create_cli("argparse")
    assert runtime._default_context_instance() is None
    set_default_context(installed_context)

    installed_context.config.set("heavenbase.dashboard.open", False)
    run_argparse(parser, ["config", "get", "heavenbase.dashboard.open", "--json"])
    assert capsys.readouterr().out.strip() == "false"
    run_argparse(parser, ["ws", "list"])
    assert "installed-only" in capsys.readouterr().out
