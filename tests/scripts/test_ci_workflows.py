"""Hosted CI workflow contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
MATRIX_PYTHON = "${{ matrix.python-version }}"
pytestmark = pytest.mark.fast


def _python_matrix_jobs() -> list[tuple[str, dict]]:
    """Return every workflow job that fans out over ``matrix.python-version``."""
    if not WORKFLOWS.is_dir():
        pytest.skip("hosted CI workflows ship only in the source repository")
    jobs = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).items():
            if "python-version" in ((job.get("strategy") or {}).get("matrix") or {}):
                jobs.append((f"{path.name}:{name}", job))
    return jobs


def test_python_matrix_jobs_run_their_matrix_interpreter():
    """The repository pins `.python-version`, so a matrix leg tests its Python only when uv is told to use it."""
    jobs = _python_matrix_jobs()

    assert jobs
    for name, job in jobs:
        assert (job.get("env") or {}).get("UV_PYTHON") == MATRIX_PYTHON, f"{name} must set UV_PYTHON to the matrix Python"
        checks = [step for step in job["steps"] if (step.get("env") or {}).get("EXPECTED_PYTHON") == MATRIX_PYTHON]
        assert checks and "sys.version_info" in checks[0].get("run", ""), f"{name} must verify the interpreter uv selected"
