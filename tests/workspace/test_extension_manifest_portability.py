"""An include_system manifest imports under another HOME and from 0.1.2.2.

Extension code loaded from the Registry runs in a package whose name differs per
HOME and Context root, so the agent extension's lambda derives export a reference
that names another package than the importing workspace's. The import compares
such references without that package name; it cannot compile a lambda derive.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast

# Exported by HeavenBase 0.1.2.2 (master c8976704) from a detached in-memory workspace with the memory extension.
_MANIFEST_0122 = Path(__file__).resolve().parents[1] / "fixtures" / "manifests" / "heavenbase-0122-memory-include-system.json"


def _derive_refs(manifest, entity_id):
    entity = next(item for item in manifest["entities"] if item["entity_id"] == entity_id)
    return [compute["fn"] for derive in entity.get("derives", []) for compute in derive["computes"]]


def _export(path):
    ws = hb.HeavenBase("portable-manifest-source", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension("memory")
        path.write_text(json.dumps(ws.to_manifest(include_system=True)), encoding="utf-8")
    finally:
        ws.release()


def _import(data):
    restored = hb.HeavenBase.from_manifest(data, detached=True)
    try:
        exported = restored.to_manifest(include_system=True)
        assert {"agent-agent", "agent-session", "memory-note"} <= {item["entity_id"] for item in exported["entities"]}
        # The import ran where extension code loads under another package name.
        assert _derive_refs(exported, "agent-agent") != _derive_refs(data, "agent-agent")
        restored.upsert("memory-note", {"object_id": "n1", "text": "Moved with its manifest"})
        assert restored.get("n1", entity="memory-note")["text"] == "Moved with its manifest"
    finally:
        restored.release()


@pytest.mark.slow
def test_an_include_system_manifest_exported_under_another_home_imports(home_env, tmp_path):
    path = tmp_path / "exported.json"
    subprocess.run([sys.executable, str(Path(__file__).resolve()), str(path)], env=home_env(tmp_path / "other-home"), check=True)
    _import(json.loads(path.read_text(encoding="utf-8")))


def test_an_include_system_manifest_exported_by_0_1_2_2_imports():
    _import(json.loads(_MANIFEST_0122.read_text(encoding="utf-8")))


if __name__ == "__main__":
    _export(Path(sys.argv[1]))
