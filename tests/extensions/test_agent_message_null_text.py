"""Agent Message rows keep accepting a missing text projection."""

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast


def test_message_row_with_null_content_text_is_stored():
    ws = hb.HeavenBase("r10extcli-agent-message", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension("agent")
        payload = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function"}]}
        ws.upsert(
            hb.Message,
            {"object_id": "m1", "session_id": "s1", "idx": 0, "role": "assistant", "content_text": None, "tool_call_count": 1, "payload": payload},
        )

        stored = ws.get("m1", entity=hb.Message)
        assert stored["content_text"] is None
        assert stored["payload"] == payload
        wrapped = hb.Message.wrap({"role": "user", "content": "Quarterly totals " * 400}, session_id="s1", idx=1)
        ws.upsert(hb.Message, wrapped.to_dict())
        assert ws.get(wrapped.object_id, entity=hb.Message)["content_text"] == wrapped.content_text
    finally:
        ws.release()
