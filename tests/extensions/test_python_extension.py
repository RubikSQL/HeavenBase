"""In-process Python execution: result contract, namespace, and guide fidelity."""

import re

import pytest

import heavenbase as hb
from heavenbase.extensions.python import python_namespace, run_python_code
from heavenbase.extensions.python.guide import render_python_guide
from heavenbase.extensions.python.toolkit import python_tool_functions


class Paper(hb.Entity):
    identifier = "python-paper"

    venue = hb.field(hb.ShortText)
    year = hb.field(hb.Integer)
    score = hb.field(hb.Float)
    meta = hb.field(hb.Json).store(optional=True)


class Doc(hb.Entity):
    identifier = "python-doc"

    meta = hb.field(hb.Json).store(optional=True)


class Trace(hb.Entity):
    identifier = "python-trace"

    name = hb.field(hb.ShortText)
    tool_calls = hb.field(hb.Json).store(optional=True)


class Session(hb.Entity):
    identifier = "python-session"

    title = hb.field(hb.ShortText)


def _workspace(workspace_id: str) -> hb.HeavenBase:
    ws = hb.HeavenBase(workspace_id, backends={"main": {"type": "inmem"}})
    ws.register(Paper)
    ws.register(Doc)
    ws.register(Trace)
    ws.upsert_many(
        Paper,
        [{"object_id": f"p{i}", "venue": "NeurIPS" if i % 2 else "ICML", "year": 2020 + (i % 4), "score": float(i), "meta": {"idx": i}} for i in range(8)],
    )
    ws.upsert_many(Doc, [{"object_id": "d1", "meta": {"review": {"score": 9}}}, {"object_id": "d2", "meta": {"review": {"score": 7}}}])
    ws.upsert_many(
        Trace,
        [
            {"object_id": "t1", "name": "a", "tool_calls": {"run_sql": 9, "get_col_info": 7}},
            {"object_id": "t2", "name": "b", "tool_calls": {"run_sql": 5}},
        ],
    )
    return ws


def test_run_python_evaluates_statements_and_a_trailing_expression():
    """A program behaves like a notebook cell: statements run, the last expression is the result."""
    ws = _workspace("python-basic")
    namespace = python_namespace(ws)

    assert run_python_code("1 + 1", namespace)["result"] == 2

    multi = run_python_code("x = 5\nprint('hi')\nx * 2", namespace)
    assert multi["ok"] is True and multi["stdout"] == "hi\n" and multi["result"] == 10
    assert run_python_code("x + 1", namespace)["result"] == 6  # names persist between calls

    assert run_python_code("y = 3", namespace)["result"] is None  # a program need not end in an expression


def test_run_python_reports_a_failure_as_data():
    """A program's own error is the caller's answer, not an exception thrown at them."""
    ws = _workspace("python-error")
    namespace = python_namespace(ws)

    failed = run_python_code("print('before')\n1 / 0", namespace)
    assert failed["ok"] is False and failed["error"] == "ZeroDivisionError: division by zero"
    assert "ZeroDivisionError" in failed["traceback"] and failed["stdout"] == "before\n"  # output up to the failure survives

    with pytest.raises(SyntaxError):
        run_python_code("def (", namespace)  # unparseable code is the caller's bug, raised at them


def test_run_python_results_carry_their_own_coverage():
    """A frame result keeps `total`/`truncated`, so a page cannot read as a whole."""
    ws = _workspace("python-coverage")
    namespace = python_namespace(ws)

    whole = run_python_code("ws.query(Paper).execute()", namespace)["result"]
    assert whole["n"] == 8 and "truncated" not in whole  # nothing withheld, nothing to declare

    page = run_python_code("ws.query(Paper).limit(3).execute()", namespace)["result"]
    assert page["n"] == 3 and page["truncated"] is True and page["total"] == 8

    capped = run_python_code("'x' * 50000", namespace)
    assert capped["result_truncated"] is True and capped["result_chars"] == 50000 and len(capped["result"]) == 20000

    printed = run_python_code("print('y' * 50000)", namespace, result_chars=100)
    assert len(printed["stdout"]) == 100
    assert printed["stdout_truncated"] is True and printed["stdout_chars"] == 50001

    with pytest.raises(ValueError, match="positive"):
        run_python_code("1", namespace, result_chars=0)


def test_python_namespace_binds_the_workspace_and_its_entities():
    """A program starts from `ws` and entity classes rather than resolving names itself."""
    ws = _workspace("python-namespace")
    namespace = python_namespace(ws)

    assert namespace["ws"] is ws and namespace["hb"] is hb
    assert namespace["Paper"] is ws.entities["python-paper"]
    assert run_python_code("ws.count(Paper)", namespace)["result"] == 8


def test_python_namespace_prefers_a_callers_entity_over_a_packaged_one_with_the_same_name():
    """A user ``Session`` binds as ``Session`` even though the agent extension ships its own ``Session``."""
    ws = hb.HeavenBase("python-name-clash", backends={"main": {"type": "inmem"}})
    ws.register(Session)
    assert "agent-session" in ws.entities  # the default agent extension's Session is present as well
    namespace = python_namespace(ws)
    assert namespace["Session"] is ws.entities["python-session"]
    ws.upsert(Session, {"object_id": "s1", "title": "mine"})
    assert run_python_code("ws.count(Session)", namespace)["result"] == 1


def test_python_namespace_prefers_a_schema_defined_callers_entity_over_a_packaged_one():
    """A ``Session`` registered from a schema mapping is the caller's, although it compiles into ``heavenbase.dynamic``."""
    ws = hb.HeavenBase("python-schema-clash", backends={"main": {"type": "inmem"}})
    entity = ws.register({"entity_id": "python-schema-session", "name": "Session", "fields": {"title": {"type": "ShortText"}}})
    assert entity.__module__ == "heavenbase.dynamic"
    assert "agent-session" in ws.entities
    namespace = python_namespace(ws)
    assert namespace["Session"] is ws.entities["python-schema-session"]
    assert namespace["Message"] is ws.entities["agent-message"]


def test_python_guide_lists_exactly_the_names_a_program_starts_with():
    """A class name two entities share is listed once, and the shadowed entity is named by its lookup."""
    ws = hb.HeavenBase("python-guide-clash", backends={"main": {"type": "inmem"}})
    ws.register(Session)
    guide = python_tool_functions(ws)["python_guide"]()
    listed = guide.split("## Bound in this workspace\n\n", 1)[1].splitlines()[0].split(", ")
    namespace = python_namespace(ws)
    assert listed == sorted(name for name in namespace if name not in {"hb", "ws", "__name__"})  # the guide names the real bindings
    assert listed.count("Session") == 1
    assert 'Other entities available by lookup\n\n`ws.entities["agent-session"]`' in guide
    assert "Other entities available by lookup" not in render_python_guide(_workspace("python-guide-plain"))  # no clash, no note


def test_python_tools_expose_run_python_and_its_guide():
    """The toolkit publishes both tools, and the guide names this workspace's entities."""
    ws = _workspace("python-tools")
    tools = python_tool_functions(ws)
    assert sorted(tools) == ["python_guide", "run_python"]

    guide = tools["python_guide"]()
    assert "Paper" in guide and "run_python" in guide

    out = tools["run_python"]("ws.count(Paper)")
    assert out["ok"] is True and out["result"] == 8


def test_guide_examples_execute_against_a_live_workspace():
    """Every fenced example in the guide runs, so the text cannot drift from the API."""
    ws = _workspace("python-guide")
    namespace = python_namespace(ws)
    blocks = re.findall(r"```python\n(.*?)```", render_python_guide(), flags=re.DOTALL)
    assert len(blocks) >= 6  # the guide is expected to teach, not to shrink to nothing silently

    for index, block in enumerate(blocks):
        outcome = run_python_code(block, namespace)
        assert outcome["ok"] is True, f"guide block {index} failed: {outcome.get('error')}\n{block}"
