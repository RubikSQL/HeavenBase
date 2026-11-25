"""A print through `sys.stdout` runs no more of the capture module than when `write` and `flush` were plain methods.

Each write runs three of its functions, the view's `write`, the lookup of the
writing thread's capture and the write of a run's capture or the lookup of the
wrapped stream; each flush runs three in a run and four on a thread outside
runs, where the wrapped stream is looked up too.
"""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import sys
from threading import Event

import pytest

from heavenbase.extensions.python import _stdout as stdout_capture
from heavenbase.extensions.python import run_python_code

pytestmark = pytest.mark.fast


def _calls_into_the_capture_module(action):
    """Return the class and name of each function of the capture module that `action` runs, in order, as a profiler sees them."""
    calls = []

    def record(frame, event, arg):
        if event == "call" and frame.f_code.co_filename == stdout_capture.__file__:
            calls.append((type(frame.f_locals.get("self")).__name__, frame.f_code.co_name))

    previous = sys.getprofile()
    sys.setprofile(record)
    try:
        action()
    finally:
        sys.setprofile(previous)
    return calls


@pytest.mark.parametrize("flush", [False, True], ids=["print", "flushed print"])
def test_a_print_in_a_run_runs_three_capture_functions_per_write_and_flush(monkeypatch, flush):
    monkeypatch.setattr(sys, "stdout", StringIO())
    seen = {}
    code = "seen['calls'] = calls_into(lambda: print('north', flush=flush))"

    payload = run_python_code(code, {"seen": seen, "calls_into": _calls_into_the_capture_module, "flush": flush})

    assert payload["ok"] and payload["stdout"] == "north\n", payload
    assert len(seen["calls"]) <= 3 * 2 + (3 if flush else 0), seen["calls"]  # the text, the line end, then the flush


@pytest.mark.parametrize("flush", [False, True], ids=["print", "flushed print"])
def test_a_print_on_a_host_thread_during_a_run_runs_three_capture_functions_per_write_and_four_per_flush(monkeypatch, flush):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(10)

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        try:
            calls = _calls_into_the_capture_module(lambda: print("host line", flush=flush))
        finally:
            release.set()
        assert task.result(timeout=10)["ok"]

    assert len(calls) <= 3 * 2 + (4 if flush else 0), calls  # the text, the line end, then the flush
    assert host.getvalue() == "host line\n"
