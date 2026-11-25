"""A capture view stays alive while a thread may still print through it, but never keeps the host's stream alive.

On Python 3.10 and 3.11, `print()` holds `sys.stdout` by a borrowed reference
across the separate writes of its arguments, separators and line end. A view
that was freed when a run ended and put back the stream it wrapped would then
be used after it was freed, which crashes the interpreter, so that scenario
runs in fresh interpreters. Python 3.12 and later hold a strong reference, so
the crash does not show there. A view kept alive that way must still let the
host's own stream go as soon as the host drops it, as Python would.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import redirect_stdout
import copy
import gc
import io
from io import StringIO
import multiprocessing
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
import weakref

import pytest

from heavenbase.extensions.python import _stdout as stdout_capture
from heavenbase.extensions.python import run_python_code

pytestmark = pytest.mark.fast

SRC = Path(__file__).resolve().parents[2] / "src"

_PRINT_RACE_SCRIPT = """
import contextlib
import io
import os
import sys
import threading

sys.path.insert(0, sys.argv[1])
from heavenbase.extensions.python import run_python_code

runs = int(sys.argv[2])
# "buffers": each run sits inside a host redirect to a fresh buffer that the
# host drops after the run, so the views of dropped streams must stay alive too.
dropped_buffers = len(sys.argv) > 3 and sys.argv[3] == "buffers"
# A short switch interval makes the printing thread give up the interpreter
# inside print() often, between the writes that make up one line.
sys.setswitchinterval(1e-4)
sink = open(os.devnull, "w")
sys.stdout = sink
stop = threading.Event()
# A printer that used a freed object can also fail with an exception instead of a crash.
errors = []
threading.excepthook = lambda args: errors.append(args.exc_type.__name__)


def report_progress():
    while not stop.is_set():
        print("progress", 1, 2, 3)


printer = threading.Thread(target=report_progress)
printer.start()
for _ in range(runs):
    if dropped_buffers:
        report = io.StringIO()
        with contextlib.redirect_stdout(report):
            run_python_code("total = 1", {})
        del report
    else:
        run_python_code("total = 1", {})
stop.set()
printer.join()
sys.stdout = sys.__stdout__
print("printer failed" if errors else "finished", errors[:2], flush=True)
"""


@pytest.mark.parametrize("host_streams", ["one stream", "buffers"])
def test_thread_printing_while_runs_start_and_end_never_crashes(tmp_path, home_env, host_streams):
    # Several interpreters race at once, because one race can miss the window.
    processes = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", _PRINT_RACE_SCRIPT, str(SRC), "1000", host_streams],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=home_env(tmp_path),
        )
        for _ in range(8)
    ]
    deadline = time.monotonic() + 120
    outcomes = []
    for process in processes:
        try:
            out, err = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            outcomes.append("timed out")
            continue
        finished = process.returncode == 0 and out.strip() == "finished []"
        outcomes.append("ok" if finished else f"exit {process.returncode}: {out.strip()[-200:]} {err.strip()[-300:]}")

    assert outcomes == ["ok"] * len(processes)


def test_runs_over_one_stream_reuse_one_view(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    views = []

    for _ in range(50):
        payload = run_python_code("import sys\nviews.append(weakref.ref(sys.stdout))\nprint('daily totals')", {"views": views, "weakref": weakref})
        assert payload["stdout"] == "daily totals\n", payload

    assert all(view() is not None and view() is views[0]() for view in views)


def test_redirect_buffer_is_released_when_the_host_drops_it(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    buffer = StringIO()
    released = weakref.ref(buffer)
    with redirect_stdout(buffer):
        assert run_python_code("print('weekly totals')", {})["stdout"] == "weekly totals\n"
    del buffer
    gc.collect()

    assert released() is None


@pytest.mark.parametrize("make_copy", [copy.copy, copy.deepcopy], ids=["copy", "deep copy"])
def test_a_copy_of_stdout_that_a_run_leaves_installed_releases_the_host_buffer(monkeypatch, make_copy):
    monkeypatch.setattr(sys, "stdout", StringIO())
    buffer = StringIO()
    released = weakref.ref(buffer)
    with redirect_stdout(buffer):
        payload = run_python_code("import sys\nsys.stdout = make_copy(sys.stdout)\nprint('totals line')", {"make_copy": make_copy})
        assert sys.stdout is buffer
    del buffer
    gc.collect()

    assert payload["stdout"] == "totals line\n", payload
    assert released() is None


@pytest.mark.parametrize("make_copy", [copy.copy, copy.deepcopy], ids=["copy", "deep copy"])
def test_a_copy_of_stdout_that_a_program_keeps_releases_the_host_buffer(monkeypatch, make_copy):
    monkeypatch.setattr(sys, "stdout", StringIO())
    buffer = StringIO()
    released = weakref.ref(buffer)
    namespace = {"make_copy": make_copy}
    with redirect_stdout(buffer):
        payload = run_python_code("import sys\nkept = make_copy(sys.stdout)\nprint('totals line', file=kept)", namespace)
        print("host line", file=namespace["kept"])
        written = buffer.getvalue()
    del buffer
    gc.collect()

    assert payload["stdout"] == "totals line\n", payload
    assert written == "host line\n"
    assert released() is None


class _SlottedReport:
    """A host stream that takes no weak references."""

    __slots__ = ("lines",)

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)
        return len(text)

    def flush(self):
        pass


def test_a_copy_of_stdout_writes_to_a_host_stream_that_takes_no_weak_references(monkeypatch):
    report = _SlottedReport()
    monkeypatch.setattr(sys, "stdout", report)
    copies = []

    assert run_python_code("keep()", {"keep": lambda: copies.append(copy.copy(sys.stdout))})["ok"]
    copies[0].write("host line\n")

    assert report.lines == ["host line\n"]


def test_report_file_is_flushed_when_the_host_drops_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    path = tmp_path / "daily_report.txt"
    report = open(path, "w")
    with redirect_stdout(report):
        print("report header")
        assert run_python_code("print('inside the run')", {})["stdout"] == "inside the run\n"
        print("report footer")
    del report
    gc.collect()

    assert path.read_text() == "report header\nreport footer\n"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
def test_report_file_reaches_disk_in_a_forked_worker_that_ends_with_os_exit(tmp_path, monkeypatch):
    # A forked worker ends with os._exit, as multiprocessing fork workers do, so
    # only what was already flushed reaches the disk.
    monkeypatch.setattr(sys, "stdout", StringIO())
    path = tmp_path / "worker_report.txt"
    pid = os.fork()
    if pid == 0:
        try:
            report = open(path, "w")
            with redirect_stdout(report):
                print("worker summary")
                run_python_code("total = 1", {})
            del report
        finally:
            os._exit(0)
    os.waitpid(pid, 0)

    assert path.read_text() == "worker summary\n"


def _write_worker_log(path):
    log = open(path, "w")
    with redirect_stdout(log):
        print("worker started")
        run_python_code("print('inside the run')", {})
        print("worker finished")


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="the fork start method is not available on this platform")
def test_multiprocessing_fork_worker_log_reaches_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    path = tmp_path / "worker.log"
    worker = multiprocessing.get_context("fork").Process(target=_write_worker_log, args=(str(path),))
    worker.start()
    worker.join(30)

    assert worker.exitcode == 0
    assert path.read_text() == "worker started\nworker finished\n"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
def test_a_dropped_report_is_written_once_when_the_host_later_forks_a_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    path = tmp_path / "shared_report.txt"
    report = open(path, "w")
    with redirect_stdout(report):
        print("parent summary")
        run_python_code("total = 1", {})
    del report
    pid = os.fork()
    if pid == 0:
        try:
            run_python_code("total = 2", {})
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    run_python_code("total = 3", {})
    gc.collect()

    assert path.read_text() == "parent summary\n"


class _SlowReport(io.TextIOBase):
    """A report stream whose close takes a while, as a flush to a slow disk or pipe does."""

    def __init__(self, record):
        self.record = record

    def write(self, text):
        return len(text)

    def close(self):
        if not self.closed:
            self.record["closed under the capture lock"] = stdout_capture._LOCK._is_owned()
            time.sleep(1.0)
        super().close()


def test_a_dropped_host_stream_is_not_closed_under_the_capture_lock(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    record = {}
    report = _SlowReport(record)
    with redirect_stdout(report):
        assert run_python_code("total = 1", {})["ok"]
    del report
    timings = {}

    def timed_run(name, delay):
        time.sleep(delay)
        started = time.perf_counter()
        assert run_python_code("total = 2", {})["ok"]
        timings[name] = time.perf_counter() - started

    runs = [threading.Thread(target=timed_run, args=("first", 0.0)), threading.Thread(target=timed_run, args=("second", 0.2))]
    for thread in runs:
        thread.start()
    for thread in runs:
        thread.join(30)

    assert record == {"closed under the capture lock": False}
    assert timings["second"] < 0.5


def test_a_stream_a_program_assigned_is_closed_where_another_run_can_start(monkeypatch):
    # A stream that only `sys.stdout` refers to is closed when the run's end puts
    # back what the run found. Its close here runs another program on a worker
    # thread and waits for it, which needs the capture lock to be free.
    monkeypatch.setattr(sys, "stdout", StringIO())
    summaries = []

    with ThreadPoolExecutor(max_workers=1) as pool:

        class SummaryReport(StringIO):
            def close(self):
                if not self.closed:
                    summary = pool.submit(run_python_code, "print('summary')", {})
                    done, _ = wait([summary], timeout=10)
                    summaries.append(summary.result()["stdout"] if done else "the other run waits for this one")
                super().close()

        payload = run_python_code("import sys\nsys.stdout = make_report()\nprint('rows')", {"make_report": SummaryReport})

    assert payload["ok"], payload
    assert summaries == ["summary\n"]


def test_run_cost_stays_flat_while_the_host_keeps_its_redirect_buffers(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    kept, timings = [], []
    for _ in range(6000):
        buffer = StringIO()
        kept.append(buffer)
        started = time.perf_counter()
        with redirect_stdout(buffer):
            run_python_code("total = 1", {})
        timings.append(time.perf_counter() - started)

    assert statistics.median(timings[-200:]) < 5 * statistics.median(timings[:200])


def test_alternating_host_streams_route_every_run_and_host_line(monkeypatch):
    first, second = StringIO(), StringIO()
    for number in range(30):
        target = first if number % 2 == 0 else second
        monkeypatch.setattr(sys, "stdout", target)
        payload = run_python_code("print('run', number)", {"number": number})
        assert payload["stdout"] == f"run {number}\n", payload
        print("host", number)
        assert sys.stdout is target

    assert first.getvalue() == "".join(f"host {n}\n" for n in range(0, 30, 2))
    assert second.getvalue() == "".join(f"host {n}\n" for n in range(1, 30, 2))


def test_runs_while_stdout_is_none_reuse_one_view(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    run_python_code("print('line')", {})
    views = len(stdout_capture._VIEWS)

    for _ in range(200):
        assert run_python_code("print('line')", {})["stdout"] == "line\n"
        assert sys.stdout is None

    assert len(stdout_capture._VIEWS) == views
