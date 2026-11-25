"""What a thread outside runs can do to the host's stdout through a run's `sys.stdout`.

Such a thread, a thread that a run left behind included, writes and flushes
through the capture view to the host's stream, but it gets no `buffer`,
`reconfigure()` or file descriptor of that stream, as 0.1.2.2's run buffer had
none, so it can neither empty, close, re-wrap nor reconfigure the host's file.
"""

from concurrent.futures import ThreadPoolExecutor
import io
import os
import sys
from threading import Event

import pytest

from heavenbase.extensions.python import run_python_code

pytestmark = pytest.mark.fast


def _rewrap_for_utf8(out):
    # A common idiom: wrap stdout's bytes in a UTF-8 text stream, write, and drop the wrapper.
    report = io.TextIOWrapper(out.buffer, encoding="utf-8")
    report.write("weekly totals\n")
    report.flush()
    del report


# Ways host code reaches past a text stream to the file under it, and what the view raises instead.
REACHES = {
    "empty it through its buffer": (lambda out: (out.buffer.seek(0), out.buffer.truncate(0)), AttributeError),
    "close it through its buffer": (lambda out: out.buffer.close(), AttributeError),
    "change its line endings": (lambda out: out.reconfigure(newline="\r\n"), AttributeError),
    "re-wrap it for UTF-8": (_rewrap_for_utf8, AttributeError),
    "empty it through its file descriptor": (lambda out: os.ftruncate(out.fileno(), 0), io.UnsupportedOperation),
}


def _outcome(action, out):
    """Return the type of the exception that `action(out)` raises, or "reached" when it raises none."""
    try:
        action(out)
    except Exception as error:
        return type(error)
    return "reached"


def _after_the_run(action):
    """Give a thread that a run starts the run's `sys.stdout`; return what `action` does with it there after the run ended."""
    go, done = Event(), Event()
    results = {}

    def later(out):
        assert go.wait(10)
        results["outcome"] = _outcome(action, out)
        done.set()

    payload = run_python_code("import sys, threading\nthreading.Thread(target=later, args=(sys.stdout,)).start()\n", {"later": later})
    go.set()
    assert done.wait(10)
    assert payload["ok"], payload
    return results["outcome"]


def _during_a_run(action):
    """Return what `action` does with `sys.stdout` on this thread, outside runs, while a run on another thread is active."""
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(10)
        print("run line", flush=True)

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        try:
            outcome = _outcome(action, sys.stdout)
        finally:
            release.set()
        payload = task.result(timeout=10)
    assert payload["ok"] and payload["stdout"] == "run line\n", payload
    return outcome


@pytest.mark.slow
@pytest.mark.parametrize("when", [_after_the_run, _during_a_run], ids=["thread a run left behind", "host thread during a run"])
@pytest.mark.parametrize("reach", list(REACHES))
def test_a_thread_outside_runs_cannot_reach_the_host_file_under_stdout(tmp_path, monkeypatch, when, reach):
    action, refusal = REACHES[reach]
    # newline="\n" writes the same bytes on every platform, so a changed line ending shows in the file
    host = open(tmp_path / "host.txt", "w+", encoding="utf-8", newline="\n")
    host.write("host report\n")
    host.flush()
    monkeypatch.setattr(sys, "stdout", host)

    outcome = when(action)
    closed = host.closed
    if not closed:
        print("host line after the run")
    monkeypatch.undo()
    if not closed:
        host.close()

    assert outcome is refusal
    assert closed is False
    assert (tmp_path / "host.txt").read_bytes() == b"host report\nhost line after the run\n"


class _LineSink:
    """A host stdout that only takes writes, as some GUI and logging bridges are; it is no context manager itself."""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)
        return len(text)

    def flush(self):
        pass


def test_with_stdout_on_a_host_thread_enters_and_leaves_without_closing_anything(monkeypatch):
    # The view is a text stream, so `with` works through it even over a sink
    # that is no context manager, and leaving the block closes nothing.
    sink = _LineSink()
    monkeypatch.setattr(sys, "stdout", sink)

    def block(out):
        with out:
            print("host line in the block", file=out)

    outcome = _during_a_run(block)

    assert outcome == "reached"
    assert sink.lines == ["host line in the block", "\n"]
