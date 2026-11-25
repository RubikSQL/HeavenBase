"""Executable import guards for the accepted package-ownership model."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "heavenbase"


def _imports(path: Path) -> list[str]:
    """Return absolute import targets from one HeavenBase source file."""
    package = ("heavenbase", *path.relative_to(ROOT).with_suffix("").parts[:-1])
    targets: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                prefix = package[:keep]
                suffix = tuple((node.module or "").split(".")) if node.module else ()
                targets.append(".".join((*prefix, *suffix)))
            elif node.module:
                targets.append(node.module)
    return targets


def _package_files(*packages: str) -> list[Path]:
    """Return Python files below the selected top-level owner packages."""
    return [path for package in packages for path in sorted((ROOT / package).rglob("*.py"))]


def _assert_no_imports(paths: list[Path], forbidden: tuple[str, ...]) -> None:
    """Assert that selected files do not import forbidden package prefixes."""
    violations = []
    for path in paths:
        for target in _imports(path):
            if any(target == prefix or target.startswith(f"{prefix}.") for prefix in forbidden):
                violations.append(f"{path.relative_to(ROOT)} -> {target}")
    assert violations == []


def test_guard_1_utils_remains_domain_free():
    violations = []
    for path in _package_files("utils"):
        for target in _imports(path):
            if target.startswith("heavenbase.") and not (
                target == "heavenbase._lazy" or target == "heavenbase.utils" or target.startswith("heavenbase.utils.")
            ):
                violations.append(f"{path.relative_to(ROOT)} -> {target}")
    assert violations == []


def test_guard_2_authored_vocabulary_and_placement_avoid_physical_interfaces():
    _assert_no_imports(
        _package_files("entity", "schema", "types", "strategies", "filters", "placement"),
        (
            "heavenbase.backends",
            "heavenbase.database",
            "heavenbase.workspace",
            "heavenbase.api",
            "heavenbase.cli",
            "heavenbase.dashboard",
        ),
    )


def test_guard_3_execution_avoids_workspace_and_query_policy():
    _assert_no_imports(
        _package_files("execution"),
        (
            "heavenbase.workspace",
            "heavenbase.query",
            "heavenbase.api",
            "heavenbase.cli",
            "heavenbase.dashboard",
            "heavenbase.extensions",
            "heavenbase.capsule",
            "heavenbase.toolkit",
        ),
    )


def test_guard_4_execution_never_imports_physical_providers():
    _assert_no_imports(
        _package_files("execution"),
        ("heavenbase.backends", "heavenbase.database"),
    )


def test_guard_5_query_owns_requests_not_routes_or_frame_materialization():
    paths = _package_files("query")
    _assert_no_imports(
        paths,
        (
            "heavenbase.backends",
            "heavenbase.database",
            "heavenbase.workspace",
            "heavenbase.api",
            "heavenbase.cli",
            "heavenbase.dashboard",
        ),
    )
    calls = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "ResultFrame" and node.func.attr in {"from_rows", "from_table"}:
                    calls.append(f"{path.relative_to(ROOT)}:{node.lineno} ResultFrame.{node.func.attr}")
    assert calls == []


def test_guard_6_lower_owners_do_not_reenter_workspace():
    _assert_no_imports(
        _package_files(
            "modules",
            "entity",
            "schema",
            "types",
            "strategies",
            "filters",
            "placement",
            "query",
            "execution",
            "database",
            "backends",
            "capabilities",
            "interop",
            "llm",
        ),
        ("heavenbase.workspace", "heavenbase.api", "heavenbase.cli", "heavenbase.dashboard"),
    )


def test_guard_7_workspace_does_not_import_replaceable_bundles():
    _assert_no_imports(
        _package_files("workspace"),
        (
            "heavenbase.capsule",
            "heavenbase.toolkit",
            "heavenbase.extensions.agent",
            "heavenbase.extensions.database",
            "heavenbase.extensions.memory",
            "heavenbase.extensions.prompt",
        ),
    )


def test_guard_8_interfaces_depend_on_sdk_and_context_contracts():
    _assert_no_imports(
        _package_files("api", "cli", "dashboard"),
        (
            "heavenbase.backends",
            "heavenbase.database",
            "heavenbase.execution",
            "heavenbase.placement",
        ),
    )
    neutral_cli = [
        ROOT / "cli" / "spec.py",
        ROOT / "cli" / "registry.py",
        *_package_files("cli/backends"),
    ]
    _assert_no_imports(
        neutral_cli,
        (
            "heavenbase.cli.context",
            "heavenbase.context",
            "heavenbase.workspace",
        ),
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in neutral_cli)
    assert "PackageCLIContext" not in text
    assert "CM_HVNB" not in text
    _assert_no_imports(
        _package_files("workspace/mcp"),
        (
            "heavenbase.database",
            "heavenbase.execution",
            "heavenbase.placement",
        ),
    )
