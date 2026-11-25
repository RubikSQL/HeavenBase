"""A logged assistant turn's usage covers every provider call the turn made, compaction calls included."""

import re

import pytest

import heavenbase as hb
from heavenbase.api.service import DashboardAPI
import heavenbase.extensions.agent_log as agent_log_package
from heavenbase.utils import path_to_file_uri
from heavenbase.workspace.registry import WorkspaceSpec

pytestmark = pytest.mark.fast

_ASSIST = "heavenbase.dashboard.assistant"
_REPLAY_USAGE = {"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000}
_MEMO_USAGE = {"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330}
_ROUND_USAGE = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


def _compacting_mock():
    """Return a mock provider: the history summary, the in-turn summary, three tool rounds and an answer, each with its own usage."""
    rounds = []

    def mock_chat(spec, messages, stream=False, **kwargs):
        first = str((messages[0] or {}).get("content") or "")
        if first.startswith("Compress the earlier turns"):
            payload, usage = {"role": "assistant", "content": "summary of the earlier turns"}, _REPLAY_USAGE
        elif first.startswith("Compress this turn's"):
            payload, usage = {"role": "assistant", "content": "MEMO: folded"}, _MEMO_USAGE
        else:
            rounds.append(len(rounds))
            if len(rounds) > 3 or "tools" not in kwargs:
                payload, usage = {"role": "assistant", "content": "final answer"}, _ROUND_USAGE
            else:
                call = {"id": f"c{len(rounds)}", "type": "function", "function": {"name": "list_entities", "arguments": "{}"}}
                payload, usage = {"role": "assistant", "content": "", "tool_calls": [call]}, _ROUND_USAGE
        if stream:
            delta = dict(payload)
            if "tool_calls" in payload:
                delta["tool_calls"] = [{"index": 0, **payload["tool_calls"][0]}]
            return iter([{"choices": [{"delta": delta}], "usage": dict(usage)}])
        return {"choices": [{"message": payload}], "usage": dict(usage)}

    return mock_chat


@pytest.fixture()
def compacting_api(monkeypatch, tmp_path):
    """Build a session-logged DashboardAPI that compacts both the replayed history and the turn's tool rounds."""
    import heavenbase.llm as llm_module

    suffix = re.sub(r"[^a-z0-9]", "", tmp_path.name.lower())[-8:] or "x"
    log_ws = f"assist-turn-usage-log-{suffix}"
    data_ws = f"assist-turn-usage-data-{suffix}"
    settings = {
        "session_log": True,
        "session_log_workspace": log_ws,
        "session_log_backend": {"main": {"type": "sqlite", "name": "main", "database": path_to_file_uri(tmp_path / "session-log.db")}},
        "history_chars": 4000,  # the history below is larger, so its oldest turns are summarized
        "compact_ratio": 0.001,  # every tool round overflows, so the turn summarizes its earlier rounds
    }
    for key, value in settings.items():
        hb.CM_HVNB.set(f"{_ASSIST}.{key}", value)
    real_llm = llm_module.LLM
    ws = hb.HeavenBase(data_ws, backends={"main": {"type": "inmem"}})
    WorkspaceSpec.register(data_ws, config={"backends": {"main": {"type": "inmem"}}})
    api = DashboardAPI()
    api.open(data_ws)

    def build(stream_chat):
        mock = _compacting_mock()
        monkeypatch.setattr(
            llm_module, "LLM", lambda **kwargs: real_llm(preset="mock", mock_chat=mock, mock_pause=(0, 0), cache=False, stream_chat=stream_chat)
        )
        return api

    try:
        yield build, log_ws
    finally:
        for key in settings:
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


def _long_history():
    history = []
    for index in range(6):
        history += [{"role": "user", "content": f"question {index} " + "x" * 1500}, {"role": "assistant", "content": f"answer {index} " + "y" * 1500}]
    return history


@pytest.mark.slow
@pytest.mark.parametrize("streamed", [False, True], ids=["blocking", "streamed"])
@pytest.mark.parametrize("stream_chat", [False, True], ids=["provider-blocking", "provider-streaming"])
def test_turn_and_session_usage_cover_the_turns_compaction_calls(compacting_api, streamed, stream_chat):
    build, log_ws = compacting_api
    api = build(stream_chat)
    if streamed:
        events = list(api.assist_stream("and now?", history=_long_history(), conversation_id="conv-usage", turn_id="turn-1"))
        assert events[-1]["kind"] == "done", events[-1]
        assert events[-1]["text"] == "final answer"
    else:
        assert api.assist("and now?", history=_long_history(), conversation_id="conv-usage", turn_id="turn-1")["text"] == "final answer"

    log = hb.HeavenBase(log_ws)
    rows = [row for row in log.rows(agent_log_package.AgentTrace) if row["conversation_id"] == "conv-usage"]
    responses = [row for row in rows if row["kind"] == "response"]
    calls = [row["call"] for row in responses]
    assert calls.count("replay_compaction") == 1
    assert calls.count("turn_compaction") >= 1
    assert calls.count("round") == 4
    (turn,) = [row for row in rows if row["kind"] == "turn"]
    assert not turn["payload"].get("error"), turn["payload"].get("error")
    counts = {key: sum(row["payload"]["usage"][key] for row in responses) for key in _ROUND_USAGE}
    assert counts["total_tokens"] == 1000 + 330 * calls.count("turn_compaction") + 12 * 4
    # The turn row records what the turn spent: every provider call it made, not only the answer's rounds.
    assert turn["payload"]["usage"] == counts
    (session,) = list(log.rows(hb.Session))  # this log holds only this conversation
    assert {key: session["usage"][key] for key in counts} == counts
