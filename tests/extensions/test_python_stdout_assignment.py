"""What a Python run leaves in `sys.stdout`.

A run's end puts back what the run found, as 0.1.2.2's redirect did, also while
other runs are active, unless a capture view in use by a run is there; a view
gives way to the stream it wraps once no active run writes through it. No
module's type changes.
"""

import _thread
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import importlib
import io
import pickle
import sys
import threading
from threading import Event
import types

import pytest

from heavenbase.extensions.python import _stdout as stdout_capture
from heavenbase.extensions.python import run_python_code

pytestmark = pytest.mark.fast


def test_module_imported_during_a_run_is_an_ordinary_module(monkeypatch, tmp_path):
    (tmp_path / "invoice_rules.py").write_text("stdout = True\nTAX_RATE = 0.2\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "invoice_rules", raising=False)

    payload = run_python_code("import sys, types, invoice_rules\ninvoice_rules.stdout = False\ntype(sys) is types.ModuleType", {})

    assert payload["ok"] and payload["result"] is True, payload
    module = sys.modules["invoice_rules"]
    assert type(module) is types.ModuleType and type(sys) is types.ModuleType
    assert module.stdout is False
    buffer = io.BytesIO()
    pickler = pickle.Pickler(buffer)
    pickler.dispatch_table = {types.ModuleType: lambda value: (importlib.import_module, (value.__name__,))}
    pickler.dump(module)
    assert pickle.loads(buffer.getvalue()) is module


def test_program_that_assigns_stdout_gives_the_host_its_stdout_back_when_the_run_ends(monkeypatch):
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    namespace = {}

    payload = run_python_code("import io, sys\nprint('monthly totals')\nsys.stdout = scratch = io.StringIO()\nprint('kept')", namespace)
    print("host line")

    assert payload["ok"] and payload["stdout"] == "monthly totals\n", payload
    assert namespace["scratch"].getvalue() == "kept\n"
    assert sys.stdout is host and host.getvalue() == "host line\n"
    following = run_python_code("print('next report')", {})
    assert following["stdout"] == "next report\n"
    assert sys.stdout is host


@pytest.mark.parametrize("call", ["step()", "worker = threading.Thread(target=step)\nworker.start()\nworker.join()"], ids=["same thread", "worker thread"])
def test_a_nested_run_that_assigns_stdout_gives_its_outer_run_the_capture_back(monkeypatch, call):
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    steps = []

    def step():
        steps.append(run_python_code("import io, sys\nprint('step')\nsys.stdout = io.StringIO()\nprint('scratch')", {}))

    payload = run_python_code(f"import threading\nprint('before')\n{call}\nprint('after')", {"step": step})

    assert [done["stdout"] for done in steps] == ["step\n"], steps
    assert payload["ok"] and payload["stdout"] == "before\nafter\n", payload
    assert sys.stdout is host and host.getvalue() == ""


def test_a_run_that_assigns_stdout_leaves_a_longer_run_on_another_thread_capturing(monkeypatch):
    # The short run puts back what it found when it ends, as 0.1.2.2's redirect
    # did, so no later output goes to the stream its program assigned.
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    started, release = Event(), Event()
    namespace = {}

    def job():
        started.set()
        assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        longer = pool.submit(run_python_code, "print('early')\njob()\nprint('late')", {"job": job})
        assert started.wait(10)
        short = run_python_code("import io, sys\nprint('short')\nsys.stdout = scratch = io.StringIO()", namespace)
        print("host line")
        release.set()
        payload = longer.result(timeout=10)

    assert short["ok"] and short["stdout"] == "short\n", short
    assert payload["ok"] and payload["stdout"] == "early\nlate\n", payload
    assert namespace["scratch"].getvalue() == ""
    assert sys.stdout is host and host.getvalue() == "host line\n"


def test_a_stream_a_run_assigned_does_not_come_back_with_a_run_that_started_after_it_ended(monkeypatch):
    # The first run assigns stdout and ends while the second is active; the third
    # starts after that and outlives the second, so it must not find that stream.
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    started, release = [Event() for _ in range(3)], [Event() for _ in range(3)]

    def job(number):
        def wait_for_release():
            started[number].set()
            assert release[number].wait(10)

        return wait_for_release

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(run_python_code, "import io, sys\njob()\nsys.stdout = io.StringIO()", {"job": job(0)})
        assert started[0].wait(10)
        second = pool.submit(run_python_code, "job()\nprint('second totals')", {"job": job(1)})
        assert started[1].wait(10)
        release[0].set()
        payloads = [first.result(timeout=10)]
        third = pool.submit(run_python_code, "job()\nprint('third totals')", {"job": job(2)})
        assert started[2].wait(10)
        release[1].set()
        payloads.append(second.result(timeout=10))
        release[2].set()
        payloads.append(third.result(timeout=10))
    print("host line")

    assert [payload["stdout"] for payload in payloads] == ["", "second totals\n", "third totals\n"], payloads
    assert sys.stdout is host and host.getvalue() == "host line\n"


@pytest.mark.parametrize("assigned", ["saved", "copy.copy(saved)"], ids=["the stdout an earlier run saved", "a copy of it"])
def test_program_that_assigns_an_unused_view_gives_back_what_its_run_found(monkeypatch, assigned):
    # One session over two calls, each inside a host redirect of its own, as of a
    # host that collects what it prints around each call.
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    first, second = io.StringIO(), io.StringIO()
    session = {}

    with redirect_stdout(first):
        assert run_python_code("import copy, io, sys\nsaved = sys.stdout\nsys.stdout = draft = io.StringIO()", session)["ok"]
    with redirect_stdout(second):
        payload = run_python_code(f"sys.stdout = {assigned}\nprint('totals')", session)
        print("host line")

    assert payload["ok"] and payload["stdout"] == "totals\n", payload
    assert first.getvalue() == "" and second.getvalue() == "host line\n"


def test_program_that_restores_its_stdout_assignment_leaves_the_host_stdout(monkeypatch):
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    code = "import io, sys\nsaved = sys.stdout\nsys.stdout = io.StringIO()\nprint('draft')\nsys.stdout = saved\nprint('final')"

    payload = run_python_code(code, {})
    print("report saved")

    assert payload["ok"] and payload["stdout"] == "final\n", payload
    assert sys.stdout is host and host.getvalue() == "report saved\n"


def test_host_redirect_that_ends_during_a_worker_run_finds_its_target_back_as_in_0122(monkeypatch):
    # The worker run found the redirect's target in stdout, and puts it back when it
    # ends, as 0.1.2.2's redirect did: later host output goes to that finished target.
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    report = io.StringIO()
    run_started, redirect_closed = Event(), Event()

    def job():
        run_started.set()
        assert redirect_closed.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with redirect_stdout(report):
            print("weekly report")
            task = pool.submit(run_python_code, "job()", {"job": job})
            assert run_started.wait(10)
        redirect_closed.set()
        payload = task.result(timeout=10)

    assert payload["ok"], payload
    print("report saved")
    assert report.getvalue() == "weekly report\nreport saved\n"
    assert sys.stdout is report and host.getvalue() == ""


def test_host_assignment_during_a_run_is_undone_at_its_end_as_in_0122(monkeypatch):
    # The run puts back what it found, as 0.1.2.2's redirect did, over both its
    # program's assignment and the host's later one.
    host, log = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    assigned, switched = Event(), Event()

    def job():
        sys.stdout = io.StringIO()
        assigned.set()
        assert switched.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert assigned.wait(10)
        sys.stdout = log
        switched.set()
        payload = task.result(timeout=10)

    assert payload["ok"], payload
    print("audit entry")
    assert sys.stdout is host and host.getvalue() == "audit entry\n"
    assert log.getvalue() == ""


def test_ending_run_undoes_a_redirect_of_a_run_on_another_thread_as_in_0122(monkeypatch):
    # The short run puts back what it found when it ends, as 0.1.2.2's redirect did,
    # so the report run's redirect receives nothing more; that run's later output
    # is in its own capture.
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    report = io.StringIO()
    short_started, redirected, short_done = Event(), Event(), Event()

    def short_job():
        short_started.set()
        assert redirected.wait(10)

    def prepare_report():
        assert short_started.wait(10)
        with redirect_stdout(report):
            print("weekly report")
            redirected.set()
            assert short_done.wait(10)
            print("totals")
        print("report saved")

    with ThreadPoolExecutor(max_workers=2) as pool:
        short_run = pool.submit(run_python_code, "short_job()", {"short_job": short_job})
        report_run = pool.submit(run_python_code, "prepare_report()", {"prepare_report": prepare_report})
        short = short_run.result(timeout=10)
        short_done.set()
        payload = report_run.result(timeout=10)

    assert short["ok"] and payload["ok"], (short, payload)
    assert report.getvalue() == "weekly report\n"
    assert payload["stdout"] == "totals\nreport saved\n"
    assert sys.stdout is host and host.getvalue() == ""


def test_host_stdout_comes_back_after_a_redirect_that_spans_the_last_run_end(monkeypatch):
    host, report = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        with redirect_stdout(report):
            release.set()
            assert task.result(timeout=10)["ok"]
    print("monthly report")
    second = run_python_code("print('second')", {})
    print("report saved")

    assert second["stdout"] == "second\n", second
    assert sys.stdout is host
    assert host.getvalue() == "monthly report\nreport saved\n" and report.getvalue() == ""


class _Log(io.TextIOBase):
    """A host stream that keeps what it is given in a list."""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)
        return len(text)


class _SlottedLog:
    """A host stream that takes no weak references."""

    __slots__ = ("lines",)

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)
        return len(text)

    def flush(self):
        pass


def _end_the_only_run_inside_a_host_redirect():
    """Enter a host redirect during a run and leave it after the run ended, which puts the run's view back."""
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        with redirect_stdout(io.StringIO()):
            release.set()
            assert task.result(timeout=10)["ok"]


@pytest.mark.parametrize("make_log", [_Log, _SlottedLog], ids=["only sys.stdout refers to it", "takes no weak references"])
def test_process_stdout_comes_back_for_a_stream_that_a_put_back_view_lost(monkeypatch, make_log):
    # 0.1.2.2 lost the host's output here too, in the ended run's buffer, but
    # never left host code with a stdout that cannot be written to.
    process_stdout = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", process_stdout)
    monkeypatch.setattr(sys, "stdout", make_log())

    _end_the_only_run_inside_a_host_redirect()
    sys.stdout.write("lost\n")
    second = run_python_code("print('second')", {})
    sys.stdout.write("host line\n")
    sys.stdout.flush()

    assert second["stdout"] == "second\n", second
    assert sys.stdout is process_stdout and process_stdout.getvalue() == "host line\n"


def _closed_stdout():
    stream = io.StringIO()
    stream.close()
    return stream


def _detached_stdout():
    stream = io.TextIOWrapper(io.BytesIO())
    stream.detach()
    return stream


@pytest.mark.parametrize("make_process_stdout", [lambda: None, _closed_stdout, _detached_stdout], ids=["no process stdout", "closed", "detached"])
def test_view_that_lost_its_stream_stays_where_the_process_stdout_takes_no_output(monkeypatch, make_process_stdout):
    monkeypatch.setattr(sys, "__stdout__", make_process_stdout())
    monkeypatch.setattr(sys, "stdout", _Log())

    _end_the_only_run_inside_a_host_redirect()
    payloads = [run_python_code(f"import sys\nsys.stdout.label = 'draft'\nprint('run', {number})", {}) for number in range(2)]
    labelled = hasattr(sys.stdout, "label")
    written = sys.stdout.write("host line\n")
    sys.stdout.flush()

    assert [payload["stdout"] for payload in payloads] == ["run 0\n", "run 1\n"], payloads
    assert not labelled  # what a program sets on sys.stdout ends with its run
    assert written == len("host line\n")


def test_run_captures_through_a_view_a_host_redirect_puts_back_while_it_runs(monkeypatch):
    host, report = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    first_started, release_first, second_started, release_second = Event(), Event(), Event(), Event()

    def first_job():
        first_started.set()
        assert release_first.wait(10)

    def second_job():
        second_started.set()
        assert release_second.wait(10)
        print("quarterly totals")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_python_code, "first_job()", {"first_job": first_job})
        assert first_started.wait(10)
        with redirect_stdout(report):
            release_first.set()
            assert first.result(timeout=10)["ok"]
            second = pool.submit(run_python_code, "second_job()", {"second_job": second_job})
            assert second_started.wait(10)
        release_second.set()
        payload = second.result(timeout=10)

    assert payload["ok"] and payload["stdout"] == "quarterly totals\n", payload
    assert host.getvalue() == "" and report.getvalue() == ""
    assert sys.stdout is host


def test_program_that_deletes_stdout_returns_its_payload_and_the_host_stdout_comes_back(monkeypatch):
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    start = threading.Thread.start

    payload = run_python_code("import sys\nprint('before')\ndel sys.stdout", {})

    assert payload["ok"], payload
    assert payload["stdout"] == "before\n"
    assert sys.stdout is host
    assert threading.Thread.start is start
    print("host line")
    assert host.getvalue() == "host line\n"


def test_program_that_deletes_stdout_leaves_another_active_run_capturing(monkeypatch):
    host = io.StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(10)
        print("second run line")

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        payload = run_python_code("import sys\nprint('first run line')\ndel sys.stdout", {})
        view_back = type(sys.stdout) is stdout_capture._ThreadStdout
        release.set()
        other = task.result(timeout=10)

    assert payload["ok"] and payload["stdout"] == "first run line\n", payload
    assert view_back
    assert other["ok"] and other["stdout"] == "second run line\n", other
    assert sys.stdout is host and host.getvalue() == ""


def test_run_started_while_stdout_is_missing_raises_and_changes_nothing(monkeypatch):
    run_python_code("total = 0", {})  # a finished run leaves no thread hooks behind
    start, start_new_thread = threading.Thread.start, _thread.start_new_thread
    monkeypatch.delattr(sys, "stdout")

    with pytest.raises(AttributeError):
        run_python_code("total = 1", {})

    assert threading.Thread.start is start and _thread.start_new_thread is start_new_thread
    assert not hasattr(sys, "stdout")
