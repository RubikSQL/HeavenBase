"""An agent ``Session`` accumulates turn usage with its own documented merge rule."""

import heavenbase as hb
from heavenbase.extensions.agent.session import _merge_usage as session_merge
from heavenbase.llm.base import _merge_usage as llm_merge


def _session(name: str):
    ws = hb.HeavenBase(name, detached=True, backends={"main": {"type": "inmem"}})
    ws.enable_extension("agent")
    session = hb.Session(title="Run", ws=ws)
    ws.upsert(hb.Session, session.to_dict())
    return ws, session


def _appended_usage(name, usages):
    ws, session = _session(name)
    for index, usage in enumerate(usages):
        session.append_messages([{"role": "user", "content": f"q{index}"}, {"role": "assistant", "content": "a"}], usage=usage)
    return hb.Session.load(session.object_id, ws=ws).usage


def test_session_usage_adds_counts_and_a_number_replaces_a_null():
    usage = _appended_usage("agent-session-usage-null", [{"cached_tokens": None, "total_tokens": 3}, {"cached_tokens": 2, "total_tokens": 4}])
    assert usage == {"cached_tokens": 2, "total_tokens": 7}
    assert llm_merge([{"cached_tokens": None, "total_tokens": 3}, {"cached_tokens": 2, "total_tokens": 4}]) == usage  # both rules agree here


def test_session_usage_keeps_the_first_non_empty_value_and_derives_no_total():
    turns = [
        {"prompt_tokens": 10, "completion_tokens": 2, "model": None},
        {"prompt_tokens": 20, "completion_tokens": 5, "model": "chat-b"},
    ]
    usage = _appended_usage("agent-session-usage-first", turns)
    # Documented: any other value keeps the first non-empty one; Session.usage never invents a total.
    assert usage == {"prompt_tokens": 30, "completion_tokens": 7, "model": "chat-b"}
    # The LLM merge keeps the first value even when it is empty and derives total_tokens, so it
    # cannot replace the Session merge without changing documented Session.usage / rebuild_session results.
    merged = llm_merge(turns)
    assert merged["model"] is None and "total_tokens" in merged and merged != usage
    assert session_merge(session_merge({}, turns[0]), turns[1]) == usage
