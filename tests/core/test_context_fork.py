"""A child forked by a thread that holds the Context lock goes on using the Context."""

from pathlib import Path
import os
import subprocess
import sys

import pytest

_FORK_CHILD = Path(__file__).with_name("context_fork_child.py")


@pytest.mark.full
@pytest.mark.slow
@pytest.mark.skipif(not hasattr(os, "fork"), reason="the platform has no fork")
@pytest.mark.parametrize("closing", ["no-close", "close-waits"])
def test_a_child_forked_by_the_owner_of_the_context_lock_goes_on_using_the_context(tmp_path, closing):
    done = subprocess.run([sys.executable, str(_FORK_CHILD), str(tmp_path / "ctx"), closing], capture_output=True, text=True, timeout=120)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert done.stdout.strip().splitlines()[-1] == "ok"
