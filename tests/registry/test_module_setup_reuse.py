"""A workspace construction reuses unchanged packaged-module readings, and notices every change.

Module setup runs on every workspace construction, including each hidden-store
open and each rebuild after a drop. It keeps its per-process readings of the
package folders, the store captures it verified, and the handler seeding while
their inputs are unchanged. It still checks the module-set marker against the
Registry on every call.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import time

import pytest

import heavenbase as hb
from heavenbase.modules import ModuleService
from heavenbase.modules import artifacts as module_artifacts
from heavenbase.modules import discovery
from heavenbase.modules.artifacts import ModuleArtifactStore, SourceUnavailable
from heavenbase.utils.config import ConfigManager
from heavenbase.utils.registry import Registry

pytestmark = pytest.mark.fast


class ReuseNote(hb.Entity):
    identifier = "module-setup-reuse-note"
    title = hb.field(hb.ShortText)


_WARM: dict[int, object] = {}


@pytest.fixture(scope="module")
def shared_context(tmp_path_factory):
    """One Context whose module set and handler seeding are warm, shared to keep these tests fast."""
    context = _context(tmp_path_factory.mktemp("module-setup-reuse"))
    _WARM[id(context)] = hb.HeavenBase("reuse-warm", context=context, detached=True, backends={"main": {"type": "inmem"}})
    yield context
    _WARM.pop(id(context), None)
    context.close()


def _context(root: Path) -> hb.Context:
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 4 * 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )


class _Counter:
    """Count the calls of one module-level function while delegating to it."""

    def __init__(self, monkeypatch, owner, name):
        self.calls = 0
        original = getattr(owner, name)

        def counted(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)


def _package(root: Path, value: str) -> Path:
    """Write a package with one packaged module folder that uses a path source."""
    (root / "resources").mkdir(parents=True)
    (root / "resources" / "modules.yaml").write_text("schema: heaven.modules/v1\nroots: [modules]\n", encoding="utf-8")
    folder = root / "modules" / "fixture"
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text("", encoding="utf-8")
    (folder / "value.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
    (folder / "meta.yaml").write_text(
        "manifest_version: 2\ncoordinate: heavenbase/reuse-fixture\nversion: 1.0.0\ncompatibility: {}\nitems:\n"
        "  - kind: reuse_value\n    identifier: fixture\n    name: Fixture\n    desc: Packaged path source.\n"
        "    source: path\n    target: {module: value, qualname: VALUE}\n    active: true\n    meta: {schema_version: 1}\n",
        encoding="utf-8",
    )
    return folder


def _use_package(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(discovery.resources, "files", lambda package_name: root)


def _settle_now(monkeypatch) -> None:
    """Let files written by this test count as settled, so the test need not wait two seconds."""
    try:
        signature = importlib.import_module("heavenbase.modules._signature")
    except ModuleNotFoundError:  # a version that reads every folder on every setup
        return
    monkeypatch.setattr(signature, "_RECENT_NS", 0)


@pytest.mark.slow
def test_a_current_module_set_is_not_read_again_by_the_next_setup(shared_context, tmp_path, monkeypatch):
    # The readings belong to the process: another test's forced setup drops them all, and a package
    # file changed within the last two seconds is read every time.
    _settle_now(monkeypatch)
    shared_context._setup_modules()
    descriptors = _Counter(monkeypatch, discovery, "_mapping")
    folders = _Counter(monkeypatch, module_artifacts, "_read_tree_once")

    assert shared_context._setup_modules() is False
    assert (descriptors.calls, folders.calls) == (0, 0)

    # A new Context has its own module Registry and capture store: the marker
    # check sets it up, from the readings this process already has.
    second = _context(tmp_path / "second")
    try:
        assert second._setup_modules() is True
        assert descriptors.calls == 0
        captured = {path.name for path in (tmp_path / "second" / "artifacts" / "modules" / "objects" / "sha256").glob("*/*")}
        digests = {row["record"]["target"]["artifact"] for row in second.modules().inspect() if row["record"]["source"] == "artifact"}
        assert digests and digests <= captured
        assert second._setup_modules() is False
    finally:
        second.close()


def test_a_changed_packaged_module_folder_is_set_up_again(tmp_path, monkeypatch):
    _settle_now(monkeypatch)
    package = tmp_path / "package"
    folder = _package(package, "first")
    _use_package(monkeypatch, package)
    service = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(tmp_path / "artifacts"))

    assert service.setup() is True
    assert service.resolve("reuse_value", "fixture") == "first"
    descriptors = _Counter(monkeypatch, discovery, "_mapping")
    folders = _Counter(monkeypatch, module_artifacts, "_read_tree_once")
    assert service.setup() is False
    assert (descriptors.calls, folders.calls) == (0, 0)

    (folder / "value.py").write_text("VALUE = 'second, a longer value'\n", encoding="utf-8")
    assert service.setup() is True
    assert descriptors.calls > 0 and folders.calls > 0
    assert service.resolve("reuse_value", "fixture") == "second, a longer value"

    (folder / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")
    assert service.setup() is True
    assert service.setup() is False


def test_a_same_size_rewrite_within_the_timestamp_window_is_noticed(tmp_path, monkeypatch):
    package = tmp_path / "package"
    folder = _package(package, "aaaa")
    _use_package(monkeypatch, package)
    service = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(tmp_path / "artifacts"))
    assert service.setup() is True

    # Same size, and the modification time put back: only the change time and
    # the recent-write rule tell the two contents apart.
    stat = (folder / "value.py").stat()
    (folder / "value.py").write_text("VALUE = 'bbbb'\n", encoding="utf-8")
    os.utime(folder / "value.py", ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert service.setup() is True
    assert service.resolve("reuse_value", "fixture") == "bbbb"


def test_a_new_registry_or_a_forced_setup_still_sets_the_modules_up(tmp_path, monkeypatch):
    _settle_now(monkeypatch)
    package = tmp_path / "package"
    _package(package, "value")
    _use_package(monkeypatch, package)
    store = ModuleArtifactStore(tmp_path / "artifacts")
    assert ModuleService(Registry("system-modules"), artifacts=store).setup() is True

    # A Registry without the module-set marker, as after a drop of its database.
    fresh = ModuleService(Registry("system-modules"), artifacts=store)
    assert fresh.setup() is True
    assert fresh.resolve("reuse_value", "fixture") == "value"

    descriptors = _Counter(monkeypatch, discovery, "_mapping")
    assert fresh.setup(force=True) is True
    assert descriptors.calls == 2  # resources/modules.yaml and the one meta.yaml


def test_a_damaged_capture_is_reported_by_the_next_setup(tmp_path, monkeypatch):
    _settle_now(monkeypatch)
    package = tmp_path / "package"
    _package(package, "value")
    _use_package(monkeypatch, package)
    store_root = tmp_path / "artifacts"
    service = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(store_root))
    assert service.setup() is True

    stored = next(store_root.glob("objects/sha256/*/*/root/value.py"))
    stored.chmod(0o644)
    stored.write_text("VALUE = 'tampered'\n", encoding="utf-8")

    with pytest.raises(SourceUnavailable, match="digest mismatch"):
        ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(store_root)).setup()


@pytest.mark.slow
def test_handler_seeding_is_reused_until_the_catalog_publishes(shared_context, monkeypatch):
    from heavenbase.execution.handlers import seed

    resolver = shared_context.modules()
    seeded = seed.seed_handlers(resolver=resolver)
    seedings = _Counter(monkeypatch, seed, "_handler_seed_context")

    again = seed.seed_handlers(resolver=resolver)
    assert seedings.calls == 0
    assert again is not seeded
    assert again.capabilities() == seeded.capabilities()

    def compile_eq(field_schema, value, ctx):
        raise AssertionError("not compiled in this test")

    handler = hb.ext.Handler(hb.ShortText, "eq", "inmem", hb.InlineColumn, compile_eq, resolver=resolver)
    again.register(handler, replace=True)
    assert again.get(hb.ShortText, "eq", "inmem", hb.InlineColumn) is handler
    assert seeded.get(hb.ShortText, "eq", "inmem", hb.InlineColumn) is not handler
    assert seed.seed_handlers(resolver=resolver).get(hb.ShortText, "eq", "inmem", hb.InlineColumn) is not handler

    resolver.register(
        "reuse_marker",
        "seeding",
        {
            "name": "Marker",
            "desc": "Publishes once.",
            "source": "inline",
            "target": "definition",
            "active": True,
            "meta": {"schema_version": 1, "definition": {}},
        },
        coordinate="example/reuse-seeding",
    )
    seed.seed_handlers(resolver=resolver)
    assert seedings.calls == 1


def test_an_entity_registration_does_not_rebuild_unchanged_registry_rows(shared_context, monkeypatch):
    from heavenbase.workspace.metaschema import projection

    notes = _WARM[id(shared_context)]
    notes._metaschema.publish_workspace_rows()
    rows = _Counter(monkeypatch, projection._MetaSchemaRegistryRows, "_module_row")
    notes.register(ReuseNote)
    assert rows.calls == 0

    # A handler registration publishes a new handler snapshot, so the rows are built again.
    def compile_eq(field_schema, value, ctx):
        raise AssertionError("not compiled in this test")

    hb.ext.Handler(hb.ShortText, "eq", "inmem", hb.InlineColumn, compile_eq, resolver=notes.module_resolver).register(notes.handlers, replace=True)
    notes._metaschema.publish_workspace_rows()
    assert rows.calls > 0
    built = rows.calls
    notes._metaschema.publish_workspace_rows()
    assert rows.calls == built


@pytest.mark.slow
def test_a_handler_shared_into_another_context_does_not_keep_that_context_alive(shared_context, tmp_path):
    import gc
    import weakref

    from heavenbase.execution.handlers import seed
    from heavenbase.execution.handlers.registry import HandlerRegistry

    key = (hb.ShortText, "eq", "inmem", hb.InlineColumn)
    keeper = shared_context.modules()
    shared = seed.seed_handlers(resolver=keeper).get(*key)
    other = _context(tmp_path / "other")
    other._setup_modules()
    target = HandlerRegistry(resolver=other.modules())

    # Registering rebinds the descriptor itself, which belongs to one seeding only.
    registered = target.register(shared, replace=True)
    assert registered is shared and target.get(*key) is registered
    assert registered._module_resolver is other.modules()
    later = seed.seed_handlers(resolver=keeper).get(*key)
    assert later is not shared and later._module_resolver is keeper

    # So does any other change to a descriptor.
    later.compile = None
    assert callable(seed.seed_handlers(resolver=keeper).get(*key).compile)

    other_ref = weakref.ref(other)
    modules_ref = weakref.ref(other.modules())
    other.close()
    del other, target, registered, shared
    gc.collect()
    assert modules_ref() is None
    assert other_ref() is None


def test_a_same_size_rewrite_with_its_time_put_back_changes_the_fingerprint(tmp_path):
    from heavenbase.modules._signature import interpreter_cache, tree_signature

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "value.py").write_text("VALUE = 'aaaa'\n", encoding="utf-8")
    stat = (folder / "value.py").stat()
    before = tree_signature(folder, ignore=interpreter_cache, settled=False, contents=lambda name: True)
    time.sleep(0.05)  # past the file system's change-time tick, as the settle window ensures
    (folder / "value.py").write_text("VALUE = 'bbbb'\n", encoding="utf-8")
    os.utime(folder / "value.py", ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert tree_signature(folder, ignore=interpreter_cache, settled=False, contents=lambda name: True) != before


def test_windows_stats_still_notice_a_same_size_rewrite_after_the_window(tmp_path, monkeypatch):
    """Windows stats keep no change time, file ID or volume in a directory entry; descriptor and module bytes still count."""
    from types import SimpleNamespace

    signature = importlib.import_module("heavenbase.modules._signature")
    _settle_now(monkeypatch)

    def windows_stat(entry):
        info = os.stat(entry.path)
        return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_mtime_ns=info.st_mtime_ns, st_ctime_ns=0, st_ino=0, st_dev=0)

    monkeypatch.setattr(signature, "_WINDOWS", True)
    monkeypatch.setattr(signature, "_entry_stat", windows_stat)
    package = tmp_path / "package"
    folder = _package(package, "aaaa")
    _use_package(monkeypatch, package)
    service = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(tmp_path / "artifacts"))
    assert service.setup() is True
    assert service.setup() is False

    for name, text in (("value.py", "VALUE = 'bbbb'\n"), ("meta.yaml", None)):
        path = folder / name
        stat = path.stat()
        content = path.read_text(encoding="utf-8")
        path.write_text(text if text is not None else content.replace("Fixture", "Fixturf"), encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert service.setup() is True
    assert service.resolve("reuse_value", "fixture") == "bbbb"
    assert service.inspect("reuse_value", "fixture")[0]["record"]["name"] == "Fixturf"
