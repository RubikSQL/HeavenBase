"""A Capsule registry call under the Context lock does not deadlock with a registry build on another thread.

A compute hook runs inside its workspace write, under the Context lock, and
may ask for the Capsule registry. Building a registry facade opens the
Context's hidden store, which takes the Context lock, so a build that held
the registry lock meanwhile deadlocked with the hook. Each scenario runs in a
child process (``capsule_lock_order_child.py``) so a deadlock fails the test
instead of hanging the suite.
"""

from pathlib import Path
import subprocess
import sys

import pytest

# Each case builds several workspaces (10 to 22 s), so they run in the full tier.
pytestmark = [pytest.mark.full, pytest.mark.slow]

_CHILD = Path(__file__).with_name("capsule_lock_order_child.py")


@pytest.mark.parametrize(
    ("scenario", "notes"),
    [
        ("ordered", 1),  # the hook asks while the other thread waits for the Context lock in its build
        ("race", 200),  # both threads loop without ordering
    ],
)
def test_a_registry_call_from_a_compute_hook_does_not_deadlock_with_a_registry_build(tmp_path, scenario, notes):
    done = subprocess.run([sys.executable, str(_CHILD), str(tmp_path), scenario], capture_output=True, text=True, timeout=300)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert done.stdout.strip().splitlines()[-1] == f"ok {notes}"
