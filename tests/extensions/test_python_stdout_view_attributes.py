"""What a run's program sets on `sys.stdout` ends with its run, as it ended with 0.1.2.2's run buffer.

A capture view lives for the whole process and is reused by later runs, so
anything set on the view itself is cleared when the view retires and again
before it is reused.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import gc
from io import StringIO
import sys
from threading import Event
import weakref

import pytest

from heavenbase.extensions.python import run_python_code

pytestmark = pytest.mark.fast


def test_a_patched_write_ends_with_its_run(monkeypatch):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)

    first = run_python_code("import sys\nsys.stdout.write = lambda text: len(text)\nprint('silenced line')", {})
    second = run_python_code("print('second run line')", {})
    print("host line")

    assert first["ok"] and first["stdout"] == "", first
    assert second["stdout"] == "second run line\n", second
    assert host.getvalue() == "host line\n"


def test_a_tee_on_write_sees_only_its_own_run(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    log = []
    code = "import sys\noriginal = sys.stdout.write\ndef tee(text):\n    log.append(text)\n    return original(text)\nsys.stdout.write = tee\nprint('first run line')\n"

    first = run_python_code(code, {"log": log})
    second = run_python_code("print('second run line')", {})

    assert first["stdout"] == "first run line\n", first
    assert second["stdout"] == "second run line\n", second
    assert log == ["first run line", "\n"]


def test_an_attribute_set_on_stdout_in_a_run_is_gone_in_the_next_run(monkeypatch):
    # It is found and listed in its own run, though the capture has no such attribute.
    monkeypatch.setattr(sys, "stdout", StringIO())
    seen = {}
    code = "import sys\nsys.stdout.report_title = 'weekly totals'\nseen['same run'] = (sys.stdout.report_title, 'report_title' in dir(sys.stdout))"

    first = run_python_code(code, {"seen": seen})
    run_python_code("import sys\nseen['next run'] = (hasattr(sys.stdout, 'report_title'), 'report_title' in dir(sys.stdout))", {"seen": seen})

    assert first["ok"], first
    assert seen == {"same run": ("weekly totals", True), "next run": (False, False)}


def test_an_attribute_set_on_stdout_on_a_host_thread_is_found_there_and_gone_in_the_next_run(monkeypatch):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    started, release = Event(), Event()
    seen = {}

    def job():
        started.set()
        assert release.wait(10)

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        try:
            sys.stdout.report_title = "weekly totals"
            seen["host thread"] = (sys.stdout.report_title, "report_title" in dir(sys.stdout))
        finally:
            release.set()
        assert task.result(timeout=10)["ok"]
    run_python_code("import sys\nseen['next run'] = hasattr(sys.stdout, 'report_title')", {"seen": seen})

    assert seen == {"host thread": ("weekly totals", True), "next run": False}
    assert sys.stdout is host and not hasattr(host, "report_title")


def test_an_attribute_set_on_a_kept_view_after_its_run_is_gone_in_the_next_run(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    kept, seen = {}, {}
    run_python_code("import sys\nkept['stdout'] = sys.stdout", {"kept": kept})
    kept["stdout"].report_title = "late title"
    del kept["stdout"]

    run_python_code("import sys\nseen['next run'] = hasattr(sys.stdout, 'report_title')", {"seen": seen})

    assert seen == {"next run": False}


def test_an_attribute_set_on_a_view_put_back_after_its_run_is_gone_in_the_next_run(monkeypatch):
    # A host redirect that spans the end of a run puts that run's view back in
    # `sys.stdout`; what is set on it then is cleared when a run takes it up.
    host, report = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    started, release = Event(), Event()
    seen = {}

    def job():
        started.set()
        assert release.wait(10)

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        with redirect_stdout(report):
            release.set()
            assert task.result(timeout=10)["ok"]
    sys.stdout.report_title = "late title"

    run_python_code("import sys\nseen['next run'] = hasattr(sys.stdout, 'report_title')", {"seen": seen})

    assert seen == {"next run": False}
    assert sys.stdout is host


def test_a_patched_write_reaches_neither_a_later_run_nor_its_host_threads(monkeypatch):
    host = StringIO()
    monkeypatch.setattr(sys, "stdout", host)
    assert run_python_code("import sys\nsys.stdout.write = lambda text: len(text)", {})["ok"]
    started, release = Event(), Event()

    def job():
        started.set()
        assert release.wait(10)
        print("later run line")

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run_python_code, "job()", {"job": job})
        assert started.wait(10)
        print("host line during the later run")
        release.set()
        later = task.result(timeout=10)
    print("host line after the later run")

    assert later["stdout"] == "later run line\n", later
    assert host.getvalue() == "host line during the later run\nhost line after the later run\n"


def test_a_patched_write_does_not_reach_a_later_run_over_another_host_stream(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    run_python_code("import sys\nsys.stdout.write = lambda text: len(text)", {})
    monkeypatch.setattr(sys, "stdout", StringIO())

    later = run_python_code("print('report for another host stream')", {})

    assert later["stdout"] == "report for another host stream\n", later


class _Marker:
    pass


def test_a_runs_namespace_is_released_after_its_program_patched_stdout(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    namespace = {"marker": _Marker()}
    released = weakref.ref(namespace["marker"])
    code = "import sys\noriginal = sys.stdout.write\ndef tee(text):\n    return original(text)\nsys.stdout.write = tee\nprint('patched')\n"

    payload = run_python_code(code, namespace)
    del namespace
    gc.collect()

    assert payload["ok"] and payload["stdout"] == "patched\n", payload
    assert released() is None
