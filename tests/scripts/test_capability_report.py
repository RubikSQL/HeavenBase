"""Generated capability-report freshness contracts."""

from __future__ import annotations

from collections import Counter
import copy
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPORT = runpy.run_path(str(ROOT / "scripts" / "gen_capabilities.py"))
CAPABILITIES = ROOT / "docs" / "resources" / "reports" / "capabilities.md"
pytestmark = pytest.mark.fast


def _table(lines: list[str], header: str) -> tuple[list[str], list[list[str]]]:
    """Return one Markdown table's column names and body cells."""
    start = next(index for index, line in enumerate(lines) if line.startswith(header))
    columns = [cell.strip() for cell in lines[start].strip().strip("|").split("|")]
    body = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        body.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return columns, body


def test_capability_report_check_is_non_mutating_and_write_is_explicit(tmp_path):
    """Check mode must reject drift without silently replacing evidence."""
    path = tmp_path / "capabilities.md"
    path.write_text("current\n", encoding="utf-8")

    REPORT["_write_report"]("current", path, check=True)
    with pytest.raises(SystemExit, match="generated document is out of date"):
        REPORT["_write_report"]("replacement", path, check=True)
    assert path.read_text(encoding="utf-8") == "current\n"

    REPORT["_write_report"]("replacement", path, check=False)
    assert path.read_text(encoding="utf-8") == "replacement\n"


def test_published_totals_partition_the_matrix_they_summarise():
    """Totals must classify every handler exactly as the matrix cell above them does.

    A handler on a logical type the backend declares unsupported is drawn 🚫 and can
    never execute, so counting it as Native or Fallback publishes a pushdown figure
    over a population the same document rules out.
    """
    lines = CAPABILITIES.read_text(encoding="utf-8").splitlines()
    backends, matrix = _table(lines, "| Op | Type | Storage |")
    columns, totals = _table(lines, "| Backend | Handlers |")
    counts: dict[str, Counter] = {backend: Counter() for backend in backends[3:]}
    for row in matrix:
        for backend, cell in zip(backends[3:], row[3:]):
            counts[backend][cell] += 1

    assert columns == ["Backend", "Handlers", "Native", "Fallback", "n/a"]
    assert [row[0] for row in totals] == backends[3:]
    assert any(counts[backend]["🚫"] for backend in counts), "no backend blocks a storage type; this contract proves nothing"
    for backend, handlers, native, fallback, blocked in totals:
        published = (int(native), int(fallback), int(blocked))
        counted = (counts[backend]["✅"], counts[backend]["🔵"], counts[backend]["🚫"])
        assert published == counted, f"{backend} totals disagree with its matrix column"
        assert int(handlers) == sum(published), f"{backend} totals do not partition its handlers"


def test_capability_probe_fixtures_are_descriptor_owned_and_fail_closed(tmp_path):
    """Capability evidence must never invent a logical type or operand."""
    context = REPORT["_isolated_context"](str(tmp_path))
    try:
        resolver = context.modules()
        type_rows = {row["identifier"]: row for row in resolver.inspect("logical_type")}
        operation_rows = {row["identifier"]: row for row in resolver.inspect("operation")}
        capabilities = REPORT["seed_handlers"](resolver=resolver).capability_rows()
        handler_types = {row["dtype"] for row in capabilities}
        handler_ops = {row["op"] for row in capabilities}

        assert all("capability_probe" in type_rows[dtype]["record"]["meta"] for dtype in handler_types)
        assert all("capability_probe" in operation_rows[op]["record"]["meta"] for op in handler_ops)
        assert REPORT["_probe_dtype"](type_rows["categorical"], "eq", resolver).base == "string"
        assert REPORT["_probe_dtype"](type_rows["categorical"], "lt", resolver).base == "integer"
        assert REPORT["_probe_value"](type_rows["categorical"], operation_rows["eq"], "eq") == "todo"

        missing = copy.deepcopy(type_rows["short-text"])
        missing["record"]["meta"].pop("capability_probe")
        with pytest.raises(ValueError, match="must declare record.meta.capability_probe"):
            REPORT["_probe_dtype"](missing, "eq", resolver)

        mismatched = copy.deepcopy(type_rows["integer"])
        mismatched["record"]["meta"]["capability_probe"]["descriptor"] = "short-text"
        with pytest.raises(ValueError, match="capability probe built 'short-text'"):
            REPORT["_probe_dtype"](mismatched, "eq", resolver)

        missing_value = copy.deepcopy(operation_rows["eq"])
        missing_value["record"]["meta"]["capability_probe"] = {}
        with pytest.raises(ValueError, match="must declare value"):
            REPORT["_probe_value"](type_rows["categorical"], missing_value, "eq")
    finally:
        context.close()
