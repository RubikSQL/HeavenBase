"""The Python runner's stdout capture keeps working once other threads can no longer run.

Interpreter exit stops daemon threads wherever they are and only then flushes
``sys.stdout``, and a forked child inherits the parent's memory but none of
its other threads. A capture view that waited on a lock one of those threads
held would hang the exit or the child forever, so each scenario runs in a
fresh interpreter with a deadline.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.fast

SRC = Path(__file__).resolve().parents[2] / "src"

_EXIT_SCRIPT = """
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
from heavenbase.extensions.python import run_python_code

writers = int(sys.argv[2])
started = threading.Barrier(writers + 1)
program = "started.wait(60)\\nwhile True:\\n    sys.stdout.write('x')"
for _ in range(writers):
    threading.Thread(target=run_python_code, args=(program, {"sys": sys, "started": started}), daemon=True).start()
started.wait(60)
time.sleep(0.2)
print("exiting", flush=True)
"""

_FORK_SCRIPT = """
import io
import multiprocessing
import os
import signal
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
from heavenbase.extensions.python import run_python_code

writers, forks = int(sys.argv[2]), int(sys.argv[3])
report = sys.stdout
sys.stdout = io.StringIO()


def hung_children():
    # Each child writes and flushes stdout once; a child still running after 5 s is hung.
    pids = []
    for _ in range(forks):
        pid = os.fork()
        if pid == 0:
            sys.stdout.write("child\\n")
            sys.stdout.flush()
            os._exit(0)
        pids.append(pid)
    deadline = time.monotonic() + 5
    hung = 0
    for pid in pids:
        while os.waitpid(pid, os.WNOHANG)[0] == 0:
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                hung += 1
                break
            time.sleep(0.01)
    return hung


def failed_processes():
    # Each multiprocessing "fork" child prints a line and returns, which flushes
    # stdout on its way out; a child that is still running after 5 s has failed.
    context = multiprocessing.get_context("fork")
    children = [context.Process(target=print, args=("child",)) for _ in range(5)]
    for child in children:
        child.start()
    deadline = time.monotonic() + 5
    failed = 0
    for child in children:
        child.join(max(0.0, deadline - time.monotonic()))
        if child.is_alive() or child.exitcode != 0:
            failed += 1
            child.kill()
            child.join()
    return failed


def with_writers(action):
    # Run `action` while threads started inside the run keep writing to stdout.
    stop = threading.Event()

    def write():
        while not stop.is_set():
            sys.stdout.write("y")

    threads = [threading.Thread(target=write) for _ in range(writers)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    try:
        return action()
    finally:
        stop.set()
        for thread in threads:
            thread.join()


writing, done = threading.Event(), threading.Event()


def hold():
    writing.set()
    done.wait(60)


in_run = run_python_code("with_writers(hung_children)", {"with_writers": with_writers, "hung_children": hung_children})
processes = run_python_code("with_writers(failed_processes)", {"with_writers": with_writers, "failed_processes": failed_processes})
worker = threading.Thread(target=run_python_code, args=("with_writers(hold)", {"with_writers": with_writers, "hold": hold}))
worker.start()
writing.wait(60)
from_host = hung_children()
done.set()
worker.join(60)
report.write(f"{in_run['ok']} {in_run.get('result')} {from_host} {processes['ok']} {processes.get('result')}\\n")
"""


_FORK_ROUTING_SCRIPT = """
import os
import sys
import threading

sys.path.insert(0, sys.argv[1])
from heavenbase.extensions.python import run_python_code


def fork_and_write(line):
    sys.stdout.flush()
    pid = os.fork()
    if pid == 0:
        sys.stdout.write(line + "\\n")
        sys.stdout.flush()
        os._exit(0)
    os.waitpid(pid, 0)


ready, done = threading.Event(), threading.Event()


def hold():
    ready.set()
    done.wait(60)


worker = threading.Thread(target=run_python_code, args=("hold()", {"hold": hold}))
worker.start()
ready.wait(60)
fork_and_write("child of a thread outside runs")
payload = run_python_code("fork_and_write('child of a run thread')", {"fork_and_write": fork_and_write})
done.set()
worker.join(60)
print("run stdout:", repr(payload["stdout"]), flush=True)
"""


def test_interpreter_exits_while_runs_on_daemon_threads_keep_writing(tmp_path, home_env):
    # Several interpreters exit at once, because one exit can miss the moment
    # a daemon thread is inside a write.
    processes = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", _EXIT_SCRIPT, str(SRC), "4"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=home_env(tmp_path),
        )
        for _ in range(6)
    ]
    deadline = time.monotonic() + 60
    outcomes = []
    for process in processes:
        try:
            out, err = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            outcomes.append("hung at exit")
            continue
        outcomes.append("ok" if process.returncode == 0 and out.strip() == "exiting" else f"exit {process.returncode}: {err.strip()[-300:]}")

    assert outcomes == ["ok"] * len(processes)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
def test_forked_children_write_while_threads_of_a_run_keep_writing(tmp_path, home_env):
    # Children are forked from a run's own thread, from a thread outside any
    # run, and by multiprocessing inside a run, while threads a run started
    # keep writing.
    result = subprocess.run(
        [sys.executable, "-I", "-c", _FORK_SCRIPT, str(SRC), "2", "20"],
        capture_output=True,
        text=True,
        timeout=120,
        env=home_env(tmp_path),
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["True", "0", "0", "True", "0"], result.stderr[-2000:]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
def test_forked_child_keeps_the_routing_of_its_forking_thread(tmp_path, home_env):
    # While a run is active, a child forked from a thread outside runs writes to the
    # host stream; a child forked from a run's thread writes to its own copy of the
    # run's capture, which reaches neither the host stream nor the run's payload.
    result = subprocess.run(
        [sys.executable, "-I", "-c", _FORK_ROUTING_SCRIPT, str(SRC)],
        capture_output=True,
        text=True,
        timeout=120,
        env=home_env(tmp_path),
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.splitlines() == ["child of a thread outside runs", "run stdout: ''"], result.stderr[-2000:]
