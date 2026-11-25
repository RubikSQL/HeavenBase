"""LanceDB peer-commit coverage with the separate pylance package absent."""

from importlib import import_module
from importlib.machinery import PathFinder
from importlib.util import find_spec
import sys

import pytest


@pytest.mark.fast
def test_lancedb_peer_commit_regression_can_run_without_pylance(monkeypatch, tmp_path):
    pytest.importorskip("lancedb")

    class NoPyLanceFinder:
        @staticmethod
        def find_spec(fullname, path=None, target=None):
            if fullname == "lance":
                return None
            return PathFinder.find_spec(fullname, path, target)

    monkeypatch.setattr(sys, "meta_path", [NoPyLanceFinder if finder is PathFinder else finder for finder in sys.meta_path])
    monkeypatch.delitem(sys.modules, "lance", raising=False)
    monkeypatch.delitem(sys.modules, "test_native_backends", raising=False)
    assert find_spec("lance") is None
    native_tests = import_module("test_native_backends")
    peer_commit = native_tests.test_lance_failed_native_upsert_preserves_other_clients_commits
    peer_commit(tmp_path, monkeypatch)
    assert not any(mark.args[0] for mark in peer_commit.pytestmark if mark.name == "skipif")
