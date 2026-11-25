import heavenbase as hb
import pytest

from heavenbase.context.bootstrap import load_bootstrap
from heavenbase.utils.config import ConfigManager
from heavenbase.workspace import WorkspaceSpec

pytestmark = pytest.mark.fast


def context_at(root):
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": load_bootstrap(home_path=root / "no-bootstrap.yaml").backend,
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=True),
    )


def test_unregister_refreshes_a_workspace_added_by_another_context(tmp_path):
    first = context_at(tmp_path)
    second = context_at(tmp_path)
    try:
        assert WorkspaceSpec.unregister("r9ws-late", context=first) is False
        WorkspaceSpec.register("r9ws-late", context=second)
        assert WorkspaceSpec.load("r9ws-late", context=second).id == "r9ws-late"
        removed = WorkspaceSpec.unregister("r9ws-late", context=first)
        third = context_at(tmp_path)
        try:
            assert (removed, "r9ws-late" in {item.id for item in WorkspaceSpec.list(context=third)}) == (True, False)
        finally:
            third.close()
    finally:
        second.close()
        first.close()
