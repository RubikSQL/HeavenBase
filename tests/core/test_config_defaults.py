"""Regression coverage for global config and logging defaults."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("log", marks=pytest.mark.slow),
        pytest.param("invalid", marks=pytest.mark.fast),
        pytest.param("unwritable", marks=pytest.mark.fast),
    ],
)
def test_global_config_defaults(tmp_path, home_env, case):
    if case == "unwritable" and (os.name == "nt" or os.geteuid() == 0):
        pytest.skip("chmod must stop this process from writing to the test home")
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("config_defaults_child.py")), case, str(tmp_path)],
        env=home_env(home),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.slow
@pytest.mark.parametrize("store", ["invalid-bootstrap", "inmem"])
def test_cli_reports_an_unsaved_config_set(tmp_path, home_env, store):
    config_home = tmp_path / ".heavenbase"
    config_home.mkdir()
    bootstrap = {"version": 2}
    if store == "inmem":
        bootstrap = {
            "version": 1,
            "root": str(config_home),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
        }
    (config_home / "bootstrap.yaml").write_text(yaml.safe_dump(bootstrap), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "heavenbase.cli.app", "config", "set", "notes.display", "compact"],
        env=home_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode != 0
    assert "not saved" in result.stderr + result.stdout


@pytest.mark.slow
def test_config_home_with_parent_component_uses_the_same_saved_store(tmp_path, home_env):
    (tmp_path / "anchor").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    child = Path(__file__).with_name("config_defaults_child.py")
    for operation, spelling in (("write-home", tmp_path / "anchor" / ".." / "home"), ("read-home", home)):
        result = subprocess.run(
            [sys.executable, str(child), operation, str(tmp_path)],
            env=home_env(spelling),
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert result.returncode == 0, result.stderr
