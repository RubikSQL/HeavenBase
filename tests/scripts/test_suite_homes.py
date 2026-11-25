"""A test's temporary home replaces the developer's home on every platform."""

from __future__ import annotations

import ast
import ntpath
from pathlib import Path
import posixpath
import subprocess
import sys

import pytest

TESTS = Path(__file__).resolve().parents[1]
HOME_VARIABLES = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
# Calls that set or remove the variable named by their first argument.
ENV_EDITS = frozenset({"setenv", "delenv", "putenv", "unsetenv", "setdefault", "pop"})
# The developer's own profile, as a parent process on POSIX or Windows inherits it.
REAL_PROFILE = {"HOME": "/home/dev", "USERPROFILE": "C:\\Users\\dev", "HOMEDRIVE": "C:", "HOMEPATH": "\\Users\\dev", "PATH": "/usr/bin"}
# The one place a test may name a home variable: the helper behind the ``home_env`` fixture.
HOME_HELPER = (TESTS / "conftest.py", "isolated_home_env")
pytestmark = pytest.mark.fast


@pytest.mark.parametrize("home", ["/tmp/pytest-0/home", "D:\\Temp\\pytest-0\\home", "\\\\server\\share\\pytest-0\\home"])
def test_a_temporary_home_replaces_every_variable_a_platform_resolves_home_from(home_env, monkeypatch, home):
    """Windows ignores ``HOME``, so a child given only ``HOME`` resolved ``~/.heavenbase`` in the developer's profile."""
    env = home_env(home, base=REAL_PROFILE)
    for name in HOME_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        if name in env:
            monkeypatch.setenv(name, env[name])

    assert env["PATH"] == REAL_PROFILE["PATH"]
    assert posixpath.expanduser("~") == home
    assert ntpath.expanduser("~") == home
    assert ntpath.expanduser("~\\.heavenbase\\bootstrap.yaml") == ntpath.join(home, ".heavenbase", "bootstrap.yaml")
    # A tool that skips USERPROFILE and reads HOMEDRIVE and HOMEPATH lands in the
    # temporary home too, or resolves no home at all for a home without a drive.
    monkeypatch.delenv("USERPROFILE")
    assert ntpath.expanduser("~") == (home if ntpath.splitdrive(home)[0] else "~")


def _home_writes(path: Path) -> list[str]:
    """Return ``path:line`` for each place that sets or removes a home variable outside `HOME_HELPER`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exempt: set[int] = set()
    if path == HOME_HELPER[0]:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == HOME_HELPER[1]:
                exempt.update(id(inner) for inner in ast.walk(node))
    found = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        names: list[object] = []
        if isinstance(node, ast.Dict):
            names = [key.value for key in node.keys if isinstance(key, ast.Constant)]
        elif isinstance(node, ast.Call):
            names = [keyword.arg for keyword in node.keywords]
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if called in ENV_EDITS and node.args and isinstance(node.args[0], ast.Constant):
                names.append(node.args[0].value)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(node.slice, ast.Constant):
            names = [node.slice.value]
        if any(name in HOME_VARIABLES for name in names):
            found.append(f"{path.relative_to(TESTS)}:{node.lineno}")
    return found


@pytest.mark.slow
def test_tests_set_a_temporary_home_only_through_home_env():
    """Each hand-written home environment once missed a variable that Windows resolves home from."""
    found = [site for path in sorted(TESTS.rglob("*.py")) if path != Path(__file__).resolve() for site in _home_writes(path)]

    assert found == [], "Build a child's home with the home_env fixture, which also sets USERPROFILE, HOMEDRIVE and HOMEPATH:\n" + "\n".join(found)


def test_the_test_session_never_reads_the_developers_bootstrap(tmp_path, home_env):
    """The suite once selected the default Context on import, so an unusual personal bootstrap aborted every run."""
    personal = tmp_path / "home" / ".heavenbase"
    personal.mkdir(parents=True)
    (personal / "bootstrap.yaml").write_text("version: 999\n", encoding="utf-8")
    script = """
import runpy, sys
import heavenbase as hb
suite = runpy.run_path(sys.argv[1])
assert hb.DEFAULT_CONTEXT is suite["DEFAULT_CONTEXT"]
print(hb.DEFAULT_CONTEXT.root)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(TESTS / "conftest.py")],
        cwd=tmp_path,
        env=home_env(tmp_path / "home"),
        text=True,
        capture_output=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not Path(result.stdout.strip()).is_relative_to(personal.parent)
