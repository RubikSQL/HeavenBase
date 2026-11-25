"""During a run, `sys.stdout` is a text stream, as 0.1.2.2's run buffer was.

A run's thread gets its run's capture, with the capture's public attributes.
A thread outside runs gets the stream the capture view wraps cut down to a
text stream: the text-stream names that stream has and no other, with
reading, seeking, truncating, detaching and its file descriptor refused.
Closing the view closes only a run's capture and a `with` block only the
capture it entered, so none of these reaches the stream the view wraps.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
import gc
import io
from io import StringIO
import os
import sys
import threading
from threading import Event

import pytest

from heavenbase.extensions.python import _stdout as stdout_capture
from heavenbase.extensions.python import run_python_code

pytestmark = pytest.mark.fast

# What every text stream has; the view answers each of these itself.
TEXT_STREAM_NAMES = sorted(name for name in dir(io.TextIOBase) if not name.startswith("_"))
# The ones a reader can ask without changing the stream.
QUERIES = ["closed", "encoding", "errors", "newlines", "readable", "writable", "seekable", "isatty", "fileno", "tell", "read", "readline", "readlines", "flush"]

# Seconds a thread waits for another one before its test fails instead of hanging. No passing run depends on it: a
# gc.collect() on a busy worker with a large heap takes longer than 10 s.
HANG_GUARD = 120


def _answer(stream, name):
    """Return what `stream` gives for `name`: its value, the result of calling it, or the type of the exception it raises."""
    try:
        value = getattr(stream, name)
        return value() if callable(value) else value
    except Exception as error:
        return type(error)


def _during_a_run(action, line="monthly totals"):
    """Run `action` on this thread, outside runs, while a run on another thread is active; return its result and the run's payload."""
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(HANG_GUARD)
        print(line)

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(HANG_GUARD)
        try:
            seen = action()
        finally:
            release.set()
        payload = task.result(timeout=HANG_GUARD)
    return seen, payload


def test_stdout_is_a_text_stream_for_every_thread_during_a_run(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    seen = {}

    payload = run_python_code("import io, sys\nseen['run'] = (isinstance(sys.stdout, io.TextIOBase), isinstance(sys.stdout, io.IOBase))", {"seen": seen})
    outside, _ = _during_a_run(lambda: (isinstance(sys.stdout, io.TextIOBase), isinstance(sys.stdout, io.IOBase)))

    assert payload["ok"], payload
    assert seen == {"run": (True, True)}
    assert outside == (True, True)


def test_a_report_that_closes_its_output_also_works_on_stdout(monkeypatch):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    code = (
        "import sys\n"
        "def report(path=None):\n"
        "    out = open(path, 'w') if path else sys.stdout\n"
        "    with out:\n"
        "        print('monthly total', 42, file=out)\n"
        "report()\n"
        "seen['closed'] = sys.stdout.closed\n"
        "for name, call in (('flush', lambda: sys.stdout.flush()), ('writelines', lambda: sys.stdout.writelines(['skipped line\\n']))):\n"
        "    try:\n"
        "        call()\n"
        "    except ValueError:\n"
        "        seen[name] = 'refused'\n"
        "print('after the report')\n"
    )
    seen = {}

    payload = run_python_code(code, {"seen": seen})

    # As with 0.1.2.2's run buffer: closed as a stream, it refuses flush and
    # writelines, and plain writes are still captured.
    assert payload["ok"], payload
    assert payload["stdout"] == "monthly total 42\nafter the report\n"
    assert seen == {"closed": True, "flush": "refused", "writelines": "refused"}
    assert sys.stdout is host and not host.closed
    print("host line")
    assert host.getvalue() == "host line\n"


def test_stdout_iterates_and_enters_like_a_text_stream_during_a_run(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    code = (
        "import sys\n"
        "seen['names'] = [hasattr(sys.stdout, name) for name in ('__iter__', '__next__', '__enter__', '__exit__')]\n"
        "seen['iter'] = iter(sys.stdout) is sys.stdout\n"
        "with sys.stdout as out:\n"
        "    seen['entered'] = out is sys.stdout\n"
    )
    seen = {}

    payload = run_python_code(code, {"seen": seen})

    assert payload["ok"], payload
    assert seen == {"names": [True, True, True, True], "iter": True, "entered": True}


def test_pandas_writes_to_stdout_as_a_file_during_a_run(monkeypatch):
    pytest.importorskip("pandas")
    monkeypatch.setattr(sys, "stdout", StringIO())
    code = (
        "import sys\n"
        "import pandas as pd\n"
        "seen['file_like'] = pd.api.types.is_file_like(sys.stdout)\n"
        "pd.DataFrame({'region': ['north', 'south'], 'total': [120, 95]}).to_csv(sys.stdout, index=False, lineterminator='\\n')\n"
    )
    seen = {}

    payload = run_python_code(code, {"seen": seen})

    assert payload["ok"], payload
    assert seen == {"file_like": True}
    assert payload["stdout"] == "region,total\nnorth,120\nsouth,95\n"


def test_every_text_stream_attribute_answers_as_the_stream_the_thread_writes_to(tmp_path, monkeypatch):
    # A read-write file differs from io.IOBase's own answers in almost every name.
    host = open(tmp_path / "host.txt", "w+", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", host)
    seen = {}
    code = "import sys\nseen.update({name: answer(sys.stdout, name) for name in names})"

    payload = run_python_code(code, {"seen": seen, "answer": _answer, "names": QUERIES})
    outside, _ = _during_a_run(lambda: {name: _answer(sys.stdout, name) for name in QUERIES})
    direct = {name: _answer(host, name) for name in QUERIES}
    capture = stdout_capture._BoundedText(1000)
    monkeypatch.undo()
    host.close()

    # Outside runs, reading, seeking and the file descriptor are refused, as
    # on 0.1.2.2's run buffer; the other queries answer as the host's file.
    refused = dict.fromkeys(["fileno", "read", "readline", "readlines"], io.UnsupportedOperation)
    assert payload["ok"], payload
    assert seen == {name: _answer(capture, name) for name in QUERIES}
    assert outside == dict(direct, readable=False, seekable=False, **refused)
    assert [name for name in TEXT_STREAM_NAMES if name not in vars(stdout_capture._ThreadStdout)] == []


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="os.openpty is not available on this platform")
def test_a_thread_outside_runs_sees_the_terminal_but_not_its_file_descriptor(monkeypatch):
    leader, follower = os.openpty()
    terminal = os.fdopen(follower, "w")
    monkeypatch.setattr(sys, "stdout", terminal)
    try:
        seen, payload = _during_a_run(lambda: (sys.stdout.isatty(), _answer(sys.stdout, "fileno")))
    finally:
        monkeypatch.undo()
        terminal.close()
        os.close(leader)

    assert seen == (True, io.UnsupportedOperation)
    assert payload["stdout"] == "monthly totals\n"


def test_dropping_a_copy_of_stdout_closes_nothing(monkeypatch):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)

    def drop_copies():
        copy.copy(sys.stdout)
        copy.deepcopy(sys.stdout)
        gc.collect()
        return host.closed

    closed_outside, payload = _during_a_run(drop_copies)
    code = "import copy, gc, sys\ncopy.copy(sys.stdout)\ncopy.deepcopy(sys.stdout)\ngc.collect()\nprint('still open:', not sys.stdout.closed, flush=True)"
    in_run = run_python_code(code, {})

    assert closed_outside is False
    assert payload["stdout"] == "monthly totals\n"
    assert in_run["ok"] and in_run["stdout"] == "still open: True\n", in_run
    assert not host.closed


class _LineSink:
    """A host stdout that only takes writes, as some GUI and logging bridges are."""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)
        return len(text)

    def flush(self):
        pass


def test_the_view_has_only_the_text_stream_attributes_of_a_write_only_host_stream(monkeypatch):
    sink = _LineSink()
    monkeypatch.setattr(sys, "stdout", sink)
    names = TEXT_STREAM_NAMES + ["buffer", "mode", "name", "lines"]

    def public(stream):
        return sorted(name for name in dir(stream) if not name.startswith("_"))

    def outside():
        seen = {name: hasattr(sys.stdout, name) for name in names}
        seen["dir"] = public(sys.stdout)
        print("host line", flush=True)
        return seen

    seen, payload = _during_a_run(outside)
    in_run = {}
    code = "import sys\nin_run.update({name: hasattr(sys.stdout, name) for name in names})\nin_run['dir'] = public(sys.stdout)"
    run_python_code(code, {"in_run": in_run, "names": names, "public": public})
    capture = stdout_capture._BoundedText(10)

    # A thread outside runs sees the sink's text-stream methods, not its `lines`.
    assert seen == dict({name: name in TEXT_STREAM_NAMES and hasattr(sink, name) for name in names}, dir=["flush", "write"])
    assert in_run == dict({name: hasattr(capture, name) for name in names}, dir=public(capture))
    assert sink.lines == ["host line", "\n"]
    assert payload["stdout"] == "monthly totals\n"


def test_closing_stdout_on_a_thread_outside_runs_leaves_the_run_and_the_host_stream_open(monkeypatch):
    # 0.1.2.2 closed the run's buffer here, which every thread used during the run.
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    started, release = Event(), Event()
    seen = {}

    def job():
        started.set()
        assert release.wait(10)
        seen["run closed"] = sys.stdout.closed
        print("monthly totals", flush=True)

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        try:
            sys.stdout.close()
            seen["host closed"] = sys.stdout.closed
        finally:
            release.set()
        payload = task.result(timeout=10)

    assert seen == {"host closed": False, "run closed": False}
    assert payload["ok"] and payload["stdout"] == "monthly totals\n", payload
    assert not host.closed


def test_a_report_thread_that_outlives_its_run_leaves_the_host_stdout_open(monkeypatch):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    entered, finish = Event(), Event()
    errors = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_type.__name__))
    code = (
        "import sys, threading\n"
        "def report():\n"
        "    out = sys.stdout\n"
        "    with out:\n"
        "        print('monthly total', 42, file=out)\n"
        "        entered.set()\n"
        "        assert finish.wait(10)\n"
        "worker = threading.Thread(target=report)\n"
        "worker.start()\n"
        "assert entered.wait(10)\n"
    )
    namespace = {"entered": entered, "finish": finish}

    payload = run_python_code(code, namespace)
    finish.set()
    namespace["worker"].join(10)
    host_closed = host.closed
    if not host_closed:
        print("host line after the report thread")

    assert payload["ok"] and payload["stdout"] == "monthly total 42\n", payload
    assert errors == []
    assert host_closed is False
    assert host.getvalue() == "host line after the report thread\n"


def test_a_report_on_a_pool_kept_from_an_earlier_call_leaves_the_host_stdout_open(monkeypatch):
    # The run_python tool keeps its namespace from call to call, so a pool made
    # by one call serves the next; its thread belongs to the first run, which
    # has ended, so the report writes to the host's stream.
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    namespace = {}
    first = run_python_code("from concurrent.futures import ThreadPoolExecutor\npool = ThreadPoolExecutor(1)\npool.submit(sum, [1, 2]).result()", namespace)
    code = (
        "import sys\n"
        "def report(path=None):\n"
        "    out = open(path, 'w') if path else sys.stdout\n"
        "    with out:\n"
        "        print('monthly total', 42, file=out)\n"
        "pool.submit(report).result()\n"
    )

    second = run_python_code(code, namespace)
    namespace["pool"].shutdown()

    assert first["ok"] and second["ok"], (first, second)
    assert not host.closed
    assert host.getvalue() == "monthly total 42\n"


def test_a_generator_left_inside_with_stdout_does_not_close_the_host_stdout_when_collected(monkeypatch):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    code = "import sys\ndef rows():\n    with sys.stdout:\n        yield 'north'\n        yield 'south'\npending = rows()\nprint(next(pending))\n"
    namespace = {}

    payload = run_python_code(code, namespace)
    namespace.clear()
    gc.collect()

    assert payload["ok"] and payload["stdout"] == "north\n", payload
    assert not host.closed


def test_a_with_block_left_on_another_runs_thread_closes_only_the_capture_it_entered(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    kept, seen = {}, {}
    rows = "import sys\ndef rows():\n    with sys.stdout:\n        yield 'north'\n        yield 'south'\npending = rows()\nprint(next(pending))\n"
    first = run_python_code(rows, kept)
    code = "import sys\npending.close()\nseen['closed'] = sys.stdout.closed\nprint('second run line', flush=True)\n"

    second = run_python_code(code, {"pending": kept.pop("pending"), "seen": seen})

    assert first["ok"] and first["stdout"] == "north\n", first
    assert second["ok"] and second["stdout"] == "second run line\n", second
    assert seen == {"closed": False}


def test_a_generator_collected_on_another_active_runs_thread_closes_only_the_capture_it_entered(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    entered, release = Event(), Event()
    holder, seen = {}, {}

    def first_job():
        def rows():
            with sys.stdout:
                yield "north"
                yield "south"

        holder["pending"] = rows()
        print(next(holder["pending"]))
        entered.set()
        assert release.wait(HANG_GUARD)
        seen["first closed"] = sys.stdout.closed

    def second_job():
        del holder["pending"]
        gc.collect()
        seen["second closed"] = sys.stdout.closed
        print("second run line", flush=True)

    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(run_python_code, "first_job()", {"first_job": first_job})
        assert entered.wait(HANG_GUARD)
        second = run_python_code("second_job()", {"second_job": second_job})
        release.set()
        first_payload = first.result(timeout=HANG_GUARD)

    assert second["ok"] and second["stdout"] == "second run line\n", second
    assert first_payload["ok"] and first_payload["stdout"] == "north\n", first_payload
    assert seen == {"second closed": False, "first closed": True}


@pytest.mark.slow
def test_an_exit_stack_left_on_another_runs_thread_closes_only_the_capture_it_entered(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    kept, seen = {"ExitStack": ExitStack}, {}
    first = run_python_code("import sys\nstack = ExitStack()\nstack.enter_context(sys.stdout)\nprint('north')\n", kept)
    code = "import sys\nstack.close()\nseen['closed'] = sys.stdout.closed\nprint('second run line', flush=True)\n"

    second = run_python_code(code, {"stack": kept["stack"], "seen": seen})

    assert first["ok"] and first["stdout"] == "north\n", first
    assert second["ok"] and second["stdout"] == "second run line\n", second
    assert seen == {"closed": False}


def _refusals(out):
    """Try to move, cut and detach `out`, and ask whether it can seek; return what each did."""
    seen = {}
    for name, call in (("seek", lambda: out.seek(0)), ("truncate", lambda: out.truncate(0)), ("detach", lambda: out.detach())):
        try:
            call()
            seen[name] = "reached"
        except io.UnsupportedOperation:
            seen[name] = "refused"
    seen["seekable"] = out.seekable()
    return seen


_REFUSED = {"seek": "refused", "truncate": "refused", "detach": "refused", "seekable": False}


def _report_file(tmp_path):
    host = open(tmp_path / "host.txt", "w+", encoding="utf-8")
    host.write("host report\n")
    host.flush()
    return host


def test_a_finished_runs_thread_cannot_seek_truncate_or_detach_the_host_stream(tmp_path, monkeypatch):
    host = _report_file(tmp_path)
    monkeypatch.setattr(sys, "stdout", host)
    go, done = Event(), Event()
    results = {}
    code = (
        "import sys, threading\n"
        "results['during the run'] = refusals(sys.stdout)\n"
        "def later(out):\n"
        "    assert go.wait(10)\n"
        "    results['after the run'] = refusals(out)\n"
        "    done.set()\n"
        "threading.Thread(target=later, args=(sys.stdout,)).start()\n"
    )

    payload = run_python_code(code, {"go": go, "done": done, "results": results, "refusals": _refusals})
    go.set()
    assert done.wait(10)
    print("host line after the run")
    monkeypatch.undo()
    host.close()

    assert payload["ok"], payload
    assert results == {"during the run": _REFUSED, "after the run": _REFUSED}
    assert (tmp_path / "host.txt").read_text(encoding="utf-8") == "host report\nhost line after the run\n"


def test_a_thread_outside_runs_cannot_seek_truncate_or_detach_the_host_stream(tmp_path, monkeypatch):
    host = _report_file(tmp_path)
    monkeypatch.setattr(sys, "stdout", host)

    seen, payload = _during_a_run(lambda: _refusals(sys.stdout))
    print("host line after the run")
    monkeypatch.undo()
    host.close()

    assert seen == _REFUSED
    assert payload["stdout"] == "monthly totals\n"
    assert (tmp_path / "host.txt").read_text(encoding="utf-8") == "host report\nhost line after the run\n"
