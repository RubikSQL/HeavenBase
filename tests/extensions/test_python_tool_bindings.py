"""Regression coverage for the Python guide and its paired runner."""

import pytest

import heavenbase as hb
from heavenbase.extensions.python.toolkit import python_tool_functions

pytestmark = pytest.mark.fast


@pytest.mark.slow
def test_python_guide_describes_the_tools_initial_entity_bindings():
    ws = hb.HeavenBase("python-bindings-snapshot", backends={"main": {"type": "inmem"}})
    tools = python_tool_functions(ws)
    ws.register({"entity_id": "late-paper", "name": "LatePaper", "fields": {"title": {"type": "ShortText"}}})

    guide = tools["python_guide"]()
    names = guide.split("## Bound in this workspace\n\n", 1)[1].splitlines()[0].split(", ")
    assert "LatePaper" not in names
    assert 'ws.entities["late-paper"]' in guide
    assert tools["run_python"]('ws.count(ws.entities["late-paper"])')["result"] == 0
    assert tools["run_python"]("LatePaper")["error"].startswith("NameError:")

    refreshed = python_tool_functions(ws)
    assert refreshed["run_python"]("ws.count(LatePaper)")["result"] == 0
    names = refreshed["python_guide"]().split("## Bound in this workspace\n\n", 1)[1].splitlines()[0].split(", ")
    assert "LatePaper" in names
