"""Integrated query extension-path tests."""

import heavenbase as hb
import pytest

from heavenbase.execution.handlers.filters import filter_handler

pytestmark = [pytest.mark.fast, pytest.mark.integration]


def _same_initial(left, value):
    """Match values whose text begins with the same letter."""
    return str(left or "")[:1].lower() == str(value)[:1].lower()


def test_registered_operation_flows_from_json_to_result_frame():
    """One custom operation should cross every read protocol without planner edits."""
    ws = hb.HeavenBase("query-execution-protocol", backends={"main": hb.backends.InMemBackend("main")})
    op = "integration_same_initial"
    hb.ext.register_op(
        op,
        aliases=("$integration_same_initial",),
        tags={"filter": True, "filter-domains": ["text"]},
    )
    hb.ext.Predicate(op, _same_initial).register()
    seen = []

    def compile_with_authority(field_schema, value, ctx):
        seen.append((ctx.resolver, ctx.config, ctx.handlers))
        return filter_handler(op)(field_schema, value, ctx)

    hb.ext.Handler(hb.ShortText, op, "inmem", hb.InlineColumn, compile_with_authority, resolver=ws.module_resolver).register(ws.handlers)

    class ProtocolDoc(hb.Entity):
        identifier = "query-execution-protocol-doc"

        title = hb.field(hb.ShortText)

    ws.register(ProtocolDoc)
    ws.upsert_many(
        ProtocolDoc,
        [
            {"object_id": "p1", "title": "Alpha"},
            {"object_id": "p2", "title": "Beta"},
        ],
    )
    query = ws.query_json(
        ProtocolDoc,
        {
            "filter": {"title": {"$integration_same_initial": "Atom"}},
            "select": ["title"],
        },
    )

    spec = query.spec
    frame = query.execute()

    assert spec.select == ("title",)
    assert spec.filter.op == op
    assert seen == [(ws.module_resolver, ws.context.config, ws.handlers)]
    assert frame.ids == ["p1"]
    assert frame.rows() == [{"object_id": "p1", "title": "Alpha"}]
