"""The lazy facades advertise DEFAULT_CONTEXT without selecting it."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast


def test_default_context_is_discoverable_before_access(home_env, tmp_path):
    subprocess.run([sys.executable, str(Path(__file__).resolve())], env=home_env(tmp_path), check=True)


if __name__ == "__main__":
    import heavenbase as hb
    import heavenbase.context as context
    from heavenbase.context.runtime import _default_context_instance

    for facade in (hb, context):
        assert "DEFAULT_CONTEXT" in dir(facade)
        assert "DEFAULT_CONTEXT" not in vars(facade)
        assert "DEFAULT_CONTEXT" not in facade.__all__
    assert _default_context_instance() is None
