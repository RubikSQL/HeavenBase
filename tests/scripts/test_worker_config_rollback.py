"""Regression tests for the worker's per-test configuration rollback."""

import pytest

from heavenbase.utils import CM_HVNB

pytestmark = pytest.mark.fast


@pytest.fixture(scope="module", autouse=True)
def worker_config_unchanged():
    # Module teardown follows the per-test rollback, even if xdist assigns
    # only one case to this worker or the two cases run in reverse order.
    scopes = CM_HVNB.scopes()
    history = CM_HVNB.history(scope=CM_HVNB.base_scope)
    yield
    assert CM_HVNB.scopes() == scopes
    assert CM_HVNB.history(scope=CM_HVNB.base_scope) == history


def test_worker_config_rollback_removes_new_scope():
    scope = f"{CM_HVNB.base_scope}.worker-config-rollback"
    assert scope not in CM_HVNB.scopes()
    assert CM_HVNB.set("heavenbase.prompt.lang", "fr", scope=scope)
    assert scope in CM_HVNB.scopes()


def test_worker_config_rollback_restores_base_scope_history():
    scope = CM_HVNB.base_scope
    key = "heavenbase.llm.default_preset"
    saved = CM_HVNB.get(key)
    before = CM_HVNB.history(scope=scope)
    assert saved == "mock"
    assert CM_HVNB.set(key, "system", scope=scope)
    assert CM_HVNB.set(key, saved, scope=scope)
    assert CM_HVNB.get(key) == saved
    assert CM_HVNB.history(scope=scope) != before
