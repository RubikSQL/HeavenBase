"""Regression cases for package config and store authority."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    [
        "explicit",
        "bind",
        "reset",
        "hash-policy",
        "independent-store",
        pytest.param("owned-sql", marks=pytest.mark.slow),
        "log-store",
        "refresh-to-inmem",
    ],
)
def test_global_config_repair(tmp_path, home_env, case):
    if case == "reset" and (os.name == "nt" or os.geteuid() == 0):
        pytest.skip("chmod must stop this process from writing to the test home")
    child = Path(__file__).with_name("config_repair_child.py")
    result = subprocess.run([sys.executable, str(child), case], env=home_env(tmp_path), capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
