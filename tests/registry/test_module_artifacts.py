"""Focused contracts for content-addressed local module artifacts."""

from concurrent.futures import ThreadPoolExecutor
import builtins
import inspect
from pathlib import Path
import os
import re
import shutil
import sys
import threading
from types import ModuleType
import zipfile

import pytest

import heavenbase.modules.artifacts as artifact_runtime
from heavenbase.modules.artifacts import ModuleArtifact, ModuleArtifactStore, SourceUnavailable

pytestmark = pytest.mark.fast


def _package(root: Path, *, init: str = "", meta: bytes = b"manifest: [intentionally invalid") -> Path:
    root.mkdir()
    (root / "__init__.py").write_text(init, encoding="utf-8")
    (root / "meta.yaml").write_bytes(meta)
    return root


def _make_writable(path: Path) -> None:
    path.chmod(0o644)


def test_capture_is_byte_only_content_addressed_and_domain_framed(tmp_path):
    source = _package(tmp_path / "source")
    (source / "nested").mkdir()
    (source / "nested" / "__init__.py").write_text("", encoding="utf-8")
    (source / "nested" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "resources").mkdir()
    (source / "resources" / "payload.bin").write_bytes(b"a\x00b")
    (source / "empty").mkdir()
    store = ModuleArtifactStore(tmp_path / "store")

    first = store.capture(source)
    relocated = shutil.copytree(source, tmp_path / "renamed")
    second = store.capture(relocated)

    assert isinstance(first, ModuleArtifact)
    assert re.fullmatch(r"[0-9a-f]{64}", first.digest)
    assert first == second
    assert ModuleArtifactStore(store.root).get(first.digest) == first
    assert first.root != source
    assert (first.root / "meta.yaml").read_bytes() == b"manifest: [intentionally invalid"
    assert (first.root / "nested" / "helper.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (first.root / "resources" / "payload.bin").read_bytes() == b"a\x00b"
    assert (first.root / "empty").is_dir()

    changed_resource = shutil.copytree(source, tmp_path / "changed-resource")
    (changed_resource / "resources" / "payload.bin").write_bytes(b"a\x00c")
    changed_path = shutil.copytree(source, tmp_path / "changed-path")
    (changed_path / "nested" / "helper.py").rename(changed_path / "nested" / "renamed.py")
    changed_type = shutil.copytree(source, tmp_path / "changed-type")
    (changed_type / "empty").rmdir()
    (changed_type / "empty").write_bytes(b"")

    assert store.capture(changed_resource).digest != first.digest
    assert store.capture(changed_path).digest != first.digest
    assert store.capture(changed_type).digest != first.digest

    with pytest.raises(SourceUnavailable, match="unavailable"):
        store.get("0" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        store.get("invalid")


def test_capture_trusted_nonfilesystem_resource_uses_the_same_artifact_bytes(tmp_path):
    archive_path = tmp_path / "module.zip"
    meta = b"manifest: [intentionally invalid"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("portable/", b"")
        archive.writestr("portable/__init__.py", b"")
        archive.writestr("portable/meta.yaml", meta)
        archive.writestr("portable/runtime.py", b"VALUE = 42\n")
    store = ModuleArtifactStore(tmp_path / "store")
    filesystem = _package(tmp_path / "filesystem", meta=meta)
    (filesystem / "runtime.py").write_bytes(b"VALUE = 42\n")

    with zipfile.ZipFile(archive_path) as archive:
        resource = zipfile.Path(archive, at="portable/")
        assert not isinstance(resource, Path)
        captured = store.capture_resource(resource)

    assert captured.digest == store.capture(filesystem).digest
    assert store.resolve(captured, "runtime", "VALUE", coordinate="example/resource", generation=1) == 42


def test_capture_rejects_symlinks_and_special_files_but_ignores_interpreter_caches(tmp_path):
    store = ModuleArtifactStore(tmp_path / "store")
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")

    linked = _package(tmp_path / "linked")
    (linked / "escape.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        store.capture(linked)

    bytecode = _package(tmp_path / "bytecode")
    (bytecode / "cached.pyc").write_bytes(b"not bytecode")
    cached_digest = store.capture(bytecode).digest
    (bytecode / "cached.pyc").unlink()
    assert store.capture(bytecode).digest == cached_digest

    cache_folder = _package(tmp_path / "cache-folder")
    (cache_folder / "__pycache__").mkdir()
    (cache_folder / "__pycache__" / "runtime.pyc").write_bytes(b"not bytecode")
    cached_digest = store.capture(cache_folder).digest
    (cache_folder / "__pycache__" / "runtime.pyc").unlink()
    (cache_folder / "__pycache__").rmdir()
    assert store.capture(cache_folder).digest == cached_digest

    if hasattr(os, "mkfifo"):
        special = _package(tmp_path / "special")
        os.mkfifo(special / "pipe")
        with pytest.raises(ValueError, match="special file"):
            store.capture(special)


def test_capture_requires_regular_root_protocol_files(tmp_path):
    store = ModuleArtifactStore(tmp_path / "store")
    missing_meta = tmp_path / "missing-meta"
    missing_meta.mkdir()
    (missing_meta / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="regular meta.yaml"):
        store.capture(missing_meta)

    linked_init = tmp_path / "linked-init"
    linked_init.mkdir()
    (linked_init / "real.py").write_text("", encoding="utf-8")
    (linked_init / "__init__.py").symlink_to(linked_init / "real.py")
    (linked_init / "meta.yaml").write_text("invalid: [", encoding="utf-8")
    with pytest.raises(ValueError, match="symlink"):
        store.capture(linked_init)


def test_capture_fails_closed_when_nested_traversal_errors(tmp_path, monkeypatch):
    source = _package(tmp_path / "source")
    blocked = source / "blocked"
    blocked.mkdir()
    (blocked / "hidden.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / "store")
    original_scandir = os.scandir

    def deny_blocked(path):
        # Windows captures scan through extended-length (\\?\) paths, so compare the directory, not its spelling.
        if os.path.samefile(path, blocked):
            raise PermissionError("blocked traversal")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_blocked)

    with pytest.raises(ValueError, match="source changed repeatedly") as caught:
        store.capture(source)

    assert isinstance(caught.value.__cause__, artifact_runtime._SourceChanged)
    assert isinstance(caught.value.__cause__.__cause__, PermissionError)
    assert not store.root.exists()


def _remove_after_its_parent_is_listed(folder, files, monkeypatch):
    # Every walk finds `folder` freshly made, and a cleanup removes it right after
    # the walk listed its parent, before the walk would enter it.
    real_walk = os.walk

    def walk(top, *args, **kwargs):
        folder.mkdir(exist_ok=True)
        for name, content in files.items():
            (folder / name).write_bytes(content)
        for current, dir_names, file_names in real_walk(top, *args, **kwargs):
            yield current, dir_names, file_names
            if folder.exists() and os.path.samefile(current, folder.parent):
                shutil.rmtree(folder)

    monkeypatch.setattr(os, "walk", walk)


def test_capture_ignores_a_cache_folder_that_a_cleanup_removes_during_the_scan(tmp_path, monkeypatch):
    source = _package(tmp_path / "source")
    (source / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / "store")
    clean = store.capture(source)
    _remove_after_its_parent_is_listed(source / "__pycache__", {"helper.cpython-312.pyc": b"cached bytecode"}, monkeypatch)

    assert store.capture(source).digest == clean.digest


def test_capture_still_fails_when_a_source_folder_is_removed_during_the_scan(tmp_path, monkeypatch):
    source = _package(tmp_path / "source")
    store = ModuleArtifactStore(tmp_path / "store")
    _remove_after_its_parent_is_listed(source / "reports", {"summary.py": b"TOTAL = 3\n"}, monkeypatch)

    with pytest.raises(ValueError, match="source changed repeatedly") as caught:
        store.capture(source)

    assert isinstance(caught.value.__cause__, artifact_runtime._SourceChanged)
    assert not store.root.exists()


def test_capture_retries_until_two_content_scans_agree(tmp_path, monkeypatch):
    source = _package(tmp_path / "rewritten")
    payload = source / "payload.py"
    payload.write_bytes(b"A" * 64)
    store = ModuleArtifactStore(tmp_path / "store")
    original_read_tree_once = artifact_runtime._read_tree_once
    scans = 0

    def rewrite_after_first_scan(root):
        nonlocal scans
        tree = original_read_tree_once(root)
        if Path(root).resolve() == source.resolve():
            scans += 1
            if scans == 1:
                payload.write_bytes(b"B" * 64)
        return tree

    monkeypatch.setattr(artifact_runtime, "_read_tree_once", rewrite_after_first_scan)

    artifact = store.capture(source)

    assert scans == 3
    assert (artifact.root / "payload.py").read_bytes() == b"B" * 64


def test_capture_requires_two_matching_whole_tree_scans(tmp_path, monkeypatch):
    source = _package(tmp_path / "tree-generation")
    first = source / "first.py"
    second = source / "second.py"
    first.write_text("GENERATION = 'old'\n", encoding="utf-8")
    second.write_text("GENERATION = 'old'\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / "store")
    original_read_tree_once = artifact_runtime._read_tree_once
    scans = 0

    def replace_generation_after_first_scan(root):
        nonlocal scans
        tree = original_read_tree_once(root)
        if Path(root).resolve() == source.resolve():
            scans += 1
            if scans == 1:
                first.write_text("GENERATION = 'new'\n", encoding="utf-8")
                second.write_text("GENERATION = 'new'\n", encoding="utf-8")
        return tree

    monkeypatch.setattr(artifact_runtime, "_read_tree_once", replace_generation_after_first_scan)

    artifact = store.capture(source)

    assert scans == 3
    assert (artifact.root / "first.py").read_text(encoding="utf-8") == "GENERATION = 'new'\n"
    assert (artifact.root / "second.py").read_text(encoding="utf-8") == "GENERATION = 'new'\n"


def test_capture_rejects_a_source_that_changes_on_every_attempt(tmp_path, monkeypatch):
    source = _package(tmp_path / "unstable")
    payload = source / "payload.py"
    payload.write_bytes(b"A" * 64)
    store = ModuleArtifactStore(tmp_path / "store")
    original_read_tree_once = artifact_runtime._read_tree_once
    flips = 0

    def flip_after_each_scan(root):
        nonlocal flips
        tree = original_read_tree_once(root)
        if Path(root).resolve() == source.resolve():
            flips += 1
            payload.write_bytes((b"A" if flips % 2 == 0 else b"B") * 64)
        return tree

    monkeypatch.setattr(artifact_runtime, "_read_tree_once", flip_after_each_scan)

    with pytest.raises(ValueError, match="source changed repeatedly"):
        store.capture(source)

    assert flips == 3
    assert not store.root.exists()


def test_load_supports_nested_relative_imports_without_public_import_state(tmp_path, monkeypatch):
    source = _package(tmp_path / "logical_demo", init="ROOT = 'private'\n")
    (source / "shared.py").write_text("VALUE = 41\n", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "__init__.py").write_text("from .helper import build\n", encoding="utf-8")
    (source / "nested" / "helper.py").write_text("from ..shared import VALUE\n\ndef build():\n    return VALUE + 1\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)
    public = ModuleType("logical_demo")
    public.ROOT = "public"
    monkeypatch.setitem(sys.modules, "logical_demo", public)
    before = tuple(sys.path)

    build = store.resolve(artifact, "nested.helper", "build", coordinate="publisher/demo", generation=1)
    nested = store.load(artifact, "nested", coordinate="publisher/demo", generation=1)

    assert build() == 42
    assert nested.build is build
    assert nested.__name__.startswith("_heavenbase_artifact_")
    assert len(nested.__name__.split(".", 1)[0]) == len("_heavenbase_artifact_") + 64
    assert sys.modules["logical_demo"] is public
    assert tuple(sys.path) == before


def test_captures_load_beyond_the_windows_path_limit(tmp_path):
    """The content-addressed layout must not fail where a capture path passes 260 characters."""
    source = _package(tmp_path / "deep_demo")
    (source / "nested").mkdir()
    (source / "nested" / "__init__.py").write_text("", encoding="utf-8")
    (source / "nested" / "helper.py").write_text("def build():\n    return 42\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / ("store-" + "x" * 150))

    artifact = store.capture(source)

    assert len(str(artifact.root / "nested" / "helper.py")) > 260
    assert ModuleArtifactStore(store.root).get(artifact.digest) == artifact
    assert store.resolve(artifact, "nested.helper", "build", coordinate="publisher/deep", generation=1)() == 42


def test_loaded_artifact_source_remains_inspectable_for_nested_callbacks(tmp_path):
    source = _package(tmp_path / "inspectable")
    (source / "factory.py").write_text(
        "def build():\n" "    def callback(value: int) -> int:\n" "        return value + 1\n" "    return callback\n",
        encoding="utf-8",
    )
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)

    callback = store.resolve(artifact, "factory", "build", coordinate="publisher/inspectable", generation=1)()

    assert callback(1) == 2
    assert "def callback(value: int) -> int:" in inspect.getsource(callback)
    assert callback.__code__.co_filename == f"heavenbase-artifact://{artifact.digest}/factory.py"


def test_loaded_packages_expose_virtual_locations_and_verified_resources(tmp_path):
    source = _package(
        tmp_path / "resources-package",
        init=("from importlib.resources import files\n" "ROOT_PAYLOAD = files(__package__).joinpath('root.txt').read_text(encoding='utf-8')\n"),
    )
    (source / "root.txt").write_text("verified root", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "__init__.py").write_text(
        "from importlib.resources import files\nNESTED_PAYLOAD = files(__package__).joinpath('payload.bin').read_bytes()\n",
        encoding="utf-8",
    )
    (source / "nested" / "payload.bin").write_bytes(b"verified nested")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)

    root = store.load(artifact, coordinate="publisher/resources", generation=1)
    nested = store.load(artifact, "nested", coordinate="publisher/resources", generation=1)

    assert root.ROOT_PAYLOAD == "verified root"
    assert nested.NESTED_PAYLOAD == b"verified nested"
    assert root.__file__ == f"heavenbase-artifact://{artifact.digest}/__init__.py"
    assert nested.__file__ == f"heavenbase-artifact://{artifact.digest}/nested/__init__.py"
    assert str(source) not in root.__file__
    assert str(artifact.root) not in root.__file__

    captured_payload = artifact.root / "root.txt"
    _make_writable(captured_payload)
    captured_payload.write_text("corrupt", encoding="utf-8")
    from importlib.resources import files

    assert files(root).joinpath("root.txt").read_text(encoding="utf-8") == "verified root"


def test_coordinates_and_generations_isolate_the_same_logical_package(tmp_path):
    source = _package(tmp_path / "same_name", init="from .value import marker\n")
    (source / "value.py").write_text("marker = object()\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)

    first = store.load(artifact, coordinate="publisher-a/same", generation=1)
    second = store.load(artifact, coordinate="publisher-b/same", generation=1)
    upgraded = store.load(artifact, coordinate="publisher-a/same", generation=2)
    again = store.load(artifact, coordinate="publisher-a/same", generation=1)

    assert first is again
    assert len({first.__name__, second.__name__, upgraded.__name__}) == 3
    assert len({id(first.marker), id(second.marker), id(upgraded.marker)}) == 3
    assert "same_name" not in sys.modules


def test_runtime_generation_collection_waits_for_every_owner_and_preserves_disk_capture(tmp_path):
    source = _package(tmp_path / "leased", init="from .nested import VALUE\n")
    (source / "nested.py").write_text("VALUE = object()\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)

    class Owner:
        pass

    first_owner = Owner()
    second_owner = Owner()
    first = store.load(artifact, coordinate="publisher/leased", generation=1, lifecycle_owner=first_owner)
    second = store.load(artifact, coordinate="publisher/leased", generation=1, lifecycle_owner=second_owner)
    namespace = first.__name__

    assert first is second
    assert namespace in sys.modules
    assert f"{namespace}.nested" in sys.modules
    assert store.release_unreferenced((), lifecycle_owner=first_owner) == ()
    assert namespace in sys.modules

    assert store.release_unreferenced((), lifecycle_owner=second_owner) == (namespace,)
    assert not any(name == namespace or name.startswith(f"{namespace}.") for name in sys.modules)
    assert first.VALUE is second.VALUE
    assert artifact.root.is_dir()


def test_failed_first_import_releases_its_runtime_generation(tmp_path):
    source = _package(tmp_path / "failed", init="from .nested import VALUE\nraise RuntimeError('failed import')\n")
    (source / "nested.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)
    namespace = artifact_runtime._namespace("publisher/failed", 1, artifact.digest)

    with pytest.raises(RuntimeError, match="failed import"):
        store.load(artifact, coordinate="publisher/failed", generation=1)

    assert namespace not in artifact_runtime._FINDER._generations
    assert not any(name == namespace or name.startswith(f"{namespace}.") for name in sys.modules)


def test_concurrent_load_executes_one_generation_once(tmp_path, monkeypatch):
    counter = f"_heavenbase_artifact_counter_{tmp_path.name.replace('-', '_')}"
    source = _package(
        tmp_path / "concurrent",
        init=("import builtins\n" "import time\n" "time.sleep(0.05)\n" f"builtins.{counter} += 1\n" "token = object()\n"),
    )
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)
    monkeypatch.setattr(builtins, counter, 0, raising=False)
    barrier = threading.Barrier(12)

    def load() -> ModuleType:
        barrier.wait()
        return store.load(artifact, coordinate=f"test/{tmp_path.name}", generation="install-1")

    with ThreadPoolExecutor(max_workers=12) as executor:
        modules = tuple(executor.map(lambda _: load(), range(12)))

    assert len({id(module) for module in modules}) == 1
    assert len({id(module.token) for module in modules}) == 1
    assert getattr(builtins, counter) == 1


def test_lazy_relative_imports_use_verified_memory_after_capture_changes(tmp_path):
    source = _package(
        tmp_path / "lazy-package",
        init="def later():\n    from .lazy import VALUE\n    return VALUE\n",
    )
    (source / "lazy.py").write_text("VALUE = 'verified'\n", encoding="utf-8")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)
    later = store.resolve(artifact, None, "later", coordinate="publisher/lazy", generation=1)
    captured_lazy = artifact.root / "lazy.py"

    _make_writable(captured_lazy)
    captured_lazy.write_text("VALUE = 'corrupt'\n", encoding="utf-8")

    assert later() == "verified"
    with pytest.raises(SourceUnavailable, match="digest mismatch"):
        store.load(artifact, coordinate="publisher/lazy", generation=1)


def test_corruption_is_detected_before_execution(tmp_path, monkeypatch):
    counter = f"_heavenbase_artifact_preexec_{tmp_path.name.replace('-', '_')}"
    source = _package(tmp_path / "corrupt", init=f"import builtins\nbuiltins.{counter} += 1\n")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)
    monkeypatch.setattr(builtins, counter, 0, raising=False)
    captured_init = artifact.root / "__init__.py"
    _make_writable(captured_init)
    captured_init.write_text(f"import builtins\nbuiltins.{counter} += 100\n", encoding="utf-8")

    with pytest.raises(SourceUnavailable, match="digest mismatch"):
        store.load(artifact, coordinate="publisher/corrupt", generation=1)
    assert getattr(builtins, counter) == 0


def test_module_exceptions_are_not_reported_as_source_unavailability(tmp_path):
    source = _package(tmp_path / "raises", init="raise FileNotFoundError('raised by module code')\n")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)

    with pytest.raises(FileNotFoundError, match="raised by module code") as raised:
        store.load(artifact, coordinate="publisher/raises", generation=1)
    assert not isinstance(raised.value, SourceUnavailable)


def test_native_modules_fail_without_executing_the_root_package(tmp_path, monkeypatch):
    counter = f"_heavenbase_artifact_native_{tmp_path.name.replace('-', '_')}"
    source = _package(tmp_path / "native-package", init=f"import builtins\nbuiltins.{counter} += 1\n")
    (source / "native.so").write_bytes(b"not a native library")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)
    monkeypatch.setattr(builtins, counter, 0, raising=False)

    with pytest.raises(ImportError, match="unsupported native loader"):
        store.load(artifact, "native", coordinate="publisher/native", generation=1)
    assert getattr(builtins, counter) == 0


def test_forged_capture_paths_cannot_escape_the_store(tmp_path):
    source = _package(tmp_path / "source")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)
    forged = ModuleArtifact(artifact.digest, source)

    with pytest.raises(SourceUnavailable, match="escapes"):
        store.load(forged, coordinate="publisher/forged", generation=1)

    prefix = artifact.root.parent.parent
    relocated = tmp_path / "relocated-prefix"
    prefix.rename(relocated)
    prefix.symlink_to(relocated, target_is_directory=True)
    with pytest.raises(SourceUnavailable, match="regular directory"):
        store.load(artifact, coordinate="publisher/forged", generation=1)
