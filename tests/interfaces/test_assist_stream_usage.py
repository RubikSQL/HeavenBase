"""Streamed assistant turns record the same merged usage as blocking turns."""

import re

import pytest

import heavenbase as hb
from heavenbase.api.service import DashboardAPI
import heavenbase.extensions.agent_log as agent_log_package
from heavenbase.utils import path_to_file_uri
from heavenbase.workspace.registry import WorkspaceSpec

pytestmark = pytest.mark.fast

_ASSIST = "heavenbase.dashboard.assistant"


def _two_round_mock(round_usages):
    """Return a mock provider: one ``list_entities`` round, then an answer, each with its own usage."""

    def mock_chat(spec, messages, stream=False, **kwargs):
        done = sum(message.get("role") == "tool" for message in messages)
        usage = dict(round_usages[min(done, len(round_usages) - 1)])
        if done >= 1 or "tools" not in kwargs:
            payload = {"role": "assistant", "content": "two entities"}
        else:
            call = {"id": "c0", "type": "function", "function": {"name": "list_entities", "arguments": "{}"}}
            payload = {"role": "assistant", "content": "", "tool_calls": [call]}
        if stream:
            delta = dict(payload)
            if "tool_calls" in payload:
                delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([{"choices": [{"delta": delta}], "usage": usage}])
        return {"choices": [{"message": payload}], "usage": usage}

    return mock_chat


@pytest.fixture()
def usage_api(monkeypatch, tmp_path):
    """Build a session-logged DashboardAPI whose provider reports the given per-round usage."""
    import heavenbase.llm as llm_module

    suffix = re.sub(r"[^a-z0-9]", "", tmp_path.name.lower())[-8:] or "x"
    log_ws = f"assist-usage-log-{suffix}"
    data_ws = f"assist-usage-data-{suffix}"
    hb.CM_HVNB.set(f"{_ASSIST}.session_log", True)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_workspace", log_ws)
    hb.CM_HVNB.set(f"{_ASSIST}.session_log_backend", {"main": {"type": "sqlite", "name": "main", "database": path_to_file_uri(tmp_path / "session-log.db")}})
    real_llm = llm_module.LLM
    ws = hb.HeavenBase(data_ws, backends={"main": {"type": "inmem"}})
    WorkspaceSpec.register(data_ws, config={"backends": {"main": {"type": "inmem"}}})
    api = DashboardAPI()
    api.open(data_ws)

    def build(round_usages, stream_chat=True):
        mock = _two_round_mock(round_usages)
        monkeypatch.setattr(
            llm_module, "LLM", lambda **kwargs: real_llm(preset="mock", mock_chat=mock, mock_pause=(0, 0), cache=False, stream_chat=stream_chat)
        )
        return api

    try:
        yield build, log_ws
    finally:
        for key in ("session_log", "session_log_workspace", "session_log_backend"):
            hb.CM_HVNB.unset(f"{_ASSIST}.{key}")
        for name in (log_ws, data_ws):
            try:
                hb.HeavenBase.drop(name)
            except Exception:
                pass
            try:
                WorkspaceSpec.unregister(name)
            except Exception:
                pass
        del ws


def _turn_usage(log_ws, conversation_id):
    rows = hb.HeavenBase(log_ws).rows(agent_log_package.AgentTrace)
    (turn,) = [row for row in rows if row["kind"] == "turn" and row["conversation_id"] == conversation_id]
    assert not turn["payload"].get("error"), turn["payload"].get("error")
    return turn["payload"].get("usage")


@pytest.mark.slow
@pytest.mark.parametrize(
    "round_usages, expected",
    [
        pytest.param(
            [{"total_tokens": 3, "cached_tokens": None}, {"total_tokens": 4, "cached_tokens": 2}],
            {"total_tokens": 7, "cached_tokens": 2},
            id="null-count-then-number",
        ),
        pytest.param(
            [
                {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 0}},
                {"prompt_tokens": 20, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 8}},
            ],
            {"prompt_tokens": 30, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 0}, "total_tokens": 37},
            id="details-and-derived-total",
        ),
        pytest.param(
            [{"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}, {"prompt_tokens": 20, "completion_tokens": 5}],
            {"prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37},
            id="total-in-first-round-only",
        ),
        pytest.param(
            [{"prompt_tokens": 10, "completion_tokens": 2}, {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}],
            {"prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37},
            id="total-in-last-round-only",
        ),
    ],
)
def test_streamed_turn_records_the_blocking_usage_aggregate(usage_api, round_usages, expected):
    build, log_ws = usage_api
    api = build(round_usages)
    blocking = api.assist("what entities exist?", conversation_id="conv-block", turn_id="turn-1")
    assert blocking["text"] == "two entities"
    events = list(api.assist_stream("what entities exist?", conversation_id="conv-stream", turn_id="turn-1"))
    assert events[-1]["kind"] == "done", events[-1]  # the provider answered, so the client gets the answer
    assert events[-1]["text"] == "two entities"
    assert _turn_usage(log_ws, "conv-block") == expected
    assert _turn_usage(log_ws, "conv-stream") == expected  # the stream records the blocking aggregate


@pytest.mark.slow
def test_streamed_turn_matches_blocking_usage_without_provider_streaming(usage_api):
    """With ``stream_chat=False`` LLM.chat merges the raw round usages, and the stream records the same."""
    build, log_ws = usage_api
    api = build([{"prompt_tokens": 10, "completion_tokens": 2}, {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}], stream_chat=False)
    api.assist("what entities exist?", conversation_id="conv-block", turn_id="turn-1")
    events = list(api.assist_stream("what entities exist?", conversation_id="conv-stream", turn_id="turn-1"))
    assert events[-1]["kind"] == "done", events[-1]
    blocking = _turn_usage(log_ws, "conv-block")
    assert blocking["prompt_tokens"] == 30 and blocking["completion_tokens"] == 7
    assert _turn_usage(log_ws, "conv-stream") == blocking
