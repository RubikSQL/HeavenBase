#!/usr/bin/env python3
"""Dry-run or rewrite legacy HeavenBase config locators to strict file URIs."""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from heavenbase.utils.files import file_uri_to_path, path_to_file_uri

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_LOCAL_DATABASE_TYPES = frozenset({"duckdb", "sqlite"})
_LOCAL_PATH_TYPES = frozenset({"chroma", "file", "json", "pickle"})


@dataclass(frozen=True)
class LocationMigration:
    """Describe one verified syntax-only config-locator rewrite."""

    source: str
    field: str
    legacy_base: str
    old_token: str
    new_token: str
    old_native_path: str
    new_native_path: str


def migrate_document(
    data: Mapping[str, Any],
    *,
    source: str,
    package_home: str | None = None,
    home: str | None = None,
    resource: str | None = None,
    legacy_base: str | None = None,
) -> tuple[dict[str, Any], list[LocationMigration]]:
    """Return a migrated copy and verified locator-rewrite report.

    Args:
        data: Parsed YAML/JSON configuration mapping.
        source: Human-readable source filename for reports.
        package_home: Captured package-data root for ``%`` aliases.
        home: Captured user-home root for ``~/`` locators.
        resource: Captured resource root for ``&`` aliases.
        legacy_base: Captured old resolver base for relative/logical tokens.

    Returns:
        Migrated mapping and ordered rewrite records.
    """
    if not isinstance(data, Mapping):
        raise TypeError("Config location migration requires a mapping document")
    reports: list[LocationMigration] = []
    migrated = _walk(
        dict(data),
        path=(),
        source=source,
        package_home=_absolute_root(package_home, "package home"),
        home=_absolute_root(home, "home"),
        resource=_absolute_root(resource, "resource"),
        legacy_base=_absolute_root(legacy_base, "legacy base"),
        reports=reports,
    )
    return migrated, reports


def _walk(
    value: Any,
    *,
    path: tuple[str, ...],
    source: str,
    package_home: str | None,
    home: str | None,
    resource: str | None,
    legacy_base: str | None,
    reports: list[LocationMigration],
) -> Any:
    if isinstance(value, list):
        return [
            _walk(
                item,
                path=(*path, str(index)),
                source=source,
                package_home=package_home,
                home=home,
                resource=resource,
                legacy_base=legacy_base,
                reports=reports,
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, Mapping):
        return value
    backend_type = str(value.get("type") or value.get("provider") or "").strip().lower()
    out: dict[str, Any] = {}
    for key, item in value.items():
        field_path = (*path, str(key))
        if isinstance(item, str) and _is_locator_field(field_path, backend_type):
            out[key], report = _migrate_token(
                item,
                source=source,
                field=".".join(field_path),
                package_home=package_home,
                home=home,
                resource=resource,
                legacy_base=legacy_base,
                lance=key == "uri" and (backend_type == "lance" or "lance" in field_path),
                allow_remote=key == "uri" and (backend_type == "lance" or "lance" in field_path),
            )
            if report is not None:
                reports.append(report)
        else:
            out[key] = _walk(
                item,
                path=field_path,
                source=source,
                package_home=package_home,
                home=home,
                resource=resource,
                legacy_base=legacy_base,
                reports=reports,
            )
    return out


def _is_locator_field(path: tuple[str, ...], backend_type: str) -> bool:
    key = path[-1]
    if key == "database":
        return backend_type in _LOCAL_DATABASE_TYPES or path[-2:] == ("store", "database")
    if key == "path":
        return backend_type in _LOCAL_PATH_TYPES or any(name in _LOCAL_PATH_TYPES for name in path)
    if key == "uri":
        return backend_type == "lance" or "lance" in path
    return False


def _migrate_token(
    token: str,
    *,
    source: str,
    field: str,
    package_home: str | None,
    home: str | None,
    resource: str | None,
    legacy_base: str | None,
    lance: bool,
    allow_remote: bool,
) -> tuple[str, LocationMigration | None]:
    if token == ":memory:":
        return token, None
    if lance and "{ws_id}" in token:
        raise ValueError(f"{source}:{field}: Lance locator contains {{ws_id}}; review the data-layout migration before rewriting")
    if token.startswith("file://"):
        if token.startswith("file://&/"):
            _require_writable_resource(resource, source=source, field=field)
        file_uri_to_path(token, home=package_home, resource=resource)
        return token, None
    if "://" in token:
        if allow_remote:
            return token, None
        raise ValueError(f"{source}:{field}: network or unknown locator target is not migratable: {token!r}")
    native, base = _legacy_native(
        token,
        package_home=package_home,
        home=home,
        resource=resource,
        legacy_base=legacy_base,
        source=source,
        field=field,
    )
    new_token = path_to_file_uri(native)
    new_native = file_uri_to_path(new_token)
    if _path_identity(native) != _path_identity(new_native):
        raise ValueError(f"{source}:{field}: old/new native path mismatch: {native!r} != {new_native!r}")
    return new_token, LocationMigration(source, field, base, token, new_token, native, new_native)


def _legacy_native(
    token: str,
    *,
    package_home: str | None,
    home: str | None,
    resource: str | None,
    legacy_base: str | None,
    source: str,
    field: str,
) -> tuple[str, str]:
    value = token[5:] if token.startswith("file:") else token
    if not value:
        raise ValueError(f"{source}:{field}: empty legacy file locator")
    if value.startswith(("//", "\\\\")):
        raise ValueError(f"{source}:{field}: network/UNC targets are not migratable: {token!r}")
    if value[:2] in {"%/", "%\\"}:
        return _under_root(package_home, value[2:], source=source, field=field, name="package home"), str(package_home)
    if value[:2] in {"&/", "&\\"}:
        _require_writable_resource(resource, source=source, field=field)
        return _under_root(resource, value[2:], source=source, field=field, name="resource root"), str(resource)
    if value.startswith("~/"):
        if home is None:
            raise ValueError(f"{source}:{field}: ~/ locator requires a captured --home root")
        return _under_root(home, value[2:], source=source, field=field, name="home"), home
    if _WINDOWS_ABSOLUTE.match(value):
        native = ntpath.normpath(value).replace("\\", "/")
        return f"{native[0].upper()}{native[1:]}", "<absolute>"
    if value.startswith("/"):
        return os.path.abspath(os.path.normpath(value)), "<absolute>"
    if legacy_base is None:
        raise ValueError(f"{source}:{field}: relative/logical locator {token!r} requires --legacy-base")
    return _under_root(legacy_base, value, source=source, field=field, name="legacy base"), legacy_base


def _under_root(root: str | None, relative: str, *, source: str, field: str, name: str) -> str:
    if root is None:
        raise ValueError(f"{source}:{field}: locator requires a captured {name}")
    if not relative or relative.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE.match(relative):
        raise ValueError(f"{source}:{field}: invalid relative locator child {relative!r}")
    if _WINDOWS_ABSOLUTE.match(root):
        joined = ntpath.normpath(ntpath.join(root, relative.replace("/", "\\")))
        if ntpath.commonpath((ntpath.normcase(root), ntpath.normcase(joined))) != ntpath.normcase(root):
            raise ValueError(f"{source}:{field}: locator escapes {name}")
        return joined.replace("\\", "/")
    joined = os.path.abspath(os.path.normpath(os.path.join(root, relative)))
    if os.path.commonpath((root, joined)) != root:
        raise ValueError(f"{source}:{field}: locator escapes {name}")
    return joined


def _absolute_root(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if value.startswith(("//", "\\\\")) or not (value.startswith("/") or _WINDOWS_ABSOLUTE.match(value)):
        raise ValueError(f"{name} must be an absolute local path")
    if _WINDOWS_ABSOLUTE.match(value):
        return ntpath.normpath(value).replace("\\", "/")
    return os.path.abspath(os.path.normpath(value))


def _require_writable_resource(root: str | None, *, source: str, field: str) -> None:
    if root is None:
        raise ValueError(f"{source}:{field}: packaged-resource locator requires a captured resource root")
    if not os.path.isdir(root) or not os.access(root, os.W_OK):
        raise ValueError(f"{source}:{field}: packaged-resource target is not a stable writable directory: {root!r}")


def _path_identity(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value)) if _WINDOWS_ABSOLUTE.match(value) else os.path.normcase(os.path.normpath(value))


def _load(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, Mapping):
        raise TypeError(f"{path}: expected a mapping document")
    return data


def _dump(path: Path, data: Mapping[str, Any]) -> None:
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="YAML or JSON config documents")
    parser.add_argument("--package-home", help="captured package-data root for % legacy locators")
    parser.add_argument("--home", help="captured user-home root for ~/ legacy locators")
    parser.add_argument("--resource", help="captured packaged-resource root for & legacy locators")
    parser.add_argument("--legacy-base", help="captured old resolver base for relative/logical locators")
    parser.add_argument("--write", action="store_true", help="write verified rewrites; default is dry-run")
    args = parser.parse_args(argv)
    staged: list[tuple[Path, Mapping[str, Any], list[LocationMigration]]] = []
    try:
        for path in args.paths:
            migrated, reports = migrate_document(
                _load(path),
                source=str(path),
                package_home=args.package_home,
                home=args.home,
                resource=args.resource,
                legacy_base=args.legacy_base,
            )
            staged.append((path, migrated, reports))
    except Exception as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    for _, _, reports in staged:
        for report in reports:
            print(json.dumps(asdict(report), sort_keys=True))
    if args.write:
        for path, migrated, _ in staged:
            _dump(path, migrated)
    print(f"{'WROTE' if args.write else 'DRY-RUN'}: {sum(len(item[2]) for item in staged)} verified locator rewrite(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
