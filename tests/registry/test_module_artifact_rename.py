"""Publishing a capture survives a transient Windows rename failure, and only that."""

from __future__ import annotations

import os

import pytest

from heavenbase.modules import artifacts as module_artifacts
from heavenbase.modules.artifacts import ModuleArtifactStore

pytestmark = pytest.mark.fast

_REAL_RENAME = os.rename


def _folder(tmp_path):
    folder = tmp_path / "module"
    folder.mkdir()
    (folder / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (folder / "meta.yaml").write_text("manifest_version: 2\n", encoding="utf-8")
    return folder


def _denied(winerror):
    error = PermissionError(13, "Access is denied")
    error.winerror = winerror
    return error


def _rename(monkeypatch, behaviour):
    calls = []

    def rename(source, target):
        calls.append(target)
        behaviour(len(calls), source, target)

    monkeypatch.setattr(module_artifacts.os, "rename", rename)
    monkeypatch.setattr(module_artifacts, "_RENAME_FIRST_DELAY", 0.0)
    return calls


@pytest.mark.parametrize("winerror", [5, 32])
def test_a_transient_windows_rename_failure_is_retried(tmp_path, monkeypatch, winerror):
    monkeypatch.setattr(module_artifacts, "_WINDOWS", True)

    def behaviour(call, source, target):
        if call <= 2:
            raise _denied(winerror)
        _REAL_RENAME(source, target)

    calls = _rename(monkeypatch, behaviour)
    artifact = ModuleArtifactStore(tmp_path / "store").capture(_folder(tmp_path))

    assert len(calls) == 3
    assert (artifact.root / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not list((tmp_path / "store").glob(".capture-*"))


def test_a_lasting_windows_rename_failure_still_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(module_artifacts, "_WINDOWS", True)

    def behaviour(call, source, target):
        raise _denied(5)

    calls = _rename(monkeypatch, behaviour)
    with pytest.raises(PermissionError):
        ModuleArtifactStore(tmp_path / "store").capture(_folder(tmp_path))
    assert len(calls) == module_artifacts._RENAME_ATTEMPTS


@pytest.mark.parametrize("windows, winerror", [(False, 5), (True, 2)])
def test_other_rename_failures_are_not_retried(tmp_path, monkeypatch, windows, winerror):
    monkeypatch.setattr(module_artifacts, "_WINDOWS", windows)

    def behaviour(call, source, target):
        raise _denied(winerror)

    calls = _rename(monkeypatch, behaviour)
    with pytest.raises(PermissionError):
        ModuleArtifactStore(tmp_path / "store").capture(_folder(tmp_path))
    assert len(calls) == 1


def test_a_capture_another_process_published_meanwhile_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(module_artifacts, "_WINDOWS", True)
    folder = _folder(tmp_path)
    store = tmp_path / "store"

    def behaviour(call, source, target):
        # Another process publishes the same object, then this rename is refused.
        _REAL_RENAME(source, target)
        raise _denied(5)

    calls = _rename(monkeypatch, behaviour)
    artifact = ModuleArtifactStore(store).capture(folder)

    assert len(calls) == 1
    assert (artifact.root / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def _discard(monkeypatch, behaviour):
    calls = []
    real = module_artifacts._discard

    def discard(path):
        calls.append(path)
        behaviour(len(calls), real, path)

    monkeypatch.setattr(module_artifacts, "_discard", discard)
    monkeypatch.setattr(module_artifacts, "_RENAME_FIRST_DELAY", 0.0)
    return calls


@pytest.mark.parametrize("winerror", [5, 32, 145])
def test_a_transient_windows_cleanup_failure_is_retried(tmp_path, monkeypatch, winerror):
    monkeypatch.setattr(module_artifacts, "_WINDOWS", True)

    def behaviour(call, real, path):
        if call <= 2:
            raise _denied(winerror)
        real(path)

    calls = _discard(monkeypatch, behaviour)
    artifact = ModuleArtifactStore(tmp_path / "store").capture(_folder(tmp_path))

    assert len(calls) == 3
    assert (artifact.root / "__init__.py").is_file()
    assert not list((tmp_path / "store").glob(".capture-*"))


@pytest.mark.parametrize("windows", [True, False])
def test_a_cleanup_that_keeps_failing_never_fails_the_capture(tmp_path, monkeypatch, windows):
    monkeypatch.setattr(module_artifacts, "_WINDOWS", windows)
    warnings = []
    monkeypatch.setattr(module_artifacts._logger, "warning", lambda message, *args: warnings.append(message % args))

    def behaviour(call, real, path):
        raise _denied(32)

    calls = _discard(monkeypatch, behaviour)
    artifact = ModuleArtifactStore(tmp_path / "store").capture(_folder(tmp_path))

    assert len(calls) == (module_artifacts._RENAME_ATTEMPTS if windows else 1)
    assert (artifact.root / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert len(warnings) == 1 and "temporary module capture folder" in warnings[0]


def test_a_failing_cleanup_does_not_hide_the_capture_error(tmp_path, monkeypatch):
    monkeypatch.setattr(module_artifacts, "_WINDOWS", True)

    def refuse(call, source, target):
        raise _denied(2)

    def behaviour(call, real, path):
        raise _denied(32)

    _rename(monkeypatch, refuse)
    _discard(monkeypatch, behaviour)
    with pytest.raises(PermissionError) as raised:
        ModuleArtifactStore(tmp_path / "store").capture(_folder(tmp_path))
    assert raised.value.winerror == 2
