"""HeavenBase-backed semantic LLM cache behavior."""

from __future__ import annotations

from inspect import signature

import heavenbase as hb
import pytest
from heavenbase.llm import LLMSession
from heavenbase.llm import base as llm_base
from heavenbase.llm.cache import LLMCache, LLMEmbeddingEntry, LLMTextEntry
from heavenbase.utils import dumps_json


def _cache(policy: str = "deterministic") -> LLMCache:
    return LLMCache.from_dict(
        {
            "enabled": True,
            "workspace": "test-llm-cache",
            "backend": {"main": {"type": "inmem"}},
            "policy": policy,
            "namespaces": {"text": {"enabled": True}, "embedding": {"enabled": True}, "image": {"enabled": True}},
        }
    )


def test_llm_cache_default_config_is_enabled():
    root = hb.LLMEngine(config={}).root()

    assert root["cache"]["enabled"] is True
    assert root["cache"]["workspace"] == "llm-cache"
    assert root["cache"]["namespaces"]["text"]["enabled"] is True
    assert root["cache"]["namespaces"]["embedding"]["touch_hits"] is False
    assert root["default_args"]["embedding_batch_size"] == 256
    assert root["default_args"]["embedding_max_workers"] == 8
    assert root["providers"]["dashscope"]["default_args"]["embedding_batch_size"] == 10
    assert "limits" not in root["providers"]["dashscope"]
    assert "embedding_batch_size" in signature(hb.LLM.embed).parameters
    assert "embedding_max_workers" in signature(hb.LLM.embed).parameters
    assert hb.LLMCache is LLMCache


def test_llm_text_key_canonicalizes_projection_stream_aliases_and_tools():
    cache = _cache()
    direct = hb.LLM(model="deepseek-v4-flash", provider="deepseek", gateway="openai")
    alias = hb.LLM(model="ds-flash", provider="deepseek", gateway="openai")
    messages = llm_base.format_messages("hello")
    args = {
        "temperature": 0,
        "seed": 42,
        "include": "text",
        "reduce": False,
        "stream": False,
        "tools": [
            {"type": "function", "function": {"name": "z", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}},
        ],
    }
    same_args = args | {"stream": True, "include": ["raw"], "tools": list(reversed(args["tools"]))}
    hot_args = args | {"temperature": 0.7}
    structured_args = args | {"response_format": {"type": "json_object"}}

    key = cache.text_key(direct.spec, messages, args)

    assert key["object_id"] == cache.text_key(alias.spec, messages, same_args)["object_id"]
    assert key["object_id"] != cache.text_key(direct.spec, messages, hot_args)["object_id"]
    assert key["object_id"] != cache.text_key(direct.spec, messages, structured_args)["object_id"]


def test_llm_cache_namespaces_do_not_collide():
    cache = _cache()
    llm = hb.LLM(preset="mock")
    text = cache.text_key(llm.spec, llm_base.format_messages("hello"), llm.spec.args)
    emb = cache.embedding_key(llm.spec, "hello", llm.spec.args)
    image = cache.image_key(llm.spec, "hello", [], llm.spec.args)

    assert len({text["object_id"], emb["object_id"], image["object_id"]}) == 3


def test_llm_cache_module_exposes_only_the_owning_protocol():
    from heavenbase.llm import cache as cache_module

    removed = {"text_key", "embedding_key", "image_key", "image_payloads", "images_from_payloads", "llm_cache", "reset_llm_cache"}

    assert removed.isdisjoint(cache_module.__all__)
    assert all(not hasattr(cache_module, name) for name in removed)


def test_instance_cache_override_and_stochastic_policy(monkeypatch):
    calls = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        calls.append((messages, stream, dict(kwargs)))
        yield {"choices": [{"delta": {"content": f"answer-{len(calls)}"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    cache = _cache()
    off = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", cache=False)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", cache=cache)
    local = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", cache_config=cache.to_dict() | {"workspace": "test-llm-cache-local"})

    assert off.chat("off") == "answer-1"
    assert off.chat("off") == "answer-2"
    assert llm.chat("hello") == "answer-3"
    assert llm.chat("hello") == "answer-3"
    assert local.chat("hello") == "answer-4"
    assert local.chat("hello") == "answer-4"
    assert llm.chat("hot", temperature=0.7, seed=None) == "answer-5"
    assert llm.chat("hot", temperature=0.7, seed=None) == "answer-6"
    assert llm.chat("force-hot", temperature=0.7, seed=None, cache_policy="force") == "answer-7"
    assert llm.chat("force-hot", temperature=0.7, seed=None, cache_policy="force") == "answer-8"
    assert llm.chat("seeded-hot", temperature=0.7, seed=7) == "answer-9"
    assert llm.chat("seeded-hot", temperature=0.7, seed=7) == "answer-9"
    assert len(calls) == 9


def test_stream_cache_replays_chunks_and_skips_short_consumed_streams(monkeypatch):
    calls = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        calls.append(messages)
        yield {"choices": [{"delta": {"content": "a"}}]}
        yield {"choices": [{"delta": {"content": "b"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    cache = _cache()
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    assert list(llm.stream("hello", cache=cache)) == ["a", "b"]
    assert list(llm.stream("hello", cache=cache)) == ["a", "b"]
    gen = llm.stream("short", cache=cache)
    assert next(gen) == "a"
    gen.close()
    assert list(llm.stream("short", cache=cache)) == ["a", "b"]
    assert len(calls) == 3


def test_session_tool_history_cache_replays_and_mutated_tool_result_misses(monkeypatch):
    calls = []
    add_tool = {
        "name": "hb_add",
        "description": "Add two integers.",
        "parameters": {
            "type": "object",
            "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
            "required": ["left", "right"],
        },
    }
    assistant_tool_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_add", "type": "function", "function": {"name": "hb_add", "arguments": '{"left":2,"right":3}'}}],
    }

    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is False
        assert kwargs["tools"][0]["function"]["name"] == "hb_add"
        calls.append([dict(message) for message in messages])
        tool_value = next(message["content"] for message in messages if message.get("role") == "tool")
        return {"choices": [{"message": {"role": "assistant", "content": f"sum={tool_value}; call={len(calls)}"}}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    cache = _cache()
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", cache=cache)
    base_history = [
        {"role": "user", "content": "Add 2 and 3."},
        assistant_tool_call,
        {"role": "tool", "tool_call_id": "call_add", "name": "hb_add", "content": "5"},
    ]

    first = LLMSession(llm, messages=[dict(message) for message in base_history])
    first.add_tool(add_tool)
    second = LLMSession(llm, messages=[dict(message) for message in base_history])
    second.add_tool(add_tool)

    assert first.send("Summarize.", stream_chat=False)["content"] == "sum=5; call=1"
    assert second.send("Summarize.", stream_chat=False)["content"] == "sum=5; call=1"
    assert len(calls) == 1
    assert [message["role"] for message in second.messages] == ["user", "assistant", "tool", "user", "assistant"]

    rows = cache.workspace.query(LLMTextEntry).execute().rows()
    assert len(rows) == 1
    assert rows[0]["n_hits"] == 1
    assert any(message.get("tool_calls") for message in rows[0]["messages"])
    assert any(message.get("role") == "tool" and message.get("content") == "5" for message in rows[0]["messages"])

    mutated = [
        {"role": "user", "content": "Add 2 and 3."},
        assistant_tool_call,
        {"role": "tool", "tool_call_id": "call_add", "name": "hb_add", "content": "6"},
    ]
    third = LLMSession(llm, messages=[dict(message) for message in mutated])
    third.add_tool(add_tool)

    assert third.send("Summarize.", stream_chat=False)["content"] == "sum=6; call=2"
    assert len(calls) == 2
    assert cache.workspace.count(LLMTextEntry) == 2


def test_session_executable_tools_bypass_cache_to_avoid_stale_side_effects(monkeypatch):
    calls = []
    tool_values = ["alpha", "beta"]

    def read_value() -> str:
        """Return the next mutable value."""
        return tool_values.pop(0)

    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is False
        calls.append([dict(message) for message in messages])
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"id": "call_read", "type": "function", "function": {"name": "read_value", "arguments": "{}"}}],
                        }
                    }
                ],
                "usage": {"total_tokens": 1},
            }
        return {"choices": [{"message": {"role": "assistant", "content": f"value={messages[-1]['content']}"}}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    cache = _cache()
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", cache=cache)

    first = LLMSession(llm)
    first.add_tool(read_value)
    second = LLMSession(llm)
    second.add_tool(read_value)

    assert first.send("Read the mutable value.", stream_chat=False)["content"] == "value=alpha"
    assert second.send("Read the mutable value.", stream_chat=False)["content"] == "value=beta"
    assert len(calls) == 4
    assert cache.workspace.count(LLMTextEntry) == 0


@pytest.mark.full
def test_embedding_cache_deduplicates_inputs_and_reuses_rows(monkeypatch):
    calls = []

    def fake_embed(spec, inputs, **kwargs):
        calls.append(list(inputs))
        return {"data": [{"index": index, "embedding": [float(len(text))]} for index, text in enumerate(inputs)], "usage": {"total_tokens": len(inputs)}}

    monkeypatch.setattr(llm_base, "embed_complete", fake_embed)
    cache = _cache()
    llm = hb.LLM(preset="embed")

    first = llm.embed(["a", "bb", "ccc", "a"], cache=cache, embedding_batch_size=2, include=["embeddings", "usage", "raw"], reduce=False)
    assert cache.workspace.count(LLMEmbeddingEntry) == 3
    second = llm.embed(["bb", "dddd", "a", "dddd"], cache=cache, embedding_batch_size=2, include=["embeddings", "usage", "raw"], reduce=False)
    assert cache.workspace.count(LLMEmbeddingEntry) == 4
    before_cached_only = len(calls)
    third = llm.embed(["dddd", "a", "dddd", "bb"], cache=cache, embedding_batch_size=2, include=["embeddings", "usage", "raw"], reduce=False)

    assert first["embeddings"] == [[1.0], [2.0], [3.0], [1.0]]
    assert first["usage"] == {"total_tokens": 3}
    assert first["raw"]["misses"] == 3
    assert first["raw"]["embedding_batch_size"] == 2
    assert len(first["raw"]["batches"]) == 2
    assert second["embeddings"] == [[2.0], [4.0], [1.0], [4.0]]
    assert second["raw"]["cached"] == 2
    assert second["raw"]["misses"] == 1
    assert third["embeddings"] == [[4.0], [1.0], [4.0], [2.0]]
    assert third["raw"]["cached"] == 3
    assert third["raw"]["misses"] == 0
    assert third["raw"]["batches"] == []
    assert len(calls) == before_cached_only
    assert sorted(calls) == sorted([["a", "bb"], ["ccc"], ["dddd"]])


@pytest.mark.full
def test_embedding_default_batch_size_and_workers(monkeypatch):
    calls = []

    def fake_embed(spec, inputs, **kwargs):
        assert "embedding_batch_size" not in kwargs
        assert "embedding_max_workers" not in kwargs
        calls.append(list(inputs))
        return {"data": [{"index": index, "embedding": [float(index)]} for index, _ in enumerate(inputs)], "usage": {"total_tokens": len(inputs)}}

    monkeypatch.setattr(llm_base, "embed_complete", fake_embed)
    cache = LLMCache.from_dict({"enabled": True, "workspace": "test-llm-cache-defaults", "backend": {"main": {"type": "inmem"}}})
    llm = hb.LLM(preset="embed", cache=cache)

    result = llm.embed([f"input-{index}" for index in range(600)], include="raw", reduce=True)

    assert result["embedding_batch_size"] == 256
    assert result["embedding_max_workers"] == 8
    assert sorted(len(call) for call in calls) == [88, 256, 256]


@pytest.mark.full
def test_dashscope_embedding_batch_size_provider_default_can_be_overridden(monkeypatch):
    calls = []

    def fake_embed(spec, inputs, **kwargs):
        calls.append(list(inputs))
        return {"data": [{"index": index, "embedding": [float(index)]} for index, _ in enumerate(inputs)], "usage": {"total_tokens": len(inputs)}}

    monkeypatch.setattr(llm_base, "embed_complete", fake_embed)
    cache = LLMCache.from_dict({"enabled": True, "workspace": "test-llm-cache-dashscope", "backend": {"main": {"type": "inmem"}}})
    llm = hb.LLM(model="text-embedding-v4", provider="dashscope", gateway="openai", cache=cache)

    result = llm.embed([f"input-{index}" for index in range(25)], include="raw", reduce=True)

    assert result["embedding_batch_size"] == 10
    assert result["embedding_max_workers"] == 8
    assert sorted(len(call) for call in calls) == [5, 10, 10]

    calls.clear()
    override = llm.embed([f"override-{index}" for index in range(25)], embedding_batch_size=12, embedding_max_workers=3, include="raw", reduce=True)

    assert override["embedding_batch_size"] == 12
    assert override["embedding_max_workers"] == 3
    assert sorted(len(call) for call in calls) == [1, 12, 12]


@pytest.mark.full
def test_embedding_controls_do_not_affect_cache_key(monkeypatch):
    calls = []

    def fake_embed(spec, inputs, **kwargs):
        calls.append(list(inputs))
        return {"data": [{"index": index, "embedding": [float(len(text))]} for index, text in enumerate(inputs)], "usage": {"total_tokens": len(inputs)}}

    monkeypatch.setattr(llm_base, "embed_complete", fake_embed)
    cache = _cache()
    llm = hb.LLM(preset="embed")

    first = llm.embed(["a", "bb", "ccc"], cache=cache, embedding_batch_size=1, include="raw", reduce=True)
    second = llm.embed(["a", "bb", "ccc"], cache=cache, embedding_batch_size=3, embedding_max_workers=3, include="raw", reduce=True)

    assert first["misses"] == 3
    assert second["cached"] == 3
    assert second["misses"] == 0
    assert sorted(calls) == [["a"], ["bb"], ["ccc"]]
    assert len(calls) == 3


def test_imagen_cache_uses_image_namespace(monkeypatch):
    calls = []

    def fake_imagen(spec, prompt, images=(), **kwargs):
        calls.append((prompt, list(images)))
        payload = hb.LLMImage.from_bytes(f"image-{len(calls)}".encode("utf-8"))
        return {"data": [{"b64_json": payload.data}], "usage": {"image_count": 1}}

    monkeypatch.setattr(llm_base, "imagen_complete", fake_imagen)
    cache = _cache()
    llm = hb.LLM(preset="mock")

    assert llm.imagen("draw", cache=cache).to_bytes() == b"image-1"
    assert llm.imagen("draw", cache=cache).to_bytes() == b"image-1"
    assert len(calls) == 1


@pytest.mark.fast
def test_text_cache_digests_multimodal_messages_and_respects_max_bytes(monkeypatch):
    """Text-cache persistence must digest images, honor budgets, and keep failures private."""
    from heavenbase.llm import cache as cache_module
    from heavenbase.llm.cache import _canon

    calls = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        calls.append(messages)
        if stream:
            yield {"choices": [{"delta": {"content": f"page-{len(calls)}"}}]}
            return
        return {
            "id": f"chat-{len(calls)}",
            "choices": [{"message": {"role": "assistant", "content": f"page-{len(calls)}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    image = hb.LLMImage.from_bytes(b"x" * 64)
    cache = LLMCache.from_dict(
        {
            "enabled": True,
            "workspace": "test-llm-cache-multimodal",
            "backend": {"main": {"type": "inmem"}},
            "policy": "deterministic",
            "namespaces": {"text": {"enabled": True, "max_bytes": 4194304}},
        }
    )
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")
    keys = []
    text_key = cache.text_key

    def capture_key(spec, messages, args):
        key = text_key(spec, messages, args)
        keys.append(key["object_id"])
        return key

    monkeypatch.setattr(cache, "text_key", capture_key)
    first = llm.chat("ocr this", images=[image], cache=cache, temperature=0, seed=1)
    assert first == "page-1"
    assert len(keys) == 1
    row = cache.workspace.get(keys[0], entity=LLMTextEntry)
    assert row is not None
    encoded = dumps_json(row["messages"])
    assert image.data not in encoded
    assert "data_hash" in encoded or "data_url_hash" in encoded

    tiny = LLMCache.from_dict(
        {
            "enabled": True,
            "workspace": "test-llm-cache-budget",
            "backend": {"main": {"type": "inmem"}},
            "policy": "deterministic",
            "namespaces": {"text": {"enabled": True, "max_bytes": 64}},
        }
    )
    second = llm.chat("keep going", cache=tiny, temperature=0, seed=2)
    assert second == "page-2"
    assert tiny.workspace.count(LLMTextEntry) == 0

    failing = LLMCache.from_dict(
        {
            "enabled": True,
            "workspace": "test-llm-cache-fail",
            "backend": {"main": {"type": "inmem"}},
            "policy": "deterministic",
            "namespaces": {"text": {"enabled": True}},
        }
    )
    workspace = failing.workspace

    def boom(*_args, **_kwargs):
        raise RuntimeError("params={'messages': 'private prompt', 'response': 'private model output', 'api_key': 'private credential'}")

    warnings = []
    monkeypatch.setattr(workspace, "upsert", boom)
    monkeypatch.setattr(cache_module._logger, "warning", lambda message, *args: warnings.append((message, args)))
    third = llm.chat("still ok", cache=failing, temperature=0, seed=3)
    assert third == "page-3"
    assert len(calls) == 3
    assert len(warnings) == 1
    warning = warnings[0][0] % warnings[0][1]
    assert "Text cache persistence failed" in warning
    assert "RuntimeError" in warning
    assert "private prompt" not in warning
    assert "private model output" not in warning
    assert "private credential" not in warning

    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("B" * 2000)}}
    digested = dumps_json(_canon([{"role": "user", "content": [part]}]))
    assert "BBBB" not in digested
    assert "data_hash" in digested or "data_url_hash" in digested

    low_detail = {"type": "image_url", "image_url": {**part["image_url"], "detail": "low"}}
    high_detail = {"type": "image_url", "image_url": {**part["image_url"], "detail": "high"}}
    assert _canon(low_detail) != _canon(high_detail)
    assert cache.size({"text": "界" * 4}) == len(dumps_json({"text": "界" * 4}, sort_keys=True, compact=True).encode("utf-8"))
