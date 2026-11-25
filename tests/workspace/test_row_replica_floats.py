"""Replicated floats that are not finite agree when both routes hold the same value."""

import math

import pytest

import heavenbase as hb
from heavenbase.execution.contracts import RowOp

pytestmark = pytest.mark.fast


@pytest.fixture
def routes():
    class Route(hb.Entity):
        identifier = "replicated-route"

        cost = hb.field(hb.Float).store(to="main").store(to="mirror").optional()

    ws = hb.HeavenBase("replicated-route", detached=True, backends={"main": {"type": "inmem"}, "mirror": {"type": "inmem"}})
    try:
        ws.register(Route, catalog="none")
        yield ws, Route
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("cost", [math.inf, -math.inf, 12.5])
def test_equal_replicas_of_a_float_read_back(routes, cost):
    ws, Route = routes
    ws.upsert(Route, {"object_id": "unreachable", "cost": cost})
    assert ws.get("unreachable", entity=Route)["cost"] == cost


def test_equal_nan_replicas_read_back(routes):
    ws, Route = routes
    ws.upsert(Route, {"object_id": "unknown", "cost": math.nan})
    assert math.isnan(ws.get("unknown", entity=Route)["cost"])


@pytest.mark.slow
@pytest.mark.parametrize(("stored", "replica"), [(math.inf, -math.inf), (math.inf, 1e308), (math.nan, 0.0), (math.nan, math.inf)])
def test_different_non_finite_replicas_still_conflict(routes, stored, replica):
    ws, Route = routes
    ws.upsert(Route, {"object_id": "diverged", "cost": stored})
    ws.backends.get("mirror").upsert([RowOp(Route.identifier, "diverged", {"cost": replica})])
    with pytest.raises(ValueError, match="not unique across routed backends"):
        ws.get("diverged", entity=Route)
