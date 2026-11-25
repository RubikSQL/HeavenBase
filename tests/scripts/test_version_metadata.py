"""Package-version projection and release metadata gates."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import runpy
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNC = runpy.run_path(str(ROOT / "scripts" / "sync-env.py"))
RELEASE = runpy.run_path(str(ROOT / "scripts" / "release.py"))
pytestmark = pytest.mark.fast


def _project(root: Path, version: str = "0.1.2.3dev1") -> tuple[Path, Path, str]:
    """Create one stale project-metadata fixture."""
    pyproject = root / "pyproject.toml"
    pyproject.write_text('[project]\nname = "fixture"\n\n[tool.poetry]\nversion = "0.1.2.2"\ndescription = "kept"\n', encoding="utf-8")
    package = root / "src" / "heavenbase"
    package.mkdir(parents=True)
    (package / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    manifest = package / "feature" / "meta.yaml"
    manifest.parent.mkdir()
    body = (
        "items:\n"
        "  - kind: fixture\n"
        "    identifier: example\n"
        "    meta:\n"
        "      schema_version: 1\n"
        "      definition:\n"
        "        version: author-owned\n"
    )
    manifest.write_text(
        "manifest_version: 2\n" "coordinate: heavenbase/feature\n" "version: 0.1.2.2\n" "compatibility: {heavenbase: {min: 0.1.2.2, before: 0.1.3.0}}\n" + body,
        encoding="utf-8",
    )
    return pyproject, manifest, body


def _release_project(root: Path, version: str) -> None:
    package = root / "src" / "heavenbase"
    package.mkdir(parents=True)
    (package / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def _release_artifacts(dist: Path, version: str, payloads: dict[str, bytes]) -> None:
    dist.mkdir()
    required = {
        "heavenbase/resources/modules.yaml": b"modules: []\n",
        "heavenbase/backends/elasticsearch/README.md": b"# Elasticsearch\n",
    }
    gui = {
        "host/HeavenBase.icns": b"icns",
        "host/macos.js": b"function run() {}\n",
        "host/macos_app.js": b"function run() {}\n",
        "ui/index.html": b"<!doctype html>\n",
        "ui/styles.css": b":root {}\n",
        "ui/js/api.js": b"export const api = {};\n",
        "ui/js/app.js": b'import { api } from "./api.js";\n',
    }
    openapi = b'{"openapi":"3.1.0"}\n'
    with zipfile.ZipFile(dist / f"heavenbase-{version}-py3-none-any.whl", "w") as archive:
        for name, content in {**payloads, **required}.items():
            archive.writestr(name, content)
        for name, content in gui.items():
            archive.writestr(f"heavenbase-{version}.data/data/share/heavenbase/gui/{name}", content)
        archive.writestr(f"heavenbase-{version}.data/data/share/heavenbase/openapi/dashboard-openapi.json", openapi)
    with tarfile.open(dist / f"heavenbase-{version}.tar.gz", "w:gz") as archive:
        for name, content in {**payloads, **required}.items():
            info = tarfile.TarInfo(f"heavenbase-{version}/src/{name}")
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
        for name, content in gui.items():
            info = tarfile.TarInfo(f"heavenbase-{version}/apps/gui/{name}")
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
        info = tarfile.TarInfo(f"heavenbase-{version}/docs/resources/reference/dashboard-openapi.json")
        info.size = len(openapi)
        archive.addfile(info, BytesIO(openapi))


@pytest.mark.parametrize(
    ("version", "before"),
    (("0.1.2.3dev0", "0.1.3.0"), ("0.1.2.3", "0.1.3.0"), ("1.9.9.0dev12", "1.9.10.0")),
)
def test_compatibility_projection_uses_the_next_three_part_train(version, before):
    assert SYNC["_compatibility_before"](version) == before


def test_version_projection_preserves_author_owned_manifest_content(tmp_path):
    pyproject, manifest, body = _project(tmp_path)

    projection = SYNC["version_metadata_projection"](tmp_path, "0.1.2.3dev1")

    assert projection[pyproject] == '[project]\nname = "fixture"\n\n[tool.poetry]\nversion = "0.1.2.3dev1"\ndescription = "kept"\n'
    assert projection[manifest] == (
        "manifest_version: 2\n"
        "coordinate: heavenbase/feature\n"
        "version: 0.1.2.3dev1\n"
        "compatibility: {heavenbase: {min: 0.1.2.3dev1, before: 0.1.3.0}}\n" + body
    )
    assert "version: 0.1.2.2" in manifest.read_text(encoding="utf-8")


def test_sync_reports_drift_without_writing_then_updates_all_projections(tmp_path):
    pyproject, manifest, _ = _project(tmp_path)

    assert set(SYNC["sync_version_metadata"](tmp_path, check=True)) == {
        "pyproject.toml",
        "src/heavenbase/feature/meta.yaml",
    }
    assert 'version = "0.1.2.2"' in pyproject.read_text(encoding="utf-8")
    assert "version: 0.1.2.2" in manifest.read_text(encoding="utf-8")

    SYNC["sync_version_metadata"](tmp_path)

    assert SYNC["sync_version_metadata"](tmp_path, check=True) == ()
    assert 'version = "0.1.2.3dev1"' in pyproject.read_text(encoding="utf-8")
    assert "version: 0.1.2.3dev1" in manifest.read_text(encoding="utf-8")


def test_projection_rejects_duplicate_builtin_coordinates(tmp_path):
    _, manifest, _ = _project(tmp_path)
    duplicate = manifest.parent.parent / "other" / "meta.yaml"
    duplicate.parent.mkdir()
    duplicate.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ValueError, match="coordinate .* is repeated"):
        SYNC["version_metadata_projection"](tmp_path, "0.1.2.3dev1")


def test_repository_version_projections_are_current():
    assert SYNC["sync_version_metadata"](ROOT, check=True) == ()


def test_release_artifact_version_preserves_dev_source_and_normalizes_filename(monkeypatch, tmp_path):
    _release_project(tmp_path, "0.1.2.3dev1")
    monkeypatch.setitem(RELEASE["source_artifact_version"].__globals__, "ROOT", str(tmp_path))

    source_text, artifact_version = RELEASE["source_artifact_version"]()

    assert source_text == "0.1.2.3dev1"
    assert str(artifact_version) == "0.1.2.3.dev1"
    with pytest.raises(SystemExit, match="stable four-part"):
        RELEASE["source_version"]()


def test_release_artifact_version_rejects_noncanonical_dev_source(monkeypatch, tmp_path):
    _release_project(tmp_path, "0.1.2.03dev01")
    monkeypatch.setitem(RELEASE["source_artifact_version"].__globals__, "ROOT", str(tmp_path))

    with pytest.raises(SystemExit, match="must be canonical"):
        RELEASE["source_artifact_version"]()


def test_release_module_gate_uses_normalized_dev_artifact_names_and_raw_descriptors(monkeypatch, tmp_path):
    _release_project(tmp_path, "0.1.2.3dev1")
    payloads = {"heavenbase/feature/meta.yaml": b"version: 0.1.2.3dev1\n"}
    _release_artifacts(tmp_path / "dist", "0.1.2.3.dev1", payloads)
    projected_versions = []
    globals_ = RELEASE["check_modules"].__globals__
    monkeypatch.setitem(globals_, "ROOT", str(tmp_path))
    monkeypatch.setitem(globals_, "_source_module_payloads", lambda version: projected_versions.append(version) or payloads)

    RELEASE["check_modules"](SimpleNamespace(dist="dist"))

    assert projected_versions == ["0.1.2.3dev1"]


def test_release_payload_gate_rejects_missing_and_changed_descriptors():
    source = {"heavenbase/feature/meta.yaml": b"source"}

    with pytest.raises(SystemExit, match="missing=.*feature/meta.yaml"):
        RELEASE["_verify_module_payloads"]("wheel", source, {})
    with pytest.raises(SystemExit, match="content differs"):
        RELEASE["_verify_module_payloads"]("wheel", source, {"heavenbase/feature/meta.yaml": b"changed"})


def test_release_inventory_rejects_every_retired_package_owner():
    retired = {
        "heavenbase/registry/__init__.py",
        "heavenbase/storage/rule.py",
        "heavenbase/utils/db/config.py",
        "heavenbase/utils/dialect/spec.py",
        "heavenbase/utils/llm/base.py",
        "heavenbase/utils/cmd.py",
        "heavenbase/utils/lazy.py",
        "heavenbase/extensions/system/extension.py",
        "heavenbase/workspace/_bootstrap.py",
        "heavenbase_registry/__init__.py",
        "heavenbase/provider/registration.yaml",
        "heavenbase/resources/registry/backend/meta.yaml",
    }

    assert set(RELEASE["_retired_package_paths"]({*retired, "heavenbase/current.py"})) == retired
    assert RELEASE["_retired_package_paths"]({"heavenbase/workspace/__init__.py", "heavenbase/workspace/__init__.pyi"}) == []
