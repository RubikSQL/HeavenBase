"""Regressions for the Python runner's stdout and result contract."""

import _thread
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import redirect_stdout
import gc
from io import StringIO
import json
import sys
from threading import Event, Thread
import weakref

import pytest

from heavenbase.execution.frame.result import ResultFrame
from heavenbase.extensions.python import run_python_code

pytestmark = pytest.mark.fast


def test_nested_python_run_captures_through_outer_redirection():
    original = sys.stdout
    buffer = StringIO()

    def outer():
        print("outer start")
        with redirect_stdout(buffer):
            print("redirected start")
            nested = run_python_code("print(56)", {})
            assert sys.stdout is buffer
            print("redirected end")
        print("outer end")
        return nested["stdout"]

    result = run_python_code("outer()", {"outer": outer})

    assert result["ok"] is True
    assert result["result"] == "56\n"
    assert result["stdout"] == "outer start\nouter end\n"
    assert buffer.getvalue() == "redirected start\nredirected end\n"
    assert sys.stdout is original


def test_python_run_captures_its_joined_worker_threads(capsys):
    def program():
        with ThreadPoolExecutor(1) as pool:
            pool.submit(print, "worker").result(timeout=10)
        print("runner")

    result = run_python_code("program()", {"program": program})

    assert result["ok"] is True
    assert result["stdout"] == "worker\nrunner\n"
    assert capsys.readouterr().out == ""


def test_background_run_does_not_restore_a_finished_redirect():
    original = sys.stdout
    report = StringIO()
    report_open, background_started, report_closed = Event(), Event(), Event()

    def background():
        background_started.set()
        assert report_closed.wait(10)
        print("calculation finished")

    def run_background():
        assert report_open.wait(10)
        return run_python_code("background()", {"background": background})

    def prepare_report():
        with redirect_stdout(report):
            print("weekly report")
            report_open.set()
            assert background_started.wait(10)
            print("totals")
        report_closed.set()
        payload = task.result(timeout=10)
        print("report saved")
        return payload

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_background)
        payload = run_python_code("prepare_report()", {"prepare_report": prepare_report})

    assert sys.stdout is original
    assert payload["ok"], payload
    assert payload["result"]["stdout"] == "calculation finished\n"
    assert payload["stdout"] == "report saved\n"
    assert report.getvalue() == "weekly report\ntotals\n"


def test_python_run_captures_low_level_worker(capsys):
    printed = Event()

    def print_totals(label, *, count):
        print(f"{label}: {count}")
        printed.set()

    def calculate():
        _thread.start_new_thread(print_totals, ("Invoices",), {"count": 12})
        assert printed.wait(10)

    payload = run_python_code("calculate()", {"calculate": calculate})

    assert payload["ok"], payload
    assert payload["stdout"] == "Invoices: 12\n"
    assert capsys.readouterr().out == ""


def test_late_write_racing_the_run_end_reaches_the_run_or_the_host(monkeypatch):
    # A thread the run started, and did not join, is inside its capture's write when the run ends.
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    paused, resume = Event(), Event()
    holder = []

    def tracer(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "write" and type(frame.f_locals.get("self")).__name__ == "_BoundedText":
            paused.set()
            resume.wait(10)
            sys.settrace(None)
            return None
        return tracer

    def late_writer():
        sys.settrace(tracer)
        sys.stdout.write("late line\n")
        sys.settrace(None)

    def program():
        worker = Thread(target=late_writer)
        worker.start()
        holder.append(worker)
        assert paused.wait(10)

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "program()", {"program": program})
        assert paused.wait(10)
        try:
            task.result(timeout=2)
            run_ended_first = True
        except FutureTimeout:
            run_ended_first = False
        resume.set()
        payload = task.result(timeout=10)
        holder[0].join(10)

    where = [name for name, text in (("run", payload["stdout"]), ("host", host.getvalue())) if "late line" in text]
    assert where in (["run"], ["host"]), f"late write lost or doubled: run_ended_first={run_ended_first} payload={payload!r} host={host.getvalue()!r}"


def test_finished_threads_of_a_running_program_are_released():
    def program():
        finished = []
        for _ in range(1000):
            worker = Thread(target=print, args=("item",))
            worker.start()
            worker.join()
            finished.append(weakref.ref(worker))
        del worker
        gc.collect()
        return sum(thread() is not None for thread in finished)

    payload = run_python_code("program()", {"program": program})

    assert payload["ok"], payload
    assert payload["result"] == 0
    assert payload["stdout"] == "item\n" * 1000


def test_copies_of_stdout_during_a_run_route_like_stdout(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    code = "import copy, sys\ncopy.copy(sys.stdout).write('copied\\n')\ncopy.deepcopy(sys.stdout).write('deep copied\\n')\nprint('report saved')"

    payload = run_python_code(code, {})

    assert payload["ok"], payload
    assert payload["stdout"] == "copied\ndeep copied\nreport saved\n"


def test_threads_outside_runs_see_a_null_stream_while_stdout_is_none(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(10)
        print("monthly totals")

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        stream = sys.stdout
        seen = (stream is None or stream.isatty(), stream is None or stream.write("dropped line\n"))
        print("dropped too")
        release.set()
        payload = task.result(timeout=10)

    assert seen == (False, 13)
    assert payload["ok"] and payload["stdout"] == "monthly totals\n", payload
    assert sys.stdout is None


def _during_a_run(action, line="monthly totals"):
    """Run `action` on this thread while a run on another thread is active; return its result and the run's payload."""
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(10)
        print(line)

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        try:
            seen = action()
        finally:
            release.set()
        payload = task.result(timeout=10)
    return seen, payload


def test_a_host_close_of_the_null_stream_does_not_outlive_its_run(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    _during_a_run(lambda: sys.stdout.close())

    def later_checks():
        stream = sys.stdout
        print("a later host line", flush=True)
        return stream.isatty(), stream.flush()

    seen, payload = _during_a_run(later_checks, "later run line")

    assert seen == (False, None)
    assert payload["stdout"] == "later run line\n"
    assert sys.stdout is None


def test_copies_of_stdout_during_a_run_with_a_file_as_host_stdout(tmp_path, monkeypatch):
    host = open(tmp_path / "host.txt", "w")
    monkeypatch.setattr(sys, "stdout", host)
    code = "import copy, sys\ncopy.copy(sys.stdout).write('copied\\n')\ncopy.deepcopy(sys.stdout).write('deep copied\\n')\nprint('report saved')"

    payload = run_python_code(code, {})
    monkeypatch.undo()
    host.close()

    assert payload["ok"], payload
    assert payload["stdout"] == "copied\ndeep copied\nreport saved\n"


def test_a_deep_copy_of_stdout_sends_host_writes_to_the_host_stream(monkeypatch):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    copies = {}

    def program():
        import copy

        copies["deep"] = copy.deepcopy(sys.stdout)
        copies["shallow"] = copy.copy(sys.stdout)

    assert run_python_code("program()", {"program": program})["ok"]
    copies["shallow"].write("shallow copy line\n")
    copies["deep"].write("deep copy line\n")

    assert host.getvalue() == "shallow copy line\ndeep copy line\n"


def test_host_writes_are_dropped_during_a_run_that_started_while_stdout_was_none(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)

    def host():
        print("host line one")
        sys.stdout.write("host line two\n")
        return sys.stdout.isatty()

    seen, payload = _during_a_run(host)

    assert seen is False
    assert payload["stdout"] == "monthly totals\n"
    assert sys.stdout is None


def test_cyclic_python_result_has_a_json_serializable_payload():
    value = {}
    value["child"] = {"parent": value}

    result = run_python_code("value", {"value": value})

    assert result["ok"] is True
    assert result["result"] == repr(value)
    json.dumps(result)


def test_cyclic_frame_cell_uses_text_and_keeps_frame_metadata():
    budget = {"department": "Finance"}
    budget["reports"] = [{"quarter": "Q2", "budget": budget}]
    frame = ResultFrame.from_table([{"budget": budget, "year": 2026}])

    payload = run_python_code("frame", {"frame": frame})

    assert payload["ok"], payload
    assert payload["result"]["rows"] == [{"budget": repr(budget), "year": 2026}]
    assert payload["result"]["columns"]["budget"] == [repr(budget)]
    assert payload["result"]["n"] == 1
    json.dumps(payload, allow_nan=False)
