"""Documentation task/plan lifecycle contract tests."""

from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("heavenbase_docs_contract", ROOT / "scripts" / "docs.py")
assert SPEC is not None and SPEC.loader is not None
DOCS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DOCS
SPEC.loader.exec_module(DOCS)


def _milestone(
    identifier: str,
    title: str,
    *,
    state: str,
    depends_on: str,
    checked: bool,
    evidence: str,
    review: str | None = None,
    blocker: str = "",
    unblock_when: str = "",
) -> str:
    blocked = f"- Blocker: {blocker}\n- Unblock when: {unblock_when}\n" if state == "Blocked" else ""
    mark = "x" if checked else " "
    review = review or ("Pass." if state == "Accepted" else "Pending.")
    return f"""## {identifier} — {title}

- State: {state}
- Depends on: {depends_on}
- Owner: Codex
- Updated: 2026-07-22
- Review: {review}
- Documentation: Keep the durable contract synchronized.
- Rollback: Revert this milestone as one unit.
{blocked}
### Acceptance

- [{mark}] Observable contract passes.

### Verification

- `rtk bash scripts/test.bash tests/docs/test_docs_contract.py -q`

### Evidence

- {evidence}

"""


def _write_repo(
    root: Path,
    *,
    task_status: str = "active",
    plan_status: str = "In progress",
    first_state: str = "Accepted",
    first_checked: bool = True,
    first_evidence: str = "[contract](../../README.en.md)",
    first_review: str | None = None,
    second_state: str = "In progress",
    second_depends: str = "M0",
    linked: bool = True,
    extra: str = "",
    task_blocker: str = "",
    task_unblock: str = "",
    plan_blocker: str = "",
    plan_unblock: str = "",
) -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "README.en.md").write_text("# User\n", encoding="utf-8")
    (root / "docs" / "README.md").write_text("# Engineering\n", encoding="utf-8")
    (root / "docs" / "DEVLOG.md").write_text(
        "# Development Log\n\n## 2026-07-22 — Fixture\n\n- Task: HB-001\n- Changed: Fixture.\n- Verified: Fixture.\n- Next: HB-001\n",
        encoding="utf-8",
    )
    task = {
        "id": "HB-001",
        "title": "Fixture task",
        "status": task_status,
        "priority": "P1",
        "owner": "Codex" if task_status != "ready" else None,
        "parent": None,
        "updated": "2026-07-22",
        "target": "Validate milestone state.",
        "gap": "Fixture gap.",
        "acceptance": ["Milestone contract passes."],
        "depends_on": [],
        "links": ["docs/plans/PLAN.md"] if linked else [],
    }
    if task_status == "blocked":
        task["blocker"] = task_blocker
        task["unblock_when"] = task_unblock
    queue = {"schema": "heaven.tasks/v1", "project": "Fixture", "updated": "2026-07-22", "tasks": [task]}
    (root / "docs" / "tasks.yaml").write_text(yaml.safe_dump(queue, sort_keys=False), encoding="utf-8")
    plan = f"""# Fixture plan

- Task: `HB-001`
- Status: {plan_status}
- Updated: 2026-07-22

{_milestone('M0', 'First gate', state=first_state, depends_on='none', checked=first_checked, evidence=first_evidence, review=first_review)}
{_milestone('M1', 'Second gate', state=second_state, depends_on=second_depends, checked=False, evidence='Pending.', blocker=plan_blocker, unblock_when=plan_unblock)}
{extra}"""
    (root / "docs" / "plans" / "PLAN.md").write_text(plan, encoding="utf-8")
    return root


def _errors(root: Path) -> list[str]:
    return DOCS.validate(root, today=date(2026, 7, 22))[0]


def test_valid_milestone_plan_reports_current_and_next_progress(tmp_path, capsys):
    root = _write_repo(tmp_path)

    errors, tasks = DOCS.validate(root, today=date(2026, 7, 22))
    DOCS._print_progress(root, tasks, "HB-001")

    assert errors == []
    assert capsys.readouterr().out.splitlines() == [
        "HB-001: 1/2 milestones accepted",
        "Current: M1 — Second gate (In progress)",
        "Next: none",
    ]


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"plan_status": "Planned"}, "PLAN_STATE_MISMATCH:"),
        (
            {
                "extra": _milestone(
                    "M2",
                    "Third gate",
                    state="In progress",
                    depends_on="M0",
                    checked=False,
                    evidence="Pending.",
                )
            },
            "PLAN_MILESTONE_ACTIVE:",
        ),
        ({"first_evidence": "Pending."}, "PLAN_ACCEPTED_EVIDENCE:"),
        ({"first_checked": False}, "PLAN_ACCEPTED_UNCHECKED:"),
        ({"first_review": "Fail — P1 remains."}, "PLAN_ACCEPTED_REVIEW:"),
        ({"first_state": "Planned"}, "PLAN_MILESTONE_DEPENDENCY:"),
        (
            {
                "task_status": "blocked",
                "plan_status": "Blocked",
                "second_state": "Blocked",
                "task_blocker": "waiting for A",
                "task_unblock": "A is available",
                "plan_blocker": "waiting for B",
                "plan_unblock": "B is available",
            },
            "PLAN_BLOCKER_DRIFT:",
        ),
        ({"linked": False}, "PLAN_QUEUE:"),
    ),
)
def test_invalid_milestone_fixtures_fail_the_top_level_contract(tmp_path, changes, expected):
    root = _write_repo(tmp_path, **changes)

    assert any(error.startswith(expected) for error in _errors(root))
