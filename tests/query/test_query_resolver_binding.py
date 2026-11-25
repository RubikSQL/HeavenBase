"""Workspace-owned resolver binding for Python and JSON query intents."""

from typing import Any, Callable, Mapping

import pytest

import heavenbase as hb
from heavenbase.filters import Filter
from heavenbase.filters.ops import register_op
from heavenbase.modules import ModuleService
from heavenbase.query.aggregate import register_aggregate_reducer
from heavenbase.query import Query
from heavenbase.query.json import load as load_json_query
from heavenbase.utils.config import ConfigManager

pytestmark = [pytest.mark.fast, pytest.mark.unit]

_FILTER_ALIAS = "$context_filter"
_REDUCER_ALIAS = "$context_reducer"


class _ResolverDoc(hb.Entity):
    """Entity used to compare isolated query-authority bindings."""

    identifier = "query-resolver-doc"

    value = hb.field(hb.Integer)


class _QueryWorkspace:
    """Expose the workspace authority needed by a query value."""

    def __init__(self, context: hb.Context) -> None:
        self.context = context

    @property
    def module_resolver(self) -> ModuleService:
        """Return the Context-owned module resolver."""
        return self.context.modules()


def _context(root: Any) -> hb.Context:
    """Create one isolated Context with an in-memory control Backend."""
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {
                "type": "inmem",
                "module": "heavenbase.backends.inmem.backend",
                "name": "system",
            },
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(default={}, setup=False),
    )


def _first_fold(values: list[int], factor: int = 1) -> int:
    """Return a distinguishable first-context reduction."""
    return sum(values) * factor


def _second_fold(values: list[int], factor: int = 1) -> int:
    """Return a distinguishable second-context reduction."""
    return sum(values) * factor + 1


def _increment_factor(options: dict[str, Any]) -> dict[str, Any]:
    """Expose duplicate reducer binding by changing each explicit factor."""
    return {**options, "factor": options["factor"] + 1}


def _register_filter(service: ModuleService, identifier: str) -> None:
    """Register one context-local canonical operation behind a shared alias."""
    register_op(identifier, aliases=(_FILTER_ALIAS,), tags={"filter": True}, resolver=service)


def _register_reducer(
    service: ModuleService,
    identifier: str,
    fold: Callable[..., Any],
    *,
    normalize_options: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> None:
    """Register one context-local reducer behind a shared operation alias."""
    register_op(identifier, aliases=(_REDUCER_ALIAS,), tags={"aggregate": True}, resolver=service)
    register_aggregate_reducer(identifier, fold, normalize_options=normalize_options, resolver=service)


def test_python_and_json_filter_trees_bind_through_the_workspace_context(tmp_path: Any) -> None:
    """One raw filter tree should canonicalize independently and match JSON."""
    first = _context(tmp_path / "first")
    second = _context(tmp_path / "second")
    try:
        _register_filter(first.modules(), "context_filter_first")
        _register_filter(second.modules(), "context_filter_second")
        raw = _ResolverDoc.value.op(_FILTER_ALIAS, 1) & ~_ResolverDoc.value.op(_FILTER_ALIAS, 2)
        json = {
            "filter": {
                "$and": [
                    {"value": {_FILTER_ALIAS: 1}},
                    {"$not": {"value": {_FILTER_ALIAS: 2}}},
                ]
            }
        }

        first_python = Query(_QueryWorkspace(first), _ResolverDoc).where(raw).spec
        second_python = Query(_QueryWorkspace(second), _ResolverDoc).where(raw).spec
        first_json = load_json_query(_QueryWorkspace(first), _ResolverDoc, json).spec
        second_json = load_json_query(_QueryWorkspace(second), _ResolverDoc, json).spec

        assert raw.to_dict()["and"][0]["filter"]["op"] == _FILTER_ALIAS
        assert first_python.filter.to_dict() == first_json.filter.to_dict()
        assert second_python.filter.to_dict() == second_json.filter.to_dict()
        assert first_python.filter.to_dict()["and"][0]["filter"]["op"] == "context_filter_first"
        assert second_python.filter.to_dict()["and"][0]["filter"]["op"] == "context_filter_second"
    finally:
        first.close()
        second.close()


def test_python_and_json_reducers_and_having_bind_through_the_workspace_context(tmp_path: Any) -> None:
    """Reducer aliases, positional args, and HAVING should share one authority."""
    first = _context(tmp_path / "first-reducer")
    second = _context(tmp_path / "second-reducer")
    try:
        _register_filter(first.modules(), "context_having_first")
        _register_filter(second.modules(), "context_having_second")
        _register_reducer(first.modules(), "context_reducer_first", _first_fold, normalize_options=_increment_factor)
        _register_reducer(second.modules(), "context_reducer_second", _second_fold, normalize_options=_increment_factor)
        raw_metric = _ResolverDoc.value.aggregate(_REDUCER_ALIAS, 3)

        first_python = Query(_QueryWorkspace(first), _ResolverDoc).aggregate(total=raw_metric).having(Filter("total", _FILTER_ALIAS, 0)).spec
        second_python = Query(_QueryWorkspace(second), _ResolverDoc).aggregate(total=raw_metric).having(Filter("total", _FILTER_ALIAS, 0)).spec
        json = {
            "aggregate": {"total": {_REDUCER_ALIAS: {"field": "value", "factor": 3}}},
            "having": {"total": {_FILTER_ALIAS: 0}},
        }
        first_json = load_json_query(_QueryWorkspace(first), _ResolverDoc, json).spec
        second_json = load_json_query(_QueryWorkspace(second), _ResolverDoc, json).spec

        assert raw_metric.op == _REDUCER_ALIAS
        assert raw_metric.args == (3,)
        assert first_python.aggregate.to_dict() == first_json.aggregate.to_dict()
        assert second_python.aggregate.to_dict() == second_json.aggregate.to_dict()
        assert first_python.aggregate.metrics["total"].op == "context_reducer_first"
        assert second_python.aggregate.metrics["total"].op == "context_reducer_second"
        assert dict(first_python.aggregate.metrics["total"].options) == {"factor": 4}
        assert first_python.aggregate.metrics["total"].args == ()
        assert first_python.having.op == first_json.having.op == "context_having_first"
        assert second_python.having.op == second_json.having.op == "context_having_second"
    finally:
        first.close()
        second.close()
