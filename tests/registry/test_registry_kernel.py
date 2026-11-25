"""Registry utility kernel, store, and extraction contracts."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
import importlib
import inspect
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

import heavenbase as hb
from heavenbase.context.registry import HeavenBaseRegistryStore
from heavenbase.utils.registry import Registry, RegistryConflictError, RegistryStore, SQLiteRegistryStore

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "heavenbase" / "utils" / "registry"


class _FakeStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, tuple[int, bytes, str]] = {}
        self.prepared = False
        self.lock = threading.Lock()

    def prepare(self) -> None:
        self.prepared = True

    def read(self, namespace: str) -> tuple[int, bytes, str] | None:
        return self.snapshots.get(namespace)

    def compare_and_set(
        self,
        namespace: str,
        *,
        expected_revision: int,
        revision: int,
        payload: bytes,
        digest: str,
    ) -> bool:
        with self.lock:
            current = self.snapshots.get(namespace)
            current_revision = 0 if current is None else current[0]
            if current_revision != expected_revision:
                return False
            self.snapshots[namespace] = revision, payload, digest
            return True


def _noop() -> None:
    pass


@dataclass
class _RegistryCase:
    registry: Registry[str, object]
    expected_revision: int
    close: Callable[[], None] = _noop


@pytest.fixture(params=("process", "fake", "sqlite", "heavenbase"))
def registry_case(request, tmp_path):
    if request.param == "process":
        case = _RegistryCase(Registry("contract"), 0)
    elif request.param == "fake":
        case = _RegistryCase(Registry.load("contract", store=_FakeStore()), 1)
    elif request.param == "sqlite":
        store = SQLiteRegistryStore(tmp_path / "registry.db")
        case = _RegistryCase(Registry.load("contract", store=store), 1, store.close)
    else:
        backend = hb.backends.InMemBackend("registry-contract", ws_id="default")
        case = _RegistryCase(Registry.load("contract", store=HeavenBaseRegistryStore(backend)), 1)
    try:
        yield case
    finally:
        case.close()


def test_process_and_all_stores_share_one_registry_contract(registry_case):
    registry = registry_case.registry

    registry.create("alpha", {"count": 1}, aliases=("a",), groups=("demo",))
    registry.update("a", derive=lambda value: {"count": value["count"] + 1})

    assert registry.revision == (2 if registry.persistent else 0)
    assert registry.get("alpha") == {"count": 2}
    assert registry.resolve("a") == "alpha"
    assert registry.list("demo") == ("alpha",)
    assert registry.aliases("alpha") == ("a",)


def test_snapshot_policy_detaches_nested_values_at_write_and_read_boundaries(registry_case):
    registry = registry_case.registry
    source = {"nested": {"values": [1]}}

    created = registry.create("item", source)
    source["nested"]["values"].append(2)
    created["nested"]["values"].append(3)
    read = registry.get("item")
    read["nested"]["values"].append(4)
    snap = registry.snapshot()
    snap["item"]["nested"]["values"].append(5)

    assert registry.get("item") == {"nested": {"values": [1]}}

    replacement = {"nested": {"values": [6]}}
    updated = registry.update("item", replacement)
    replacement["nested"]["values"].append(7)
    updated["nested"]["values"].append(8)

    assert registry.get("item") == {"nested": {"values": [6]}}


def test_failed_derive_and_batch_cannot_leak_nested_mutation(registry_case):
    registry = registry_case.registry
    registry.create("item", {"nested": {"values": [1]}})
    revision = registry.revision

    def fail_derive(current):
        current["nested"]["values"].append(2)
        raise RuntimeError("derive failed")

    with pytest.raises(RuntimeError, match="derive failed"):
        registry.update("item", derive=fail_derive)

    def fail_batch(draft):
        def change(current):
            current["nested"]["values"].append(3)
            return current

        draft.update("item", derive=change)
        raise RuntimeError("batch failed")

    with pytest.raises(RuntimeError, match="batch failed"):
        registry.mutate(fail_batch)

    assert registry.revision == revision
    assert registry.get("item") == {"nested": {"values": [1]}}


def test_persistent_writer_publishes_the_same_canonical_value_as_reloader():
    store = _FakeStore()
    writer = Registry.load(
        "canonical-values",
        store=store,
        encode_value=lambda value: {"name": value["name"].strip().lower()},
        decode_value=lambda value: {"name": value["name"]},
    )
    source = {"name": " Alpha "}

    published = writer.create("item", source, aliases=("Alias",), groups=("Group",))
    reloaded = Registry.load(
        "canonical-values",
        store=store,
        encode_value=lambda value: {"name": value["name"].strip().lower()},
        decode_value=lambda value: {"name": value["name"]},
    )

    source["name"] = "changed"
    published["name"] = "changed"
    assert writer.get("item") == {"name": "alpha"}
    assert writer.get("item") == reloaded.get("item")
    assert writer.revision == reloaded.revision == 1
    assert writer.view().fingerprint == reloaded.view().fingerprint


def test_registry_view_is_coherent_read_only_and_stable_across_publication():
    store = _FakeStore()
    registry = Registry.load("view", store=store, normalize=str.lower, normalize_alias=str.lower, normalize_group=str.lower)
    registry.create("Alpha", {"count": 1}, aliases=("A",), groups=("Core",))

    view = registry.view()
    fingerprint = store.snapshots["view"][2]
    registry.update("alpha", {"count": 2}, aliases=("First",), groups=("Next",))

    assert registry.publication == 2
    assert dict(view.items) == {"alpha": {"count": 1}}
    assert dict(view.aliases) == {"a": "alpha"}
    assert dict(view.alias_names) == {"alpha": ("A",)}
    assert dict(view.groups) == {"core": ("alpha",)}
    assert dict(view.group_names) == {"core": "Core"}
    assert view.revision == 1
    assert view.fingerprint == fingerprint
    assert view.publication == 1
    with pytest.raises(TypeError):
        view.items["other"] = {"count": 3}
    with pytest.raises(TypeError):
        view.alias_names["alpha"] = ("other",)


def test_process_identity_policy_is_explicit_and_never_persistent():
    value = {"lock": threading.Lock()}
    snapshot = Registry("snapshot")
    with pytest.raises(TypeError, match="could not snapshot write value"):
        snapshot.create("item", value)
    assert snapshot.list() == ()

    registry = Registry("identity", value_policy="process_identity")

    assert registry.create("item", value) is value
    assert registry.get("item") is value
    assert registry.view().items["item"] is value

    store = _FakeStore()
    with pytest.raises(ValueError, match="process identity cannot survive reload"):
        Registry.load("identity", store=store, value_policy="process_identity")
    assert not store.prepared


def test_process_and_all_stores_publish_batch_mutations_atomically(registry_case):
    registry = registry_case.registry

    def install(draft):
        draft.create("first", 1, aliases=("one",), groups=("bundle",))
        draft.create("second", 2, aliases=("two",), groups=("bundle",))
        with pytest.raises(ValueError, match="already registered"):
            draft.create("leaked", 3, aliases=("one",))
        return draft.list("bundle")

    assert registry.mutate(install) == ("first", "second")
    assert registry.list("bundle") == ("first", "second")
    assert registry.get("leaked") is None
    assert registry.revision == registry_case.expected_revision
    assert registry.publication == 1

    revision = registry.revision

    def invalid(draft):
        draft.create("third", 3)
        draft.create("first", 9)

    with pytest.raises(ValueError, match="already registered"):
        registry.mutate(invalid)

    assert registry.revision == revision
    assert registry.publication == 1
    assert registry.list() == ("first", "second")


@pytest.mark.anyio
async def test_process_and_all_stores_share_the_async_batch_contract(registry_case):
    registry = registry_case.registry

    result = await registry.amutate(lambda draft: (draft.create("first", 1), draft.create("second", 2)))

    assert result == (1, 2)
    assert registry.items() == (("first", 1), ("second", 2))
    assert registry.revision == registry_case.expected_revision


def test_batch_cas_conflict_preserves_every_local_record_until_refresh():
    store = _FakeStore()
    first = Registry.load("shared", store=store)
    stale = Registry.load("shared", store=store)

    first.mutate(lambda draft: (draft.create("first", 1), draft.create("second", 2)))
    with pytest.raises(RegistryConflictError, match="revision 0"):
        stale.mutate(lambda draft: (draft.create("third", 3), draft.create("fourth", 4)))

    assert stale.revision == 0
    assert stale.list() == ()
    assert stale.refresh()
    assert stale.items() == (("first", 1), ("second", 2))


@pytest.fixture(params=("fake", "sqlite", "heavenbase"))
def registry_store(request, tmp_path):
    if request.param == "fake":
        store = _FakeStore()
        close = _noop
    elif request.param == "sqlite":
        store = SQLiteRegistryStore(tmp_path / "store.db")
        close = store.close
    else:
        backend = hb.backends.InMemBackend("registry-store", ws_id="default")
        store = HeavenBaseRegistryStore(backend)
        close = _noop
    try:
        yield store
    finally:
        close()


def test_fake_sqlite_and_heavenbase_stores_share_one_cas_contract(registry_store):
    assert isinstance(registry_store, RegistryStore)
    registry_store.prepare()
    registry_store.prepare()
    assert registry_store.read("contract") is None

    assert registry_store.compare_and_set(
        "contract",
        expected_revision=0,
        revision=1,
        payload=b"first",
        digest="one",
    )
    assert not registry_store.compare_and_set(
        "contract",
        expected_revision=0,
        revision=1,
        payload=b"stale",
        digest="stale",
    )
    assert registry_store.read("contract") == (1, b"first", "one")
    assert registry_store.compare_and_set(
        "contract",
        expected_revision=1,
        revision=2,
        payload=b"second",
        digest="two",
    )
    assert registry_store.read("contract") == (2, b"second", "two")


def test_sqlite_store_observes_cross_process_conflict_and_refresh(tmp_path):
    database = tmp_path / "cross-process.db"
    store = SQLiteRegistryStore(database)
    stale = Registry.load("shared", store=store)
    script = """
import sys

sys.path.insert(0, sys.argv[1])
from heavenbase.utils.registry import Registry, SQLiteRegistryStore

store = SQLiteRegistryStore(sys.argv[2])
registry = Registry.load("shared", store=store)
registry.create("remote", 1)
store.close()
"""

    subprocess.run([sys.executable, "-I", "-c", script, str(ROOT / "src"), str(database)], cwd=tmp_path, check=True)

    with pytest.raises(RegistryConflictError, match="revision 0"):
        stale.create("local", 2)
    assert stale.refresh()
    assert stale.items() == (("remote", 1),)
    store.close()


def test_registry_kernel_imports_only_the_standard_library():
    allowed = set(sys.stdlib_module_names)
    imported: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.partition(".")[0])
    assert imported <= allowed


def test_isolated_registry_kernel_runs_registry_and_sqlite_contract(tmp_path):
    isolated = tmp_path / "isolated"
    shutil.copytree(PACKAGE, isolated / "registry")
    script = """
import pathlib
import sys

sys.path.insert(0, sys.argv[1])
from registry import Registry, SQLiteRegistryStore

assert not any(name == "heavenbase" or name.startswith("heavenbase.") for name in sys.modules)
store = SQLiteRegistryStore(":memory:")
registry = Registry.load("isolated", store=store)
registry.mutate(lambda draft: (draft.create("first", 1), draft.create("second", 2)))
assert registry.items() == (("first", 1), ("second", 2))
assert registry.revision == 1
store.close()
"""

    subprocess.run([sys.executable, "-I", "-c", script, str(isolated)], cwd=tmp_path, check=True)


def test_canonical_registry_import_initializes_only_the_registry_kernel(tmp_path):
    script = """
import sys

sys.path.insert(0, sys.argv[1])
before = set(sys.modules)
from heavenbase.utils.registry import Registry

Registry("probe")
actual = {name for name in sys.modules if name == "heavenbase" or name.startswith("heavenbase.")}
kernel = {
    "heavenbase",
    "heavenbase._lazy",
    "heavenbase.utils",
    "heavenbase.utils.registry",
    "heavenbase.utils.registry.codec",
    "heavenbase.utils.registry.errors",
    "heavenbase.utils.registry.identity",
    "heavenbase.utils.registry.registry",
    "heavenbase.utils.registry.store",
    "heavenbase.utils.registry.targets",
}
# The facade loads a kernel module only once one of its exports is read.
assert "heavenbase.utils.registry.registry" in actual and actual <= kernel, sorted(actual)
nonstdlib = {
    name.partition(".")[0]
    for name in set(sys.modules) - before
    if name.partition(".")[0] not in sys.stdlib_module_names and name.partition(".")[0] != "heavenbase"
}
assert not nonstdlib, sorted(nonstdlib)
"""

    subprocess.run([sys.executable, "-I", "-c", script, str(ROOT / "src")], cwd=tmp_path, check=True)


def test_heavenbase_reexports_the_exact_utils_registry_without_an_integration_package():
    assert hb.Registry is Registry
    assert not (ROOT / "src" / "heavenbase" / "registry").exists()


def test_pre_extraction_canonical_snapshot_bytes_remain_readable():
    payload = (
        b'{"alias_names":[[{"type":"tuple","value":[{"type":"str","value":"kind"},{"type":"str","value":"item"}]},'
        b'[{"type":"tuple","value":[{"type":"str","value":"kind"},{"type":"str","value":"alias"}]}]]],'
        b'"aliases":[[{"type":"tuple","value":[{"type":"str","value":"kind"},{"type":"str","value":"alias"}]},'
        b'{"type":"tuple","value":[{"type":"str","value":"kind"},{"type":"str","value":"item"}]}]],'
        b'"codec":1,"group_names":[[{"type":"tuple","value":[{"type":"str","value":"family"},'
        b'{"type":"str","value":"demo"}]},{"type":"tuple","value":[{"type":"str","value":"family"},'
        b'{"type":"str","value":"demo"}]}]],"groups":[[{"type":"tuple","value":[{"type":"str","value":"family"},'
        b'{"type":"str","value":"demo"}]},[{"type":"tuple","value":[{"type":"str","value":"kind"},'
        b'{"type":"str","value":"item"}]}]]],"items":[[{"type":"tuple","value":[{"type":"str","value":"kind"},'
        b'{"type":"str","value":"item"}]},{"active":true}]]}'
    )
    store = _FakeStore()
    store.snapshots["contract"] = (1, payload, "4136a2fd546ab93c32727346e66c09da7d554dd2dfc2aee46dd58f5091b58a8d")

    registry = Registry.load("contract", store=store)

    assert registry.get(("kind", "alias")) == {"active": True}
    assert registry.list(("family", "demo")) == (("kind", "item"),)


def test_removed_top_level_registry_package_has_no_compatibility_shim():
    for module_name in ("heavenbase.registry", "heavenbase.bootstrap", "heavenbase_registry"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)
    parameters = inspect.signature(Registry.load).parameters
    assert "backend" not in parameters
    assert parameters["store"].kind is inspect.Parameter.KEYWORD_ONLY
