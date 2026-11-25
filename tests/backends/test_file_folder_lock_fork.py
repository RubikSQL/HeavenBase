"""A worker forked during a File Backend write can write to the same folder."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.fast, pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")]

SRC = Path(__file__).resolve().parents[2] / "src"

# A thread is inside a write, so it holds the folder lock, when the process forks a worker (multiprocessing's "fork"
# start method). The worker builds its own File Backend over the folder and writes one row; a timer kills it if it hangs.
_SCRIPT = """
import os
import signal
import sys
import threading
import warnings

warnings.simplefilter("ignore", DeprecationWarning)  # Python 3.12+ warns about forking with threads
import heavenbase as hb

path = hb.utils.path_to_file_uri(sys.argv[1])
row = lambda object_id: hb.ext.RowOp("revision-note", object_id, {"object_id": object_id, "title": object_id})
writer = hb.backends.JsonFileBackend("main", path)
save, writing, release = writer._save_record, threading.Event(), threading.Event()


def paused_save(*args, **kwargs):
    writing.set()
    release.wait(100)
    return save(*args, **kwargs)


writer._save_record = paused_save
thread = threading.Thread(target=writer.upsert, args=([row("from-the-thread")],))
thread.start()
assert writing.wait(60)
child = os.fork()
if child == 0:
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(30)
    hb.backends.JsonFileBackend("main", path).upsert([row("from-the-worker")])
    os._exit(0)
status = os.waitpid(child, 0)[1]
release.set()
thread.join(60)
assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, f"the forked worker did not write: {status}"
assert sorted(found["object_id"] for found in hb.backends.JsonFileBackend("main", path).scan("revision-note")) == ["from-the-thread", "from-the-worker"]
"""


def test_a_worker_forked_during_a_write_can_write_to_the_folder(tmp_path, home_env):
    env = home_env(tmp_path / "home")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run([sys.executable, "-c", _SCRIPT, str(tmp_path / "notes")], env=env, text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
