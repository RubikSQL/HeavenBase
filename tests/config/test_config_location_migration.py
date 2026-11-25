from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest


def _migrator():
    path = Path(__file__).parents[2] / "scripts" / "migrate_config_locations.py"
    spec = spec_from_file_location("migrate_config_locations", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_location_migrator_reports_and_preserves_native_targets(tmp_path):
    migrator = _migrator()
    home = tmp_path / "home"
    resource = tmp_path / "resources"
    legacy = tmp_path / "legacy"
    resource.mkdir()
    data = {
        "store": {"database": "file:%/config.db"},
        "backends": {
            "main": {"type": "sqlite", "database": "file:relative.db"},
            "rows": {"type": "json", "path": str(tmp_path / "rows")},
            "vec": {"type": "lance", "uri": "s3://bucket/base"},
        },
        "backend_defaults": {"lance": {"uri": "&/lance"}},
    }

    migrated, reports = migrator.migrate_document(
        data,
        source="config.yaml",
        package_home=str(home),
        resource=str(resource),
        legacy_base=str(legacy),
    )

    assert len(reports) == 4
    assert all(report.old_native_path == report.new_native_path for report in reports)
    assert migrated["store"]["database"].startswith("file:///")
    assert migrated["backends"]["main"]["database"].startswith("file:///")
    assert migrated["backends"]["vec"]["uri"] == "s3://bucket/base"


@pytest.mark.parametrize("token", ["relative.db", "file:logical.db", "../escape.db", "//server/share.db"])
def test_location_migrator_blocks_ambiguous_or_network_targets(tmp_path, token):
    migrator = _migrator()
    kwargs = {} if token != "../escape.db" else {"legacy_base": str(tmp_path)}
    with pytest.raises(ValueError):
        migrator.migrate_document({"backends": {"main": {"type": "sqlite", "database": token}}}, source="config.yaml", **kwargs)


@pytest.mark.parametrize(
    "token",
    [
        "file:/tmp/legacy.db",
        "file:C:/legacy.db",
        r"file:C:\legacy.db",
        "file:%/legacy.db",
        "file:&/legacy.db",
        "file:logical.db",
    ],
)
def test_location_migrator_classifies_every_legacy_file_shape(tmp_path, token):
    migrator = _migrator()
    (tmp_path / "resource").mkdir()
    migrated, reports = migrator.migrate_document(
        {"backends": {"main": {"type": "sqlite", "database": token}}},
        source="config.yaml",
        package_home=str(tmp_path / "package-home"),
        home=str(tmp_path / "user-home"),
        resource=str(tmp_path / "resource"),
        legacy_base=str(tmp_path / "legacy"),
    )

    assert migrated["backends"]["main"]["database"].startswith("file:///")
    assert reports[0].old_native_path == reports[0].new_native_path
    assert reports[0].legacy_base


def test_location_migrator_preserves_canonical_aliases_and_separates_home_roots(tmp_path):
    migrator = _migrator()
    data = {
        "backends": {
            "rows": {"type": "json", "path": "file://%/rows"},
            "cache": {"type": "pickle", "path": "~/cache"},
        }
    }

    migrated, reports = migrator.migrate_document(
        data,
        source="config.yaml",
        package_home=str(tmp_path / "package"),
        home=str(tmp_path / "user"),
    )

    assert migrated["backends"]["rows"]["path"] == "file://%/rows"
    assert reports[0].old_native_path == str(tmp_path / "user" / "cache")


def test_location_migrator_blocks_lance_placeholder_layout_changes(tmp_path):
    migrator = _migrator()

    with pytest.raises(ValueError, match="data-layout migration"):
        migrator.migrate_document(
            {"backends": {"vec": {"type": "lance", "uri": "file:%/lance/{ws_id}"}}},
            source="config.yaml",
            package_home=str(tmp_path),
        )


def test_location_migrator_blocks_unwritable_or_missing_packaged_resources(tmp_path):
    migrator = _migrator()

    with pytest.raises(ValueError, match="stable writable"):
        migrator.migrate_document(
            {"backends": {"rows": {"type": "json", "path": "file:&/rows"}}},
            source="config.yaml",
            resource=str(tmp_path / "missing-resource"),
        )
