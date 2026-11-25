"""Regression coverage for persistent configuration after restoring a temporary default."""

from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

import heavenbase as hb
from heavenbase.context import runtime

pytestmark = pytest.mark.fast

_MARKER = "r9_context.after_patch"


def _write_config(root):
    temporary = hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
        }
    )
    assert runtime._default_context_instance() is None
    with patch.object(runtime, "DEFAULT_CONTEXT", temporary):
        assert hb.CM_HVNB.set("r9_context.inside_patch", "temporary")
    assert runtime._default_context_instance() is None
    installed = runtime.default_context()
    assert installed.config is hb.CM_HVNB
    assert hb.CM_HVNB.set(_MARKER, "saved")
    assert hb.CM_HVNB.get(_MARKER) == "saved"
    temporary.close()
    installed.close()


def test_config_writes_persist_after_temporary_default_is_restored(tmp_path, home_env):
    home = tmp_path / "home"
    home.mkdir()
    command = [sys.executable, str(Path(__file__).resolve())]
    kwargs = dict(capture_output=True, text=True, timeout=30, env=home_env(home))
    for args in (["write", str(tmp_path / "temporary")], ["read"]):
        result = subprocess.run(command + args, **kwargs)
        assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    if sys.argv[1] == "write":
        _write_config(Path(sys.argv[2]))
    else:
        config = runtime.default_context().config
        value = config.get(_MARKER)
        assert value == "saved", value
