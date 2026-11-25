"""What leaving a `with sys.stdout:` block and a close that a run left behind close.

Leaving a `with sys.stdout:` block, or an ExitStack that entered it, calls the
close a run's program set on `sys.stdout`, as io.IOBase's `__exit__` calls
`self.close()` and as it did on 0.1.2.2's run buffer. A close that a finished
run left behind, such as `contextlib.closing(sys.stdout)` in a suspended
generator, closes nothing when a later run's thread performs it, as 0.1.2.2
closed only that finished run's own buffer then.
"""

from io import StringIO
import sys

import pytest

from heavenbase.extensions.python import run_python_code

pytestmark = pytest.mark.fast


@pytest.mark.slow
def test_leaving_with_stdout_calls_the_close_the_program_set(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    seen = {}
    code = (
        "import sys\n"
        "calls = []\n"
        "sys.stdout.close = lambda: calls.append('close')\n"
        "with sys.stdout:\n"
        "    print('monthly total', 42)\n"
        "seen['calls'] = list(calls)\n"
        "seen['closed'] = sys.stdout.closed\n"
        "print('after the block', flush=True)\n"
    )

    payload = run_python_code(code, {"seen": seen})

    assert payload["ok"], payload
    assert seen == {"calls": ["close"], "closed": False}
    assert payload["stdout"] == "monthly total 42\nafter the block\n"


def test_leaving_an_exit_stack_calls_the_close_the_program_set(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    seen = {}
    code = (
        "import sys\n"
        "from contextlib import ExitStack\n"
        "calls = []\n"
        "sys.stdout.close = lambda: calls.append('close')\n"
        "with ExitStack() as stack:\n"
        "    stack.enter_context(sys.stdout)\n"
        "    print('monthly total', 42)\n"
        "seen['calls'] = list(calls)\n"
        "seen['closed'] = sys.stdout.closed\n"
        "print('after the block', flush=True)\n"
    )

    payload = run_python_code(code, {"seen": seen})

    assert payload["ok"], payload
    assert seen == {"calls": ["close"], "closed": False}
    assert payload["stdout"] == "monthly total 42\nafter the block\n"


def test_a_no_op_close_keeps_stdout_open_through_a_report_helper_that_closes_its_output(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    code = (
        "import sys\n"
        "def report(out):\n"
        "    with out:\n"
        "        print('region,total', file=out)\n"
        "        print('north,120', file=out)\n"
        "sys.stdout.close = lambda: None\n"
        "report(sys.stdout)\n"
        "report(sys.stdout)\n"
        "print('done', flush=True)\n"
    )

    payload = run_python_code(code, {})

    assert payload["ok"], payload
    assert payload["stdout"] == "region,total\nnorth,120\nregion,total\nnorth,120\ndone\n"


LATER_RUN = "import sys\npending.close()\nseen['closed'] = sys.stdout.closed\nprint('second run line', flush=True)\n"


def _close_in_a_later_run(first_code):
    """Run `first_code`, which leaves a suspended generator `pending`; then close it in a later run and return both payloads and what that run saw."""
    kept, seen = {}, {}
    first = run_python_code(first_code, kept)
    second = run_python_code(LATER_RUN, {"pending": kept.pop("pending"), "seen": seen})
    return first, second, seen


def test_a_closing_left_by_a_finished_run_leaves_a_later_runs_capture_open(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    first_code = (
        "import sys\n"
        "from contextlib import closing\n"
        "def rows():\n"
        "    with closing(sys.stdout):\n"
        "        yield 'north'\n"
        "        yield 'south'\n"
        "pending = rows()\n"
        "print(next(pending))\n"
    )

    first, second, seen = _close_in_a_later_run(first_code)

    assert first["ok"] and first["stdout"] == "north\n", first
    assert second["ok"] and second["stdout"] == "second run line\n", second
    assert seen == {"closed": False}


def test_a_finally_close_left_by_a_finished_run_leaves_a_later_runs_capture_open(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    first_code = (
        "import sys\n"
        "def rows(out=None):\n"
        "    out = out or sys.stdout\n"
        "    try:\n"
        "        yield 'north'\n"
        "        yield 'south'\n"
        "    finally:\n"
        "        out.close()\n"
        "pending = rows()\n"
        "print(next(pending))\n"
    )

    first, second, seen = _close_in_a_later_run(first_code)

    assert first["ok"] and first["stdout"] == "north\n", first
    assert second["ok"] and second["stdout"] == "second run line\n", second
    assert seen == {"closed": False}
