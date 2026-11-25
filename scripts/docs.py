#!/usr/bin/env python3
"""Validate the repository documentation contract and inspect its task queue."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

import yaml

QUEUE_SCHEMA = "heaven.tasks/v1"
OPEN_STATUSES = {"ready", "active", "blocked"}
PRIORITIES = {"P0", "P1", "P2", "P3"}
REQUIRED_FILES = (
    "README.en.md",
    "docs/README.md",
    "docs/DEVLOG.md",
)
TEMPLATE_POLICY_FILE = ".blueprint-template.yaml"
TEMPLATE_QUEUE_FILE = "docs/tasks.template.yaml"
OPERATIONAL_QUEUE_FILE = "docs/tasks.yaml"
LEGACY_DOC_DIRS = ("docs/progress",)
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]{3,}$")
DEVLOG_ENTRY_RE = re.compile(r"^##\s+(\S+)(?:\s+.*)?$", re.MULTILINE)
DEVLOG_NEXT_RE = re.compile(r"^- Next:\s*(.+)$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
PLAN_MILESTONE_RE = re.compile(r"^##\s+(M\d+)\s+[—-]\s+(.+?)\s*$", re.MULTILINE)
PLAN_STATES = {"Planned", "In progress", "Blocked", "Accepted"}
PLAN_STATUS_BY_TASK = {"ready": "Planned", "active": "In progress", "blocked": "Blocked"}


@dataclass(frozen=True)
class _PlanMilestone:
    identifier: str
    title: str
    state: str
    depends_on: tuple[str, ...]
    owner: str
    updated: date | None
    review: str
    documentation: str
    rollback: str
    blocker: str
    unblock_when: str
    criteria: tuple[tuple[bool, str], ...]
    verification: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class _Plan:
    path: Path
    label: str
    task_id: str
    status: str
    updated: date | None
    milestones: tuple[_PlanMilestone, ...]


def _as_date(value: object, field: str, errors: list[str]) -> date | None:
    """Normalize a YAML date while collecting a stable diagnostic."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    errors.append(f"DATE_INVALID: {field} must be YYYY-MM-DD")
    return None


def _load_yaml(path: Path, errors: list[str]) -> object:
    """Load YAML without allowing parse failures to become nominal data."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"YAML_INVALID: {path}: {exc}")
        return None


def _markdown_links(path: Path) -> Iterable[str]:
    """Yield local-looking Markdown link targets outside fenced code blocks."""
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in LINK_RE.finditer(line):
            target = match.group(1).strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            elif ' "' in target:
                target = target.split(' "', 1)[0]
            yield target


def _validate_links(root: Path, errors: list[str]) -> None:
    """Reject broken repository-local Markdown links."""
    candidates = [root / name for name in ("README.en.md", "AGENTS.md", "CONTRIBUTING.md")]
    candidates.extend(sorted((root / "docs").rglob("*.md")))
    for source in candidates:
        if not source.is_file():
            continue
        for raw_target in _markdown_links(source):
            if raw_target.startswith(("#", "http://", "https://", "mailto:", "tel:")):
                continue
            target = unquote(raw_target.split("#", 1)[0])
            if not target:
                continue
            if target.startswith("/"):
                rel_source = source.relative_to(root).as_posix()
                errors.append(f"LINK_ABSOLUTE: {rel_source} -> {raw_target}")
                continue
            resolved = (source.parent / target).resolve()
            if root.resolve() not in resolved.parents and resolved != root.resolve():
                rel_source = source.relative_to(root).as_posix()
                errors.append(f"LINK_ESCAPE: {rel_source} -> {raw_target}")
                continue
            if not resolved.exists():
                rel_source = source.relative_to(root).as_posix()
                errors.append(f"LINK_MISSING: {rel_source} -> {raw_target}")


def _validate_structure(root: Path, template_mode: bool, errors: list[str]) -> None:
    """Validate documentation surfaces and the template/operational boundary."""
    queue_file = TEMPLATE_QUEUE_FILE if template_mode else OPERATIONAL_QUEUE_FILE
    for relative in (*REQUIRED_FILES, queue_file):
        if not (root / relative).is_file():
            errors.append(f"SURFACE_MISSING: {relative}")
    if template_mode and (root / OPERATIONAL_QUEUE_FILE).exists():
        errors.append(f"TEMPLATE_LIVE_QUEUE: {OPERATIONAL_QUEUE_FILE} is not allowed in template mode")
    if not template_mode and (root / TEMPLATE_QUEUE_FILE).exists():
        errors.append(f"OPERATIONAL_TEMPLATE_QUEUE: retire {TEMPLATE_QUEUE_FILE} after instantiating the live queue")
    for relative in LEGACY_DOC_DIRS:
        if (root / relative).exists():
            errors.append(f"LEGACY_SURFACE: retire {relative} into docs/DEVLOG.md")
    temp_root = root / ".temp"
    if temp_root.exists():
        tracked = _git_files(root, ".temp")
        for path in tracked:
            errors.append(f"SCRATCH_TRACKED: {path} must remain disposable")


def _git_files(root: Path, prefix: str) -> list[str]:
    """Return tracked paths below a prefix without importing repository runtime code."""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "--", prefix],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _validate_devlog(root: Path, task_ids: set[str], template_mode: bool, errors: list[str]) -> None:
    """Validate the rolling development log shape and ordering."""
    path = root / "docs" / "DEVLOG.md"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if not text.startswith("# Development Log\n"):
        errors.append("DEVLOG_HEADER: docs/DEVLOG.md must start with '# Development Log'")
    headings = list(DEVLOG_ENTRY_RE.finditer(text))
    if not headings:
        errors.append("DEVLOG_EMPTY: docs/DEVLOG.md needs at least one dated entry")
        return
    dates: list[date] = []
    entries: list[tuple[str, list[str]]] = []
    for position, heading in enumerate(headings):
        label = heading.group(1)
        try:
            dates.append(date.fromisoformat(label))
        except ValueError:
            errors.append(f"DEVLOG_DATE: invalid entry date {label}")
        end = headings[position + 1].start() if position + 1 < len(headings) else len(text)
        next_values = DEVLOG_NEXT_RE.findall(text[heading.end() : end])
        entries.append((label, next_values))
        if len(next_values) != 1:
            errors.append(f"DEVLOG_NEXT: entry {label} needs exactly one '- Next:' line")
    if len(dates) == len(headings) and dates != sorted(dates, reverse=True):
        errors.append("DEVLOG_ORDER: dated entries must be newest first")
    if len(headings) > 50:
        errors.append("DEVLOG_TOO_LONG: keep at most 50 entries; Git preserves older detail")
    if len(entries[0][1]) != 1:
        return
    for value in entries[0][1]:
        normalized = value.strip().strip("`")
        if template_mode:
            if normalized.lower() != "none":
                errors.append("TEMPLATE_DEVLOG_NEXT: the newest template DEVLOG entry must use '- Next: none'")
            continue
        if normalized.lower() == "none":
            continue
        references = set(re.findall(r"[A-Z][A-Z0-9]*-[0-9]{3,}", normalized))
        if not references:
            errors.append(f"DEVLOG_NEXT: '{value}' must reference an active task ID or none")
        for task_id in references - task_ids:
            errors.append(f"DEVLOG_TASK_MISSING: {task_id}")


def _validate_tasks(root: Path, relative: str, *, inert: bool, errors: list[str]) -> list[dict[str, object]]:
    """Validate and return an operational queue or inert template."""
    path = root / relative
    if not path.is_file():
        return []
    data = _load_yaml(path, errors)
    if not isinstance(data, dict):
        errors.append(f"QUEUE_ROOT: {relative} must contain a mapping")
        return []
    if data.get("schema") != QUEUE_SCHEMA:
        errors.append(f"QUEUE_SCHEMA: schema must be {QUEUE_SCHEMA}")
    if not isinstance(data.get("project"), str) or not str(data["project"]).strip():
        errors.append("QUEUE_PROJECT: project must be a non-empty string")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        errors.append("QUEUE_TASKS: tasks must be a list")
        return []
    if inert and tasks:
        errors.append(f"TEMPLATE_QUEUE_NOT_INERT: {relative} must contain tasks: []")
    queue_updated = _as_date(data.get("updated"), "queue.updated", errors)
    seen: set[str] = set()
    normalized: list[dict[str, object]] = []
    for position, raw_task in enumerate(tasks):
        label = f"tasks[{position}]"
        if not isinstance(raw_task, dict):
            errors.append(f"TASK_MAPPING: {label} must be a mapping")
            continue
        task = dict(raw_task)
        task_id = task.get("id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            errors.append(f"TASK_ID: {label}.id must match {TASK_ID_RE.pattern}")
            continue
        if task_id in seen:
            errors.append(f"TASK_DUPLICATE: {task_id}")
            continue
        seen.add(task_id)
        normalized.append(task)
        if not isinstance(task.get("title"), str) or not str(task["title"]).strip():
            errors.append(f"TASK_TITLE: {task_id} needs a title")
        status = task.get("status")
        if status not in OPEN_STATUSES:
            errors.append(f"TASK_STATUS: {task_id} must use one of {sorted(OPEN_STATUSES)}; closed work leaves the queue")
        if task.get("priority") not in PRIORITIES:
            errors.append(f"TASK_PRIORITY: {task_id} must use P0, P1, P2, or P3")
        task_updated = _as_date(task.get("updated"), f"{task_id}.updated", errors)
        if queue_updated and task_updated and task_updated > queue_updated:
            errors.append(f"TASK_UPDATED: {task_id} is newer than queue.updated")
        if status == "active" and not str(task.get("owner") or "").strip():
            errors.append(f"TASK_OWNER: active task {task_id} needs an owner")
        if status == "blocked" and not str(task.get("blocker") or "").strip():
            errors.append(f"TASK_BLOCKER: blocked task {task_id} needs a blocker")
        if status == "blocked" and not str(task.get("unblock_when") or "").strip():
            errors.append(f"TASK_UNBLOCK: blocked task {task_id} needs an observable unblock_when condition")
        acceptance = task.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance or not all(isinstance(item, str) and item.strip() for item in acceptance):
            errors.append(f"TASK_ACCEPTANCE: {task_id} needs a non-empty string list")
        for field in ("depends_on", "links"):
            value = task.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                errors.append(f"TASK_{field.upper()}: {task_id}.{field} must be a string list")
    by_id = {str(task["id"]): task for task in normalized}
    for task_id, task in by_id.items():
        parent = task.get("parent")
        if parent is not None and not isinstance(parent, str):
            errors.append(f"TASK_PARENT: {task_id}.parent must be a task ID or null")
        elif parent == task_id:
            errors.append(f"TASK_SELF_PARENT: {task_id}")
        elif isinstance(parent, str) and parent not in by_id:
            errors.append(f"TASK_PARENT_MISSING: {task_id} -> {parent}")
        dependencies = task.get("depends_on", [])
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if dependency == task_id:
                errors.append(f"TASK_SELF_DEPENDENCY: {task_id}")
            elif dependency not in by_id:
                errors.append(f"TASK_DEPENDENCY_MISSING: {task_id} -> {dependency}")
        links = task.get("links", [])
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, str):
                    continue
                if link.startswith(("http://", "https://")):
                    continue
                target = (root / link).resolve()
                if root.resolve() not in target.parents and target != root.resolve():
                    errors.append(f"TASK_LINK_ESCAPE: {task_id} -> {link}")
                elif not target.exists():
                    errors.append(f"TASK_LINK_MISSING: {task_id} -> {link}")
    _validate_dependency_cycles(by_id, errors)
    _validate_parent_cycles(by_id, errors)
    return normalized


def _validate_dependency_cycles(tasks: dict[str, dict[str, object]], errors: list[str]) -> None:
    """Reject dependency cycles in the active queue."""
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str, trail: list[str]) -> None:
        if task_id in visiting:
            start = trail.index(task_id)
            errors.append(f"TASK_DEPENDENCY_CYCLE: {' -> '.join(trail[start:] + [task_id])}")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        trail.append(task_id)
        dependencies = tasks[task_id].get("depends_on", [])
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if isinstance(dependency, str) and dependency in tasks:
                    visit(dependency, trail)
        trail.pop()
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in sorted(tasks):
        visit(task_id, [])


def _validate_parent_cycles(tasks: dict[str, dict[str, object]], errors: list[str]) -> None:
    """Reject cycles in delegated parent relationships."""
    for task_id in sorted(tasks):
        trail: list[str] = []
        current = task_id
        while current in tasks:
            if current in trail:
                start = trail.index(current)
                errors.append(f"TASK_PARENT_CYCLE: {' -> '.join(trail[start:] + [current])}")
                break
            trail.append(current)
            parent = tasks[current].get("parent")
            if not isinstance(parent, str):
                break
            current = parent


def _plan_field(text: str, name: str) -> str:
    """Return one bullet field from a plan header or milestone."""
    match = re.search(rf"^- {re.escape(name)}:\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip().strip("`") if match else ""


def _plan_block(text: str, name: str) -> str:
    """Return one level-three plan section without its heading."""
    match = re.search(rf"^###\s+{re.escape(name)}\s*$\n(.*?)(?=^###\s+|\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _parse_plan(path: Path, errors: list[str], *, label: str | None = None) -> _Plan:
    """Parse one milestone plan while collecting stable diagnostics."""
    text = path.read_text(encoding="utf-8")
    rel = label or path.as_posix()
    headings = list(PLAN_MILESTONE_RE.finditer(text))
    header = text[: headings[0].start()] if headings else text
    task_id = _plan_field(header, "Task")
    status = _plan_field(header, "Status")
    updated_raw = _plan_field(header, "Updated")
    if not task_id:
        errors.append(f"PLAN_TASK: {rel} needs '- Task:'")
    if not status:
        errors.append(f"PLAN_STATUS: {rel} needs '- Status:'")
    elif status not in {*PLAN_STATUS_BY_TASK.values(), "Done", "Superseded"}:
        errors.append(f"PLAN_STATUS_INVALID: {rel} -> {status}")
    updated = _as_date(updated_raw, f"{rel}.updated", errors) if updated_raw else None
    if not updated_raw:
        errors.append(f"PLAN_UPDATED: {rel} needs '- Updated:'")

    milestones: list[_PlanMilestone] = []
    seen: set[str] = set()
    for position, heading in enumerate(headings):
        identifier, title = heading.groups()
        end = headings[position + 1].start() if position + 1 < len(headings) else len(text)
        section = text[heading.end() : end]
        scope = f"{rel}:{identifier}"
        if identifier in seen:
            errors.append(f"PLAN_MILESTONE_DUPLICATE: {scope}")
        seen.add(identifier)
        state = _plan_field(section, "State")
        depends_raw = _plan_field(section, "Depends on")
        owner = _plan_field(section, "Owner")
        milestone_updated_raw = _plan_field(section, "Updated")
        review = _plan_field(section, "Review")
        documentation = _plan_field(section, "Documentation")
        rollback = _plan_field(section, "Rollback")
        blocker = _plan_field(section, "Blocker")
        unblock_when = _plan_field(section, "Unblock when")
        if state not in PLAN_STATES:
            errors.append(f"PLAN_MILESTONE_STATE: {scope} must use one of {sorted(PLAN_STATES)}")
        if not depends_raw:
            errors.append(f"PLAN_MILESTONE_DEPENDS: {scope} needs '- Depends on:'")
        depends_on = () if depends_raw.lower() == "none" else tuple(item.strip().strip("`") for item in depends_raw.split(",") if item.strip())
        if not owner:
            errors.append(f"PLAN_MILESTONE_OWNER: {scope} needs '- Owner:'")
        milestone_updated = _as_date(milestone_updated_raw, f"{scope}.updated", errors) if milestone_updated_raw else None
        if not milestone_updated_raw:
            errors.append(f"PLAN_MILESTONE_UPDATED: {scope} needs '- Updated:'")
        for field, value in (("Review", review), ("Documentation", documentation), ("Rollback", rollback)):
            if not value:
                errors.append(f"PLAN_MILESTONE_{field.upper()}: {scope} needs '- {field}:'")
        acceptance = _plan_block(section, "Acceptance")
        criteria = tuple((mark.lower() == "x", label.strip()) for mark, label in re.findall(r"^- \[([ xX])\]\s+(.+?)\s*$", acceptance, re.MULTILINE))
        if not criteria:
            errors.append(f"PLAN_MILESTONE_ACCEPTANCE: {scope} needs acceptance checkboxes")
        verification = tuple(re.findall(r"^- `([^`]+)`\s*$", _plan_block(section, "Verification"), re.MULTILINE))
        if not verification:
            errors.append(f"PLAN_MILESTONE_VERIFICATION: {scope} needs command evidence")
        evidence = tuple(line[2:].strip() for line in _plan_block(section, "Evidence").splitlines() if line.startswith("- "))
        if not evidence:
            errors.append(f"PLAN_MILESTONE_EVIDENCE: {scope} needs evidence entries")
        milestones.append(
            _PlanMilestone(
                identifier=identifier,
                title=title,
                state=state,
                depends_on=depends_on,
                owner=owner,
                updated=milestone_updated,
                review=review,
                documentation=documentation,
                rollback=rollback,
                blocker=blocker,
                unblock_when=unblock_when,
                criteria=criteria,
                verification=verification,
                evidence=evidence,
            )
        )
    if status in PLAN_STATUS_BY_TASK.values() and not milestones:
        errors.append(f"PLAN_MILESTONES: {rel} needs at least one '## M<N> — <title>' section")
    expected = tuple(f"M{index}" for index in range(len(milestones)))
    actual = tuple(milestone.identifier for milestone in milestones)
    if actual != expected:
        errors.append(f"PLAN_MILESTONE_ORDER: {rel} expected {expected}, got {actual}")
    return _Plan(path, rel, task_id, status, updated, tuple(milestones))


def _is_pending(value: str) -> bool:
    """Return whether plan evidence still contains a placeholder."""
    return value.strip().lower().rstrip(".") in {"", "pending", "none", "n/a", "unassigned"}


def _validate_plan_state(plan: _Plan, task: dict[str, object], errors: list[str]) -> None:
    """Validate queue/plan state alignment and milestone evidence."""
    rel = plan.label
    task_id = str(task["id"])
    expected_status = PLAN_STATUS_BY_TASK.get(str(task.get("status")))
    if plan.task_id != task_id:
        errors.append(f"PLAN_TASK_MISMATCH: {rel} declares {plan.task_id or 'none'}, queue owner is {task_id}")
    if expected_status and plan.status != expected_status:
        errors.append(f"PLAN_STATE_MISMATCH: {task_id} {task.get('status')} requires {expected_status}, got {plan.status}")
    task_updated_raw = task.get("updated")
    task_updated = task_updated_raw.date() if isinstance(task_updated_raw, datetime) else task_updated_raw
    if isinstance(task_updated, str):
        try:
            task_updated = date.fromisoformat(task_updated)
        except ValueError:
            task_updated = None
    if isinstance(task_updated, date) and plan.updated and task_updated != plan.updated:
        errors.append(f"PLAN_DATE_MISMATCH: {task_id} queue={task_updated.isoformat()} plan={plan.updated.isoformat()}")

    by_id = {milestone.identifier: milestone for milestone in plan.milestones}
    live = [milestone for milestone in plan.milestones if milestone.state in {"In progress", "Blocked"}]
    expected_live = {"active": "In progress", "blocked": "Blocked"}.get(str(task.get("status")))
    if expected_live:
        matching = [milestone for milestone in live if milestone.state == expected_live]
        if len(live) != 1 or len(matching) != 1:
            errors.append(f"PLAN_MILESTONE_ACTIVE: {task_id} needs exactly one {expected_live} milestone")
    elif live:
        errors.append(f"PLAN_MILESTONE_ACTIVE: {task_id} is ready but has an active or blocked milestone")

    for milestone in plan.milestones:
        scope = f"{task_id}:{milestone.identifier}"
        for dependency in milestone.depends_on:
            target = by_id.get(dependency)
            if target is None:
                errors.append(f"PLAN_MILESTONE_DEPENDENCY_MISSING: {scope} -> {dependency}")
            elif milestone.state in {"Accepted", "In progress", "Blocked"} and target.state != "Accepted":
                errors.append(f"PLAN_MILESTONE_DEPENDENCY: {scope} requires accepted {dependency}")
        if milestone.state == "Accepted":
            if any(not checked for checked, _ in milestone.criteria):
                errors.append(f"PLAN_ACCEPTED_UNCHECKED: {scope}")
            if _is_pending(milestone.review) or not milestone.review.casefold().startswith("pass"):
                errors.append(f"PLAN_ACCEPTED_REVIEW: {scope} needs a passing review result")
            if any(_is_pending(item) for item in milestone.evidence) or not all(LINK_RE.search(item) for item in milestone.evidence):
                errors.append(f"PLAN_ACCEPTED_EVIDENCE: {scope} needs non-placeholder Markdown evidence links")
        if milestone.state in {"Accepted", "In progress", "Blocked"} and _is_pending(milestone.owner):
            errors.append(f"PLAN_MILESTONE_OWNER_ACTIVE: {scope} needs an owner")
        if milestone.state in {"In progress", "Blocked"} and plan.updated and milestone.updated != plan.updated:
            errors.append(f"PLAN_MILESTONE_DATE: {scope} must match plan updated date")
        if milestone.state == "Blocked":
            if milestone.blocker != str(task.get("blocker") or ""):
                errors.append(f"PLAN_BLOCKER_DRIFT: {scope}")
            if milestone.unblock_when != str(task.get("unblock_when") or ""):
                errors.append(f"PLAN_UNBLOCK_DRIFT: {scope}")


def _validate_plan_coverage(
    root: Path,
    tasks: list[dict[str, object]],
    template_mode: bool,
    errors: list[str],
) -> dict[str, _Plan]:
    """Keep plans subordinate to exactly one active queue item."""
    links: dict[str, list[str]] = {}
    for task in tasks:
        task_id = str(task["id"])
        for link in task.get("links", []):
            if isinstance(link, str):
                links.setdefault(link, []).append(task_id)
    active_plans: dict[str, list[str]] = {}
    parsed: dict[str, _Plan] = {}
    for path in sorted((root / "docs" / "plans").glob("*.md")):
        if path.name in {"README.md", "RAW.md"}:
            continue
        rel = path.relative_to(root).as_posix()
        plan = _parse_plan(path, errors, label=rel)
        status = plan.status
        owners = links.get(rel, [])
        if status in {"Planned", "In progress", "Blocked"}:
            if template_mode:
                task_match = re.search(r"^- Task:\s*(.+?)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
                if not task_match:
                    errors.append(f"PLAN_AUTHORITY: {rel} needs one direct-request or external-tracker reference")
                elif TASK_ID_RE.fullmatch(task_match.group(1).strip().strip("`")):
                    errors.append(f"PLAN_TEMPLATE_TASK: {rel} must not invent a local queue task ID")
            elif len(owners) != 1:
                errors.append(f"PLAN_QUEUE: {rel} with status {status} needs exactly one queue owner")
            else:
                active_plans.setdefault(owners[0], []).append(rel)
                parsed[owners[0]] = plan
                task = next(task for task in tasks if str(task["id"]) == owners[0])
                _validate_plan_state(plan, task, errors)
        if status in {"Done", "Superseded"} and owners:
            errors.append(f"PLAN_CLOSED_QUEUED: {rel} -> {', '.join(owners)}")
        if status not in {"Planned", "In progress", "Blocked", "Done", "Superseded"}:
            errors.append(f"PLAN_STATUS_INVALID: {rel} -> {status}")
    for task_id, plans in sorted(active_plans.items()):
        if len(plans) > 1:
            errors.append(f"TASK_MULTIPLE_PLANS: {task_id} -> {', '.join(sorted(plans))}")
    return parsed


def _validate_scratch(root: Path, today: date, errors: list[str]) -> None:
    """Validate tracked scratch notes and force timely promotion or deletion."""
    scratch = root / "docs" / "scratch"
    if not scratch.is_dir():
        return
    for path in sorted(scratch.rglob("*.md")):
        if path.name == "README.md" or path.name.endswith(".local.md") or "local" in path.parts:
            continue
        match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root).as_posix()
        if not match:
            errors.append(f"SCRATCH_FRONTMATTER: {rel}")
            continue
        try:
            meta = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            errors.append(f"SCRATCH_YAML: {rel}: {exc}")
            continue
        if not isinstance(meta, dict) or meta.get("status") != "scratch":
            errors.append(f"SCRATCH_STATUS: {rel} must declare status: scratch")
            continue
        created = _as_date(meta.get("created"), f"{rel}.created", errors)
        expires = _as_date(meta.get("expires"), f"{rel}.expires", errors)
        if created and expires:
            if expires <= today:
                errors.append(f"SCRATCH_EXPIRED: {rel} expired on {expires.isoformat()}")
            if expires > created + timedelta(days=45):
                errors.append(f"SCRATCH_TTL: {rel} may live for at most 45 days")
        if not str(meta.get("task") or "").strip():
            errors.append(f"SCRATCH_TASK: {rel} must name its owning task or direct request")


def validate(root: Path, *, today: date | None = None) -> tuple[list[str], list[dict[str, object]]]:
    """Return deterministic contract errors and normalized active tasks."""
    root = root.resolve()
    errors: list[str] = []
    template_mode = (root / TEMPLATE_POLICY_FILE).is_file()
    _validate_structure(root, template_mode, errors)
    queue_file = TEMPLATE_QUEUE_FILE if template_mode else OPERATIONAL_QUEUE_FILE
    tasks = _validate_tasks(root, queue_file, inert=template_mode, errors=errors)
    task_ids = {str(task["id"]) for task in tasks}
    _validate_devlog(root, task_ids, template_mode, errors)
    _validate_plan_coverage(root, tasks, template_mode, errors)
    _validate_scratch(root, today or date.today(), errors)
    _validate_links(root, errors)
    return sorted(set(errors)), tasks


def _print_tasks(tasks: list[dict[str, object]], *, ready_only: bool) -> None:
    """Print the queue in file order for humans and agents."""
    selected = [task for task in tasks if not ready_only or (task.get("status") == "ready" and not task.get("depends_on"))]
    if not selected:
        print("No actionable tasks." if ready_only else "Task queue is empty.")
        return
    for task in selected:
        owner = task.get("owner") or "unassigned"
        print(f"{task['id']} [{task['priority']} {task['status']}] {task['title']} — {owner}")


def _print_progress(root: Path, tasks: list[dict[str, object]], task_id: str) -> None:
    """Print accepted milestone count plus the current and next gates."""
    task = next((item for item in tasks if item.get("id") == task_id), None)
    if task is None:
        raise KeyError(f"Unknown active task {task_id!r}")
    plan_links = [
        str(link)
        for link in task.get("links", [])
        if isinstance(link, str) and link.startswith("docs/plans/") and link.endswith(".md") and Path(link).name not in {"README.md", "RAW.md"}
    ]
    if len(plan_links) != 1:
        raise ValueError(f"Task {task_id!r} needs exactly one linked milestone plan")
    parse_errors: list[str] = []
    plan = _parse_plan(root / plan_links[0], parse_errors, label=plan_links[0])
    if parse_errors:
        raise ValueError(parse_errors[0])
    accepted = sum(milestone.state == "Accepted" for milestone in plan.milestones)
    current_index = next(
        (index for index, milestone in enumerate(plan.milestones) if milestone.state in {"In progress", "Blocked"}),
        next((index for index, milestone in enumerate(plan.milestones) if milestone.state != "Accepted"), None),
    )
    current = plan.milestones[current_index] if current_index is not None else None
    next_milestone = plan.milestones[current_index + 1] if current_index is not None and current_index + 1 < len(plan.milestones) else None
    print(f"{task_id}: {accepted}/{len(plan.milestones)} milestones accepted")
    print(f"Current: {current.identifier} — {current.title} ({current.state})" if current else "Current: complete")
    print(f"Next: {next_milestone.identifier} — {next_milestone.title}" if next_milestone else "Next: none")


def main() -> int:
    """Run documentation validation or print the active queue."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="Validate documentation, links, scratch state, and the task queue")
    check.add_argument("--today", type=date.fromisoformat, default=None, help="Override today's date for deterministic tests")
    tasks = subparsers.add_parser("tasks", help="Print the canonical active task queue")
    tasks.add_argument("--ready", action="store_true", help="Show only dependency-free ready tasks")
    progress = subparsers.add_parser("progress", help="Print milestone progress for one active task")
    progress.add_argument("task_id", help="Canonical task identifier, for example HB-001")
    args = parser.parse_args()
    root = args.root.resolve()
    template_mode = (root / TEMPLATE_POLICY_FILE).is_file()
    errors, queue = validate(root, today=getattr(args, "today", None))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    if args.command == "tasks":
        if template_mode:
            print("Template source has no live task queue; instantiate a concrete project first.")
        else:
            _print_tasks(queue, ready_only=args.ready)
    elif args.command == "progress":
        if template_mode:
            print("Template source has no live task progress.")
        else:
            try:
                _print_progress(root, queue, args.task_id)
            except (KeyError, ValueError) as error:
                print(error, file=sys.stderr)
                return 1
    elif template_mode:
        print("Documentation contract is valid (template source; no live task queue).")
    else:
        print(f"Documentation contract is valid ({len(queue)} active task(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
