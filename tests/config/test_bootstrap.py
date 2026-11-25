"""Bootstrap parsing and dependency-boundary tests."""

from copy import deepcopy
import re

import pytest

from heavenbase.context import bootstrap as bootstrap_module
from heavenbase.context import load_bootstrap
from heavenbase.database import database as database_module


def _bootstrap(tmp_path):
    return {
        "version": 1,
        "root": str(tmp_path),
        "workspace": "default",
        "backend": {
            "type": "inmem",
            "module": "heavenbase.backends.inmem.backend",
            "name": "system",
        },
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 4096},
    }


def test_explicit_bootstrap_has_precedence_over_home_file(tmp_path):
    home = tmp_path / "bootstrap.yaml"
    home.write_text("not: [valid", encoding="utf-8")

    spec = load_bootstrap(_bootstrap(tmp_path / "explicit"), home_path=home)

    assert spec.root == str(tmp_path / "explicit")
    assert spec.backend["type"] == "inmem"


def test_existing_malformed_home_bootstrap_fails_without_fallback(tmp_path):
    home = tmp_path / "bootstrap.yaml"
    home.write_text("version: [", encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(str(home))):
        load_bootstrap(home_path=home)


def test_existing_home_bootstrap_has_precedence_over_packaged_default(tmp_path, monkeypatch):
    home = tmp_path / "bootstrap.yaml"
    home.write_text(
        f"""version: 1
root: {tmp_path / 'home'}
workspace: home
backend:
  type: inmem
  module: heavenbase.backends.inmem.backend
  name: system
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap_module, "_packaged_bootstrap", lambda: pytest.fail("packaged bootstrap was read"))

    spec = load_bootstrap(home_path=home)

    assert spec.root == str(tmp_path / "home")
    assert spec.workspace == "home"


def test_packaged_bootstrap_is_used_when_home_file_is_absent(tmp_path, monkeypatch):
    packaged = _bootstrap(tmp_path / "packaged")
    monkeypatch.setattr(bootstrap_module, "_packaged_bootstrap", lambda: deepcopy(packaged))

    spec = load_bootstrap(home_path=tmp_path / "missing.yaml")

    assert spec.root == str(tmp_path / "packaged")
    assert spec.backend == packaged["backend"]


def test_bootstrap_preserves_strict_root_relative_database_uri(tmp_path):
    raw = _bootstrap(tmp_path)
    raw["backend"]["database"] = "file://%/system.db"

    spec = load_bootstrap(raw)

    assert spec.backend["database"] == "file://%/system.db"


def test_bootstrap_preserves_explicit_control_backend_tags(tmp_path):
    raw = _bootstrap(tmp_path)
    raw["backend"]["tags"] = {
        "workspace-database-namespace": False,
        "provider.bootstrap-mode": "embedded",
    }

    spec = load_bootstrap(raw)

    assert spec.backend["tags"] == raw["backend"]["tags"]


@pytest.mark.parametrize("value", (None, 0, 1, "false", [], {}))
def test_bootstrap_requires_boolean_workspace_database_namespace_tag(tmp_path, value):
    raw = _bootstrap(tmp_path)
    raw["backend"]["tags"] = {"workspace-database-namespace": value}

    with pytest.raises(ValueError, match="workspace-database-namespace.*Boolean"):
        load_bootstrap(raw)


@pytest.mark.parametrize(
    "tags",
    (
        [],
        {1: True},
        {"": True},
        {" leading": True},
        {"trailing ": True},
    ),
)
def test_bootstrap_rejects_invalid_backend_tag_mappings(tmp_path, tags):
    raw = _bootstrap(tmp_path)
    raw["backend"]["tags"] = tags

    with pytest.raises(ValueError, match="backend 'tags' must be a mapping|tag identifiers"):
        load_bootstrap(raw)


def test_bootstrap_rejects_legacy_file_uri(tmp_path):
    raw = _bootstrap(tmp_path)
    raw["backend"]["database"] = "file:%/system.db"

    with pytest.raises(ValueError, match="canonical file://"):
        load_bootstrap(raw)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("version", 2, "version must be 1"),
        ("version", True, "version must be 1"),
        ("root", "", "root must be"),
        ("workspace", "", "workspace is required"),
        ("workspace", 1, "workspace is required"),
    ),
)
def test_bootstrap_rejects_invalid_required_values(tmp_path, field, value, message):
    raw = _bootstrap(tmp_path)
    raw[field] = value

    with pytest.raises(ValueError, match=message):
        load_bootstrap(raw)


def test_bootstrap_rejects_invalid_registry_bounds(tmp_path):
    raw = _bootstrap(tmp_path)
    raw["registry"]["max_bytes"] = 0

    with pytest.raises(ValueError, match="max_bytes"):
        load_bootstrap(raw)


def test_bootstrap_materializes_registry_policy_defaults(tmp_path):
    raw = _bootstrap(tmp_path)
    raw.pop("registry")

    spec = load_bootstrap(raw)

    assert spec.registry == {"hash": "sha256", "max_items": 65536, "max_bytes": 4 * 1024 * 1024}


def test_bootstrap_merges_partial_registry_policy(tmp_path):
    raw = _bootstrap(tmp_path)
    raw["registry"] = {"max_bytes": 2048}

    spec = load_bootstrap(raw)

    assert spec.registry == {"hash": "sha256", "max_items": 65536, "max_bytes": 2048}


def test_bootstrap_preserves_explicit_unbounded_registry_policy(tmp_path):
    raw = _bootstrap(tmp_path)
    raw["registry"] = {"max_items": None, "max_bytes": None}

    spec = load_bootstrap(raw)

    assert spec.registry == {"hash": "sha256", "max_items": None, "max_bytes": None}


@pytest.mark.parametrize("value", (True, "1024", 1.0, 1.5, 0, -1))
def test_bootstrap_rejects_non_integer_registry_bounds(tmp_path, value):
    raw = _bootstrap(tmp_path)
    raw["registry"]["max_items"] = value

    with pytest.raises(ValueError, match="positive integer or null"):
        load_bootstrap(raw)


def test_bootstrap_rejects_non_mapping_registry_policy(tmp_path):
    raw = _bootstrap(tmp_path)
    raw["registry"] = []

    with pytest.raises(ValueError, match="registry must be a mapping"):
        load_bootstrap(raw)


def test_bootstrap_normalizes_required_text_fields(tmp_path):
    raw = _bootstrap(tmp_path)
    raw["root"] = f"  {tmp_path}  "
    raw["workspace"] = "  Default  "
    raw["backend"].update(type=" inmem ", module=" heavenbase.backends.inmem.backend ", name=" system ")

    spec = load_bootstrap(raw)

    assert spec.root == str(tmp_path)
    assert spec.workspace == "default"
    assert {name: spec.backend[name] for name in ("type", "module", "name")} == {
        "type": "inmem",
        "module": "heavenbase.backends.inmem.backend",
        "name": "system",
    }


def test_database_bootstrap_catalog_is_complete_and_config_manager_independent(tmp_path, monkeypatch):
    from heavenbase.database.dialect.registry import BootstrapDialectCatalog, database_dialects

    override = load_bootstrap(home_path=tmp_path / "missing-bootstrap.yaml").backend["_override"]
    catalog = BootstrapDialectCatalog(override["dialects"])
    engine = database_module.DBEngine(config=override, manager=None, dialects=catalog)
    monkeypatch.setattr(database_dialects, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ambient resolver used")))

    database = database_module.Database(database=":memory:", provider="sqlite", config_engine=engine)

    assert override["local_root"] == "file://%/database"
    assert engine.dialects is catalog
    assert database.spec.provider == "sqlite"


def test_database_bootstrap_catalog_detaches_and_freezes_input_data():
    from heavenbase.database.dialect.registry import BootstrapDialectCatalog

    definitions = {
        "embedded": {
            "requires_host": False,
            "sqlalchemy_aliases": ["embedded-alias"],
            "memory_connect_args": {"options": {"flags": ["safe"]}},
        }
    }
    catalog = BootstrapDialectCatalog(definitions)
    definitions["embedded"]["requires_host"] = True
    definitions["embedded"]["memory_connect_args"]["options"]["flags"].append("mutated")

    dialect = catalog.get("embedded-alias")

    assert dialect.requires_host is False
    assert dialect.to_dict()["memory_connect_args"] == {"options": {"flags": ["safe"]}}
    with pytest.raises(TypeError):
        catalog.snapshot()["replacement"] = dialect
    with pytest.raises(TypeError):
        dialect.memory_connect_args["replacement"] = True
    with pytest.raises(TypeError):
        dialect.memory_connect_args["options"]["replacement"] = True
