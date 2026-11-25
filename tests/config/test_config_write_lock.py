"""Config writers that share one Context home run one after another instead of racing their compare-and-sets."""

from __future__ import annotations

from dataclasses import replace
import errno
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import heavenbase as hb
from heavenbase.context.registry import HeavenBaseRegistryStore
from heavenbase.utils import cmd
from heavenbase.utils.config import ConfigManager
from heavenbase.utils.config import manager as manager_module
from heavenbase.utils.config.manager import _home_write_lock

pytestmark = pytest.mark.fast

SRC = Path(__file__).resolve().parents[2] / "src"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-lock behaviour")

# Each writer counts, for every write, how often it lost the compare-and-set
# (the commit loop draws a backoff pause only then), and prints the largest
# count. A writer that holds the home's write lock can lose at most once: to a
# write saved before it took the lock, which it had not read yet.
_WRITER_SCRIPT = """
import random as real_random
import sys

sys.path.insert(0, sys.argv[1])
index, writes = int(sys.argv[2]), int(sys.argv[3])
from heavenbase.utils import CM_HVNB
import heavenbase.utils.config.manager as manager

losses = []


class CountingRandom:
    @staticmethod
    def uniform(low, high):
        losses.append(1)
        return real_random.uniform(low, high)


manager.random = CountingRandom
manager._WRITE_LOCK_WAIT = 600.0  # this test measures the lock, not its give-up deadline
CM_HVNB.load()
print("ready", flush=True)
sys.stdin.readline()
worst = 0
for j in range(writes):
    before = len(losses)
    CM_HVNB.set(f"heavenbase.write_lock_probe.w{index}.k{j}", j)
    worst = max(worst, len(losses) - before)
print("worst", worst, flush=True)
"""

_READER_SCRIPT = """
import sys

sys.path.insert(0, sys.argv[1])
from heavenbase.utils import CM_HVNB

saved = CM_HVNB.get("heavenbase.write_lock_probe", default={}) or {}
print(sum(len(keys) for keys in saved.values()))
"""


@pytest.mark.slow
def test_writers_of_one_home_never_lose_twice_and_keep_every_write(tmp_path_factory, home_env):
    home = tmp_path_factory.mktemp("config-write-lock-home")
    cmd(
        [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); from heavenbase.utils import CM_HVNB; CM_HVNB.load()", str(SRC)],
        check=True,
        env=home_env(home),
    )
    writers, writes = 6, 10
    processes = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", _WRITER_SCRIPT, str(SRC), str(index), str(writes)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=home_env(home),
        )
        for index in range(writers)
    ]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready", process.stderr.read()
        # Release every writer at once, so their writes contend.
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        results = [process.communicate(timeout=300) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
    failures = [err.strip().splitlines()[-1:] for process, (_, err) in zip(processes, results) if process.returncode != 0]
    assert failures == []
    worst = [int(out.split()[-1]) for out, _ in results]
    assert max(worst) <= 1, worst

    stored = subprocess.run([sys.executable, "-I", "-c", _READER_SCRIPT, str(SRC)], capture_output=True, text=True, env=home_env(home), timeout=300)
    assert stored.stdout.strip() == str(writers * writes), stored.stderr


@pytest.fixture
def contexts(tmp_path):
    """Open Contexts on one home, each with its own ConfigManager and Registry, like separate processes."""
    opened = []

    def open_context(root=None):
        context = hb.Context(replace(hb.DEFAULT_CONTEXT.bootstrap, root=str(root or tmp_path)))
        opened.append(context)
        context.config.load()
        return context

    try:
        yield open_context
    finally:
        for context in reversed(opened):
            context.close()


def _in_thread(action, timeout):
    """Run ``action`` in a thread; return whether it finished within ``timeout`` and its error."""
    errors = []

    def run():
        try:
            action()
        except BaseException as error:  # reported to the test
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive(), errors


def test_the_lock_file_lives_in_the_context_home(contexts, tmp_path):
    context = contexts()
    context.config.set("k", 1)
    assert (Path(context.root) / ".config-write.lock").exists()


def test_a_manager_without_a_context_leaves_no_lock_file_in_its_root(tmp_path):
    # ``root`` of a plain manager resolves relative paths; it may be a project directory.
    backend = hb.backends.InMemBackend("config-write-lock", ws_id="config-write-lock")
    manager = ConfigManager(
        package="write-lock-test",
        root=str(tmp_path),
        default={},
        registry=hb.Registry.load("config-write-lock", store=HeavenBaseRegistryStore(backend)),
        refresh_interval=999.0,
    )
    manager.set("k", 1)
    assert manager.get("k") == 1
    assert not (tmp_path / ".config-write.lock").exists()


def test_a_write_waits_for_the_home_write_lock_and_then_saves(contexts, monkeypatch):
    context = contexts()
    monkeypatch.setattr(manager_module, "_WRITE_LOCK_WAIT", 60.0, raising=False)
    held = _home_write_lock(context.root)
    held.__enter__()
    done = threading.Event()
    try:
        threading.Thread(target=lambda: (context.config.set("k", 1), done.set()), daemon=True).start()
        # This thread holds the lock, as another process could.
        assert not done.wait(0.5)
    finally:
        held.__exit__(None, None, None)
    assert done.wait(30)
    assert context.config.get("k") == 1


@pytest.mark.slow
def test_a_write_stops_waiting_for_a_stuck_holder_and_saves_with_compare_and_set(contexts, monkeypatch):
    context = contexts()
    monkeypatch.setattr(manager_module, "_WRITE_LOCK_WAIT", 0.3, raising=False)
    release = threading.Event()
    holding = threading.Event()

    def stuck_holder():
        with _home_write_lock(context.root):
            holding.set()
            release.wait(60)

    threading.Thread(target=stuck_holder, daemon=True).start()
    assert holding.wait(10)
    try:
        finished, errors = _in_thread(lambda: context.config.set("k", 1), 30)
        assert finished and errors == []
        assert context.config.get("k") == 1
    finally:
        release.set()


@POSIX_ONLY
@pytest.mark.parametrize("code", [errno.ENOLCK, errno.ENOTSUP], ids=["ENOLCK", "ENOTSUP"])
def test_a_home_whose_filesystem_refuses_the_lock_writes_with_compare_and_set(contexts, monkeypatch, code):
    import fcntl

    context = contexts()
    real_flock = fcntl.flock

    def refuse(fd, operation):
        if operation & fcntl.LOCK_EX:
            raise OSError(code, os.strerror(code))
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", refuse)
    context.config.set("k", 1)
    assert context.config.get("k") == 1


@pytest.mark.slow
@pytest.mark.parametrize("spelling", ["dot", "symlink"])
def test_a_write_made_inside_a_write_of_the_same_home_does_not_wait_for_itself(contexts, tmp_path, spelling):
    if spelling == "dot":
        other_root = str(tmp_path) + os.sep + "."
    else:
        other_root = str(tmp_path.parent / f"{tmp_path.name}-link")
        try:
            os.symlink(tmp_path, other_root, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available")
    writer, other = contexts(), contexts(other_root)
    registry = writer.config._scope_registry
    real_mutate = registry.mutate
    nested = []

    def mutate(change):
        if not nested:
            nested.append(True)
            other.config.set("peer", 2)  # a write of the same home, made while this thread holds its lock
        return real_mutate(change)

    registry.mutate = mutate
    finished, errors = _in_thread(lambda: writer.config.set("mine", 1), 30)
    assert finished, "a nested write waited for the lock its own thread holds"
    assert errors == []
    writer.config.refresh()
    assert writer.config.get("mine") == 1 and writer.config.get("peer") == 2


@POSIX_ONLY
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_read_only_home_writes_without_the_lock_and_keeps_errors_unchained(tmp_path):
    home = tmp_path / "read-only-home"
    home.mkdir()
    home.chmod(0o500)
    try:
        with _home_write_lock(str(home)):
            pass
        assert not (home / ".config-write.lock").exists()
        with pytest.raises(ValueError) as raised:
            with _home_write_lock(str(home)):
                raise ValueError("body failed")
        assert raised.value.__context__ is None
    finally:
        home.chmod(0o700)


# A writer forks a child (a multiprocessing "fork" worker, say) while it holds
# the lock, then dies without releasing it. The child must not keep the lock.
_FORK_SCRIPT = """
import os
import sys
import time

sys.path.insert(0, sys.argv[1])
import heavenbase.utils.config.manager as manager

with manager._home_write_lock(sys.argv[2]):
    child = os.fork()
    if child == 0:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)  # let the parent's caller see its output end now
        os.dup2(devnull, 2)
        time.sleep(60)
        os._exit(0)
    print(child, flush=True)
    os._exit(0)  # the writer dies while it holds the lock
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_child_forked_during_a_write_does_not_keep_the_dead_writers_lock(tmp_path):
    import fcntl
    import signal

    result = subprocess.run([sys.executable, "-I", "-c", _FORK_SCRIPT, str(SRC), str(tmp_path)], capture_output=True, text=True, timeout=120)
    child = int(result.stdout.strip() or 0)
    assert child, result.stderr
    fd = os.open(tmp_path / ".config-write.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError while the child keeps the lock
    finally:
        os.close(fd)
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


# Another library's fork hook that saves config runs on the forking thread,
# before or after HeavenBase's own fork hooks depending on which was
# registered first. The fork must return, and the write must save.
_FORK_HOOK_SCRIPT = """
import os
import sys

sys.path.insert(0, sys.argv[1])
kind, order = sys.argv[2], sys.argv[3]


def save():
    from heavenbase.utils import CM_HVNB

    CM_HVNB.set("heavenbase.fork_hook_probe." + kind, os.getpid())
    print("saved in", "child" if kind == "after_in_child" else "parent", flush=True)


if order == "hook-first":
    os.register_at_fork(**{kind: save})
from heavenbase.utils import CM_HVNB

if order == "hook-last":
    os.register_at_fork(**{kind: save})
CM_HVNB.load()
pid = os.fork()
if pid == 0:
    os._exit(0)
os.waitpid(pid, 0)
print("forked", flush=True)
"""


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="needs os.fork")
@pytest.mark.parametrize("order", ["hook-first", "hook-last"])
@pytest.mark.parametrize("kind", ["before", "after_in_parent", "after_in_child"])
def test_a_config_write_in_another_librarys_fork_hook_never_blocks_the_fork(tmp_path, home_env, kind, order):
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _FORK_HOOK_SCRIPT, str(SRC), kind, order],
            capture_output=True,
            text=True,
            timeout=60,
            env=home_env(tmp_path),
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"{kind} hook registered {order}: the fork never returned; output so far {error.stdout!r}")
    assert result.returncode == 0, result.stderr
    where = "child" if kind == "after_in_child" else "parent"
    assert result.stdout.splitlines() == [f"saved in {where}", "forked"], (result.stdout, result.stderr)
