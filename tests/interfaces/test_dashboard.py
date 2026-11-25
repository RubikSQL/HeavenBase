"""Dashboard GUI API and HTTP route contracts."""

import asyncio
from datetime import datetime
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
import inspect
import json
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import threading
import time
import warnings

import pytest

import heavenbase as hb
from heavenbase.context.registry import HeavenBaseRegistryStore
from heavenbase.api import DashboardAPI, build_app, openapi_schema
from heavenbase.workspace import WorkspaceSpec
from heavenbase.utils import CM_HVNB, cmd, get_file_dir, path_to_file_uri, pj
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


def _store(backend):
    return HeavenBaseRegistryStore(backend)


def _cm(tmp_path) -> ConfigManager:
    backend = hb.backends.SQLiteBackend("dashboard-config", path_to_file_uri(tmp_path / "config.db"), ws_id="dashboard-config")
    registry = hb.Registry.load("dashboard-test-config", store=_store(backend))
    return ConfigManager(package="heavenbase", scope="heavenbase", root=str(tmp_path), registry=registry, refresh_interval=999.0)


def _context(tmp_path) -> hb.Context:
    return hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=_cm(tmp_path),
    )


def _demo_workspace_module():
    path = pj(get_file_dir(get_file_dir(get_file_dir(__file__))), "scripts", "demo_workspace.py", abs=True)
    spec = spec_from_file_location("heavenbase_demo_workspace", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DashPaper(hb.Entity):
    object_id: str = hb.field(hb.Identifier).pk()
    title: str = hb.field(hb.ShortText).desc("Paper title")
    year: int = hb.field(hb.Integer).desc("Publication year")
    meta: dict = hb.field(hb.Json).desc("Open metadata")


@pytest.fixture()
def dash_ws():
    ws = hb.HeavenBase("dash-test", backends={"main": {"type": "inmem"}})
    WorkspaceSpec.register("dash-test", config={"backends": {"main": {"type": "inmem"}}})
    ws.register(DashPaper)
    ws.upsert_many(DashPaper, [{"object_id": f"p{i:02d}", "title": f"Paper {i}", "year": 2020 + (i % 4), "meta": {"idx": i}} for i in range(12)])
    yield ws
    hb.HeavenBase.drop("dash-test")
    WorkspaceSpec.unregister("dash-test")


def test_column_stats_examples_never_quote_a_prefix_as_the_value():
    """An example cut for display must say so, and two long values must not collapse into one."""

    class LongPaper(hb.Entity):
        identifier = "dash-long-paper"

        body = hb.field(hb.LongText)

    ws = hb.HeavenBase("dash-long-test", backends={"main": {"type": "inmem"}})
    WorkspaceSpec.register("dash-long-test", config={"backends": {"main": {"type": "inmem"}}})
    ws.register(LongPaper)
    shared = "x" * 200  # a prefix both rows share, longer than the display cut
    ws.upsert_many(LongPaper, [{"object_id": "l1", "body": f"{shared}alpha"}, {"object_id": "l2", "body": f"{shared}beta"}])
    api = DashboardAPI()
    api.open("dash-long-test")
    try:
        examples = api.column_stats("dash-long-paper", "body")["examples"]
        # both survive: dedup keys on the whole value, not on the shown prefix
        assert len(examples) == 2
        assert {example["value_len"] for example in examples} == {len(shared) + 5, len(shared) + 4}
        for example in examples:
            assert example["value_truncated"] is True
            assert len(example["value"]) < example["value_len"]
    finally:
        hb.HeavenBase.drop("dash-long-test")
        WorkspaceSpec.unregister("dash-long-test")


def test_config_tree_origins(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    tree = api.config_tree()
    assert tree["origins"]["heavenbase.dashboard.port"] == "builtin"
    api.config_set("heavenbase.dashboard.port", 43210)
    api.config_set("heavenbase.llm.providers.myprov.api_key", "${env:MY_KEY}")
    tree = api.config_tree()
    assert tree["origins"]["heavenbase.dashboard.port"] == "override"
    assert tree["origins"]["heavenbase.llm.providers.myprov.api_key"] == "custom"
    assert tree["tree"]["heavenbase"]["llm"]["providers"]["myprov"]["api_key"] == "${env:MY_KEY}"
    meta = api.config_meta()
    assert meta["scope"] == "heavenbase" and "heavenbase" in meta["scopes"]
    assert meta["root"] == str(tmp_path) and "database" not in meta


def test_config_set_unset_reset(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    with pytest.raises(ValueError):
        api.config_unset("heavenbase.dashboard.port")
    with pytest.raises(ValueError):
        api.config_set("", 1)
    api.config_set("heavenbase.dashboard.port", 43210)
    assert api.config_reset("heavenbase.dashboard.port")["origin"] == "builtin"
    assert api.config_tree()["tree"]["heavenbase"]["dashboard"]["port"] == 42777
    api.config_set("heavenbase.llm.providers.myprov", {"api_key": "k"})
    assert api.config_reset("heavenbase.llm.providers.myprov")["origin"] == "custom"
    assert "myprov" not in api.config_tree()["tree"]["heavenbase"]["llm"]["providers"]
    # Deleting a custom key under a branch absent from defaults must not leak a tombstone.
    api.config_set("heavenbase.custom.x", 1)
    api.config_unset("heavenbase.custom.x")
    tree = api.config_tree()
    assert "__HB_REMOVE__" not in repr(tree["tree"])
    assert not any("__HB_REMOVE__" in key for key in tree["origins"])
    assert "x" not in tree["tree"]["heavenbase"].get("custom", {})


def test_config_history_versions_and_safe_diff(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.port", 43210)
    history = api.config_history(limit=10)
    assert history and history[0]["ver"] >= 1
    assert isinstance(api.config_diff(version_a=-1)["diff"], str)

    # A retained version without a scope must inspect the active scope, not the latest merged chain.
    oldest = history[-1]["ver"]
    tree = api.config_tree(version=oldest)
    assert tree["version"] == oldest
    assert tree["tree"]["heavenbase"]["dashboard"]["port"] != 43210

    api.config_set("heavenbase.custom.codex_probe", 1)
    api.config_reset("heavenbase.custom.codex_probe")
    diff = api.config_diff(version_a=-2)["diff"]
    assert "__HB_REMOVE__" not in diff
    assert "-        codex_probe: 1" in diff


def test_dashboard_preferences_are_shared_and_validated(tmp_path):
    from starlette.testclient import TestClient

    context = _context(tmp_path)
    api = DashboardAPI(context=context)
    client = TestClient(build_app(api), base_url="http://localhost")
    try:
        assert client.get("/api/preferences").json() == {
            "theme": "theme-light",
            "language": "en",
            "font": "default",
            "font_scale": "default",
            "configured": False,
        }

        saved = client.post("/api/preferences", json={"theme": "theme-anthropic", "font_scale": "large"})
        assert saved.status_code == 200
        assert saved.json() == {
            "theme": "theme-anthropic",
            "language": "en",
            "font": "default",
            "font_scale": "large",
            "configured": True,
        }
        assert DashboardAPI(context=context).preferences() == saved.json()
        assert client.post("/api/preferences", json={"theme": "sepia"}).status_code == 400

        # A hand-edited invalid config value cannot break the response contract.
        context.config.set("heavenbase.dashboard.preferences.theme", "invalid", scope=context.config.base_scope)
        assert client.get("/api/preferences").json()["theme"] == "theme-light"
        context.config.set("heavenbase.dashboard.preferences", "invalid", scope=context.config.base_scope)
        assert client.get("/api/preferences").json()["configured"] is False
    finally:
        context.close()


def test_dashboard_open_persists_workspace_selection(tmp_path):
    context = _context(tmp_path)
    try:
        WorkspaceSpec.register("remembered", config={"backends": {"main": {"type": "inmem"}}}, context=context)
        api = DashboardAPI(context=context)

        assert api.open("remembered")["ws_id"] == "remembered"
        assert context.active_workspace_id == "remembered"
        assert context.config.get("heavenbase.registry.active.workspace") == "remembered"
        assert any(item["id"] == "remembered" and item["active"] for item in DashboardAPI(context=context).workspaces())
    finally:
        context.close()


def test_dashboard_default_workspace_wins_over_alphabetical_registration(tmp_path):
    context = _context(tmp_path)
    try:
        WorkspaceSpec.register("aaa-stale", config={"backends": {"main": {"type": "inmem"}}}, context=context)
        WorkspaceSpec.register("default", config={"backends": {"main": {"type": "inmem"}}}, context=context)

        specs = DashboardAPI(context=context).workspaces()

        assert specs[0]["id"] == "aaa-stale"
        assert next(item for item in specs if item["id"] == "default")["active"] is True
    finally:
        context.close()


def test_dashboard_remove_workspace_releases_session_without_dropping_data(tmp_path):
    context = _context(tmp_path)
    try:
        WorkspaceSpec.register("removable", config={"backends": {"main": {"type": "inmem"}}}, context=context)
        api = DashboardAPI(context=context)
        api.open("removable")
        opened = api.ws

        result = api.remove_workspace("removable")

        assert result == {"ws_id": "removable", "removed": True, "was_open": True}
        assert api.ws is None and api.ws_id is None
        assert context.active_workspace_id is None
        assert opened.health()["workspace"] == "removable"
        with pytest.raises(KeyError):
            WorkspaceSpec.load("removable", context=context)
        assert api.remove_workspace("removable") == {"ws_id": "removable", "removed": False, "was_open": False}
    finally:
        context.close()


def test_dashboard_remove_workspace_preserves_live_session_when_unregister_fails(tmp_path, monkeypatch):
    context = _context(tmp_path)
    try:
        WorkspaceSpec.register("remove-failure", config={"backends": {"main": {"type": "inmem"}}}, context=context)
        api = DashboardAPI(context=context)
        api.open("remove-failure")
        opened = api.ws

        def fail_unregister(cls, ws_id, *, context=None):
            raise RuntimeError(f"registry unavailable for {ws_id}")

        monkeypatch.setattr(WorkspaceSpec, "unregister", classmethod(fail_unregister))
        with pytest.raises(RuntimeError, match="registry unavailable"):
            api.remove_workspace("remove-failure")

        assert api.ws is opened and api.ws_id == "remove-failure"
        assert hb.HeavenBase.load("remove-failure", context=context) is opened
        assert WorkspaceSpec.load("remove-failure", context=context).id == "remove-failure"
    finally:
        context.close()


def test_workspace_flow(dash_ws):
    api = DashboardAPI()
    dash_ws.context.activate_workspace("dash-test")
    specs = api.workspaces()
    assert any(spec["id"] == "dash-test" and spec["active"] is True for spec in specs)
    assert all("scope" not in spec for spec in specs)
    health = api.open("dash-test")
    assert health["ws_id"] == "dash-test" and health["backends"]
    api.ws_id = "stale-session-id"
    assert api.health()["ws_id"] == "dash-test"  # health identity comes from its workspace snapshot
    api.ws_id = "dash-test"
    cards = api.entities()
    assert [card["entity_id"] for card in cards] == ["agent-agent", "agent-message", "agent-session", "agent-skill", "dash-paper"]
    paper = next(card for card in cards if card["entity_id"] == "dash-paper")
    assert paper["count"] == 12 and paper["system"] is False
    system_ids = {card["entity_id"] for card in api.entities(include_system=True)}
    assert {"dash-paper", "sys-catalog", "sys-metaschema"} <= system_ids
    with pytest.raises(KeyError):
        api.open("dash-missing")
    with pytest.raises(ValueError):
        DashboardAPI().health()


def test_entity_schema(dash_ws):
    api = DashboardAPI()
    api.open("dash-test")
    info = api.entity_schema("dash-paper")
    assert info["schema"]["fields"]["title"]["desc"] == "Paper title"
    assert info["storage"]["title"][0]["strategy"]
    assert any(row["subject_id"] for row in info["backends"])
    assert isinstance(info["capabilities"], dict)
    with pytest.raises(KeyError):
        api.entity_schema("nope")


def test_entity_rows_paging(dash_ws):
    api = DashboardAPI()
    api.open("dash-test")
    page = api.entity_rows("dash-paper", order=["-year", "object_id"], offset=0, limit=5)
    assert len(page["rows"]) == 5 and page["has_more"] and page["total"] == 12
    assert page["rows"][0]["year"] >= page["rows"][1]["year"]
    tail = api.entity_rows("dash-paper", offset=10, limit=5)
    assert len(tail["rows"]) == 2 and not tail["has_more"]
    filtered = api.entity_rows("dash-paper", filter={"year": {"$gte": 2023}})
    assert filtered["total"] == 3 and all(row["year"] >= 2023 for row in filtered["rows"])
    selected = api.entity_rows("dash-paper", select=["title"], limit=3)
    assert set(selected["columns"]) == {"object_id", "title"}
    with pytest.raises(ValueError):
        api.entity_rows("dash-paper", offset=-1)
    with pytest.raises(KeyError):
        api.entity_rows("nope")


def test_column_stats(dash_ws):
    api = DashboardAPI()
    api.open("dash-test")
    stats = api.column_stats("dash-paper", "year")
    assert stats["kind"] == "numeric" and stats["total"] == 12 and stats["scanned"] == 12 and stats["sampled"] is False
    assert stats["nulls"] == 0 and stats["null_pct"] == 0.0 and stats["distinct"] == 4
    assert stats["min"] == 2020 and stats["max"] == 2023 and stats["mean"] == 2021.5
    assert stats["percentiles"] == {"p25": 2020.75, "p50": 2021.5, "p75": 2022.25}
    assert sum(bucket["count"] for bucket in stats["histogram"]) == 12 and len(stats["histogram"]) == 4
    assert stats["top"] and stats["top"][0]["count"] == 3
    text = api.column_stats("dash-paper", "title")
    assert text["kind"] == "text" and text["length"] == {"min": 7, "mean": 7.2, "max": 8}
    assert text["length_histogram"] and "top" not in text and len(text["examples"]) == 3
    assert all(set(example) == {"value"} for example in text["examples"])  # short values carry no elision marker
    meta = api.column_stats("dash-paper", "meta")
    assert meta["kind"] == "json" and meta["keys"] == [{"key": "idx", "count": 12}]
    assert meta["keys_exact"] is True and meta["distinct_keys"] == 1
    assert meta["key_count"] == {"min": 1, "mean": 1.0, "max": 1} and meta["size"]["max"] >= meta["size"]["mean"]
    with pytest.raises(KeyError):
        api.column_stats("dash-paper", "nope")
    with pytest.raises(KeyError):
        api.column_stats("nope", "year")


def test_column_stats_json_keys_are_exact_and_scan_independent(dash_ws):
    """Json key counts come from an engine aggregate, so no cap or sample bound applies."""
    ws = hb.HeavenBase.load("dash-test")
    ws.upsert_many(
        DashPaper,
        # 30 distinct keys, far past any top-N list, with one key deliberately rare
        [{"object_id": f"k{index:02d}", "title": f"K{index}", "year": 2020, "meta": {f"tag{index:02d}": 1, "shared": 1}} for index in range(30)],
    )
    api = DashboardAPI()
    api.open("dash-test")

    stats = api.column_stats("dash-paper", "meta")
    keys = {row["key"]: row["count"] for row in stats["keys"]}
    assert len(keys) == 32 and stats["distinct_keys"] == 32  # idx + shared + 30 tags, none dropped
    assert keys["shared"] == 30 and keys["idx"] == 12 and keys["tag07"] == 1
    assert stats["keys_exact"] is True
    assert [row["count"] for row in stats["keys"]] == sorted((row["count"] for row in stats["keys"]), reverse=True)

    assert stats["scanned"] == stats["total"] and stats["sampled"] is False


def test_column_stats_mixed_payload_column_stays_engine_exact(dash_ws):
    """Arrays and scalars sharing a Json column do not defeat the engine fold."""
    ws = hb.HeavenBase.load("dash-test")
    ws.upsert_many(
        DashPaper,
        [
            {"object_id": "mx1", "title": "M1", "year": 2020, "meta": {"a": 1}},
            {"object_id": "mx2", "title": "M2", "year": 2020, "meta": ["not", "an", "object"]},
            {"object_id": "mx3", "title": "M3", "year": 2020, "meta": 5},
        ],
    )
    api = DashboardAPI()
    api.open("dash-test")

    stats = api.column_stats("dash-paper", "meta")
    # the reducer skips a non-object payload rather than failing,
    # so the counts stay exact and describe the object-valued rows
    assert stats["keys_exact"] is True
    assert {row["key"]: row["count"] for row in stats["keys"]} == {"idx": 12, "a": 1}


def test_assistant_guidance_examples_actually_run(dash_ws):
    """Every spec the system prompt shows the model must execute as written."""
    api = DashboardAPI()
    api.open("dash-test")
    ws = api._open_ws()
    system = api._assist_system({})

    # the code_reduce one-off, quoted verbatim from the prompt
    assert '"code": "lambda v: max(v) - min(v)"' in system
    spread = ws.query_json("dash-paper", {"aggregate": {"spread": {"code_reduce": {"field": "year", "code": "lambda v: max(v) - min(v)", "dtype": "float"}}}})
    assert spread.execute().scalar() == 3.0  # 2023 - 2020

    # the multi-output aggregate the prompt tells the model to prefer over several calls
    assert '"lo": {"min": {"field": "price"}}' in system
    frame = ws.query_json("dash-paper", {"aggregate": {"lo": {"min": {"field": "year"}}, "hi": {"max": {"field": "year"}}}}).execute()
    assert frame.rows() == [{"lo": 2020, "hi": 2023}]

    # every op the prompt names must be one the engine actually registers
    from heavenbase.query.aggregate import reducer_for

    for op in ("count", "min", "max", "sum", "avg", "median", "percentile", "key_sums", "key_counts", "value_counts", "code_reduce"):
        assert f"{op}" in system
        assert reducer_for(op, resolver=ws.module_resolver) is not None


def test_column_stats_coverage_flags_are_measured_not_asserted(dash_ws, monkeypatch):
    """`sampled` must be able to become true, or it documents nothing."""
    api = DashboardAPI()
    api.open("dash-test")
    ws = api._open_ws()

    honest = api.column_stats("dash-paper", "year")
    assert honest["scanned"] == honest["total"] == 12 and honest["sampled"] is False

    # a path that hands the fold less than the entity holds must be reported, not
    # believed; the entity count and the fold's row count are separate readings
    monkeypatch.setattr(ws, "count", lambda *args, **kwargs: 40)
    short = api.column_stats("dash-paper", "year")
    assert short["total"] == 40 and short["scanned"] == 12
    assert short["sampled"] is True  # the flag moved on its own, nobody set it

    # nulls stay derived from the fold's own two counts, so they cannot go negative
    assert short["nulls"] == 0 and short["null_pct"] == 0.0

    # and the Json exactness flags follow the same reading rather than asserting
    honest_json = api.column_stats("dash-paper", "meta")
    short_json = api.column_stats("dash-paper", "meta")
    assert honest_json["keys_exact"] is False and short_json["sampled"] is True


def test_column_stats_refuses_rather_than_answering_from_a_sample(dash_ws, monkeypatch):
    """With no scan left to fall back to, a failed fold must raise instead of inventing coverage."""
    api = DashboardAPI()
    api.open("dash-test")
    ws = api._open_ws()
    engine_query = ws.query_json

    def _fold_fails(entity_id, spec, *args, **kwargs):
        if isinstance(spec, dict) and "column_summary" in str(spec.get("aggregate") or ""):
            raise RuntimeError("engine refused the fold")
        return engine_query(entity_id, spec, *args, **kwargs)

    monkeypatch.setattr(ws, "query_json", _fold_fails)
    # the old code answered from a bounded scan here and flagged it; there is no
    # partial answer to give now, and a raise cannot be mistaken for one
    with pytest.raises(RuntimeError, match="engine refused the fold"):
        api.column_stats("dash-paper", "meta")


def test_column_stats_survives_refusing_caller_authored_code(dash_ws):
    """Refusing caller-authored expressions must not disable the statistics endpoint."""
    api = DashboardAPI()
    api.open("dash-test")
    # restore the prior value rather than unsetting: the key has a builtin
    # default, so an unset leaves the gate closed for every later test
    previous = hb.CM_HVNB.get("heavenbase.query.allow_code_reduce", default=True)
    hb.CM_HVNB.set("heavenbase.query.allow_code_reduce", False)
    try:
        # column_summary is registered, so it needs no permission to run; only a
        # caller writing their own expression is refused
        stats = api.column_stats("dash-paper", "year")
        assert stats["min"] == 2020 and stats["max"] == 2023 and stats["distinct"] == 4
        with pytest.raises(ValueError, match="allow_code_reduce"):
            api._open_ws().query_json("dash-paper", {"aggregate": {"x": {"code_reduce": {"field": "year", "code": "lambda v: len(v)"}}}}).execute()
    finally:
        hb.CM_HVNB.set("heavenbase.query.allow_code_reduce", previous)


def test_column_stats_marks_a_truncated_most_common_list(dash_ws):
    """A most-common list cut at its limit declares the cut instead of reading as complete."""
    ws = hb.HeavenBase.load("dash-test")
    ws.upsert_many(DashPaper, [{"object_id": f"t{i:02d}", "title": "Same", "year": 2020, "meta": {"idx": i}} for i in range(9)])
    api = DashboardAPI()
    api.open("dash-test")

    text = api.column_stats("dash-paper", "title")
    assert len(text["top"]) == 5 and text["top_truncated"] is True  # more than five distinct titles exist

    bools = api.column_stats("dash-paper", "year")
    assert "top" not in bools or "top_truncated" not in bools  # four distinct years fit under the limit, so no flag appears


def test_column_stats_counts_whole_values_and_marks_elided_ones(dash_ws):
    """Values are counted whole and only elided for display, so `distinct` never merges them."""
    ws = hb.HeavenBase.load("dash-test")
    shared = "B" * 130  # a common prefix long enough to collapse under prefix bucketing
    ws.upsert_many(
        DashPaper,
        [
            {"object_id": "lv1", "title": f"{shared}-one", "year": 2020, "meta": {"idx": 1}},
            {"object_id": "lv2", "title": f"{shared}-two", "year": 2020, "meta": {"idx": 2}},
            # a repeat so the text branch publishes a top list at all
            {"object_id": "lv3", "title": f"{shared}-two", "year": 2020, "meta": {"idx": 3}},
        ],
    )
    api = DashboardAPI()
    api.open("dash-test")

    stats = api.column_stats("dash-paper", "title")
    assert stats["distinct"] == 14  # 12 fixture titles plus two that differ only past character 130

    top = stats["top"][0]
    assert top["count"] == 2 and top["value_truncated"] is True  # the repeated long title, counted whole
    assert len(top["value"]) == 120 and top["value_len"] == len(shared) + 4  # the quote says it is a quote


def test_entity_row_and_search(dash_ws):
    api = DashboardAPI()
    api.open("dash-test")
    assert api.entity_row("dash-paper", "p03")["meta"] == {"idx": 3}
    with pytest.raises(KeyError):
        api.entity_row("dash-paper", "missing")
    results = api.search("paper")
    assert isinstance(results["rows"], list) and results["n"] == len(results["rows"])
    # the default cap withholds cards here, and ranking sees them all, so the shortfall is exact
    assert results["truncated"] is True and results["total"] > results["n"]

    cut = api.search("paper", limit=1)
    assert cut["n"] == 1 and cut["truncated"] is True and cut["total"] == results["total"]  # the count does not move with the cap

    whole = api.search("paper", limit=results["total"] + 10)
    assert whole["truncated"] is False and whole["total"] == whole["n"]  # an uncut search states its own completeness

    tools = {tool.__name__: tool for tool in api._assist_tools()}
    found = tools["search_workspace"]("paper")
    assert found["total"] == results["total"] and str(found["total"]) in found["note"]  # the model is told what it did not receive


def test_http_routes(dash_ws):
    from starlette.testclient import TestClient

    api = DashboardAPI()
    client = TestClient(build_app(api), base_url="http://localhost")
    meta = client.get("/api/meta").json()
    assert meta["name"] == "HeavenBase" and isinstance(meta.get("version"), str) and meta["version"]
    assert client.get("/api/config/meta").json()["scope"] == "heavenbase"
    assert client.post("/api/workspace/open", json={"ws_id": "dash-test"}).status_code == 200
    page = client.post("/api/workspace/entity/dash-paper/rows", json={"limit": 3}).json()
    assert len(page["rows"]) == 3 and page["total"] == 12
    assert client.get("/api/workspace/entity/nope/schema").status_code == 404
    assert client.post("/api/config/unset", json={"key": "heavenbase.dashboard.port"}).status_code == 400
    assert client.post("/api/assist", json={"question": ""}).status_code == 400
    for body in (None, "hello", [1, 2]):
        assert client.post("/api/config/set", json=body).status_code == 400
    index = client.get("/")
    assert index.status_code == 200 and "/js/codemirror.js" in index.text
    assert client.get("/codemirror.css").status_code == 200
    assert client.get("/js/codemirror-json.js").status_code == 200


def test_http_workspace_remove_uses_typed_delete_route(tmp_path):
    from starlette.testclient import TestClient

    context = _context(tmp_path)
    try:
        WorkspaceSpec.register("remove-http", config={"backends": {"main": {"type": "inmem"}}}, context=context)
        client = TestClient(build_app(DashboardAPI(context=context)), base_url="http://localhost")

        response = client.delete("/api/workspaces/remove-http")

        assert response.status_code == 200
        assert response.json() == {"ws_id": "remove-http", "removed": True, "was_open": False}
        assert all(item["id"] != "remove-http" for item in client.get("/api/workspaces").json())
    finally:
        context.close()


def test_http_workspace_open_returns_contextual_server_error(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    class BrokenSpec:
        id = "broken"

        def open(self, *, context=None):
            raise RuntimeError("backend socket unavailable")

    monkeypatch.setattr(WorkspaceSpec, "load", classmethod(lambda cls, ws_id=None, context=None: BrokenSpec()))
    context = _context(tmp_path)
    api = DashboardAPI(context=context)
    try:
        response = TestClient(build_app(api), base_url="http://localhost").post("/api/workspace/open", json={"ws_id": "broken"})

        assert response.status_code == 500
        assert response.json() == {"error": "could not open workspace 'broken': backend socket unavailable"}
        assert api.ws is None and api.ws_id is None
        assert context.active_workspace_id is None
    finally:
        context.close()


def test_openapi_is_served_from_the_runtime_operation_catalog():
    from heavenbase.api.models import DASHBOARD_OPERATIONS
    from starlette.testclient import TestClient

    schema = openapi_schema()
    served = TestClient(build_app(DashboardAPI()), base_url="http://localhost").get("/openapi.json")

    assert served.status_code == 200
    assert served.json() == schema
    assert schema["openapi"] == "3.1.0"
    documented = {(method.upper(), path, operation["operationId"]) for path, path_item in schema["paths"].items() for method, operation in path_item.items()}
    assert documented == {(operation.method, operation.path, operation.operation_id) for operation in DASHBOARD_OPERATIONS}
    assert schema["paths"]["/api/config/set"]["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ConfigSetRequest"
    }
    assert set(schema["paths"]["/api/config/set"]["post"]["responses"]) == {"200", "400", "403", "404", "500"}


def test_http_models_reject_invalid_input_without_calling_service():
    from starlette.testclient import TestClient

    class RecordingAPI(DashboardAPI):
        called = False

        def config_set(self, key, value, scope=None, parse="auto"):
            self.called = True
            return super().config_set(key, value, scope=scope, parse=parse)

    api = RecordingAPI()
    client = TestClient(build_app(api), base_url="http://localhost")

    assert client.post("/api/config/set", json={"key": "", "value": 1}).status_code == 400
    assert client.post("/api/config/set", json={"key": "x", "value": 1, "unknown": True}).status_code == 400
    assert client.get("/api/config/history?limit=zero").status_code == 400
    assert api.called is False


def test_assist_stream_route_preserves_context_across_chunks():
    from contextvars import ContextVar

    from starlette.testclient import TestClient

    marker = ContextVar("dashboard_stream_marker", default="missing")

    class ContextAPI(DashboardAPI):
        def assist_stream(self, question, context=None, history=None, llm=None, preset=None, read_only=None, conversation_id=None, turn_id=None):
            token = marker.set("active")
            try:
                yield {"kind": "text", "text": "hello"}
                assert marker.get() == "active"
                yield {"kind": "done", "text": "hello"}
            finally:
                marker.reset(token)

    response = TestClient(build_app(ContextAPI()), base_url="http://localhost").post("/api/assist/stream", json={"question": "hello"})

    assert response.status_code == 200
    assert '"kind": "text"' in response.text and '"kind": "done"' in response.text


def test_assist_stream_disconnect_tears_down_without_waiting_for_the_provider():
    # A stop while the provider stalls must end the response immediately and
    # close the generator at its next yield boundary — never block on next().
    from contextvars import copy_context

    from heavenbase.api import http as dashboard_http

    release = threading.Event()
    blocked = threading.Event()
    closed = threading.Event()

    def stalled_stream():
        try:
            yield {"kind": "text", "text": "first"}
            blocked.set()
            release.wait(timeout=10)  # a provider that hangs before its next event
            yield {"kind": "text", "text": "second"}
        finally:
            closed.set()

    async def scenario():
        stream = dashboard_http._assist_sse(iter(stalled_stream()), copy_context())
        assert "first" in await stream.__anext__()
        pending = asyncio.create_task(stream.__anext__())
        while not blocked.is_set():
            await asyncio.sleep(0)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert not closed.is_set()  # the stalled generator is parked on its own daemon thread
    release.set()  # the provider finally yields again...
    assert closed.wait(timeout=5)  # ...and the worker closes the generator at that boundary


def test_assist_stream_bridge_preserves_response_backpressure():
    from contextvars import copy_context

    from heavenbase.api import http as dashboard_http

    advanced = threading.Event()

    def source():
        yield {"kind": "text", "text": "first"}
        advanced.set()
        yield {"kind": "text", "text": "second"}

    async def scenario():
        stream = dashboard_http._assist_sse(iter(source()), copy_context())
        assert "first" in await stream.__anext__()
        await asyncio.sleep(0.05)
        assert not advanced.is_set()
        assert "second" in await stream.__anext__()
        await stream.aclose()

    asyncio.run(scenario())


def test_assist_stream_teardown_failure_is_reported_without_hanging():
    from contextvars import copy_context

    from heavenbase.api import http as dashboard_http

    class BrokenClose:
        emitted = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.emitted:
                raise StopIteration
            self.emitted = True
            return {"kind": "text", "text": "first"}

        def close(self):
            raise RuntimeError("close failed")

    async def scenario():
        stream = dashboard_http._assist_sse(BrokenClose(), copy_context())
        return [line async for line in stream]

    lines = asyncio.run(asyncio.wait_for(scenario(), timeout=2))
    assert len(lines) == 2
    assert '"kind": "text"' in lines[0]
    assert "assist failed during stream teardown: close failed" in lines[1]


def test_assist_stream_beats_keepalive_comments_while_the_provider_is_silent(monkeypatch):
    from contextvars import copy_context

    from heavenbase.api import http as dashboard_http

    monkeypatch.setattr(dashboard_http, "ASSIST_KEEPALIVE_SECONDS", 0.05)
    release = threading.Event()

    def stalled_stream():
        yield {"kind": "text", "text": "first"}
        release.wait(timeout=10)  # a healthy provider mid-tool-call: alive but silent
        yield {"kind": "done", "text": "first"}

    async def scenario():
        stream = dashboard_http._assist_sse(iter(stalled_stream()), copy_context())
        first = await stream.__anext__()
        while first.startswith(":"):  # pump-thread startup may already beat once
            first = await stream.__anext__()
        assert "first" in first
        # Provider silence produces an SSE comment frame, not a contract event.
        assert await stream.__anext__() == ": keepalive\n\n"
        release.set()
        rest = [line async for line in stream]
        data = [line for line in rest if line.startswith("data: ")]
        assert len(data) == 1 and '"kind": "done"' in data[0]
        # Whatever else arrived while racing the release is only more beats.
        assert all(line == ": keepalive\n\n" for line in rest if line not in data)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_assist_stream_route_rejects_events_outside_the_openapi_contract():
    from starlette.testclient import TestClient

    class InvalidEventAPI(DashboardAPI):
        def assist_stream(self, question, context=None, history=None, llm=None, preset=None, read_only=None, conversation_id=None, turn_id=None):
            yield {"kind": "not-a-real-event"}

    response = TestClient(build_app(InvalidEventAPI()), base_url="http://localhost").post(
        "/api/assist/stream",
        json={"question": "hello"},
    )

    assert response.status_code == 200
    assert '"kind": "error"' in response.text
    assert "invalid stream event" in response.text


def test_assist_routes_serve_overrides_written_without_the_session_log_identifiers():
    """An override with the 0.1.2.2 parameter list serves every client, the bundled UI included.

    The bundled UI sends ``conversation_id``/``turn_id`` on every request, as
    ``null`` or as minted ids. An override that does not take them is served
    without them; one that takes ``**kwargs`` receives them. A required field
    the override cannot bind answers 400 on both routes, never an unhandled 500.
    """
    from starlette.testclient import TestClient

    received = []

    class LegacyAPI(DashboardAPI):
        def assist(self, question, context=None, history=None, llm=None, progress_id=None, preset=None, read_only=None):
            return {"text": question, "think": "", "served_model": "m", "frames": [], "sql_frames": [], "events": [], "elapsed": 0.0}

        def assist_stream(self, question, context=None, history=None, llm=None, preset=None, read_only=None):
            yield {"kind": "text", "text": question}

    class KeywordAPI(DashboardAPI):
        def assist(self, question, **options):
            received.append(options)
            return {"text": question, "think": "", "served_model": "m", "frames": [], "sql_frames": [], "events": [], "elapsed": 0.0}

    class RenamedAPI(DashboardAPI):
        def assist(self, prompt, **options):
            return {"text": prompt, "think": "", "served_model": "m", "frames": [], "sql_frames": [], "events": [], "elapsed": 0.0}

        def assist_stream(self, prompt):
            yield {"kind": "text", "text": prompt}

    client = TestClient(build_app(LegacyAPI()), base_url="http://localhost")
    # Request bodies as apps/gui/ui/src/api.ts builds them: every key present, unused ones null.
    ui_null = {"question": "hello", "context": None, "history": [], "preset": None, "read_only": None, "conversation_id": None, "turn_id": None}
    ui_ids = dict(ui_null, conversation_id="c1", turn_id="t1")
    for body in ({"question": "hello"}, ui_null, ui_ids):
        blocking = client.post("/api/assist", json=dict(body, progress_id=None) if "turn_id" in body else body)
        assert blocking.status_code == 200 and blocking.json()["text"] == "hello", blocking.text
        streamed = client.post("/api/assist/stream", json=body)
        assert streamed.status_code == 200 and '"text": "hello"' in streamed.text, streamed.text

    keyword = TestClient(build_app(KeywordAPI()), base_url="http://localhost")
    assert keyword.post("/api/assist", json=ui_ids).status_code == 200
    assert received[-1]["conversation_id"] == "c1" and received[-1]["turn_id"] == "t1"

    renamed = TestClient(build_app(RenamedAPI()), base_url="http://localhost")
    for route in ("/api/assist", "/api/assist/stream"):
        refused = renamed.post(route, json={"question": "hello"})
        assert refused.status_code == 400, refused.text
        assert "argument" in refused.json()["error"]  # the binding TypeError: missing 'prompt' or unexpected 'question'


def test_routes_refuse_an_override_that_cannot_take_a_field_changing_the_request():
    """Only the session-log identifiers are omitted for a narrowed override.

    A config ``scope`` or an assist ``read_only`` changes what the request
    does, so an override that does not take it answers 400, as in 0.1.2.2,
    instead of silently applying the request elsewhere or with writes allowed.
    """
    from starlette.testclient import TestClient

    called = []

    class NarrowAPI(DashboardAPI):
        def config_set(self, key, value, parse="auto"):
            called.append("config_set")
            return {"ok": True}

        def config_unset(self, key):
            called.append("config_unset")
            return {"ok": True}

        def assist(self, question, context=None, history=None, llm=None, progress_id=None, preset=None):
            called.append("assist")
            return {"text": question, "think": "", "served_model": "m", "frames": [], "sql_frames": [], "events": [], "elapsed": 0.0}

        def assist_stream(self, question, context=None, history=None, llm=None, preset=None):
            called.append("assist_stream")
            yield {"kind": "text", "text": question}

    client = TestClient(build_app(NarrowAPI()), base_url="http://localhost")
    requests = [
        ("/api/config/set", {"key": "a.b", "value": 1, "scope": "workspace:other"}, "scope"),
        ("/api/config/unset", {"key": "a.b", "scope": "workspace:other"}, "scope"),
        ("/api/assist", {"question": "hello", "read_only": True, "conversation_id": "c1", "turn_id": "t1"}, "read_only"),
        ("/api/assist/stream", {"question": "hello", "read_only": True, "conversation_id": "c1", "turn_id": "t1"}, "read_only"),
    ]
    for route, body, field in requests:
        refused = client.post(route, json=body)
        assert refused.status_code == 400, (route, refused.text)
        assert f"unexpected keyword argument '{field}'" in refused.json()["error"], refused.text
    assert called == []


def test_in_default_guards_scalar_traversal(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    assert api._in_default("heavenbase.cli.backend") is True
    assert api._in_default("heavenbase.cli.backend.deeper") is True
    assert api._in_default("heavenbase.llm.providers.brand_new") is False


def test_config_dotted_entries_roundtrip_and_reset(tmp_path):
    # Built-in model names contain dots; escaped writes must round-trip and origins must stay escaped.
    api = DashboardAPI(context=_context(tmp_path))
    assert "gpt-5.6-luna" in api.config_tree()["tree"]["heavenbase"]["llm"]["models"]
    api.config_set("heavenbase.llm.models.my\\.dotted", {"providers": {"custom": {"model_id": "x"}}})
    tree = api.config_tree()
    assert tree["tree"]["heavenbase"]["llm"]["models"]["my.dotted"] == {"providers": {"custom": {"model_id": "x"}}}
    assert tree["origins"]["heavenbase.llm.models.my\\.dotted.providers.custom.model_id"] == "custom"
    key = "heavenbase.llm.models.gpt-5\\.6-luna.default_provider"
    api.config_set(key, "openai")
    assert api.config_reset(key)["origin"] == "builtin"
    assert api.config_tree()["tree"]["heavenbase"]["llm"]["models"]["gpt-5.6-luna"]["default_provider"] == "openrouter"


def test_http_guard_host_and_origin_boundaries(dash_ws):
    from starlette.testclient import TestClient

    app = build_app(DashboardAPI())
    # DNS-rebinding Host is rejected; loopback Host is served.
    assert TestClient(app, base_url="http://evil.com").get("/api/config/meta").status_code == 403
    local = TestClient(app, base_url="http://localhost")
    body = {"key": "heavenbase.dashboard.port", "value": 5}
    assert local.post("/api/config/set", json=body, headers={"Origin": "http://evil.com"}).status_code == 403
    assert local.post("/api/config/set", json=body, headers={"Origin": "http://localhost"}).status_code == 200
    # Broad-bind opt-out permits any Host but still blocks cross-origin writes.
    broad = TestClient(build_app(DashboardAPI(), allowed_hosts=None), base_url="http://192.168.0.5")
    assert broad.get("/api/config/meta").status_code == 200
    assert broad.post("/api/config/set", json=body, headers={"Origin": "http://evil.com"}).status_code == 403
    # Same host on a different port is a different origin: another local app must not write config.
    assert local.post("/api/config/set", json=body, headers={"Origin": "http://localhost:3000"}).status_code == 403
    assert local.post("/api/config/set", json=body, headers={"Origin": "null"}).status_code == 403
    assert local.post("/api/config/set", json=body, headers={"Origin": "http://localhost"}).status_code == 200
    # localhost and 127.0.0.1 on the SAME port name the same listener: both alias
    # directions must pass, while an alias on another port stays blocked.
    for base, origin in (("http://127.0.0.1:42777", "http://localhost:42777"), ("http://localhost:42777", "http://127.0.0.1:42777")):
        client = TestClient(build_app(DashboardAPI()), base_url=base)
        assert client.post("/api/config/set", json=body, headers={"Origin": origin}).status_code == 200
    client = TestClient(build_app(DashboardAPI()), base_url="http://127.0.0.1:42777")
    assert client.post("/api/config/set", json=body, headers={"Origin": "http://localhost:42778"}).status_code == 403


class _EchoLLM:
    def __init__(self):
        self.messages = None
        self.tools = None

    def chat(self, messages, include=None, tools=None, on_round=None):
        assert "text" in include and messages[0]["role"] == "system"
        self.messages = messages
        self.tools = tools
        return {"text": f"echo:{len(messages)}", "think": "step 1", "raw": {"model": "double-model"}}

    def stream(self, messages, include=None, reduce=False, tools=None):
        self.messages = messages
        self.tools = tools
        yield {
            "text": "echo-stream",
            "think": "",
            "tool_calls": [],
            "message": {"role": "assistant", "content": "echo-stream"},
            "delta": [{"role": "assistant", "content": "echo-stream"}],
            "raw": {"model": "double-model"},
            "elapsed": 0.1,
        }


class _BrokenLLM:
    def chat(self, messages, include=None, tools=None):
        raise RuntimeError("no key configured")


class _SchemaToolLLM:
    """Drives the entity_schema tool once, then answers from its real result."""

    def chat(self, messages, include=None, tools=None):
        by_name = {tool.__name__: tool for tool in tools or []}
        result = by_name["entity_schema"]("dash-paper")
        return {"text": ",".join(sorted(result["fields"])), "think": ""}


def test_assist_with_injected_llm(dash_ws):
    api = DashboardAPI()
    api.open("dash-test")
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    echo = _EchoLLM()
    reply = api.assist("what is here?", context={"route": "data", "entity": "dash-paper"}, history=history, llm=echo)
    assert reply["text"] == "echo:4" and reply["think"] == "step 1" and reply["served_model"] == "double-model"
    assert "Open workspace: dash-test" in echo.messages[0]["content"]
    # Read-only tools are handed to the model.
    assert echo.tools and {"list_entities", "entity_schema", "sample_rows"} <= {tool.__name__ for tool in echo.tools}
    with pytest.raises(ValueError):
        api.assist("  ", llm=_EchoLLM())
    with pytest.raises(ValueError):
        api.assist("hi", llm=_BrokenLLM())
    reply = api.assist("fields?", context={"route": "data", "entity": "dash-paper"}, llm=_SchemaToolLLM())
    # The tool returned the real fields, not invented ones.
    assert set(reply["text"].split(",")) == {"object_id", "title", "year", "meta"}


def test_assist_llm_options(tmp_path):
    # The assistant defaults to the ordinary chat preset; a request preset wins.
    # Retries stay off on every route (the panel has no retry feedback).
    api = DashboardAPI(context=_context(tmp_path))
    assert api._assist_llm_options() == {"preset": "chat", "max_retries": 0}
    api.config_set("heavenbase.dashboard.assistant.preset", "chat")
    assert api._assist_llm_options() == {"preset": "chat", "max_retries": 0}
    assert api._assist_llm_options(preset="reason-pro") == {"preset": "reason-pro", "max_retries": 0}
    with pytest.raises(ValueError, match="not registered"):
        api._assist_llm_options(preset="missing-preset")


def test_assist_llm_options_warn_when_a_preset_shadows_the_configured_route(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.preset", "chat")
    api.config_set("heavenbase.dashboard.assistant.model", "ds-pro")
    with pytest.warns(UserWarning, match="model ignored: preset 'chat'"):
        assert api._assist_llm_options() == {"preset": "chat", "max_retries": 0}
    # A request-level preset shadows the machine-configured route the same way.
    api.config_set("heavenbase.dashboard.assistant.preset", "")
    with pytest.warns(UserWarning, match="model ignored: preset 'reason-pro'"):
        assert api._assist_llm_options(preset="reason-pro") == {"preset": "reason-pro", "max_retries": 0}


def test_assist_llm_options_default_args_yield_to_the_selected_route(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.preset", "chat")
    api.config_set("heavenbase.dashboard.assistant.default_args", {"think": True, "temperature": 0.2})
    # The 'chat' preset owns ``think`` in its bundle: the machine-level value
    # is dropped with a warning while the non-colliding key rides along.
    with pytest.warns(UserWarning, match="default_args think ignored"):
        options = api._assist_llm_options()
    assert options == {"preset": "chat", "default_args": {"temperature": 0.2}, "max_retries": 0}
    # A scalar preset default_args owns nothing (and a string owns no
    # characters); it must not take down the assist call.
    api.config_set("heavenbase.llm.presets.chat.default_args", 7)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        options = api._assist_llm_options()
    assert options == {"preset": "chat", "default_args": {"think": True, "temperature": 0.2}, "max_retries": 0}
    # Retries configured there are kept, not replaced by the panel's default of none.
    api.config_set("heavenbase.dashboard.assistant.default_args", {"max_retries": 2})
    assert api._assist_llm_options() == {"preset": "chat", "default_args": {"max_retries": 2}}


@pytest.mark.slow
def test_a_configured_assistant_temperature_is_left_out_on_a_claude_thinking_route(tmp_path, monkeypatch):
    """The assistant's configured default_args are configuration, so a thinking Claude request drops their temperature."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from heavenbase.llm import LLM
    from heavenbase.llm.cache import LLMCache

    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = json.dumps(
                {
                    "id": "c1",
                    "object": "chat.completion",
                    "model": "m",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        api = DashboardAPI(context=_context(tmp_path))
        api.config_set("heavenbase.dashboard.assistant.preset", "")  # route by model, not the packaged chat preset
        api.config_set("heavenbase.dashboard.assistant.model", "sonnet-4.6")
        api.config_set("heavenbase.dashboard.assistant.provider", "openrouter")
        api.config_set("heavenbase.dashboard.assistant.gateway", "openai")
        api.config_set(
            "heavenbase.dashboard.assistant.default_args",
            {
                "temperature": 0,
                "think": True,
                "stream_chat": False,
                "base_url": f"http://127.0.0.1:{httpd.server_address[1]}/v1",
                "api_key": "sk-placeholder",
            },
        )
        options = api._assist_llm_options()
        # the test Context sets up no modules, so the LLM resolves its route in the default one
        LLM(cache=False, **options).chat("hi")
    finally:
        httpd.shutdown()
    [body] = bodies
    assert body["reasoning"] == {"enabled": True}
    assert "temperature" not in body and "top_k" not in body


def test_a_configured_assistant_endpoint_still_selects_the_route(tmp_path):
    """Route selectors in the assistant's default_args stay LLM keywords, so the route resolves as configured."""
    from heavenbase.llm import LLM

    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.preset", "")
    api.config_set("heavenbase.dashboard.assistant.model", "haiku")
    api.config_set("heavenbase.dashboard.assistant.provider", "anthropic")
    api.config_set("heavenbase.dashboard.assistant.gateway", "portkey")
    api.config_set("heavenbase.dashboard.assistant.default_args", {"endpoint": "anthropic", "temperature": 0, "api_key": "sk-placeholder"})
    options = api._assist_llm_options()
    assert options["endpoint"] == "anthropic"
    assert options["default_args"] == {"temperature": 0, "api_key": "sk-placeholder"}
    # the test Context sets up no modules, so the LLM resolves its route in the default one
    assert LLM(cache=False, **options).spec.endpoint == "anthropic"


def test_assist_default_llm_uses_the_dashboard_context(monkeypatch, tmp_path):
    """Dashboard prompts, tools, config, and model resolution share one Context."""
    import heavenbase.llm as llm_module

    context = _context(tmp_path)
    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat(self, messages, **kwargs):
            return {"text": "bound", "think": "", "raw": {"model": "fake"}}

    monkeypatch.setattr(llm_module, "LLM", FakeLLM)
    api = DashboardAPI(context=context)
    try:
        assert api.assist("which context?")["text"] == "bound"
        assert captured["context"] is context
        assert captured["cache"] is False
    finally:
        context.close()


def test_assist_passes_the_configured_tool_round_budget_per_call(monkeypatch, tmp_path):
    import heavenbase.llm as llm_module

    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            pass

        def chat(self, messages, **kwargs):
            captured["chat"] = kwargs.get("max_tool_turns")
            return {"text": "bound", "think": "", "raw": {"model": "fake"}}

        def stream(self, messages, include=None, reduce=False, tools=None, **kwargs):
            captured["stream"] = kwargs.get("max_tool_turns")
            yield {"text": "ok", "raw": {"model": "fake"}}

    monkeypatch.setattr(llm_module, "LLM", FakeLLM)
    api = DashboardAPI(context=_context(tmp_path))
    # The dashboard keeps the generic LLM's existing bounded tool-loop default.
    assert api.assist("budget?")["text"] == "bound"
    assert captured["chat"] == 8
    api.config_set("heavenbase.dashboard.assistant.max_tool_turns", 3)
    assert api.assist("budget?")["text"] == "bound"
    assert captured["chat"] == 3
    events = list(api.assist_stream("budget?"))
    assert events[-1]["kind"] == "done" and captured["stream"] == 3
    api.config_set("heavenbase.dashboard.assistant.max_tool_turns", 0)
    with pytest.raises(ValueError, match="between 1 and 10000"):
        api._assist_tool_turns()


def test_assist_passes_the_result_capture_clip_per_call(monkeypatch, tmp_path):
    import heavenbase.llm as llm_module

    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            pass

        def chat(self, messages, **kwargs):
            captured["chat"] = kwargs.get("tool_result_capture_chars")
            return {"text": "bound", "think": "", "raw": {"model": "fake"}}

        def stream(self, messages, include=None, reduce=False, tools=None, **kwargs):
            captured["stream"] = kwargs.get("tool_result_capture_chars")
            yield {"text": "ok", "raw": {"model": "fake"}}

    monkeypatch.setattr(llm_module, "LLM", FakeLLM)
    api = DashboardAPI(context=_context(tmp_path))
    assert api.assist("clip?")["text"] == "bound"
    assert captured["chat"] == 0
    api.config_set("heavenbase.dashboard.assistant.result_capture_chars", 0)
    assert api.assist("clip?")["text"] == "bound"
    assert captured["chat"] == 0  # explicit 0 stays a valid disable
    api.config_set("heavenbase.dashboard.assistant.result_capture_chars", 30000)
    events = list(api.assist_stream("clip?"))
    assert events[-1]["kind"] == "done" and captured["stream"] == 30000


def test_assist_history_window_is_configurable(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    assert api._assist_history_turns() == 40
    history = [{"role": "user", "content": f"t{i}"} for i in range(45)]
    echo = _EchoLLM()
    api.assist("now?", history=history, llm=echo)
    assert len(echo.messages) == 42  # system + the latest 40 turns + question
    assert "Only the last 40 of 45 prior turns" in echo.messages[0]["content"]
    api.config_set("heavenbase.dashboard.assistant.history_turns", 20)
    api.assist("now?", history=history, llm=echo)
    assert len(echo.messages) == 22  # the narrower window reaches the slice…
    assert "Only the last 20 of 45 prior turns" in echo.messages[0]["content"]  # …and the note's arithmetic


def test_assist_evidence_carry_is_opt_in(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    assert api._assist_evidence_budget() == 0
    turns = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}, "malformed"]
    # A zero budget still restores the stock prose-only replay untouched.
    assert api._assist_carry_history(turns, 0) == turns


def test_assist_evidence_is_scoped_by_workspace_and_question(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    api._assist_store_evidence("same answer", [("query", "first", "ONE")], (5000, 500), question="same question", scope="workspace-one")
    api._assist_store_evidence("same answer", [("query", "second", "TWO")], (5000, 500), question="same question", scope="workspace-two")
    api._assist_store_evidence("same answer", [("query", "third", "THREE")], (5000, 500), question="other question", scope="workspace-one")

    history = [{"role": "user", "content": "same question"}, {"role": "assistant", "content": "same answer"}]
    first = api._assist_carry_history(history, 10000, "workspace-one")[-1]["content"]
    second = api._assist_carry_history(history, 10000, "workspace-two")[-1]["content"]
    other = api._assist_carry_history(
        [{"role": "user", "content": "other question"}, {"role": "assistant", "content": "same answer"}],
        10000,
        "workspace-one",
    )[-1]["content"]

    assert "ONE" in first and "TWO" not in first and "THREE" not in first
    assert "TWO" in second and "ONE" not in second and "THREE" not in second
    assert "THREE" in other and "ONE" not in other and "TWO" not in other


def test_assist_evidence_recording_is_exception_proof():
    from heavenbase.api.service import _recorded

    class Unrenderable:
        def __str__(self):
            raise RuntimeError("no rendering")

    log = []

    def boom_tool():
        """Return a result that cannot be rendered."""
        return Unrenderable()

    def plain_tool(entity, session_id=None):
        """Return a plain result."""
        return "ok"

    assert isinstance(_recorded(boom_tool, log)(), Unrenderable)  # the result is untouched
    assert log == []  # the capture is skipped, never surfaced as a tool error
    assert _recorded(plain_tool, log)("dash-paper", session_id="s1") == "ok"
    assert log == [("plain_tool", "dash-paper", "ok")]  # session_id never rides into evidence
    assert _recorded(plain_tool, None) is plain_tool  # feature off: tools stay unwrapped


def test_assist_evidence_carry_degrades_newest_first(tmp_path):
    from heavenbase.api import service as dashboard_service

    api = DashboardAPI(context=_context(tmp_path))
    long_result = "R" * 1000
    for answer in ("a1", "a2", "a3"):
        api._assist_store_evidence(answer, [("query", f"spec-{answer}", long_result)], (5000, 500))
    with api._assist_evidence_lock:
        full3, brief3 = api._assist_evidence[dashboard_service._assist_evidence_key("a3")]
        brief2 = api._assist_evidence[dashboard_service._assist_evidence_key("a2")][1]
    assert len(full3) > len(brief3)  # the brief rendering truncates args/result per line

    def block(rendering):
        return len(f"\n\n{dashboard_service._ASSIST_EVIDENCE_HEADER}\n{rendering}")

    turns = [{"role": "assistant", "content": "a1"}, {"role": "assistant", "content": "a2"}, {"role": "assistant", "content": "a3"}]
    carried = api._assist_carry_history(turns, block(full3) + block(brief3))
    assert carried[2]["content"].endswith(full3)  # newest turn wins the full rendering
    assert carried[1]["content"].endswith(brief2)  # the next one degrades to brief
    assert carried[0] == {"role": "assistant", "content": "a1"}  # the oldest is dropped
    # Turns already carrying the header marker are never re-annotated.
    assert api._assist_carry_history([carried[2]], 100000) == [carried[2]]


def test_assist_measured_replay_ships_full_under_the_threshold(tmp_path, monkeypatch):
    import heavenbase.llm as llm_module

    class BombLLM:
        def __init__(self, **kwargs):
            raise AssertionError("no model may be constructed under the threshold")

    monkeypatch.setattr(llm_module, "LLM", BombLLM)
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.evidence_total", 300)  # the ladder would degrade at this budget
    api.config_set("heavenbase.dashboard.assistant.history_chars", 100000)
    api._assist_store_evidence("a1", [("query", "spec1", "R" * 3000)], (200000, 100), question="q1")
    api._assist_store_evidence("a2", [("query", "spec2", "S" * 3000)], (200000, 100), question="q2")
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    echo = _EchoLLM()
    api.assist("next?", history=history, llm=echo)
    replayed = [message for message in echo.messages if message["role"] == "assistant"]
    # Zero degradation: every turn carries its FULL rendering, ladder budget ignored.
    assert "R" * 3000 in replayed[0]["content"] and "S" * 3000 in replayed[1]["content"]
    assert "compacted" not in echo.messages[0]["content"]


def test_assist_measured_replay_compacts_the_oldest_prefix_and_caches_it(tmp_path, monkeypatch):
    import heavenbase.llm as llm_module

    prompts = []
    seen = []

    class CompactLLM:
        def __init__(self, **kwargs):
            pass

        def chat(self, prompt, **kwargs):
            if isinstance(prompt, str):  # the one-shot compaction call
                prompts.append(prompt)
                return "MEMO: q1 asked; a1 counted 12 rows"
            seen.append(prompt)
            return {"text": "turn answer", "think": "", "raw": {"model": "m"}}

    monkeypatch.setattr(llm_module, "LLM", CompactLLM)
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.evidence_total", 1000000)
    api.config_set("heavenbase.dashboard.assistant.history_chars", 40000)
    api.config_set("heavenbase.dashboard.assistant.compact_chars", 300)
    api._assist_store_evidence("a1", [("query", "old-spec", "R" * 60000)], (200000, 100), question="q1-unique")
    api._assist_store_evidence("a2", [("query", "new-spec", "S" * 22000)], (200000, 100), question="q2")
    history = [
        {"role": "user", "content": "q1-unique"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    assert api.assist("next?", history=history)["text"] == "turn answer"
    # One call summarizes the prefix turns' prose AND their full evidence.
    assert len(prompts) == 1 and "q1-unique" in prompts[0] and "R" * 1000 in prompts[0]
    system = seen[-1][0]["content"]
    assert "Summary of the earlier conversation (compacted): MEMO: q1 asked; a1 counted 12 rows" in system
    assert "2 older turns were compacted into the summary above" in system
    replayed = [message for message in seen[-1] if message["role"] == "assistant"]
    assert len(replayed) == 1 and "S" * 22000 in replayed[0]["content"]  # the suffix still ships full
    assert not any("q1-unique" in message["content"] for message in seen[-1][1:])  # the prefix left the verbatim turns
    assert sum(len(message["content"]) for message in seen[-1]) <= 40000  # measured back under the threshold
    # A stable prefix pays its compaction exactly once.
    assert api.assist("next again?", history=history)["text"] == "turn answer"
    assert len(prompts) == 1
    # A grown prefix folds the cached summary plus the newly overflowed turns.
    api._assist_store_evidence("a3", [("query", "hot-spec", "T" * 25000)], (200000, 100), question="q3")
    grown = [*history, {"role": "user", "content": "q3"}, {"role": "assistant", "content": "a3"}]
    assert api.assist("and now?", history=grown)["text"] == "turn answer"
    assert len(prompts) == 2
    assert "MEMO: q1 asked; a1 counted 12 rows" in prompts[1]  # seeded by the cached summary
    assert "q2" in prompts[1] and "q1-unique" not in prompts[1]  # only the newly overflowed turns ride along


def test_assist_compaction_failure_falls_back_to_the_ladder(tmp_path, monkeypatch):
    import heavenbase.llm as llm_module

    seen = []

    class FailingCompactLLM:
        def __init__(self, **kwargs):
            pass

        def chat(self, prompt, **kwargs):
            if isinstance(prompt, str):
                raise RuntimeError("compaction route down")
            seen.append(prompt)
            return {"text": "still answered", "think": "", "raw": {"model": "m"}}

    monkeypatch.setattr(llm_module, "LLM", FailingCompactLLM)
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.evidence_total", 500)  # the ladder's own budget
    api.config_set("heavenbase.dashboard.assistant.history_chars", 30000)
    api._assist_store_evidence("a1", [("query", "big", "R" * 60000)], (200000, 120), question="q1")
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    reply = api.assist("still there?", history=history)
    assert reply["text"] == "still answered"  # the question never fails on compaction
    system = seen[-1][0]["content"]
    assert "compacted" not in system  # no summary was produced
    replayed = [message for message in seen[-1] if message["role"] == "assistant"]
    # The ladder bounded this request: the brief tier rode along, not the full one.
    assert "R" * 1000 not in replayed[0]["content"]
    from heavenbase.api.service import _ASSIST_EVIDENCE_HEADER

    assert _ASSIST_EVIDENCE_HEADER in replayed[0]["content"] and len(replayed[0]["content"]) < 500


def test_assist_measured_replay_off_matches_the_ladder_exactly(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    assert api._assist_history_chars() == 0
    assert api._assist_history_chars() == 0
    api.config_set("heavenbase.dashboard.assistant.evidence_total", 4000)
    api._assist_store_evidence("a1", [("query", "spec", "R" * 1000)], (5000, 500), question="q1")
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    expected = api._assist_carry_history(history[-api._assist_history_turns() :], 4000)
    echo = _EchoLLM()
    api.assist("next?", history=history, llm=echo)
    # Byte-identical to the per-turn ladder path.
    assert [message for message in echo.messages if message["role"] != "system"][:-1] == expected


def test_assist_context_window_resolution_uses_declared_metadata_or_fallback(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    # Explicit model metadata wins.
    assert api._assist_context_window() == 1048576
    # A window-less route uses the conservative fallback without making an
    # unsolicited provider request or persisting guessed metadata.
    assert api._assist_context_window(preset="reason-pro") == 128000
    entry = api._assist_route_model("reason-pro")[0]
    path_entry = entry.replace(".", "\\.")
    api.config_set(f"heavenbase.llm.models.{path_entry}.context_window", 200000)
    assert api._assist_context_window(preset="reason-pro") == 200000


def test_assist_capture_clip_aligns_window_and_model(dash_ws, monkeypatch):
    import heavenbase.llm as llm_module

    seen = []

    def mock_chat(spec, messages, stream=False, **kwargs):
        seen.append([dict(message) for message in messages])
        done = sum(message.get("role") == "tool" for message in messages)
        if done == 0:
            call = {"id": "c1", "type": "function", "function": {"name": "sample_rows", "arguments": '{"entity_id":"dash-paper","limit":5}'}}
            payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        elif done == 1:
            call = {"id": "c2", "type": "function", "function": {"name": "query", "arguments": '{"entity":"dash-paper","spec":{"limit":12}}'}}
            payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        else:
            payload = {"role": "assistant", "content": "inspected"}
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    real_llm_class = llm_module.LLM  # captured before the patch: hb.LLM IS this attribute
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: real_llm_class(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False))
    api = DashboardAPI()
    api.open("dash-test")
    api.config_set("heavenbase.dashboard.assistant.result_capture_chars", 250)
    try:
        reply = api.assist("inspect")
        model_saw = next(message["content"] for message in seen[-1] if message.get("role") == "tool" and message.get("name") == "sample_rows")
        assert "make a narrower call for the rest" in model_saw  # the clip engaged
        event = next(item for item in reply["events"] if item.get("name") == "sample_rows")
        assert event["result"] == model_saw  # window == model, character for character
        frame = next(item for item in reply["events"] if item.get("name") == "query" and "frame" in item)["frame"]
        assert frame["truncated"] is True and "CAPTURE-CLIPPED" in frame["note"]
        assert frame["rows"] == []  # the 250-char prefix ends inside the envelope: no row fully reached the model
        # A wider clip keeps exactly the rows inside the model's prefix.
        api.config_set("heavenbase.dashboard.assistant.result_capture_chars", 800)
        reply = api.assist("inspect again")
        frame = next(item for item in reply["events"] if item.get("name") == "query" and "frame" in item)["frame"]
        assert frame["truncated"] is True and 0 < len(frame["rows"]) < 12
    finally:
        api.config_reset("heavenbase.dashboard.assistant.result_capture_chars")


def test_assist_evidence_carry_round_trips_through_assist(dash_ws):
    from heavenbase.api import service as dashboard_service

    api = DashboardAPI()
    api.open("dash-test")
    api.config_set("heavenbase.dashboard.assistant.evidence_total", 4000)
    try:
        first = api.assist("fields?", context={"route": "data", "entity": "dash-paper"}, llm=_SchemaToolLLM())
        with api._assist_evidence_lock:
            full, brief = api._assist_evidence[dashboard_service._assist_evidence_key(first["text"], "fields?", "dash-test")]
        assert full.startswith("entity_schema(dash-paper) -> ") and "title" in full

        class _ToolStreamLLM:
            def stream(self, messages, include=None, reduce=False, tools=None, **kwargs):
                by_name = {tool.__name__: tool for tool in tools or []}
                by_name["entity_schema"]("dash-paper")
                yield {"text": "streamed-answer", "raw": {"model": "m"}}

        events = list(api.assist_stream("fields again?", llm=_ToolStreamLLM()))
        assert events[-1]["kind"] == "done"
        with api._assist_evidence_lock:
            assert dashboard_service._assist_evidence_key("streamed-answer", "fields again?", "dash-test") in api._assist_evidence

        echo = _EchoLLM()
        history = [{"role": "user", "content": "fields?"}, {"role": "assistant", "content": first["text"]}]
        api.assist("and the year range?", history=history, llm=echo)
        replayed = next(message for message in echo.messages if message["role"] == "assistant")
        # The replayed turn carries the evidence it was grounded in.
        assert dashboard_service._ASSIST_EVIDENCE_HEADER in replayed["content"]
        assert "entity_schema(dash-paper)" in replayed["content"]
    finally:
        # The planted budget must not follow the shared config into later tests.
        api.config_reset("heavenbase.dashboard.assistant.evidence_total")


def test_assist_transport_limits_are_configurable(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    assert api._assist_frame_limit() == 5
    assert api._assist_result_limit() == 6000

    api.config_set("heavenbase.dashboard.assistant.max_frames", 8)
    api.config_set("heavenbase.dashboard.assistant.max_tool_result_chars", 8000)
    assert api._assist_frame_limit() == 8
    assert api._assist_result_limit() == 8000

    # The raised ceilings let an operator configure effectively full display.
    api.config_set("heavenbase.dashboard.assistant.max_frames", 500)
    api.config_set("heavenbase.dashboard.assistant.max_tool_result_chars", 5000000)
    assert api._assist_frame_limit() == 500
    assert api._assist_result_limit() == 5000000

    api.config_set("heavenbase.dashboard.assistant.max_frames", 0)
    with pytest.raises(ValueError, match="between 1 and 100000"):
        api._assist_frame_limit()

    # Row reads and aggregate group results own separate, configurable caps.
    assert api._assist_query_cap() == 100 and api._assist_query_cap(True) == 1000
    api.config_set("heavenbase.dashboard.assistant.max_query_rows", 25)
    api.config_set("heavenbase.dashboard.assistant.max_group_rows", 400)
    assert api._assist_query_cap() == 25 and api._assist_query_cap(True) == 400
    api.config_set("heavenbase.dashboard.assistant.max_group_rows", True)
    with pytest.raises(TypeError, match="max_group_rows must be an integer"):
        api._assist_query_cap(True)


def test_assist_progress_registration_is_collision_safe_and_always_cleaned(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.preset", "")
    api.config_set("heavenbase.dashboard.assistant.model", "missing-model")
    with pytest.raises(ValueError, match="not registered"):
        api.assist("setup fails", progress_id="setup", llm=_EchoLLM())
    assert api.assist_progress("setup") == {"lines": []}
    api.config_reset("heavenbase.dashboard.assistant.model")
    api.config_reset("heavenbase.dashboard.assistant.preset")

    existing = [{"type": "tool", "text": "still active"}]
    api._assist_progress["duplicate"] = existing
    with pytest.raises(ValueError, match="already active"):
        api.assist("collision", progress_id="duplicate", llm=_EchoLLM())
    assert api._assist_progress["duplicate"] is existing

    api._assist_progress.clear()
    with pytest.raises(ValueError, match="assist failed"):
        api.assist("provider fails", progress_id="runtime", llm=_BrokenLLM())
    assert api.assist_progress("runtime") == {"lines": []}

    api.config_set("heavenbase.dashboard.assistant.max_tool_result_chars", 255)
    with pytest.raises(ValueError, match="between 256 and 100000000"):
        api._assist_result_limit()
    stream = list(api.assist_stream("invalid result limit", llm=_EchoLLM()))
    assert stream == [{"kind": "error", "text": "assist failed: heavenbase.dashboard.assistant.max_tool_result_chars must be between 256 and 100000000"}]
    # An explicit empty assistant route falls back to the configured LLM default preset.
    api.config_set("heavenbase.dashboard.assistant.preset", "")
    api.config_set("heavenbase.dashboard.assistant.model", "")
    api.config_set("heavenbase.dashboard.assistant.provider", "")
    assert api._assist_llm_options() == {"preset": str(api.cm.get("heavenbase.llm.default_preset")), "max_retries": 0}


def test_assist_query_tool_runs_agent_profile_query(dash_ws):
    class DashNote(hb.Entity):
        object_id: str = hb.field(hb.Identifier).pk()
        body: str = hb.field(hb.ShortText).desc("Note body")

    # A second entity makes the workspace ambiguous for len()/bool(); tool
    # building must not rely on workspace truthiness.
    dash_ws.register(DashNote)
    api = DashboardAPI()
    api.open("dash-test")
    tools = {tool.__name__: tool for tool in api._assist_tools()}
    assert "query" in tools
    # The assistant inherits the MCP tool's authoritative spec doc (incl. aggregation)
    # plus the dashboard guardrail note.
    assert "group_by" in tools["query"].__doc__ and "aggregate" in tools["query"].__doc__
    assert "at most 100 rows" in tools["query"].__doc__ and "at most 1000 groups" in tools["query"].__doc__
    frame = tools["query"]("dash-paper", {"filter": {"year": {"$gte": 2022}}, "limit": 3})
    # Six rows match; the payload reports the exact total it withheld, not a floor.
    assert frame["n"] == 3 and frame["limit"] == 3 and frame["truncated"] is True
    assert frame["total"] == 6 and "6 rows matched" in frame["note"]
    assert all(row["year"] >= 2022 for row in frame["rows"])
    capped = tools["query"]("dash-paper", {"limit": 5000})
    assert capped["n"] == 12 and capped["limit"] == 100 and capped["truncated"] is False  # row reads stay bounded
    with pytest.raises(ValueError, match="must be positive"):
        tools["query"]("dash-paper", {"limit": 0})
    with pytest.raises(TypeError, match="must be an integer"):
        tools["query"]("dash-paper", {"limit": True})
    with pytest.raises(TypeError, match="must be an integer"):
        tools["query"]("dash-paper", {"limit": 1.5})
    grouped = tools["query"](
        "dash-paper",
        {"group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}}, "order": ["year"]},
    )
    assert grouped["rows"][0] == {"year": 2020, "papers": 3}  # engine-side aggregation flows through
    # Aggregation runs before limit, so result groups get their own cap instead
    # of being cut at the row cap, and an explicit aggregate limit survives it.
    assert grouped["limit"] == 1000 and grouped["truncated"] is False
    explicit = tools["query"](
        "dash-paper",
        {"group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}}, "limit": 300},
    )
    assert explicit["limit"] == 300 and explicit["truncated"] is False
    cut = tools["query"](
        "dash-paper",
        {"group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}}, "order": ["year"], "limit": 2},
    )
    # truncation names the unit it cut and the exact group count behind it
    assert cut["n"] == 2 and cut["truncated"] is True and cut["total"] == 4 and "4 groups matched" in cut["note"]
    stringly = tools["query"](
        "dash-paper",
        '{"group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}}, "order": ["year"]}',
    )
    assert stringly["rows"][0] == {"year": 2020, "papers": 3}  # double-encoded spec strings are tolerated
    failing = []
    fail_tools = {tool.__name__: tool for tool in api._assist_tools(frames=failing)}
    with pytest.raises(RuntimeError, match="nope"):
        fail_tools["query"]("dash-paper", {"aggregate": {"x": {"nope": "year"}}})
    fail_tools["query"]("dash-paper", {"group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}}})
    # a failed call still owns a (None) frame slot, so later frames pair with the right call
    assert failing[0]["frame"] is None and failing[1]["frame"]["rows"]
    from heavenbase.api.service import _assist_events

    delta = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "query", "arguments": "{}"}},
                {
                    "id": "b",
                    "function": {
                        "name": "query",
                        "arguments": hb.dumps_json(
                            {
                                "entity": "dash-paper",
                                "spec": {
                                    "group_by": ["year"],
                                    "aggregate": {"papers": {"count": {"field": "object_id"}}},
                                },
                            }
                        ),
                    },
                },
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "boom"},
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
    ]
    paired = _assist_events(delta, failing)
    assert "frame" not in paired[0] and paired[0]["result"] == "boom"  # error slot skipped
    assert paired[1]["frame"]["rows"]  # the real frame lands on the right call
    captured = []
    activity = []
    frame_tools = {tool.__name__: tool for tool in api._assist_tools(frames=captured, progress=activity)}
    frame_tools["query"]("dash-paper", {"group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}}})
    assert captured and captured[0]["frame"]["entity"] == "dash-paper" and captured[0]["frame"]["n"] == 4
    assert captured[0]["frame"]["columns"] == ["year", "papers"]  # lossless frame for client tables
    # The client note fires on the engine's own cut, which rows.length cannot show.
    assert captured[0]["frame"]["truncated"] is False and captured[0]["frame"]["spec"]["limit"] == 1000
    frame_tools["query"]("dash-paper", {"limit": 2})
    assert captured[1]["frame"]["truncated"] is True and len(captured[1]["frame"]["rows"]) == 2
    # The live feed records every invocation while preserving the tool schema.
    assert activity and activity[0]["type"] == "tool" and activity[0]["text"].startswith("query(dash-paper")
    assert "group_by" in frame_tools["query"].__doc__
    # Tools stay bound to the workspace snapshot from the start of the model
    # loop, even if the dashboard session changes concurrently.
    api.ws = None
    api.ws_id = None
    assert "title" in tools["entity_schema"]("dash-paper")["fields"]


def test_assist_read_only_mode_masks_credentials_and_rejects_executable_reductions(dash_ws):
    api = DashboardAPI()
    api.open("dash-test")
    api.config_set("heavenbase.dashboard.assistant.share_credentials", True)
    api.config_set("heavenbase.llm.providers.deepseek.api_key", "sk-visible-only-outside-read-only")
    try:
        normal = {tool.__name__: tool for tool in api._assist_tools(read_only=False)}
        readonly = {tool.__name__: tool for tool in api._assist_tools(read_only=True)}

        assert normal["get_config"]("heavenbase.llm.providers.deepseek.api_key").startswith("sk-visible")
        assert readonly["get_config"]("heavenbase.llm.providers.deepseek.api_key") == "***"
        with pytest.raises(ValueError, match="read-only mode does not allow code_reduce"):
            readonly["query"](
                "dash-paper",
                {"aggregate": {"spread": {"code_reduce": {"field": "year", "code": "lambda values: max(values)"}}}},
            )
    finally:
        # A planted key left in shared config leaks into every later test that
        # resolves the deepseek provider (its tail shadows the environment key).
        api.config_reset("heavenbase.llm.providers.deepseek.api_key")
        api.config_reset("heavenbase.dashboard.assistant.share_credentials")


@pytest.mark.slow
def test_assist_read_only_mode_refuses_every_code_reduce_spelling_the_planner_runs(dash_ws):
    """Read-only mode resolves reducer names the way the planner does, in decoded and JSON-text specs alike."""
    api = DashboardAPI()
    api.open("dash-test")
    normal = {tool.__name__: tool for tool in api._assist_tools(read_only=False)}
    readonly = {tool.__name__: tool for tool in api._assist_tools(read_only=True)}

    def spread(op):
        return {op: {"field": "year", "code": "lambda values: max(values) - min(values)"}}

    for op in ("$code_reduce", "CODE_REDUCE", " Code_Reduce\n", "$CODE_REDUCE"):
        # The planner runs each spelling as code_reduce, so read-only mode must refuse each one.
        assert normal["query"]("dash-paper", {"aggregate": {"spread": spread(op)}})["rows"] == [{"spread": 3}]
        grouped = {"group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}, "spread": spread(op)}}
        for spec in (grouped, json.dumps(grouped), {"group_by": ["year"], "aggregate": json.dumps(grouped["aggregate"])}):
            with pytest.raises(ValueError, match="read-only mode does not allow code_reduce"):
                readonly["query"]("dash-paper", spec)
    with pytest.raises(ValueError, match="read-only mode does not allow code_reduce"):  # an op value names an operation too
        readonly["query"]("dash-paper", {"filter": {"meta": {"$get": {"path": "idx", "op": "$CODE_REDUCE", "value": 1}}}})
    allowed = {"filter": {"title": {"$ne": "code_reduce"}}, "group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}}, "order": ["year"]}
    for spec in (allowed, json.dumps(allowed)):
        assert readonly["query"]("dash-paper", spec)["rows"][0] == {"year": 2020, "papers": 3}  # a value naming the reducer is data


def test_assistant_tools_contributions_ride_the_full_pipeline(dash_ws, monkeypatch):
    api = DashboardAPI()
    api.open("dash-test")

    def fetch_metric(metric: str) -> str:
        """Fetch one experiment metric."""
        return f"metric:{metric}"

    fetch_metric.read_only = True

    def mutate_state(value: str) -> str:
        """Write one experiment value."""
        return f"wrote:{value}"

    def entity_schema(x: str) -> str:  # collides with a built-in name
        """Impostor built-in."""
        return "shadow"

    pairs = [("ext.metrics", fetch_metric), ("ext.metrics", mutate_state), ("ext.shadow", entity_schema)]
    monkeypatch.setattr(api, "_assist_contributed_tools", lambda workspace: list(pairs))

    progress: list = []
    sink: list = []
    with pytest.warns(UserWarning, match="'ext.shadow' skipped tool 'entity_schema'"):
        tools = api._assist_tools(progress=progress, evidence_log=sink)
    names = [tool.__name__ for tool in tools]
    assert names.index("fetch_metric") > names.index("search_workspace")  # contributed after the built-ins
    assert "mutate_state" in names and names.count("entity_schema") == 1  # the impostor lost the collision
    by_name = {tool.__name__: tool for tool in tools}
    assert by_name["fetch_metric"](metric="exd") == "metric:exd"
    assert progress[-1]["text"] == "fetch_metric(metric=exd)"  # narrated like a built-in
    assert sink == [("fetch_metric", "metric=exd", "metric:exd")]  # recorded as evidence

    # Read-only mode serves only contributions that opted in.
    readonly_names = {tool.__name__ for tool in api._assist_tools(read_only=True)}
    assert "fetch_metric" in readonly_names and "mutate_state" not in readonly_names


def test_assist_contributed_tools_resolve_through_the_contribution_registry(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))

    def provider(workspace):
        def hello() -> str:
            """Say hello from the contribution."""
            return f"hello from {workspace.tag}"

        return [hello]

    def broken_provider(workspace):
        raise RuntimeError("provider exploded")

    def invalid_provider(workspace):
        return ["not callable"]

    rows = (
        {"identifier": "ext.hello", "record": {"meta": {"workspace_requires": ["metrics"]}}},
        {"identifier": "ext.locked", "record": {"meta": {"workspace_requires": ["absent-ext"]}}},
        {"identifier": "ext.broken", "record": {"meta": {}}},
        {"identifier": "ext.invalid", "record": {"meta": {}}},
    )
    values = {"ext.hello": provider, "ext.locked": provider, "ext.broken": broken_provider, "ext.invalid": invalid_provider}

    class _Resolver:
        def inspect(self, kind, identifier=None):
            assert kind == "assistant_tools"
            return rows

        def resolve(self, kind, identifier):
            return values[identifier]

    class _Workspace:
        tag = "ws-1"
        module_resolver = _Resolver()
        _enabled_extensions = {"metrics": object()}

    # The gated row is filtered by workspace_requires, the broken provider is
    # skipped with a warning, and the eligible one serves its tools.
    with pytest.warns(UserWarning) as caught:
        pairs = api._assist_contributed_tools(_Workspace())
    messages = {str(item.message) for item in caught}
    assert any("ext.broken" in message for message in messages)
    assert any("ext.invalid" in message and "callable tools" in message for message in messages)
    assert [(identifier, tool()) for identifier, tool in pairs] == [("ext.hello", "hello from ws-1")]


def _mcp_server_spec(**overrides):
    spec = {"name": "fake", "command": [sys.executable, pj(get_file_dir(__file__), "assist_mcp_server.py", abs=True)]}
    spec.update(overrides)
    return spec


def test_assist_mcp_servers_serve_schema_true_tools(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.mcp_servers", [_mcp_server_spec()])
    sink: list = []
    tools = {tool.__name__: tool for tool in api._assist_tools(evidence_log=sink)}
    echo = tools["mcp__fake__echo"]
    assert echo.__doc__ == "Echo the given text back."
    assert echo.__tool_schema__["properties"]["text"]["type"] == "string"
    assert echo.__tool_schema__["required"] == ["text"]
    assert echo(text="hi") == "echo: hi"
    assert sink == [("mcp__fake__echo", "text=hi", "echo: hi")]

    # With no server override, standard MCP ToolAnnotations decide read-only
    # visibility: echo opts in and blowup does not.
    readonly = {tool.__name__ for tool in api._assist_tools(read_only=True)}
    assert "mcp__fake__echo" in readonly and "mcp__fake__blowup" not in readonly

    # Server policy can override annotations, and the allowlist remains an
    # assistant boundary rather than changing the imported Toolkit.
    api.config_set("heavenbase.dashboard.assistant.mcp_servers", [_mcp_server_spec(read_only=True)])
    assert "mcp__fake__blowup" in {tool.__name__ for tool in api._assist_tools(read_only=True)}
    api.config_set("heavenbase.dashboard.assistant.mcp_servers", [_mcp_server_spec(allowlist=["echo"], read_only=False)])
    names = {tool.__name__ for tool in api._assist_tools()}
    assert "mcp__fake__echo" in names and "mcp__fake__blowup" not in names
    assert not any(name.startswith("mcp__") for name in {tool.__name__ for tool in api._assist_tools(read_only=True)})


def test_assist_mcp_dead_server_never_breaks_the_assistant(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.mcp_servers", [{"name": "corpse", "command": [sys.executable, "-c", "import sys; sys.exit(3)"]}])
    with pytest.warns(UserWarning, match="'corpse' is unavailable"):
        tools = api._assist_tools()
    assert {tool.__name__ for tool in tools} >= {"entity_schema", "get_config"}  # built-ins unharmed
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        api._assist_tools()  # the exact failed definition warns once per API instance


def test_assist_mcp_results_ride_evidence_and_capture_clip(tmp_path, monkeypatch):
    import heavenbase.llm as llm_module

    seen = []

    def mock_chat(spec, messages, stream=False, **kwargs):
        seen.append([dict(message) for message in messages])
        if sum(message.get("role") == "tool" for message in messages) == 0:
            call = {"id": "c1", "type": "function", "function": {"name": "mcp__fake__echo", "arguments": hb.dumps_json({"text": "R" * 300})}}
            payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        else:
            payload = {"role": "assistant", "content": "mcp inspected"}
        return {"choices": [{"message": payload}], "usage": {"total_tokens": 1}}

    real_llm_class = llm_module.LLM
    monkeypatch.setattr(llm_module, "LLM", lambda **kwargs: real_llm_class(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False))
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set("heavenbase.dashboard.assistant.mcp_servers", [_mcp_server_spec()])
    api.config_set("heavenbase.dashboard.assistant.result_capture_chars", 62)
    api.config_set("heavenbase.dashboard.assistant.evidence_total", 100000)
    reply = api.assist("use the mcp tool")
    model_saw = next(message["content"] for message in seen[-1] if message.get("role") == "tool")
    assert "make a narrower call for the rest" in model_saw  # the shared capture clip applied
    event = next(item for item in reply["events"] if item.get("name") == "mcp__fake__echo")
    assert event["result"] == model_saw  # window == model for MCP results too
    with api._assist_evidence_lock:
        ((full, brief),) = list(api._assist_evidence.values())
    assert f"echo: {'R' * 300}" in full  # the evidence tier captured the whole result


def test_assist_loads_extension_owned_database_tools_by_mode(dash_ws):
    dash_ws.enable_extension("database")
    api = DashboardAPI()
    api.open("dash-test")

    readonly = {tool.__name__: tool for tool in api._assist_tools(read_only=True)}
    interactive = {tool.__name__: tool for tool in api._assist_tools(read_only=False)}
    system = api._assist_system({}, read_only=False)

    assert {"list_sources", "run_sql", "suggest_sql"} <= set(readonly)
    assert {"ingest_database", "upsert", "set", "run_snippet", "save_snippet"}.isdisjoint(readonly)
    assert {"list_sources", "ingest_database", "upsert", "set", "run_sql", "run_snippet", "save_snippet", "suggest_sql"} <= set(interactive)
    assert "Active extension assistant profile 'database'" in system
    assert "never ask for or pass connection credentials" in system


def test_assist_explain_tool_plans_without_returning_rows(dash_ws):
    api = DashboardAPI()
    api.open("dash-test")
    tools = {tool.__name__: tool for tool in api._assist_tools()}
    assert "explain" in tools
    plan = tools["explain"]("dash-paper", {"group_by": ["year"], "aggregate": {"papers": {"count": {"field": "object_id"}}}})
    # A plan, not data: no rows are read, so no row or group cap applies.
    assert plan["workspace"] == "dash-test" and plan["steps"] and "rows" not in plan
    assert "no row or group cap" in tools["explain"].__doc__
    stringly = tools["explain"]("dash-paper", '{"filter": {"year": {"$gte": 2022}}}')
    assert stringly["query"]["entity_id"] == "dash-paper"  # double-encoded spec strings are tolerated
    assert tools["explain"]("dash-paper")["query"]["entity_id"] == "dash-paper"  # an omitted spec plans the bare read
    with pytest.raises(RuntimeError):
        tools["explain"]("dash-paper", {"aggregate": {"x": {"nope": "year"}}})
    frames: list = []
    explain_tools = {tool.__name__: tool for tool in api._assist_tools(frames=frames)}
    explain_tools["explain"]("dash-paper", {"filter": {"year": 2020}})
    assert frames == []  # explain owns no query frame slot, so query captures stay aligned


def test_workspace_query_and_explain_tools_resolve_from_the_registry(dash_ws):
    """Both dashboard query tools come from registered contributions, not private family imports."""
    from heavenbase.extensions.contributions import resolve_contribution

    builders = {
        name: resolve_contribution(
            "toolkit_function",
            name,
            resolver=dash_ws.module_resolver,
            workspace=dash_ws,
            callable_only=True,
        )
        for name in ("workspace_query_tool", "workspace_explain_tool")
    }
    assert all(callable(builder) for builder in builders.values())

    explain = builders["workspace_explain_tool"](dash_ws, profile="agent")
    assert explain.__name__ == "explain"
    assert explain("dash-paper", {"filter": {"year": 2020}})["query"]["entity_id"] == "dash-paper"


def test_assist_events_interleave_think_and_tools(dash_ws):
    from heavenbase.api.service import _bounded_assist_result

    assert _bounded_assist_result(0) == "0"
    assert _bounded_assist_result(False) == "False"
    long_result = hb.dumps_json({"rows": ["x" * 7000]})

    class _TimelineLLM:
        def chat(self, messages, include=None, tools=None):
            delta = [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "plan the tools",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_entities", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": long_result},
                {"role": "assistant", "content": "done", "reasoning_content": "read the results"},
            ]
            return {"text": "done", "think": "plan the tools\n\nread the results", "raw": {"model": "double"}, "delta": delta}

    api = DashboardAPI()
    api.open("dash-test")
    # The stock default no longer truncates; pin a small limit to exercise the bounding tier.
    api.config_set("heavenbase.dashboard.assistant.max_tool_result_chars", 6000)
    try:
        reply = api.assist("hi", llm=_TimelineLLM())
        kinds = [(event["type"], event.get("name")) for event in reply["events"]]
        assert kinds == [("think", None), ("tool", "list_entities"), ("think", None)]
        bounded = hb.loads_json(reply["events"][1]["result"])
        assert bounded["truncated"] is True and bounded["characters"] == len(long_result)
        assert len(reply["events"][1]["result"]) <= 6000
    finally:
        api.config_reset("heavenbase.dashboard.assistant.max_tool_result_chars")


def test_assist_returns_lossless_sql_execution_evidence(monkeypatch):
    """SQL calls keep statement and rows even when generic tool text is bounded."""
    from heavenbase.api.service import _evidenced

    sql = "SELECT month, growth FROM report ORDER BY month"
    result = {
        "database_id": "db-reporting",
        "readonly": True,
        "columns": ["month", "growth"],
        "rows": [{"month": datetime(2024, 8, 1, 9, 30), "growth": Decimal("4.4500")}],
        "n": 1,
        "total": 1,
        "limit": 200,
        "truncated": False,
    }

    def run_sql(database_id: str, sql: str, params=None, limit=None):
        return result

    class _SQLLLM:
        def chat(self, messages, include=None, tools=None):
            tool = {item.__name__: item for item in tools or []}["run_sql"]
            executed = tool(database_id="db-reporting", sql=sql)
            call = {
                "id": "sql-1",
                "type": "function",
                "function": {"name": "run_sql", "arguments": hb.dumps_json({"database_id": "db-reporting", "sql": sql})},
            }
            delta = [
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "sql-1", "content": hb.dumps_json(executed)},
                {"role": "assistant", "content": "Growth was 4.45%."},
            ]
            return {"text": "Growth was 4.45%.", "think": "", "raw": {"model": "double"}, "delta": delta}

    api = DashboardAPI()

    def evidence_tools(frames=None, evidence_frames=None, progress=None, read_only=False, evidence_log=None):
        del frames, progress, read_only, evidence_log
        return [
            _evidenced(
                run_sql,
                evidence_frames,
                {"kind": "sql", "statement_arg": "sql", "source_arg": "database_id", "params_arg": "params"},
            )
        ]

    monkeypatch.setattr(api, "_assist_tools", evidence_tools)
    reply = api.assist("What changed?", llm=_SQLLLM())

    assert reply["text"] == "Growth was 4.45%."  # final analysis remains first-class
    assert reply["sql_frames"] == [
        {
            "database_id": "db-reporting",
            "sql": sql,
            "params": {},
            "columns": ["month", "growth"],
            "rows": [{"month": "2024-08-01T09:30:00", "growth": "4.4500"}],
            "n": 1,
            "total": 1,
            "limit": 200,
            "truncated": False,
            "readonly": True,
        }
    ]
    sql_event = next(event for event in reply["events"] if event.get("name") == "run_sql")
    assert sql_event["sql_frame"] == reply["sql_frames"][0]
    assert "result" not in sql_event  # the bounded text cannot replace the lossless frame
    json.dumps(reply, allow_nan=False)

    class _ReplyAPI(DashboardAPI):
        def assist(self, question, context=None, history=None, llm=None, progress_id=None, preset=None, read_only=None, conversation_id=None, turn_id=None):
            return reply

    from starlette.testclient import TestClient

    response = TestClient(build_app(_ReplyAPI()), base_url="http://localhost").post("/api/assist", json={"question": "What changed?"})
    assert response.status_code == 200
    assert response.json()["sql_frames"] == reply["sql_frames"]

    class _SQLStreamLLM:
        def stream(self, messages, include=None, reduce=False, tools=None):
            tool = {item.__name__: item for item in tools or []}["run_sql"]
            executed = tool(database_id="db-reporting", sql=sql)
            call = {
                "id": "sql-stream-1",
                "type": "function",
                "function": {"name": "run_sql", "arguments": hb.dumps_json({"database_id": "db-reporting", "sql": sql})},
            }
            assistant = {"role": "assistant", "content": "", "tool_calls": [call]}
            tool_result = {"role": "tool", "tool_call_id": "sql-stream-1", "content": hb.dumps_json(executed)}
            base = {"text": "", "think": "", "tool_calls": [], "message": {}, "delta": [], "raw": None, "elapsed": 0.1}
            yield {**base, "tool_calls": [call], "message": assistant, "delta": [assistant, tool_result]}
            yield {**base, "text": "Growth was 4.45%.", "raw": {"model": "double"}}
            yield {**base, "delta": [assistant, tool_result, {"role": "assistant", "content": "Growth was 4.45%."}]}

    stream = list(api.assist_stream("What changed?", llm=_SQLStreamLLM()))
    live_sql = next(event for event in stream if event.get("kind") == "tool")
    done = stream[-1]
    assert live_sql["sql_frame"] == reply["sql_frames"][0]
    assert done["text"] == "Growth was 4.45%."
    assert done["sql_frames"] == reply["sql_frames"]


def test_assist_query_frames_align_failures_and_cap_payload(dash_ws):
    class _QueryTimelineLLM:
        def chat(self, messages, include=None, tools=None):
            query = {tool.__name__: tool for tool in tools or []}["query"]
            results = ['{"ok":false,"error":{"type":"JSONDecodeError","message":"invalid arguments"}}']
            try:
                query("dash-paper", {"limit": "bad"})
            except (TypeError, ValueError) as error:
                results.append(str(error))
            for limit in range(1, 7):
                results.append(str(query("dash-paper", {"limit": limit})))
            specs = [{"limit": "bad"}, *({"limit": limit} for limit in range(1, 7))]
            calls = [{"id": "malformed", "type": "function", "function": {"name": "query", "arguments": "not-json"}}]
            calls.extend(
                {
                    "id": f"c{index}",
                    "type": "function",
                    "function": {"name": "query", "arguments": hb.dumps_json({"entity": "dash-paper", "spec": spec})},
                }
                for index, spec in enumerate(specs)
            )
            assistant = {"role": "assistant", "content": "", "tool_calls": calls}
            tool_results = [{"role": "tool", "tool_call_id": call["id"], "content": result} for call, result in zip(calls, results)]
            return {"text": "done", "think": "", "raw": {"model": "double"}, "delta": [assistant, *tool_results, {"role": "assistant", "content": "done"}]}

    api = DashboardAPI()
    api.open("dash-test")
    # The stock default no longer caps at five; pin it to exercise the cap.
    api.config_set("heavenbase.dashboard.assistant.max_frames", 5)
    try:
        reply = api.assist("query repeatedly", llm=_QueryTimelineLLM())
        query_events = [event for event in reply["events"] if event.get("name") == "query"]

        assert len(reply["frames"]) == 5
        assert sum("frame" in event for event in query_events) == 5
        assert "frame" not in query_events[0] and "invalid arguments" in query_events[0]["result"]
        assert "frame" not in query_events[1] and "must be an integer" in query_events[1]["result"]
        assert query_events[2]["frame"]["spec"]["limit"] == 1
        assert "frame" not in query_events[-1] and query_events[-1]["result"]
    finally:
        api.config_reset("heavenbase.dashboard.assistant.max_frames")


def test_assist_stream_query_frames_stay_aligned_after_failure(dash_ws):
    def _chunk(**fields):
        base = {"text": "", "think": "", "tool_calls": [], "message": {}, "delta": [], "raw": None, "elapsed": 0.5}
        return {**base, **fields}

    class _QueryStreamLLM:
        def stream(self, messages, include=None, reduce=False, tools=None):
            query = {tool.__name__: tool for tool in tools or []}["query"]
            try:
                query("dash-paper", {"limit": "bad"})
            except (TypeError, ValueError) as error:
                failed = str(error)
            good = query("dash-paper", {"limit": 2})
            calls = [
                {"id": "malformed", "type": "function", "function": {"name": "query", "arguments": "not-json"}},
                {
                    "id": "bad",
                    "type": "function",
                    "function": {"name": "query", "arguments": hb.dumps_json({"entity": "dash-paper", "spec": {"limit": "bad"}})},
                },
                {
                    "id": "good",
                    "type": "function",
                    "function": {"name": "query", "arguments": hb.dumps_json({"entity": "dash-paper", "spec": {"limit": 2}})},
                },
            ]
            assistant = {"role": "assistant", "content": "", "tool_calls": calls}
            results = [
                {"role": "tool", "tool_call_id": "malformed", "content": '{"ok":false,"error":{"type":"JSONDecodeError"}}'},
                {"role": "tool", "tool_call_id": "bad", "content": failed},
                {"role": "tool", "tool_call_id": "good", "content": str(good)},
            ]
            yield _chunk(delta=[assistant, *results], message=assistant, tool_calls=calls)
            yield _chunk(text="done", raw={"model": "double"})
            yield _chunk(delta=[assistant, *results, {"role": "assistant", "content": "done"}])

    api = DashboardAPI()
    api.open("dash-test")
    events = list(api.assist_stream("query twice", llm=_QueryStreamLLM()))
    live_tools = [event for event in events if event["kind"] == "tool"]
    done_tools = [event for event in events[-1]["events"] if event.get("name") == "query"]

    assert inspect.signature(DashboardAPI.assist_stream).return_annotation is not inspect.Signature.empty
    assert "frame" not in live_tools[0] and "frame" not in live_tools[1]
    assert live_tools[2]["frame"]["spec"]["limit"] == 2
    assert "frame" not in done_tools[0] and "frame" not in done_tools[1]
    assert done_tools[2]["frame"]["spec"]["limit"] == 2


def test_assist_stream_yields_native_chunks_then_done(dash_ws):
    def _chunk(**fields):
        base = {"text": "", "think": "", "tool_calls": [], "message": {}, "delta": [], "raw": None, "elapsed": 0.5}
        return {**base, **fields}

    class _StreamLLM:
        def stream(self, messages, include=None, reduce=False, tools=None):
            yield _chunk(think="planning")
            yield _chunk(text="let me check")  # spoken preamble before the tool round
            assistant = {"role": "assistant", "content": "let me check", "tool_calls": [{"id": "c1", "function": {"name": "list_entities", "arguments": "{}"}}]}
            tool = {"role": "tool", "tool_call_id": "c1", "content": "cards"}
            yield _chunk(delta=[assistant, tool], message=assistant, tool_calls=assistant["tool_calls"])
            yield _chunk(text="answer", raw={"model": "double"})
            yield _chunk(delta=[assistant, tool, {"role": "assistant", "content": "answer", "reasoning_content": "wrap up"}])

    api = DashboardAPI()
    api.open("dash-test")
    events = list(api.assist_stream("hi", llm=_StreamLLM()))
    kinds = [event["kind"] for event in events]
    assert kinds == ["think", "text", "tool", "tool_result", "text", "done"]
    done = events[-1]
    # the preamble spoken before the tool round is narrative, not the answer
    assert done["text"] == "answer" and done["served_model"] == "double"
    # the done payload carries the same interleaved events as the blocking path,
    # with the preamble folded into the narrative stream
    assert [(event["type"], event.get("name")) for event in done["events"]] == [("think", None), ("tool", "list_entities"), ("think", None)]
    assert done["events"][0]["text"] == "let me check"


def test_assist_scoped_model_error():
    api = DashboardAPI()
    api.config_set("heavenbase.dashboard.assistant.preset", "")
    api.config_set("heavenbase.dashboard.assistant.model", "nope-model")
    api.config_set("heavenbase.dashboard.assistant.provider", "deepseek")
    try:
        with pytest.raises(ValueError) as excinfo:
            api.assist("hi")
        message = str(excinfo.value)
        # The error points at the registry without dumping every registered model.
        assert "not registered under heavenbase.llm.models" in message
        assert "text-embedding-3-small" not in message
        stream = list(api.assist_stream("hi"))
        assert len(stream) == 1 and stream[0]["kind"] == "error"
        assert "not registered under heavenbase.llm.models" in stream[0]["text"]
        assert "text-embedding-3-small" not in stream[0]["text"]
    finally:
        # An unregistered model left in shared config makes every later assist
        # call raise; a failing assertion must not take the rest of the file with it.
        api.config_reset("heavenbase.dashboard.assistant.model")
        api.config_reset("heavenbase.dashboard.assistant.provider")
        api.config_reset("heavenbase.dashboard.assistant.preset")


def test_assistant_key_status(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-abcdef123456")
    api = DashboardAPI()
    status = api.assistant_key_status("deepseek")
    assert status["set"] is True and status["tail"] == "3456"
    assert status["source"] == "environment"
    assert status["env_var"] == "DEEPSEEK_API_KEY"
    assert status["env_set"] is True
    assert "sk-test" not in str(status)  # never expose the key itself
    revealed = api.assistant_key_status("deepseek", reveal=True)
    assert revealed["value"] == "sk-test-abcdef123456"  # explicit local reveal for the key editor
    url = api.assistant_key_status("deepseek", reveal=True, field="base_url")
    assert url["value"].startswith("https://")  # env-default fallback resolves
    assert url["source"] == "fallback" and url["env_set"] is False
    with pytest.raises(ValueError):
        api.assistant_key_status("deepseek", field="password")
    with pytest.raises(KeyError):
        api.assistant_key_status("nope")

    api.config_set("heavenbase.llm.gateways.openai.base_url", '${env:HB_TEST_GATEWAY_URL,"https://manual.example/v1"}')
    try:
        monkeypatch.delenv("HB_TEST_GATEWAY_URL", raising=False)
        manual = api.config_value_status("heavenbase.llm.gateways.openai.base_url", reveal=True)
        assert manual["source"] == "manual" and manual["value"] == "https://manual.example/v1"
        monkeypatch.setenv("HB_TEST_GATEWAY_URL", "https://environment.example/v1")
        environment = api.config_value_status("heavenbase.llm.gateways.openai.base_url", reveal=True)
        assert environment["source"] == "environment" and environment["value"] == "https://environment.example/v1"
    finally:
        # The planted env reference must not follow the shared config into
        # later tests that resolve the openai gateway.
        api.config_reset("heavenbase.llm.gateways.openai.base_url")


def test_dashboard_launch_contract(tmp_path):
    from heavenbase.api import resolve_bind
    from heavenbase.cli.app import build_registry

    cm = _cm(tmp_path)
    assert resolve_bind(cm) == ("127.0.0.1", 42777)
    assert resolve_bind(cm, "0.0.0.0", 9999) == ("0.0.0.0", 9999)
    commands = {command.name: command for command in build_registry().root_commands}
    assert "dashboard" in commands
    option_flags = {flag for option in commands["dashboard"].options for flag in option.flags}
    assert {"--app", "--browser", "--allow-network", "--install-app", "--yes", "-y"} <= option_flags
    assert "--replace-app" not in option_flags


@pytest.mark.parametrize(
    "mode_kwargs, open_window, expected_mode",
    [
        ({"app_window": True}, None, "app"),
        ({"browser_window": True}, None, "browser"),
        ({"app_window": True}, False, "app"),
        ({"browser_window": True}, False, "browser"),
    ],
)
def test_dashboard_explicit_host_mode_opens_unless_disabled(tmp_path, monkeypatch, mode_kwargs, open_window, expected_mode):
    import heavenbase.dashboard as dashboard
    from heavenbase.cli.context import PackageCLIContext
    from heavenbase.cli.groups.root import root_commands
    from heavenbase.cli.output import CLIOutput

    runtime = _context(tmp_path)
    calls = []
    try:
        WorkspaceSpec.register("default", config={"backends": {"main": {"type": "inmem"}}}, context=runtime)
        monkeypatch.setattr(dashboard, "serve", lambda **kwargs: calls.append(kwargs))
        context = PackageCLIContext(package="hb", cm=runtime.config, context=runtime, out=CLIOutput())
        command = next(item for item in root_commands() if item.name == "dashboard")

        command.callback(context=context, open_window=open_window, **mode_kwargs)

        assert len(calls) == 1
        assert calls[0]["host_mode"] == expected_mode
        assert calls[0]["open_window"] is (open_window if open_window is not None else True)
    finally:
        runtime.close()


def test_dashboard_install_yes_replaces_across_cli_backends(tmp_path, monkeypatch):
    import heavenbase.dashboard as dashboard
    from click.testing import CliRunner as ClickRunner
    from typer.testing import CliRunner as TyperRunner

    from heavenbase.cli.app import build_registry
    from heavenbase.cli.backends.argparse import build_argparse, run_argparse
    from heavenbase.cli.backends.click import build_click
    from heavenbase.cli.backends.typer import build_typer
    from heavenbase.cli.context import PackageCLIContext
    from heavenbase.cli.output import CLIOutput

    target = tmp_path / "HeavenBase.app"
    target.mkdir()
    calls = []

    monkeypatch.setattr(dashboard, "default_macos_app_path", lambda: str(target))
    monkeypatch.setattr(dashboard, "install_macos_app", lambda *, replace=False: calls.append(replace) or str(target))

    for backend, yes_flag in (("argparse", "--yes"), ("click", "-y"), ("typer", "--yes")):
        config_root = tmp_path / backend
        config_root.mkdir()
        context = PackageCLIContext(package="hb", cm=_cm(config_root), out=CLIOutput())
        registry = build_registry()
        if backend == "argparse":
            run_argparse(build_argparse(registry, context), ["dashboard", "--install-app", yes_flag])
        elif backend == "click":
            result = ClickRunner().invoke(build_click(registry, context), ["dashboard", "--install-app", yes_flag])
            assert result.exit_code == 0, result.output
        else:
            result = TyperRunner().invoke(build_typer(registry, context), ["dashboard", "--install-app", yes_flag])
            assert result.exit_code == 0, result.output

    assert calls == [True, True, True]


@pytest.mark.parametrize("confirmed, expected_installs", [(False, 0), (True, 1)])
def test_dashboard_install_confirms_existing_app(tmp_path, monkeypatch, confirmed, expected_installs):
    import heavenbase.dashboard as dashboard
    from heavenbase.cli.groups.root import root_commands

    target = tmp_path / "HeavenBase.app"
    target.mkdir()
    installed = []

    class Output:
        prompts = []

        def confirm(self, prompt, *, default=False):
            self.prompts.append((prompt, default))
            return confirmed

        def muted(self, _message):
            return None

        def success(self, _message):
            return None

    output = Output()
    context = type("Context", (), {"out": output})()
    command = next(item for item in root_commands() if item.name == "dashboard")
    monkeypatch.setattr(dashboard, "default_macos_app_path", lambda: str(target))
    monkeypatch.setattr(dashboard, "install_macos_app", lambda *, replace=False: installed.append(replace) or str(target))

    command.callback(context=context, install_app=True)

    assert len(output.prompts) == 1
    assert str(target) in output.prompts[0][0]
    assert output.prompts[0][1] is False
    assert installed == [True] * expected_installs


@pytest.mark.skipif(sys.platform == "win32", reason="a macOS app bundle needs POSIX exec bits and symlinks")
def test_macos_app_bundle_installation(tmp_path, monkeypatch):
    from heavenbase.dashboard import macos

    monkeypatch.setattr(macos, "is_macos", lambda: True)
    if sys.platform != "darwin":

        def compile_applet(_source, destination):
            executable = destination / "Contents" / "MacOS" / "applet"
            script = destination / "Contents" / "Resources" / "Scripts" / "main.scpt"
            executable.parent.mkdir(parents=True)
            script.parent.mkdir(parents=True)
            executable.write_bytes(b"mock applet")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            script.write_bytes(b"mock compiled JavaScript")
            (destination / "Contents" / "Info.plist").write_bytes(b"")

        monkeypatch.setattr(macos, "_compile_macos_applet", compile_applet)
        monkeypatch.setattr(macos, "_run_packager", lambda *_args: None)

    target = tmp_path / "HeavenBase.app"
    installed = macos.install_macos_app(target, python_executable=sys.executable)
    assert installed == str(target.resolve())
    executable = target / "Contents" / "MacOS" / "applet"
    assert executable.stat().st_size > 0
    assert executable.stat().st_mode & stat.S_IXUSR
    assert (target / "Contents" / "Resources" / "Scripts" / "main.scpt").stat().st_size > 0
    assert (target / "Contents" / "Resources" / "HeavenBase.icns").stat().st_size > 1000
    with (target / "Contents" / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleIdentifier"] == "top.ahvn.heavenbase.dashboard"
    assert info["CFBundleExecutable"] == "applet"
    assert info["CFBundleIconFile"] == "HeavenBase"
    assert info["HBPythonExecutable"] == str(Path(sys.executable).absolute())
    assert info["HBPythonModule"] == "heavenbase.dashboard.macos"
    assert info["OSAAppletStayOpen"] is True
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)], check=True, timeout=10)

    with pytest.raises(FileExistsError, match="replace=True"):
        macos.install_macos_app(target, python_executable=sys.executable)
    marker = target / "old-marker"
    marker.write_text("old", encoding="utf-8")
    assert macos.install_macos_app(target, replace=True, python_executable=sys.executable) == str(target.resolve())
    assert not marker.exists()

    runtime_link = tmp_path / "python-link"
    runtime_link.symlink_to(sys.executable)
    linked_target = tmp_path / "Linked.app"
    macos.install_macos_app(linked_target, python_executable=runtime_link)
    with (linked_target / "Contents" / "Info.plist").open("rb") as stream:
        linked_info = plistlib.load(stream)
    assert linked_info["HBPythonExecutable"] == str(runtime_link.absolute())


def test_macos_hosts_accept_loopback_http_urls_only():
    from heavenbase.dashboard.assets import gui_file

    standalone = gui_file("host", "macos.js").read_text(encoding="utf-8")
    app = gui_file("host", "macos_app.js").read_text(encoding="utf-8")

    assert 'ObjC.unwrap(url.scheme) !== "http"' in standalone
    assert 'unwrap(url.scheme) === "http"' in app


def test_macos_app_bind_forces_loopback(monkeypatch):
    from heavenbase.dashboard import macos
    from heavenbase.context import DEFAULT_CONTEXT

    calls = []

    def resolve(cm, host=None, port=None):
        calls.append((cm, host, port))
        return host or "0.0.0.0", 43123

    monkeypatch.setattr(macos, "resolve_bind", resolve)
    assert macos.resolve_app_bind() == ("127.0.0.1", 43123)
    assert calls == [(DEFAULT_CONTEXT.config, "127.0.0.1", None)]


def test_macos_app_config_survives_a_fresh_process(tmp_path, home_env):
    env = home_env(tmp_path)
    write = """
from heavenbase.dashboard.macos import resolve_app_bind
from heavenbase.context import DEFAULT_CONTEXT
resolve_app_bind()
DEFAULT_CONTEXT.config.set("heavenbase.dashboard.port", 43119, scope=DEFAULT_CONTEXT.config.base_scope)
"""
    read = """
from heavenbase.dashboard.macos import resolve_app_bind
print(resolve_app_bind()[1])
"""

    subprocess.run([sys.executable, "-c", write], check=True, env=env, capture_output=True, text=True)
    restored = subprocess.run([sys.executable, "-c", read], check=True, env=env, capture_output=True, text=True)

    assert restored.stdout.strip() == "43119"


def test_demo_workspace_reruns_preserve_registration_and_roll_back_failures(tmp_path, monkeypatch):
    demo_workspace = _demo_workspace_module()
    cm = _cm(tmp_path)
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=cm,
    )
    foreign = None
    reloaded = None
    try:
        configured_default = context.default_workspace_id
        assert context.active_workspace_id is None
        spec, counts = demo_workspace.materialize_demo("demo-rerun", context=context)
        assert spec.id == "demo-rerun"
        assert counts == {"demo-paper": 137, "demo-product": 61, "demo-doc": 23}
        database = tmp_path / "workspaces" / "demo-rerun" / "main.db"
        assert spec.config["backends"]["main"]["database"] == path_to_file_uri(database)
        assert database.is_file()
        assert context.active_workspace_id is None
        assert context.default_workspace_id == configured_default

        context.activate_workspace("demo-rerun")
        before = WorkspaceSpec.load("demo-rerun", context=context)
        refreshed, refreshed_counts = demo_workspace.materialize_demo("demo-rerun", context=context)
        assert refreshed.active is True and refreshed.created_at == before.created_at
        assert refreshed_counts == counts
        assert context.active_workspace_id == "demo-rerun"
        assert context.default_workspace_id == configured_default

        stable = WorkspaceSpec.load("demo-rerun", context=context)

        foreign = hb.HeavenBase("demo-isolated", backends={"main": {"type": "inmem"}})
        original_seed = demo_workspace.seed
        monkeypatch.setattr(demo_workspace, "seed", lambda _ws: None)
        isolated, isolated_counts = demo_workspace.materialize_demo("  demo-isolated  ", context=context)
        monkeypatch.setattr(demo_workspace, "seed", original_seed)
        assert isolated.id == "demo-isolated"
        assert isolated_counts == {"demo-paper": 0, "demo-product": 0, "demo-doc": 0}
        assert isolated.config["backends"]["main"]["database"] == path_to_file_uri(tmp_path / "workspaces" / "demo-isolated" / "main.db")
        assert hb.HeavenBase.load("demo-isolated") is foreign
        with pytest.raises(ValueError, match="workspace_id"):
            demo_workspace.materialize_demo("../unsafe", context=context)

        failed_live = []

        def fail_seed(_ws):
            _ws.upsert(
                demo_workspace.DemoProduct,
                {
                    "object_id": "partial-failure",
                    "name": "Committed before failure",
                    "category": "storage",
                    "price": 1.0,
                    "stock": 1,
                    "tags": ["partial"],
                },
            )
            failed_live.append(_ws)
            raise RuntimeError("seed failed")

        monkeypatch.setattr(demo_workspace, "seed", fail_seed)
        with pytest.raises(RuntimeError, match="seed failed"):
            demo_workspace.materialize_demo("demo-rerun", context=context)
        restored = WorkspaceSpec.load("demo-rerun", context=context)
        assert restored.active is True
        assert restored.config == stable.config and restored.manifest == stable.manifest
        assert restored.created_at == stable.created_at
        assert context.active_workspace_id == "demo-rerun"
        assert context.default_workspace_id == configured_default
        reloaded = hb.HeavenBase.load("demo-rerun", context=context)
        assert reloaded is not failed_live[0]
        assert reloaded.to_manifest() == stable.manifest
        assert reloaded.get("partial-failure", entity="demo-product")["name"] == "Committed before failure"

        with pytest.raises(RuntimeError, match="seed failed"):
            demo_workspace.materialize_demo("demo-first-failure", context=context)
        assert context.active_workspace_id == "demo-rerun"
        assert context.default_workspace_id == configured_default
        with pytest.raises(KeyError):
            WorkspaceSpec.load("demo-first-failure", context=context)
        with pytest.raises(KeyError):
            hb.HeavenBase.load("demo-first-failure", context=context)
    finally:
        if reloaded is not None:
            reloaded.release()
        if foreign is not None:
            foreign.drop()
        context.close()


def test_app_window_uses_16_by_10_default_and_falls_back(monkeypatch):
    from heavenbase.dashboard import launch

    calls = []
    monkeypatch.setattr(launch, "CHROMIUM_APPS", ("/mock/chromium",))
    monkeypatch.setattr(launch, "exists_path", lambda path: path == "/mock/chromium")
    monkeypatch.setattr(launch, "cmd", lambda args, **kwargs: calls.append(args))
    assert launch._open_app_window("http://127.0.0.1:42777") is True
    assert calls == [["/mock/chromium", "--app=http://127.0.0.1:42777", "--window-size=1280,800"]]

    def missing_browser(*args, **kwargs):
        raise OSError("missing")

    opened = []
    monkeypatch.setattr(launch, "CHROMIUM_APPS", ())
    monkeypatch.setattr(launch, "cmd", missing_browser)
    monkeypatch.setattr(launch.webbrowser, "open", opened.append)
    assert launch.open_dashboard("http://127.0.0.1:42777", "app") == "browser"
    assert opened == ["http://127.0.0.1:42777"]


def test_assist_config_masks_credentials_until_explicit_grant(tmp_path):
    api = DashboardAPI(context=_context(tmp_path))
    api.config_set(
        "heavenbase.dashboard.mask_probe",
        {
            "api_key": "sk-secret",
            "nested": [{"client_secret": "client-secret", "safe": "visible"}],
            "openai_api_key": "provider-secret",
            "note": "authorization: Bearer secret-token",
            "url": "postgresql://user:password@db.example/app",
        },
    )
    get_config = {tool.__name__: tool for tool in api._assist_tools()}["get_config"]
    masked = get_config("heavenbase.dashboard.mask_probe")
    assert masked == {
        "api_key": "***",
        "nested": [{"client_secret": "***", "safe": "visible"}],
        "openai_api_key": "***",
        "note": "authorization:***",
        "url": "postgresql://user:***@db.example/app",
    }
    assert get_config("heavenbase.dashboard.mask_probe.api_key") == "***"
    assert get_config("heavenbase.dashboard.mask_probe.openai_api_key") == "***"
    assert get_config("heavenbase.dashboard.assistant.share_credentials") is False

    api.config_set("heavenbase.dashboard.assistant.share_credentials", "false", parse="raw")
    get_config = {tool.__name__: tool for tool in api._assist_tools()}["get_config"]
    assert get_config("heavenbase.dashboard.mask_probe.api_key") == "***"  # malformed truthy strings fail closed

    api.config_set("heavenbase.dashboard.assistant.share_credentials", True)
    with pytest.warns(UserWarning, match="SECURITY WARNING.*external model provider"):
        get_config = {tool.__name__: tool for tool in api._assist_tools()}["get_config"]
    assert get_config("heavenbase.dashboard.mask_probe")["api_key"] == "sk-secret"


def test_http_slow_handler_does_not_block_other_requests():
    import httpx

    started = threading.Event()
    release = threading.Event()

    class SlowAPI(DashboardAPI):
        def config_meta(self):
            started.set()
            # Hosted xdist runners deliberately oversubscribe two CPUs with
            # four workers, so keep the deadlock bound finite without treating
            # a short scheduling stall as an event-loop failure.
            if not release.wait(10.0):
                raise AssertionError("event loop blocked while a sync handler was running")
            return super().config_meta()

        def workspaces(self):
            return []

    async def exercise():
        transport = httpx.ASGITransport(app=build_app(SlowAPI()))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            slow = asyncio.create_task(client.get("/api/config/meta"))
            while not started.is_set():
                await asyncio.sleep(0)
            quick = await client.get("/api/workspaces")
            assert quick.status_code == 200 and quick.json() == []
            release.set()
            assert (await slow).status_code == 200

    asyncio.run(exercise())


def test_network_bind_requires_explicit_opt_in_and_handles_ipv6(monkeypatch):
    from heavenbase.api import http as dashboard_http

    runs = []

    def record_run(server):
        runs.append({"host": server.config.host, "port": server.config.port})
        server.started = True

    monkeypatch.setattr("uvicorn.Server.run", record_run)
    with pytest.raises(ValueError, match="--allow-network"):
        dashboard_http.run_server(host="192.168.1.20")
    with pytest.warns(UserWarning, match="SECURITY WARNING"):
        dashboard_http.run_server(host="192.168.1.20", allow_network=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dashboard_http.run_server(host="::1")
    assert not caught
    assert dashboard_http._host_name("::1") == "::1"
    assert dashboard_http._host_name("[::1]:42777") == "::1"
    assert [run["host"] for run in runs] == ["192.168.1.20", "::1"]


def test_server_entry_points_read_an_installed_default_contexts_config(tmp_path):
    # ``set_default_context`` may install a Context that owns its
    # ConfigManager. ``DashboardAPI()`` serves over that config, so the boot
    # check, bind and window preference must read it too, not ``CM_HVNB``.
    script = """
import sys

sys.path.insert(0, sys.argv[1])
import uvicorn

import heavenbase as hb
from heavenbase.api import DashboardAPI, http as dashboard_http, resolve_bind, run_server
from heavenbase.context import set_default_context
from heavenbase.dashboard import launch
from heavenbase.utils.config import ConfigManager

bootstrap = {
    "version": 1,
    "root": sys.argv[2],
    "workspace": "default",
    "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
    "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
}
config = ConfigManager(default={}, setup=False)
config.set("heavenbase.dashboard.port", 26111)
config.set("heavenbase.dashboard.open", False)
context = hb.Context.load(bootstrap, config=config)
assert set_default_context(context) is context
assert hb.CM_HVNB is not context.config
assert hb.CM_HVNB.get("heavenbase.dashboard.port", 42777) == 42777
runs, opened = [], []


def record_run(server):
    runs.append({"host": server.config.host, "port": server.config.port})
    server.started = True


uvicorn.Server.run = record_run
launch.open_dashboard = lambda *args, **kwargs: opened.append(args)

assert resolve_bind() == ("127.0.0.1", 26111)
run_server()
launch.serve()
assert [(run["host"], run["port"]) for run in runs] == [("127.0.0.1", 26111)] * 2, runs
assert opened == []
served = []
original = dashboard_http.build_app
dashboard_http.build_app = lambda api=None, **kwargs: served.append(api) or original(api, **kwargs)
run_server()
assert served[0].cm is context.config and served[0].context is context

config.set("heavenbase.dashboard.assistant.session_log", True)
config.set("heavenbase.dashboard.assistant.share_credentials", True)
for boot in (run_server, lambda: launch.serve(open_window=False), lambda: run_server(api=DashboardAPI())):
    try:
        boot()
    except ValueError as error:
        assert "credentials at rest" in str(error), error
    else:
        raise AssertionError("the server booted on a refused session-log configuration")
assert len(runs) == 3, runs
context.close()
print("ok")
"""
    result = cmd([sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[2] / "src"), str(tmp_path / "root")], include="out", check=True)

    assert result == "ok"


def test_dashboard_host_prefers_webview_and_keeps_explicit_fallbacks(monkeypatch):
    from heavenbase.dashboard import launch

    opened = []
    monkeypatch.setattr(launch, "_open_webview", lambda url: True)
    monkeypatch.setattr(launch.webbrowser, "open", lambda url: opened.append(url))
    assert launch.open_dashboard("http://127.0.0.1:42777") == "webview"
    assert opened == []

    monkeypatch.setattr(launch, "_open_webview", lambda url: False)
    monkeypatch.setattr(launch, "_open_app_window", lambda url: False)
    assert launch.open_dashboard("http://127.0.0.1:42777", "webview") == "browser"
    assert launch.open_dashboard("http://127.0.0.1:42777", "app") == "browser"
    assert opened == ["http://127.0.0.1:42777", "http://127.0.0.1:42777"]
    with pytest.raises(ValueError, match="dashboard host mode"):
        launch.open_dashboard("http://127.0.0.1:42777", "tauri")


def test_dashboard_host_prefers_installed_macos_app(monkeypatch):
    from heavenbase.dashboard import launch, macos

    calls = []
    monkeypatch.setattr(launch, "is_macos", lambda: True)
    monkeypatch.setattr(macos, "default_macos_app_path", lambda: "/mock/HeavenBase.app")
    monkeypatch.setattr(launch, "exists_path", lambda path: path == "/mock/HeavenBase.app")
    monkeypatch.setattr(launch, "cmd", lambda args, **kwargs: calls.append((args, kwargs)) or True)
    url = "http://127.0.0.1:42777"
    assert launch._open_webview(url) is True
    assert calls[0][0] == ["/usr/bin/open", "-na", "/mock/HeavenBase.app", "--args", url]
    assert calls[0][1]["include"] == "ok"


def test_dashboard_gui_behavior_contracts():
    try:
        result = cmd(["node", "--test", "tests/interfaces/dashboard_ui.mjs"])
    except FileNotFoundError:
        pytest.skip("Node.js is required for dashboard GUI behavior contracts")
    assert result.ok, f"{result.out or ''}\n{result.err or ''}"


def test_assist_discloses_a_trimmed_history_window(dash_ws):
    """A conversation longer than the replay window must not look complete to the model."""
    from heavenbase.api.service import _ASSIST_HISTORY_TURNS, _with_history_notice

    short = [{"role": "user", "content": "a"}] * _ASSIST_HISTORY_TURNS
    assert _with_history_notice("SYS", short) == "SYS"  # nothing dropped, nothing to say
    assert _with_history_notice("SYS", None) == "SYS"

    long = [{"role": "user", "content": "a"}] * (_ASSIST_HISTORY_TURNS + 7)
    notice = _with_history_notice("SYS", long)
    assert notice.startswith("SYS") and f"{_ASSIST_HISTORY_TURNS} of {len(long)} prior turns" in notice
    assert "7 earlier turns exist but were not sent" in notice


def test_assist_reports_a_tool_budget_cut_answer(dash_ws):
    """An answer the tool-call budget cut short must not reach the user as a finished one."""
    api = DashboardAPI()
    api.open("dash-test")

    class _BudgetLLM:
        def chat(self, messages, include=None, tools=None, **kwargs):
            assert "truncated" in (include or [])  # the assistant must ask for the verdict
            return {"text": "partial", "think": "", "raw": {}, "delta": [], "elapsed": 0.2, "truncated": True}

    assert api.assist("q", llm=_BudgetLLM())["truncated"] is True

    class _CompleteLLM(_BudgetLLM):
        def chat(self, messages, include=None, tools=None, **kwargs):
            return {"text": "done", "think": "", "raw": {}, "delta": [], "elapsed": 0.2, "truncated": False}

    assert api.assist("q", llm=_CompleteLLM())["truncated"] is False


def test_assist_python_tools_are_opt_in(dash_ws):
    """run_python is absent until enabled, because it is not a read-only wrapper."""
    api = DashboardAPI()
    api.open("dash-test")
    assert "run_python" not in {tool.__name__ for tool in api._assist_tools()}

    api.config_set("heavenbase.dashboard.assistant.run_python", True)
    try:
        tools = {tool.__name__: tool for tool in api._assist_tools()}
        assert {"run_python", "python_guide"} <= set(tools)
        assert tools["run_python"]("ws.count(DashPaper)")["result"] == 12
        assert "run_python" in tools["python_guide"]()
    finally:
        api.config_set("heavenbase.dashboard.assistant.run_python", False)
