"""MCP ``describe_entity`` advertises a field's filter operation only when the query compiler accepts it."""

import pytest

import heavenbase as hb
from heavenbase.utils import path_to_file_uri

pytestmark = pytest.mark.fast

# A representative value for each operation on the two field types the gram option changes.
_LABELS_VALUES = {"eq": ["work"], "ne": ["work"], "in": [["work"]], "array_contains": "work", "contained_in": "notes about work today"}
_TITLE_VALUES = {
    "eq": "work",
    "ne": "work",
    "lt": "work",
    "lte": "work",
    "gt": "work",
    "gte": "work",
    "in": ["work"],
    "match": "work",
    "like": "wo%",
    "ilike": "wo%",
    "wildcard": "wo*",
    "regex": "^wo",
    "contains": "or",
    "starts_with": "wo",
    "ends_with": "rk",
    "contained_in": "notes about work today",
}


def _definition(entity_id, gram):
    return {
        "entity_id": entity_id,
        "fields": {"object_id": {"type": "identifier", "pk": True}, "title": {"type": "short-text"}, "labels": {"type": "array[short-text]"}},
        "gram": gram,
    }


@pytest.fixture(params=["sqlite", "inmem"])
def toolkit(request, tmp_path):
    config = {"type": "sqlite", "database": path_to_file_uri(tmp_path / "ops.db")} if request.param == "sqlite" else {"type": "inmem"}
    ws = hb.HeavenBase("mcp-describe-filter-ops", detached=True, backends={"main": config})
    try:
        toolkit = ws.to_mcp(name="mcp-describe-filter-ops")
        for entity_id, gram in (("tagged-on", True), ("tagged-off", False)):
            toolkit.run("define_entity", entity=_definition(entity_id, gram))
            toolkit.run("upsert", entity=entity_id, row={"object_id": "n1", "title": "work", "labels": ["work"]})
        yield toolkit
    finally:
        ws.release()


def _registered_by_another_test(op):
    """Whether ``op`` was registered through ``register_op`` (by another test in this process), not shipped built in."""
    found = hb.DEFAULT_CONTEXT.modules().inspect("operation", op)
    if not found:
        return False
    installation = (found[0]["record"].get("meta") or {}).get("installation") or {}
    return str(installation.get("coordinate", "")).startswith("programmatic/operation/")


def _filter_steps(plan):
    if isinstance(plan, dict):
        if plan.get("node") == "filter":
            yield plan
        for value in plan.values():
            yield from _filter_steps(value)
    elif isinstance(plan, list):
        for item in plan:
            yield from _filter_steps(item)


@pytest.mark.slow
def test_gram_false_entity_does_not_advertise_contained_in_on_an_array_of_short_text(toolkit):
    described = toolkit.run("describe_entity", entity="tagged-off")

    assert "contained_in" not in described["filter_ops"]["labels"]
    assert "array_contains" in described["filter_ops"]["labels"]
    assert "contained_in" in described["filter_ops"]["title"]  # a scalar ShortText field needs no keyword index
    with pytest.raises(RuntimeError, match="gram=False"):
        toolkit.run("query", entity="tagged-off", spec={"filter": {"labels": {"$contained_in": "notes about work today"}}})


@pytest.mark.slow
def test_gram_true_entity_still_advertises_and_runs_contained_in(toolkit):
    described = toolkit.run("describe_entity", entity="tagged-on")

    assert "contained_in" in described["filter_ops"]["labels"]
    frame = toolkit.run("query", entity="tagged-on", spec={"filter": {"labels": {"$contained_in": "notes about work today"}}})
    assert [row["object_id"] for row in frame["rows"]] == ["n1"]


@pytest.mark.slow
@pytest.mark.parametrize("entity_id", ["tagged-on", "tagged-off"])
def test_every_advertised_operation_compiles(toolkit, entity_id):
    described = toolkit.run("describe_entity", entity=entity_id)
    for field, values, expected in (("labels", _LABELS_VALUES, {"array_contains"}), ("title", _TITLE_VALUES, {"eq", "contains"})):
        # An operation another test registered in this process through register_op is not this test's subject; every
        # other advertised operation needs a sample value here.
        checked = [op for op in described["filter_ops"][field] if not _registered_by_another_test(op)]
        assert expected <= set(checked), (field, described["filter_ops"][field])
        for op in checked:
            assert op in values, f"no sample value for {field}.{op}"
            plan = toolkit.run("explain", entity=entity_id, spec={"filter": {field: {f"${op}": values[op]}}})
            steps = list(_filter_steps(plan))
            assert steps, plan
            assert all(step.get("unsupported_reason") is None for step in steps), (field, op, steps)
