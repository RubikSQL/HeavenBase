"""Regression coverage for capturing stdout during Python tool runs."""

import io
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

import pytest

import heavenbase as hb
from heavenbase.extensions.python import run_python_code
from heavenbase.toolkit.families import registered_workspace_toolkit_families

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("registry_backed", [False, True])
def test_python_runs_isolate_stdout(monkeypatch, registry_backed):
    original = sys.stdout
    if registry_backed:
        workspace = hb.HeavenBase("python-stdout-registry", backends={"main": {"type": "inmem"}})
        try:
            family = registered_workspace_toolkit_families(resolver=workspace.module_resolver)["python"]
            assert family.builder.__module__.startswith("_heavenbase_artifact_")
            run_python = family.builder(workspace)["run_python"]

            def run_second(program):
                workspace.stdout_test_program = program
                return run_python("ws.stdout_test_program()")

            _assert_concurrent_stdout(monkeypatch, run_second)
        finally:
            sys.stdout = original
            workspace.release()
    else:
        _assert_concurrent_stdout(monkeypatch, lambda program: run_python_code("program()", {"program": program}))


def _assert_concurrent_stdout(monkeypatch, run_second):
    original = io.StringIO()
    monkeypatch.setattr(sys, "stdout", original)
    first_started = Event()
    second_started = Event()
    release_second = Event()

    def first():
        print("first starts")
        first_started.set()
        assert second_started.wait(10)
        print("first ends")

    def second():
        print("second starts")
        second_started.set()
        assert release_second.wait(10)
        print("second ends")
        raise ValueError("second failed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_run = pool.submit(run_python_code, "program()", {"program": first})
        try:
            assert first_started.wait(10)
            second_run = pool.submit(run_second, second)
            assert second_started.wait(10)
            first_result = first_run.result(timeout=10)
            print("outside both runs")
        finally:
            release_second.set()
        second_result = second_run.result(timeout=10)

    assert sys.stdout is original
    assert first_result["ok"] and first_result["stdout"] == "first starts\nfirst ends\n"
    assert second_result["error"] == "ValueError: second failed"
    assert second_result["stdout"] == "second starts\nsecond ends\n"
    assert original.getvalue() == "outside both runs\n"


def test_nested_python_runs_restore_the_outer_capture():
    original = sys.stdout

    def outer():
        print("outer start")
        result = run_python_code("import sys\nsys.stdout.write('inner')", {})
        print("outer end")
        return result["stdout"]

    result = run_python_code("outer()", {"outer": outer})

    assert sys.stdout is original
    assert result["stdout"] == "outer start\nouter end\n"
    assert result["result"] == "inner"


def test_threads_of_one_run_writing_at_once_are_all_counted():
    def program():
        def write_lines():
            for _ in range(2000):
                sys.stdout.write("0123456789\n")

        threads = [Thread(target=write_lines) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    payload = run_python_code("program()", {"program": program}, result_chars=1000)

    assert payload["ok"], payload
    assert payload["stdout"] == ("0123456789\n" * 91)[:1000]
    assert payload["stdout_truncated"] is True and payload["stdout_chars"] == 8 * 2000 * 11
