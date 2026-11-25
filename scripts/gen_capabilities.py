"""Regenerate capability and built-in Tag references from live module records.

Run:
    rtk uv run python scripts/gen_capabilities.py
    rtk uv run python scripts/gen_capabilities.py --check
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Mapping
import copy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heavenbase.context import BootstrapSpec, Context, load_bootstrap
from heavenbase.execution import HandlerContext
from heavenbase.execution.handlers import HandlerRegistry
from heavenbase.execution.handlers.seed import seed_handlers
from heavenbase.modules import ModuleResolver, tag, tag_definition
from heavenbase.query import aggregate_reducers
from heavenbase.schema.model import FieldSchema
from heavenbase.types import logical_type_from_any
from heavenbase.utils import Any
from heavenbase.utils.config import ConfigManager
from heavenbase.utils.config.data import to_mutable_data
from heavenbase.utils.serialize import dumps_json, save_txt


def _catalog_rows(resolver: ModuleResolver, kind: str) -> tuple[dict[str, Any], dict[str, int]]:
    """Return one kind's records and content-defined presentation order."""
    rows = resolver.inspect(kind)
    by_identifier = {str(row["identifier"]): row for row in rows}
    if len(by_identifier) != len(rows):
        raise ValueError(f"Module catalog contains duplicate {kind!r} identifiers")

    def order(row: Mapping[str, Any]) -> tuple[int, int, str]:
        record = row.get("record")
        meta = record.get("meta") if isinstance(record, Mapping) else None
        priority = meta.get("priority") if isinstance(meta, Mapping) else None
        if priority is None:
            return (1, 0, str(row["identifier"]))
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise TypeError(f"{kind} {row['identifier']!r} priority must be an integer")
        return (0, priority, str(row["identifier"]))

    ordered = sorted(rows, key=order)
    return by_identifier, {str(row["identifier"]): index for index, row in enumerate(ordered)}


def _catalog_row(rows: Mapping[str, dict[str, Any]], kind: str, identifier: str) -> dict[str, Any]:
    """Return one required module row or reject an unowned handler key."""
    try:
        return rows[identifier]
    except KeyError as error:
        raise ValueError(f"Handler capability references unregistered {kind} {identifier!r}") from error


def _probe_metadata(row: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    """Return one validated descriptor-owned capability probe mapping."""
    record = row.get("record")
    meta = record.get("meta") if isinstance(record, Mapping) else None
    probe = meta.get("capability_probe") if isinstance(meta, Mapping) else None
    if not isinstance(probe, Mapping):
        raise ValueError(f"{kind} {row.get('identifier')!r} must declare record.meta.capability_probe")
    return probe


def _probe_dtype(row: Mapping[str, Any], op: str, resolver: ModuleResolver) -> Any:
    """Build one logical-type fixture from its inert module descriptor."""
    probe = _probe_metadata(row, "Logical type")
    unknown = set(probe) - {"descriptor", "descriptors", "values"}
    if unknown:
        raise ValueError(f"Logical type {row['identifier']!r} capability_probe has unsupported fields {sorted(unknown)!r}")
    if "descriptor" not in probe:
        raise ValueError(f"Logical type {row['identifier']!r} capability_probe must declare descriptor")
    descriptors = probe.get("descriptors", {})
    values = probe.get("values", {})
    if not isinstance(descriptors, Mapping):
        raise TypeError(f"Logical type {row['identifier']!r} capability_probe.descriptors must be a mapping")
    if not isinstance(values, Mapping):
        raise TypeError(f"Logical type {row['identifier']!r} capability_probe.values must be a mapping")
    dtype = logical_type_from_any(copy.deepcopy(descriptors.get(op, probe["descriptor"])), resolver=resolver)
    actual = type(dtype).identifier
    if actual != row["identifier"]:
        raise ValueError(f"Logical type {row['identifier']!r} capability probe built {actual!r}")
    return dtype


def _probe_value(type_row: Mapping[str, Any], operation_row: Mapping[str, Any], op: str) -> Any:
    """Return a type override or the operation descriptor's default operand."""
    operation_probe = _probe_metadata(operation_row, "Operation")
    unknown = set(operation_probe) - {"value"}
    if unknown:
        raise ValueError(f"Operation {op!r} capability_probe has unsupported fields {sorted(unknown)!r}")
    if "value" not in operation_probe:
        raise ValueError(f"Operation {op!r} capability_probe must declare value")
    type_probe = _probe_metadata(type_row, "Logical type")
    values = type_probe.get("values", {})
    if not isinstance(values, Mapping):
        raise TypeError(f"Logical type {type_row['identifier']!r} capability_probe.values must be a mapping")
    if op in values:
        return copy.deepcopy(values[op])
    return copy.deepcopy(operation_probe["value"])


def _backend_unsupported_storage_types(backend_identifier: str, resolver: ModuleResolver) -> frozenset[str]:
    """Return one backend type's validated unsupported storage-type claims."""
    result = tag(resolver, "backend_type", backend_identifier, "unsupported-storage-types")
    if result is None:
        return frozenset()
    if result.status != "resolved":
        raise ValueError(f"Backend type {backend_identifier!r} has unusable unsupported-storage-types Tag: " f"{result.status} {list(result.diagnostics)!r}")
    return frozenset(result.value)


def _probe_mode(
    registry: HandlerRegistry,
    resolver: ModuleResolver,
    config: ConfigManager,
    dtype_identifier: str,
    op: str,
    backend_identifier: str,
    strategy: str,
    type_rows: Mapping[str, dict[str, Any]],
    operation_rows: Mapping[str, dict[str, Any]],
) -> str:
    """Call the registered handler and inspect fragment mode."""
    type_row = _catalog_row(type_rows, "logical type", dtype_identifier)
    operation_row = _catalog_row(operation_rows, "operation", op)
    dtype = _probe_dtype(type_row, op, resolver)
    try:
        handler = registry.get(dtype, op, backend_identifier, strategy)
    except (LookupError, KeyError, TypeError, ValueError) as error:
        key = (dtype_identifier, op, backend_identifier, strategy)
        raise RuntimeError(f"Registered capability cannot restore handler {key!r}") from error
    field_schema = FieldSchema("probe", dtype)
    value = _probe_value(type_row, operation_row, op)
    ctx = HandlerContext(
        "probe",
        backend_identifier,
        strategy,
        top_k=10,
        metric="cosine",
        resolver=resolver,
        config=config,
        handlers=registry,
    )
    try:
        fragment = handler(field_schema, value, ctx)
        mode = fragment.notes.get("mode")
        if mode == "native" or mode == "native_with_near":
            return "native"
        if mode == "py_scan":
            return "py_scan"
        raise ValueError(f"Handler fragment must declare native, native_with_near, or py_scan mode, got {mode!r}")
    except Exception as error:
        key = (dtype_identifier, op, backend_identifier, strategy)
        raise RuntimeError(f"Capability handler probe failed for {key!r}") from error


def render(resolver: ModuleResolver, config: ConfigManager) -> str:
    """Render the capability report from one isolated module authority."""
    registry = seed_handlers(resolver=resolver)
    handler_keys = tuple((row["dtype"], row["op"], row["backend_identifier"], row["strategy"]) for row in registry.capability_rows())
    aggregate_compilers = registry.aggregate_capability_rows()
    type_rows, type_rank = _catalog_rows(resolver, "logical_type")
    operation_rows, operation_rank = _catalog_rows(resolver, "operation")
    backend_rows, backend_rank = _catalog_rows(resolver, "backend_type")
    strategy_rows, strategy_rank = _catalog_rows(resolver, "strategy")
    for dtype_identifier in sorted({dtype for dtype, _, _, _ in handler_keys}, key=lambda dtype: (type_rank.get(dtype, len(type_rank)), dtype)):
        type_row = _catalog_row(type_rows, "logical type", dtype_identifier)
        type_probe = _probe_metadata(type_row, "Logical type")
        for field in ("descriptors", "values"):
            fixtures = type_probe.get(field, {})
            if isinstance(fixtures, Mapping):
                unknown = set(fixtures) - set(operation_rows)
                if unknown:
                    raise ValueError(f"Logical type {dtype_identifier!r} capability_probe.{field} references unknown operations {sorted(unknown)!r}")
    for dtype_identifier, op, backend_identifier, strategy in handler_keys:
        _catalog_row(type_rows, "logical type", dtype_identifier)
        _catalog_row(operation_rows, "operation", op)
        _catalog_row(backend_rows, "backend type", backend_identifier)
        _catalog_row(strategy_rows, "strategy", strategy)
    truth: dict[tuple[str, str, str, str], str] = {}
    for key in handler_keys:
        dtype_identifier, op, backend_identifier, strategy = key
        truth[key] = _probe_mode(registry, resolver, config, dtype_identifier, op, backend_identifier, strategy, type_rows, operation_rows)

    by_key: dict[tuple[str, str, str], dict[str, str]] = defaultdict(dict)
    backends = sorted({backend for _, _, backend, _ in handler_keys}, key=lambda backend: (backend_rank[backend], backend))
    unsupported_storage_types = {backend: _backend_unsupported_storage_types(backend, resolver) for backend in backends}
    for t, op, b, s in handler_keys:
        mode = truth[(t, op, b, s)]
        if t in unsupported_storage_types[b]:
            symbol = "🚫"
        elif mode == "native":
            symbol = "✅"
        elif mode == "py_scan":
            symbol = "🔵"
        else:
            raise RuntimeError(f"Capability probe produced unsupported mode {mode!r} for {(t, op, b, s)!r}")
        by_key[(t, op, s)][b] = symbol
    lines: list[str] = []
    lines.append("# HeavenBase capability matrix\n")
    lines.append("_Generated by `scripts/gen_capabilities.py` from live module, handler, and backend records._\n")
    lines.append("")
    lines.append(
        "Legend: ✅ verified native pushdown · 🔵 registered query surface (Python scan fallback) · "
        "🚫 n/a, the backend declares this logical type in `unsupported-storage-types` so placement rejects the field "
        "before compiler selection and the registered handler can never execute · - = unsupported\n"
    )
    lines.append("")
    lines.append(
        "Use `Query.explain()` to inspect `handler_mode` for a concrete query: `native`, `py_scan`, or provider-specific notes such as `native_with_near`.\n"
    )
    lines.append(
        "Categorical rows are base-specific: text operations apply to string categoricals, order operations apply to integer categoricals, and exact operations apply to both.\n"
    )
    lines.append("Probe fixtures and presentation order come from registered module records; missing or malformed evidence fails generation.\n")
    lines.append("")
    lines.append("| Op | Type | Storage | " + " | ".join(backends) + " |")
    lines.append("|----|------|---------|" + "|".join(["----"] * len(backends)) + "|")
    for t, op, s in sorted(by_key, key=lambda row: (operation_rank[row[1]], type_rank[row[0]], strategy_rank[row[2]], row)):
        row = by_key[(t, op, s)]
        cells = [row.get(b, "-") for b in backends]
        lines.append(f"| {op} | {t} | {s} | " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("## Portable aggregate reducers")
    lines.append("")
    lines.append("These operations are registry-derived and always have the portable post-merge fold shown below.")
    lines.append("")
    lines.append("| Operation | Parameters | Accepted aggregate input modes | Result logical type |")
    lines.append("|-----------|------------|--------------------------------|---------------------|")
    for reducer in aggregate_reducers(resolver=resolver):
        modes = ", ".join(reducer.input_modes) if reducer.input_modes else "all"
        descriptor = reducer.to_dict()
        parameters = []
        for parameter in descriptor["parameters"]:
            suffix = "" if parameter["required"] else f"={dumps_json(parameter['default'], sort_keys=True)}"
            parameters.append(f"{parameter['name']}{suffix}")
        lines.append(f"| {reducer.identifier} | {', '.join(parameters) or '-'} | {modes} | {descriptor['result_type'] or 'untyped'} |")
    lines.append("")
    lines.append("### Registered native aggregate compilers")
    lines.append("")
    lines.append("| Backend | Operations | Strategy |")
    lines.append("|---------|------------|----------|")
    for compiler in aggregate_compilers:
        lines.append(f"| {compiler['backend_identifier']} | {', '.join(compiler['operations'])} | {compiler['strategy']} |")
    lines.append("")
    lines.append(
        "Parameterized variants remain compiler-checked: SQL `count(distinct=true)` is native only for equality-parity-safe logical types; other requests use the exact portable fold."
    )

    # Summary counts, classified exactly as the matrix above classifies each cell.
    lines.append("")
    lines.append("## Totals per backend")
    lines.append("")
    lines.append(
        "`Native + Fallback + n/a == Handlers`. `n/a` counts registered handlers on logical types the backend declares in "
        "`unsupported-storage-types`: placement rejects those fields before compiler selection, so those handlers can never "
        "execute and are counted apart from the modes that can. Each row therefore matches its own column of 🚫/✅/🔵 in the matrix above."
    )
    lines.append("")
    lines.append("| Backend | Handlers | Native | Fallback | n/a |")
    lines.append("|---------|---------:|-------:|---------:|----:|")
    for b in backends:
        keys = [key for key in handler_keys if key[2] == b]
        blocked = [key for key in keys if key[0] in unsupported_storage_types[b]]
        usable = [key for key in keys if key[0] not in unsupported_storage_types[b]]
        n_native = sum(1 for key in usable if truth[key] == "native")
        n_fallback = sum(1 for key in usable if truth[key] == "py_scan")
        if n_native + n_fallback + len(blocked) != len(keys):
            raise RuntimeError(f"Capability totals for {b!r} do not partition its {len(keys)} handlers")
        lines.append(f"| {b} | {len(keys)} | {n_native} | {n_fallback} | {len(blocked)} |")

    return "\n".join(lines).rstrip()


def render_tags(resolver: ModuleResolver) -> str:
    """Render the logical built-in Tag reference from installed definitions.

    Args:
        resolver: Isolated module authority containing the packaged records.

    Returns:
        str: Deterministic Markdown reference for every installed Tag definition.

    Raises:
        ValueError: If a packaged Tag definition cannot be interpreted.
    """
    rows = sorted(resolver.inspect("tag"), key=lambda row: str(row["identifier"]))
    lines = [
        "# Built-in Tag reference",
        "",
        "_Generated by `scripts/gen_capabilities.py` from installed `kind: tag` module records._",
        "",
        (
            "Tags are open-world declarations and routing hints. These built-in definitions provide optional typed "
            "inspection; external claims do not require a definition."
        ),
        "",
        "| Tag | Schema | Default | Subjects | Policy | Layers | Owner | Description |",
        "|-----|--------|---------|----------|--------|--------|-------|-------------|",
    ]
    for row in rows:
        identifier = str(row["identifier"])
        definition = tag_definition(resolver, identifier)
        if definition is None or not definition.valid:
            diagnostics = [] if definition is None else list(definition.diagnostics)
            raise ValueError(f"Built-in Tag {identifier!r} has no usable definition: {diagnostics!r}")
        installation = row["record"]["meta"].get("installation")
        owner = str(installation.get("coordinate") or "") if isinstance(installation, Mapping) else ""
        schema = dumps_json(to_mutable_data(definition.schema), sort_keys=True, compact=True).replace("|", "\\|")
        default = dumps_json(to_mutable_data(definition.default), sort_keys=True, compact=True).replace("|", "\\|") if definition.has_default else "—"
        subjects = ", ".join(definition.subjects) or "—"
        layers = ", ".join(definition.layers) or "—"
        description = " ".join(definition.desc.split()).replace("|", "\\|")
        lines.append(f"| `{identifier}` | `{schema}` | `{default}` | {subjects} | {definition.subject_policy} | " f"{layers} | `{owner}` | {description} |")
    lines.extend(
        (
            "",
            f"Total built-in definitions: **{len(rows)}**.",
            "",
            (
                "Raw claims live only on their subject record under `meta.tags`. Definition absence, retirement, or "
                "validation diagnostics do not remove those authored claims."
            ),
        )
    )
    return "\n".join(lines).rstrip()


def _isolated_context(root: str) -> Context:
    """Create a fresh Context whose catalog contains only packaged modules."""
    packaged = load_bootstrap(home_path=Path(root) / "missing-bootstrap.yaml")
    backend = dict(packaged.backend)
    backend["database"] = (Path(root) / "system.db").as_uri()
    context = Context(BootstrapSpec(root, "capability-report", backend, dict(packaged.registry)))
    context.modules().setup(force=True)
    return context


def _write_report(report: str, path: Path, *, check: bool) -> None:
    """Write a generated document or fail when the checked artifact has drifted."""
    expected = f"{report.rstrip()}\n"
    if check:
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            raise SystemExit(f"generated document is out of date: {path}; run: rtk uv run python {Path(__file__).relative_to(ROOT)}")
        print(f"generated document is current: {path}")
        return
    save_txt(report, str(path), encoding="utf-8")
    print(f"wrote {path}")


def main(argv: list[str] | None = None) -> None:
    """Regenerate or check the deterministic report in isolated machine state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing when the generated report differs")
    args = parser.parse_args(argv)
    with TemporaryDirectory(prefix="heavenbase-capabilities-") as root:
        context = _isolated_context(root)
        try:
            report = render(context.modules(), context.config)
            tag_reference = render_tags(context.modules())
        finally:
            context.close()
    capability_path = ROOT / "docs" / "resources" / "reports" / "capabilities.md"
    tag_path = ROOT / "docs" / "resources" / "reference" / "tags.md"
    _write_report(report, capability_path, check=args.check)
    _write_report(tag_reference, tag_path, check=args.check)


if __name__ == "__main__":
    main()
