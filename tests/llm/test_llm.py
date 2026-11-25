"""LLM gateway, provider, response, and runtime behavior."""

from __future__ import annotations

import base64
import importlib
import sys
import types

import pytest

import heavenbase as hb
from heavenbase.llm.gateways import _protocols as adapters
from heavenbase.llm.gateways._protocols import _anthropic_payload, _embedding_payload, _openai_client, _openai_payload
from heavenbase.llm.gateways.anthropic import runtime as anthropic_gateway
from heavenbase.llm.gateways.bifrost import runtime as bifrost_gateway
from heavenbase.llm.gateways.litellm import runtime as litellm_gateway
from heavenbase.llm.gateways.portkey import runtime as portkey_gateway
from heavenbase.llm import base as llm_base
from heavenbase.llm import transport as llm_transport
from heavenbase.llm.cache import LLMCache
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm import LLMSession
from heavenbase.utils import dumps_json, loads_json, resolve_interpolations


@pytest.fixture(autouse=True)
def _disable_global_llm_cache(monkeypatch):
    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))


def _env(name: str, default: str = "") -> str:
    return str(resolve_interpolations({"value": f'${{env:{name},"{default}"}}'})["value"])


def _github_actions() -> bool:
    return _env("GITHUB_ACTIONS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return _env(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _route(spec, gateway=None):
    if gateway is None or gateway == spec.gateway:
        return spec
    data = spec.to_dict()["data"]
    data["gateway"] = gateway
    return LLM_ENGINE.resolve(data)


def _runtime(spec, gateway=None):
    routed = _route(spec, gateway)
    return LLM_ENGINE.apply(routed, profile=routed.gateway)


def _multi_tool_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "hb_add",
                "description": "Add two integers.",
                "parameters": {
                    "type": "object",
                    "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                    "required": ["left", "right"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hb_mul",
                "description": "Multiply two integers.",
                "parameters": {
                    "type": "object",
                    "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                    "required": ["left", "right"],
                },
            },
        },
    ]


def _tool_call_name(tool_call):
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    return function.get("name") or tool_call.get("name") or ""


def _tool_call_args(tool_call):
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    arguments = function.get("arguments", tool_call.get("arguments", ""))
    return loads_json(arguments) if isinstance(arguments, str) and arguments.strip() else arguments


def _custom_compile(spec, gateway, override):
    """Compile the programmatic gateway used by the facade contract test."""
    return {"gateway": gateway, "model": spec.model_id, "args": dict(override)}


def _custom_chat(spec, messages, stream=False, **kwargs):
    """Return one deterministic programmatic transport response."""
    return {"choices": [{"message": {"role": "assistant", "content": "programmatic"}}], "usage": {}}


@pytest.mark.detail
def test_llm_alias_resolves_provider_specific_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    hb.CM_HVNB.refresh()
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="portkey")

    runtime = _runtime(llm.spec)

    assert llm.spec.model == "deepseek-v4-pro"
    assert llm.spec.model_id == "deepseek-v4-pro"
    assert runtime["model"] == "deepseek-v4-pro"
    assert runtime["headers"]["x-portkey-provider"] == "deepseek"
    assert runtime["headers"]["Authorization"] == "Bearer ds-test"


@pytest.mark.detail
def test_portkey_zai_uses_openai_custom_host_for_tools(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "zai-test")
    monkeypatch.setenv("ZAI_ZH_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    hb.CM_HVNB.refresh()
    llm = hb.LLM(model="glm-5.2", provider="zai-zh", gateway="portkey")
    runtime = _runtime(llm.spec, "portkey")
    assert runtime["headers"]["x-portkey-provider"] == "openai"
    assert runtime["headers"]["x-portkey-custom-host"] == "https://open.bigmodel.cn/api/paas/v4"
    assert runtime["headers"]["x-portkey-forward-headers"] == "Authorization"
    assert runtime["headers"]["Authorization"] == "Bearer zai-test"


@pytest.mark.detail
def test_portkey_route_matrix_compiles_safe_routes(monkeypatch):
    monkeypatch.setenv("PORTKEY_API_KEY", "pk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")
    monkeypatch.setenv("ZAI_API_KEY", "zai-test")
    monkeypatch.setenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")
    monkeypatch.setenv("ZAI_ZH_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    hb.CM_HVNB.refresh()

    cases = [
        ({"preset": "chat", "gateway": "portkey"}, "deepseek/deepseek-v4-flash-0731", "openrouter", None),
        ({"model": "ds-flash", "provider": "deepseek", "gateway": "portkey"}, "deepseek-chat", "deepseek", None),
        ({"model": "haiku", "provider": "anthropic", "gateway": "portkey"}, "claude-haiku-4-5", "anthropic", None),
        ({"model": "glm-flash", "provider": "zai", "gateway": "portkey"}, "glm-4.7-flash", "openai", "https://api.z.ai/api/paas/v4"),
        ({"model": "glm-flash", "provider": "zai-zh", "gateway": "portkey"}, "glm-4.7-flash", "openai", "https://open.bigmodel.cn/api/paas/v4"),
    ]

    for options, model, provider_header, custom_host in cases:
        runtime = _runtime(hb.LLM(**options).spec, "portkey")
        headers = runtime["headers"]

        assert runtime["model"] == model
        assert runtime["api_key"] == "pk-test"
        assert headers["x-portkey-api-key"] == "pk-test"
        assert headers["x-portkey-strict-open-ai-compliance"] == "false"
        assert headers["x-portkey-provider"] == provider_header
        if custom_host:
            assert headers["x-portkey-custom-host"] == custom_host
            assert headers["x-portkey-forward-headers"] == "Authorization"
            assert headers["Authorization"] == "Bearer zai-test"


def test_llm_domain_is_the_only_llm_owner():
    from heavenbase.llm import LLM, LLMEngine, LLMToolCallRepair

    assert LLM is hb.LLM
    assert LLMEngine is hb.LLMEngine
    assert LLMToolCallRepair is hb.LLMToolCallRepair
    assert hb.LLMToolCallRepair.__name__ == "LLMToolCallRepair"
    assert not hasattr(hb.utils, "LLMToolCallRepair")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("heavenbase.utils.llm")


def test_tool_call_repair_module_exposes_one_owning_protocol():
    from heavenbase.llm import repair as repair_module

    removed = {"llm_tool_call_repair", "reset_tool_call_repair"}

    assert repair_module.__all__ == ["LLMToolCallRepair"]
    assert removed.isdisjoint(repair_module.__dict__)


def test_tool_call_repair_copies_disabled_calls_and_repairs_enabled_calls():
    repair = hb.LLMToolCallRepair.from_dict(enabled=False)
    call = {"id": "call_add", "function": {"name": "add", "arguments": '{"a":1'}}
    tools = [{"function": {"name": "add", "parameters": {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]}}}]

    copied = repair.repair([call])
    fixed = repair.repair([call], tools=tools, option=True)

    assert copied == [call]
    assert copied[0] is not call
    assert fixed[0]["function"]["arguments"] == '{"a":1,"b":0}'
    assert call["function"]["arguments"] == '{"a":1'


def test_llm_response_owns_public_projection_protocol():
    from heavenbase.llm import response as response_module

    response = hb.LLMResponse.from_dict({"text": "answer", "think": "brief", "usage": {"total_tokens": 3}})

    assert response_module.__all__ == ["LLMResponse"]
    assert "project_include" not in response_module.__dict__
    assert response.include("content") == "<think>\nbrief\n</think>\nanswer"
    assert response.include(["text", "usage"]) == {"text": "answer", "usage": {"total_tokens": 3}}
    assert response.get("usage") == {"total_tokens": 3}
    with pytest.raises(ValueError, match="include"):
        response.include("missing")


def test_llm_config_module_exposes_minimal_protocol():
    from heavenbase.llm import config as config_module

    assert config_module.__all__ == ["LLMEngine", "LLMSpec"]
    assert "LLM_OPTION_ENGINE" not in config_module.__dict__


def test_llm_normalized_contract_is_minimal_and_restorable():
    engine = hb.LLMEngine(config={"default_preset": "system"})
    spec = engine.resolve()
    restored = hb.ConfigSpec.from_dict(spec.to_dict())

    assert spec.preset == "system"
    assert (spec.endpoint, spec.gateway, spec.provider, spec.model) == (
        "openai",
        "openai",
        "openrouter",
        "deepseek-v4-flash",
    )
    assert set(spec.data) == {"preset", "endpoint", "gateway", "provider", "model", "args"}
    assert "model_id" not in spec.data
    assert spec.bindings["model_id"] == "deepseek/deepseek-v4-flash-0731"
    assert engine.apply(restored) == engine.apply(spec)
    assert engine.assert_idempotent(spec) == spec


def test_llm_removed_budget_alias_and_native_semantic_collisions():
    engine = hb.LLMEngine(config={"default_preset": None})
    with pytest.raises(ValueError, match="was removed; use reasoning_max_tokens"):
        engine.resolve(
            {
                "model": "ds-flash",
                "provider": "openrouter",
                "gateway": "openai",
                "thinking_budget_tokens": 2048,
            }
        )

    spec = engine.resolve(
        {
            "model": "ds-flash",
            "provider": "openrouter",
            "gateway": "openai",
            "think": True,
            "reasoning_max_tokens": 2048,
        }
    )
    assert spec.args["reasoning_max_tokens"] == 2048
    assert "thinking_budget_tokens" not in spec.args

    equivalent = _openai_payload(
        engine.apply(spec),
        spec=spec,
        messages=[],
        extra_body={"reasoning": {"enabled": True, "max_tokens": 2048}},
    )
    assert equivalent["extra_body"]["reasoning"]["max_tokens"] == 2048
    with pytest.raises(ValueError, match="extra_body.reasoning.enabled"):
        _openai_payload(engine.apply(spec), spec=spec, messages=[], extra_body={"reasoning": {"enabled": False}})


def test_openai_payload_strips_the_tool_loop_budget():
    # Loop-level keys are consumed in ``_chat_context``; the gateway runtime
    # re-merges spec args, so without their CLIENT_ARG_KEYS entries a
    # constructor-channel value reaches ``Completions.create`` and raises on
    # every openai-protocol route.
    engine = hb.LLMEngine(config={"default_preset": None})
    spec = engine.resolve({"model": "ds-flash", "provider": "deepseek", "gateway": "openai"}).with_args(
        {"max_tool_turns": 3, "tool_result_capture_chars": 9, "compact_ratio": 0.5}
    )
    payload = _openai_payload(_runtime(spec), spec=spec, messages=[])
    assert "max_tool_turns" not in payload
    assert "tool_result_capture_chars" not in payload
    assert "compact_ratio" not in payload
    assert payload["messages"] == []


def test_context_window_resolution_uses_declared_metadata_or_fallback():
    from heavenbase.llm.base import LLM as llm_class

    engine = hb.LLMEngine(config={"default_preset": None})
    spec = engine.resolve({"model": "ds-flash", "provider": "deepseek", "gateway": "openai"})
    assert spec._model_cfg().get("context_window") == 1048576  # the provider's own stated maximum

    class _SpecCarrier:
        def __init__(self, model_cfg):
            self.spec = types.SimpleNamespace(_model_cfg=lambda: model_cfg)

    assert llm_class._context_window(_SpecCarrier({"context_window": 205000})) == 205000
    assert llm_class._context_window(_SpecCarrier({})) == 128000


def test_llm_print_profile_redacts_explicit_secret_locations():
    engine = hb.LLMEngine(config={"default_preset": None})
    spec = engine.resolve(
        {
            "model": "third-party",
            "provider": "custom",
            "gateway": "openai",
            "base_url": "https://user:password@example.test/v1?token=query-secret&region=local",
            "api_key": "api-secret",
            "auth_token": "bearer-secret",
            "headers": {"Authorization": "Bearer header-secret"},
        }
    )

    rendered = engine.apply(spec, profile="print")
    text = repr(rendered)
    for secret in ("password", "query-secret", "api-secret", "bearer-secret", "header-secret"):
        assert secret not in text
    assert "region=local" in rendered["data"]["args"]["base_url"]


def test_llm_gateway_registry_compiles_custom_runtime():
    from heavenbase.llm import gateway as gateway_module

    custom = hb.LLMGateway(
        "test-custom",
        _custom_compile,
        chat=_custom_chat,
    ).register(replace=True)
    spec = hb.LLMEngine(config={"default_preset": None}).resolve({"preset": "mock"})

    assert gateway_module.__all__ == ["LLMGateway"]
    assert {"anthropic", "bifrost", "litellm", "mock", "openai", "portkey"} <= set(hb.LLMGateway.snapshot())
    assert hb.LLMGateway.load("test-custom") is custom
    assert custom.compile(spec, temperature=0) == {"gateway": "test-custom", "model": "mock", "args": {"temperature": 0}}

    routed = hb.LLMEngine(config={"default_preset": None}).resolve({"model": "mock", "provider": "mock", "gateway": "test-custom", "endpoint": "openai"})
    assert LLM_ENGINE.apply(routed) == {
        "gateway": "test-custom",
        "model": "mock",
        "args": {},
        "compiler": "openai/test-custom/semantic/v1",
        "arg_codec": "openai-semantic/v1",
    }
    assert hb.LLM(model="mock", provider="mock", gateway="test-custom", endpoint="openai", cache=False, stream_chat=False).chat("hello") == "programmatic"


def test_default_gateway_is_openai():
    llm = hb.LLM(preset="mock")

    assert hb.LLMEngine().resolve({"preset": None, "model": "dsv4"}).gateway == "openai"
    assert llm.spec.gateway == "mock"


def test_default_preset_is_runtime_scoped_with_thinking_defaults_and_descriptions():
    original = hb.CM_HVNB.get("heavenbase.llm.default_preset", default=None)
    hb.CM_HVNB.set("heavenbase.llm.default_preset", "mock")
    try:
        assert hb.LLM().spec.preset == "mock"
        assert hb.LLMEngine(config={}).resolve().preset == "mock"
    finally:
        hb.CM_HVNB.set("heavenbase.llm.default_preset", original)

    root = hb.LLMEngine(config={"default_preset": None}).root()
    assert root["tool_call_repair"] == {"enabled": False, "strict": False}
    assert root["image_url_timeout"] == 120
    for preset in (
        "system",
        "local",
        "tiny",
        "chat",
        "chat-pro",
        "worker",
        "worker-local",
        "embed",
        "embed-local",
        "imagen",
        "imagen-local",
        "ocr-local",
        "mock",
        "custom",
    ):
        assert root["presets"][preset]["desc"]
    assert list(root["presets"]).index("local") == list(root["presets"]).index("system") + 1
    for preset in ("system", "tiny", "chat", "chat-pro", "worker", "local", "worker-local", "ocr-local"):
        assert hb.LLM(preset=preset).spec.args["think"] is False

    for preset in ("reason", "reason-pro", "coder", "coder-pro"):
        assert root["presets"][preset]["desc"]
        assert hb.LLM(preset=preset).spec.args["think"] is True
    assert hb.LLM(preset="reason-pro").spec.args["reasoning_effort"] == "max"
    assert hb.LLM(preset="coder").spec.model == "claude-sonnet-5"
    assert hb.LLM(preset="coder-pro").spec.model == "claude-opus-5"


@pytest.mark.detail
def test_builtin_model_catalog_and_aliases_are_curated():
    required_models = {
        "deepseek-v4-flash",
        "deepseek-reasoner",
        "deepseek-v4-pro",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-fable-5",
        "grok-4.5",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.1-pro-preview",
        "kimi-k2.6",
        "kimi-k3",
        "gemma-4-26b-a4b-it",
        "glm-4.7-flash",
        "qwen3.6-flash",
        "text-embedding-3-small",
        "embed-v4.0",
        "voyage-4-lite",
        "embeddinggemma",
        "gpt-5-image-mini",
        "z-image-turbo",
        "glm-ocr",
    }
    engine = hb.LLMEngine(config={"default_preset": None})
    root = engine.root()
    models = root["models"]
    providers = list(root["providers"])

    assert required_models | {"mock", "custom"} <= set(models)
    assert providers.index("ollama") < providers.index("lmstudio") < providers.index("omlx") < providers.index("vllm")
    assert models["embeddinggemma"]["default_provider"] == "lmstudio"
    assert "lmstudio" in models["embeddinggemma"]["providers"]
    assert models["embeddinggemma"]["providers"]["omlx"]["model_id"] == "embeddinggemma"
    assert "openrouter" not in models["embeddinggemma"]["providers"]
    assert models["qwen3.6-flash"]["providers"]["omlx"]["model_id"] == "qwen3.6-flash"
    local_only = {"embeddinggemma", "z-image-turbo", "glm-ocr"}
    non_openrouter = local_only | {"embed-v4.0", "voyage-4-lite"}
    for model in required_models - non_openrouter:
        assert models[model]["default_provider"] == "openrouter"
        assert "openrouter" in models[model]["providers"]
    assert models["embed-v4.0"]["default_provider"] == "cohere"
    assert models["embed-v4.0"]["providers"] == {"cohere": {"model_id": "embed-v4.0"}}
    assert models["voyage-4-lite"]["default_provider"] == "voyage"
    assert models["voyage-4-lite"]["providers"] == {"voyage": {"model_id": "voyage-4-lite"}}
    assert models["z-image-turbo"]["default_provider"] == "ollama"
    assert models["z-image-turbo"]["providers"] == {"ollama": {"model_id": "x/z-image-turbo"}}
    assert models["glm-ocr"]["default_provider"] == "ollama"
    assert models["glm-ocr"]["providers"] == {"ollama": {"model_id": "glm-ocr"}, "omlx": {"model_id": "glm-ocr"}}
    assert models["gemma-4-26b-a4b-it"]["capabilities"] == {"reasoning_efforts": False}
    assert models["gpt-5.6-luna"]["default_args"]["reasoning_effort"] == "high"
    assert models["gpt-5.6-terra"]["default_args"]["reasoning_effort"] == "high"
    assert models["gpt-5.6-sol"]["default_args"]["reasoning_effort"] == "medium"
    assert {"gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.5", "gpt-5.5-pro"}.isdisjoint(models)
    assert "sonnet5" not in models["claude-sonnet-5"]["aliases"]
    assert "fable5" not in models["claude-fable-5"]["aliases"]
    assert "sonnet-4-6" not in models["claude-sonnet-4-6"]["aliases"]
    assert "glmocr" not in models["glm-ocr"]["aliases"]

    aliases = {
        "sonnet": ("claude-sonnet-5", "anthropic/claude-sonnet-5"),
        "sonnet-4.6": ("claude-sonnet-4-6", "anthropic/claude-sonnet-4.6"),
        "opus": ("claude-opus-5", "anthropic/claude-opus-5"),
        "fable": ("claude-fable-5", "anthropic/claude-fable-5"),
        "ds-flash": ("deepseek-v4-flash", "deepseek/deepseek-v4-flash-0731"),
        "ds-pro": ("deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
        "dsv4": ("deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
        "gpt-nano": ("gpt-5.6-luna", "openai/gpt-5.6-luna"),
        "gpt-mini": ("gpt-5.6-terra", "openai/gpt-5.6-terra"),
        "gpt": ("gpt-5.6-sol", "openai/gpt-5.6-sol"),
        "gpt-image-mini": ("gpt-5-image-mini", "openai/gpt-5-image-mini"),
        "text-embedding-small": ("text-embedding-3-small", "openai/text-embedding-3-small"),
        "deepseek-chat": ("deepseek-v4-flash", "deepseek/deepseek-v4-flash-0731"),
        "deepseek-reasoner": ("deepseek-reasoner", "deepseek/deepseek-v4-flash-0731"),
        "ds-flash-thinking": ("deepseek-reasoner", "deepseek/deepseek-v4-flash-0731"),
        "grok": ("grok-4.5", "x-ai/grok-4.5"),
        "gemini-flash-lite": ("gemini-3.5-flash-lite", "google/gemini-3.5-flash-lite"),
        "gemini-lite": ("gemini-3.5-flash-lite", "google/gemini-3.5-flash-lite"),
        "gemini-flash": ("gemini-3.6-flash", "google/gemini-3.6-flash"),
        "gemini-pro": ("gemini-3.1-pro-preview", "google/gemini-3.1-pro-preview"),
        "k3": ("kimi-k3", "moonshotai/kimi-k3"),
        "gemma": ("gemma-4-26b-a4b-it", "google/gemma-4-26b-a4b-it"),
        "glm-flash": ("glm-4.7-flash", "z-ai/glm-4.7-flash"),
    }
    for alias, (canonical, identifier) in aliases.items():
        spec = engine.resolve({"model": alias, "provider": "openrouter", "gateway": "openai"})
        assert spec.model == canonical
        assert _runtime(spec)["model"] == identifier

    providers = list(root["providers"])
    assert providers.index("grok") < providers.index("gemini") < providers.index("deepseek")
    assert (
        providers.index("zai")
        < providers.index("zai-zh")
        < providers.index("minimax")
        < providers.index("cohere")
        < providers.index("voyage")
        < providers.index("dashscope")
    )
    assert engine.resolve({"model": "gemini-lite", "provider": "gemini", "gateway": "openai"}).model_id == "gemini-3.5-flash-lite"
    assert engine.resolve({"model": "gemini-flash", "provider": "gemini", "gateway": "openai"}).model_id == "gemini-3.6-flash"
    assert engine.resolve({"model": "google/gemini-3.1-pro-preview", "provider": "openrouter", "gateway": "openai"}).model == "gemini-3.1-pro-preview"
    sol_max = engine.resolve({"model": "gpt-5.6-sol", "provider": "openrouter", "gateway": "openai", "reasoning_effort": "max"})
    assert _openai_payload(_runtime(sol_max), spec=sol_max, messages=[])["extra_body"]["reasoning"] == {
        "enabled": True,
        "effort": "max",
    }
    grok = engine.resolve({"model": "grok-5.0", "provider": "grok", "gateway": "openai"})
    minimax = engine.resolve({"model": "minimax-m2.5", "provider": "minimax", "gateway": "openai"})
    minimax_m27 = engine.resolve({"model": "MiniMax-M2.7", "provider": "minimax", "gateway": "openai"})
    assert grok.model_id == "grok-5.0"
    assert _route(grok, "litellm").provider_backend() == "xai"
    assert minimax.model_id == "minimax-m2.5"
    assert minimax_m27.model_id == "MiniMax-M2.7"
    assert _route(minimax, "litellm").provider_backend() == "minimax"


@pytest.mark.detail
def test_default_provider_controls_unpinned_presets_and_local_model_ids():
    engine = hb.LLMEngine(config={"default_preset": None})

    deepseek = engine.resolve({"preset": "chat", "provider": "deepseek"})
    local = engine.resolve({"preset": "local"})
    worker_local = engine.resolve({"preset": "worker-local"})
    overlay = {"default_preset": None, "presets": {"chat": {"model": "qwen3.6-flash"}}}
    ollama = hb.LLMEngine(config=overlay).resolve({"preset": "chat", "provider": "ollama"})
    lmstudio = hb.LLMEngine(config=overlay).resolve({"preset": "chat", "provider": "lmstudio"})
    omlx = hb.LLMEngine(config=overlay).resolve({"preset": "chat", "provider": "omlx"})
    vllm = hb.LLMEngine(config=overlay).resolve({"preset": "chat", "provider": "vllm"})
    imagen_local = engine.resolve({"preset": "imagen-local"})
    ocr_local = engine.resolve({"preset": "ocr-local"})

    assert deepseek.provider == "deepseek"
    assert deepseek.model_id == "deepseek-chat"
    assert local.provider == "lmstudio"
    assert local.model == "qwen3.6-flash"
    assert local.model_id == "qwen3.6-35b-a3b"
    assert worker_local.provider == "lmstudio"
    assert worker_local.model == "qwen3.6-flash"
    assert worker_local.model_id == "qwen3.6-35b-a3b"
    assert ollama.provider == "ollama"
    assert ollama.model_id == "qwen3.6"
    assert lmstudio.provider == "lmstudio"
    assert lmstudio.model_id == "qwen3.6-35b-a3b"
    assert omlx.provider == "omlx"
    assert omlx.model_id == "qwen3.6-flash"
    assert vllm.provider == "vllm"
    assert vllm.model_id == "Qwen/Qwen3.6-35B-A3B"
    assert imagen_local.provider == "ollama"
    assert imagen_local.model_id == "x/z-image-turbo"
    assert imagen_local.args["temperature"] is None
    assert imagen_local.args["seed"] is None
    assert ocr_local.provider == "ollama"
    assert ocr_local.model_id == "glm-ocr"
    assert engine.resolve({"preset": "embed-local"}).provider == "lmstudio"
    assert engine.resolve({"preset": "mock"}).provider == "mock"


@pytest.mark.detail
def test_deepseek_reasoner_is_separate_thinking_flash_model():
    engine = hb.LLMEngine(config={"default_preset": None})
    flash = engine.resolve({"model": "deepseek/deepseek-v4-flash-0731", "provider": "openrouter", "gateway": "openai"})
    reasoner = engine.resolve({"model": "deepseek-reasoner", "provider": "openrouter", "gateway": "openai"})
    official = engine.resolve({"model": "deepseek-reasoner", "provider": "deepseek", "gateway": "openai"})

    assert flash.model == "deepseek-v4-flash"
    assert flash.args["think"] is False
    assert reasoner.model == "deepseek-reasoner"
    assert _runtime(reasoner)["model"] == "deepseek/deepseek-v4-flash-0731"
    assert reasoner.args["think"] is True
    assert official.model_id == "deepseek-reasoner"
    assert _runtime(official)["model"] == "deepseek-reasoner"


@pytest.mark.detail
def test_llm_alias_accepts_provider_identifier():
    llm = hb.LLM(model="deepseek-v4-flash", provider="deepseek", gateway="openai")

    assert llm.spec.model == "deepseek-v4-flash"
    assert _runtime(llm.spec, "openai")["model"] == "deepseek-chat"


@pytest.mark.detail
def test_bifrost_compiles_provider_prefixed_model(monkeypatch):
    monkeypatch.delenv("BIFROST_BASE_URL", raising=False)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="bifrost")

    runtime = _runtime(llm.spec)

    assert runtime["base_url"] == "http://localhost:8080/v1"
    assert runtime["model"] == "deepseek/deepseek-v4-pro"
    assert "provider" not in runtime
    assert "backend" not in runtime


@pytest.mark.detail
def test_litellm_compiles_provider_prefixed_model(monkeypatch):
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="litellm")

    runtime = _runtime(llm.spec)

    assert runtime["model"] == "deepseek/deepseek-v4-pro"
    assert "provider" not in runtime
    assert "backend" not in runtime


@pytest.mark.detail
def test_anthropic_openrouter_base_url_does_not_override_model_id():
    engine = hb.LLMEngine(config={"default_preset": None})
    openai = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "openai", "base_url": "https://openrouter.ai/api/v1"})
    portkey = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "portkey", "base_url": "https://openrouter.ai/api/v1"})
    native = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "openai", "base_url": "https://api.anthropic.com/v1"})

    assert _runtime(openai, "openai")["model"] == "claude-haiku-4-5"
    assert _runtime(portkey, "portkey")["model"] == "claude-haiku-4-5"
    assert _runtime(native, "openai")["model"] == "claude-haiku-4-5"


@pytest.mark.detail
def test_anthropic_endpoint_stays_provider_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    hb.CM_HVNB.refresh()
    engine = hb.LLMEngine(config={"default_preset": None})
    bundled = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "openai"})

    assert _runtime(bundled, "openai")["base_url"] == "https://api.anthropic.com/v1"

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic-proxy.example/v1")
    hb.CM_HVNB.refresh()
    overridden = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "openai"})

    assert _runtime(overridden, "openai")["base_url"] == "https://anthropic-proxy.example/v1"


@pytest.mark.detail
def test_anthropic_gateway_compiles_native_sdk_runtime(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic")
    runtime = _runtime(llm.spec)

    assert llm.spec.gateway == "anthropic"
    assert runtime["model"] == "claude-haiku-4-5"
    assert runtime["base_url"] == "https://api.anthropic.com"
    assert _anthropic_payload(runtime, llm.spec, [{"role": "user", "content": "hi"}])["max_tokens"] == 1024

    openai = hb.LLM(model="haiku", provider="anthropic", gateway="openai")
    assert _runtime(openai.spec, "openai")["base_url"] == "https://api.anthropic.com/v1"


@pytest.mark.detail
def test_anthropic_gateway_zai_uses_provider_route_and_bearer_token(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "zai-test")
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("ZAI_ANTHROPIC_BASE_URL", raising=False)
    hb.CM_HVNB.refresh()
    spec = hb.LLMEngine(config={"default_preset": None}).resolve({"model": "glm-5.2", "provider": "zai", "gateway": "anthropic"})
    runtime = _runtime(spec, "anthropic")

    assert runtime["model"] == "glm-5.2"
    assert runtime["base_url"] == "https://api.z.ai/api/anthropic"
    assert runtime["auth_token"] == "zai-test"
    assert "api_key" not in runtime


@pytest.mark.detail
def test_anthropic_client_kwargs_prefers_auth_token_over_api_key():
    kwargs = adapters._anthropic_client_kwargs(
        {
            "api_key": "unused",
            "auth_token": "zai-test",
            "base_url": "https://api.z.ai/api/anthropic",
            "headers": {},
            "args": {},
        }
    )

    assert kwargs == {"auth_token": "zai-test", "base_url": "https://api.z.ai/api/anthropic"}


@pytest.mark.detail
def test_client_export_rejects_non_openai_client_gateways(monkeypatch):
    for options in (
        {"model": "haiku", "provider": "anthropic", "gateway": "anthropic"},
        {"model": "haiku", "provider": "anthropic", "gateway": "litellm"},
        {"preset": "mock"},
    ):
        llm = hb.LLM(**options)
        with pytest.raises(ValueError, match=f"Gateway '{llm.spec.gateway}' cannot be exported"):
            llm.to_client()
        with pytest.raises(ValueError, match=f"Gateway '{llm.spec.gateway}' cannot be exported"):
            llm.to_aclient()


@pytest.mark.detail
def test_anthropic_client_export_uses_supported_gateway(monkeypatch):
    sync_client = object()
    async_client = object()
    sync_modes = []
    async_modes = []

    def sync_export(spec, mode="anthropic"):
        sync_modes.append(mode)
        return sync_client, _runtime(spec, "anthropic" if mode == "anthropic" else mode)

    def async_export(spec, mode="anthropic"):
        async_modes.append(mode)
        return async_client, _runtime(spec, "anthropic" if mode == "anthropic" else mode)

    for runtime in (anthropic_gateway, portkey_gateway, bifrost_gateway):
        monkeypatch.setattr(runtime, "_anthropic_client", sync_export)
        monkeypatch.setattr(runtime, "_anthropic_async_client", async_export)

    for gateway in ("anthropic", "portkey", "bifrost"):
        llm = hb.LLM(model="haiku", provider="anthropic", gateway=gateway)
        assert llm.to_anthropic_client() is sync_client
        assert llm.to_anthropic_aclient() is async_client

    assert sync_modes == ["anthropic", "portkey", "bifrost"]
    assert async_modes == ["anthropic", "portkey", "bifrost"]


@pytest.mark.detail
def test_anthropic_client_export_rejects_unsupported_gateway(monkeypatch):
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="openai")

    with pytest.raises(ValueError, match="gateway='anthropic', 'portkey', or 'bifrost'"):
        llm.to_anthropic_client()
    with pytest.raises(ValueError, match="gateway='anthropic', 'portkey', or 'bifrost'"):
        llm.to_anthropic_aclient()


@pytest.mark.detail
def test_portkey_anthropic_client_runtime_uses_messages_endpoint(monkeypatch):
    monkeypatch.setenv("PORTKEY_API_KEY", "pk-test")
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://api.portkey.ai/v1/")
    hb.CM_HVNB.refresh()
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="portkey")

    runtime = adapters._anthropic_gateway_runtime(llm.spec, "portkey")

    assert runtime["base_url"] == "https://api.portkey.ai"
    assert runtime["api_key"] == "portkey"
    assert runtime["headers"]["x-portkey-api-key"] == "pk-test"
    assert runtime["headers"]["x-portkey-provider"] == "anthropic"


@pytest.mark.detail
def test_bifrost_anthropic_client_runtime_uses_sdk_endpoint(monkeypatch):
    monkeypatch.delenv("BIFROST_BASE_URL", raising=False)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="bifrost")

    runtime = adapters._anthropic_gateway_runtime(llm.spec, "bifrost")

    assert runtime["base_url"] == "http://localhost:8080/anthropic"
    assert runtime["model"] == "deepseek/deepseek-v4-pro"


@pytest.mark.detail
def test_anthropic_gateway_converts_messages_tools_and_usage(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **payload):
            captured.update(payload)
            return {
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "text", "text": "native ok"}],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), _runtime(spec, mode)))
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=64)

    raw = llm_transport.chat_complete(
        llm.spec,
        [
            {"role": "system", "content": "be exact"},
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_add", "type": "function", "function": {"name": "hb_add", "arguments": '{"left":2,"right":3}'}}],
            },
            {"role": "tool", "tool_call_id": "call_add", "content": "5"},
            {"role": "user", "content": "done?"},
        ],
        stream=False,
        tools=_multi_tool_tools(),
    )

    assert captured["system"] == "be exact"
    assert captured["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_add", "name": "hb_add", "input": {"left": 2, "right": 3}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_add", "content": "5"}]},
        {"role": "user", "content": "done?"},
    ]
    assert captured["tools"][0]["name"] == "hb_add"
    assert captured["tools"][0]["input_schema"]["required"] == ["left", "right"]
    assert captured["max_tokens"] == 64
    assert raw["choices"][0]["message"]["content"] == "native ok"
    assert raw["usage"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }


@pytest.mark.detail
def test_anthropic_gateway_streams_text_thinking_and_tool_calls(monkeypatch):
    class FakeMessages:
        def create(self, **payload):
            assert payload["stream"] is True
            return [
                {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "answer"}},
                {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call_add", "name": "hb_add", "input": {}}},
                {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"left":2'}},
                {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": ',"right":3}'}},
                {"type": "message_delta", "usage": {"output_tokens": 5}},
            ]

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), _runtime(spec, mode)))
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=64)

    result = llm.chat("use add", include=["text", "think", "tool_calls", "usage"], reduce=False, tools=_multi_tool_tools())

    assert result["text"] == "answer"
    assert result["think"] == "plan"
    assert result["tool_calls"] == [{"id": "call_add", "type": "function", "function": {"name": "hb_add", "arguments": '{"left":2,"right":3}'}}]
    assert result["usage"] == {
        "input_tokens": 4,
        "output_tokens": 5,
        "prompt_tokens": 4,
        "completion_tokens": 5,
        "total_tokens": 9,
    }


@pytest.mark.detail
def test_litellm_api_base_preserves_configured_value():
    engine = hb.LLMEngine(config={"default_preset": None})
    anthropic = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "litellm", "base_url": "https://openrouter.ai/api/v1"})
    direct = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "litellm", "base_url": "https://api.anthropic.com/v1"})
    deepseek = engine.resolve({"model": "dsv4", "provider": "deepseek", "gateway": "litellm", "base_url": "https://api.deepseek.com/v1"})

    assert _runtime(anthropic, "litellm")["api_base"] == "https://openrouter.ai/api/v1"
    assert _runtime(direct, "litellm")["api_base"] == "https://api.anthropic.com/v1"
    assert _runtime(deepseek, "litellm")["api_base"] == "https://api.deepseek.com/v1"


@pytest.mark.detail
def test_default_runtime_args_include_seed_temperature_and_proxies():
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    assert llm.spec.args["temperature"] == 0
    assert llm.spec.args["seed"] == 42
    assert llm.spec.args["http_proxy"] is None
    assert llm.spec.args["https_proxy"] is None
    assert llm.spec.args["no_proxy"] is None


@pytest.mark.detail
def test_backend_string_and_gateway_mapping_resolution():
    openrouter = hb.LLM(model="dsv4", provider="openrouter", gateway="litellm")
    zai = hb.LLM(model="glm-5.2", provider="zai", gateway="litellm")

    assert openrouter.spec.provider_backend("litellm") == "openrouter"
    assert _runtime(openrouter.spec, "litellm")["model"] == "openrouter/deepseek/deepseek-v4-pro"
    assert zai.spec.provider_backend("litellm") == "zai"


@pytest.mark.detail
def test_qwen_flash_local_provider_records():
    engine = hb.LLMEngine(config={"default_preset": None})
    lmstudio = engine.resolve({"model": "qwen-flash", "provider": "lmstudio", "gateway": "openai"})
    ollama = engine.resolve({"model": "qwen3.6", "provider": "ollama", "gateway": "openai"})
    omlx = engine.resolve({"model": "qwen3.6-flash", "provider": "omlx", "gateway": "openai"})

    assert lmstudio.model == "qwen3.6-flash"
    assert lmstudio.model_id == "qwen3.6-35b-a3b"
    assert ollama.model == "qwen3.6-flash"
    assert ollama.model_id == "qwen3.6"
    assert omlx.model == "qwen3.6-flash"
    assert omlx.model_id == "qwen3.6-flash"


@pytest.mark.detail
def test_omlx_openai_compatible_provider_compiles_across_gateways():
    engine = hb.LLMEngine(config={"default_preset": None})

    direct = engine.resolve({"model": "qwen3.6-flash", "provider": "omlx", "gateway": "openai"})
    litellm = engine.resolve({"model": "qwen3.6-flash", "provider": "omlx", "gateway": "litellm"})
    portkey = engine.resolve({"model": "qwen3.6-flash", "provider": "omlx", "gateway": "portkey"})
    bifrost = engine.resolve({"model": "qwen3.6-flash", "provider": "omlx", "gateway": "bifrost"})

    assert _runtime(direct, "openai")["model"] == "qwen3.6-flash"
    assert _runtime(direct, "openai")["base_url"] == "http://localhost:8000/v1"
    assert litellm.provider_backend("litellm") == "openai"
    assert _runtime(litellm, "litellm")["model"] == "openai/qwen3.6-flash"
    assert _runtime(litellm, "litellm")["api_base"] == "http://localhost:8000/v1"

    portkey_runtime = _runtime(portkey, "portkey")
    assert portkey_runtime["model"] == "qwen3.6-flash"
    assert portkey_runtime["headers"]["x-portkey-provider"] == "openai"
    assert portkey_runtime["headers"]["x-portkey-custom-host"] == "http://localhost:8000/v1"
    assert portkey_runtime["headers"]["x-portkey-forward-headers"] == "Authorization"

    assert bifrost.provider_backend("bifrost") == "openai"
    assert _runtime(bifrost, "bifrost")["model"] == "openai/qwen3.6-flash"


@pytest.mark.detail
def test_embedding_model_dims_are_configured(monkeypatch):
    assert hb.LLM(preset="embed").dim == 1536
    assert hb.LLM(preset="embed-local").dim == 768

    def fail_embed(*args, **kwargs):
        raise AssertionError("dim should use config without calling provider")

    monkeypatch.setattr(llm_base, "embed_complete", fail_embed)
    assert hb.LLM(preset="embed").dim == 1536


@pytest.mark.detail
def test_embeddinggemma_is_local_only():
    engine = hb.LLMEngine(config={"default_preset": None})
    spec = engine.resolve({"model": "embeddinggemma"})

    assert spec.provider == "lmstudio"
    assert _runtime(spec)["model"] == "text-embedding-embeddinggemma-300m"


@pytest.mark.external
def test_cohere_and_voyage_embedding_providers_compile_across_gateways():
    engine = hb.LLMEngine(config={"default_preset": None})

    cohere = engine.resolve({"model": "embed-v4.0", "provider": "cohere", "gateway": "openai"})
    voyage = engine.resolve({"model": "voyage-4-lite", "provider": "voyage", "gateway": "openai"})

    cohere_cfg = engine.root()["providers"]["cohere"]
    assert _runtime(cohere, "openai")["model"] == "embed-v4.0"
    assert _runtime(cohere, "openai")["base_url"] == cohere_cfg["base_url"]["openai"]
    assert _runtime(cohere, "litellm")["model"] == "cohere/embed-v4.0"
    assert _runtime(cohere, "litellm")["api_base"] == cohere_cfg["base_url"]["litellm"]
    assert _route(cohere, "litellm").provider_backend() == "cohere"
    assert _runtime(cohere, "bifrost")["model"] == "cohere/embed-v4.0"
    assert _runtime(cohere, "portkey")["headers"]["x-portkey-provider"] == "cohere"

    voyage_cfg = engine.root()["providers"]["voyage"]
    assert _runtime(voyage, "openai")["model"] == "voyage-4-lite"
    assert _runtime(voyage, "openai")["base_url"] == voyage_cfg["base_url"]
    assert _runtime(voyage, "litellm")["model"] == "voyage/voyage-4-lite"
    assert _route(voyage, "litellm").provider_backend() == "voyage"
    assert _runtime(voyage, "bifrost")["model"] == "voyage/voyage-4-lite"
    assert _runtime(voyage, "portkey")["headers"]["x-portkey-provider"] == "voyage"

    assert hb.LLM(model="cohere", provider="cohere", gateway="openai").dim == 1536
    assert hb.LLM(model="voyage", provider="voyage", gateway="openai").dim == 1024


@pytest.mark.external
@pytest.mark.llm
@pytest.mark.parametrize(
    "model, provider, env_name, expected_dim",
    [
        ("embed-v4.0", "cohere", "COHERE_API_KEY", 1536),
        ("voyage-4-lite", "voyage", "VOYAGE_API_KEY", 1024),
    ],
    ids=["cohere-embed-v4", "voyage-4-lite"],
)
def test_external_cohere_voyage_embedding_smoke(model, provider, env_name, expected_dim):
    if not _env(env_name):
        pytest.skip(f"{env_name} is not set")
    for gateway in ("openai", "litellm"):
        llm = hb.LLM(model=model, provider=provider, gateway=gateway, cache=False)
        vector = llm.embed("hello heavenbase")
        assert llm.dim == expected_dim
        assert len(vector) == expected_dim


@pytest.mark.detail
def test_matrix_uses_catalog_provider_and_reasoning_capabilities():
    root = hb.LLMEngine(config={"default_preset": None}).root()

    # Verify the matrix generator would not produce openrouter cases for embeddinggemma
    # because it only has local-provider records (not openrouter).
    providers = root["models"]["embeddinggemma"]["providers"]
    assert "openrouter" not in providers

    # Verify gemma-4-26b-a4b-it lacks reasoning_efforts capability,
    # so the matrix generator only produces think-off/think-on modes.
    model_info = root["models"]["gemma-4-26b-a4b-it"]
    assert model_info["capabilities"].get("reasoning_efforts", True) is False


@pytest.mark.detail
def test_think_protocol_normalizes_by_gateway():
    engine = hb.LLMEngine(config={"default_preset": None})
    openrouter = engine.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "openai", "think": True})
    openrouter_payload = _openai_payload(_runtime(openrouter), spec=openrouter, messages=[])
    assert openrouter_payload["extra_body"]["reasoning"] == {"enabled": True}

    budget_spec = openrouter.with_args({"think": True, "reasoning_effort": "high", "reasoning_max_tokens": 128})
    budget_payload = _openai_payload(_runtime(budget_spec), spec=budget_spec, messages=[])
    assert budget_payload["extra_body"]["reasoning"] == {"enabled": True, "max_tokens": 128}

    disabled = openrouter.with_args({"think": False})
    disabled_payload = _openai_payload(_runtime(disabled), spec=disabled, messages=[])
    assert disabled_payload["extra_body"]["reasoning"] == {"enabled": False}

    cases = [
        {"model": "ds-flash", "provider": "openrouter", "gateway": "openai"},
        {"model": "ds-flash", "provider": "deepseek", "gateway": "openai"},
        {"model": "ds-flash", "provider": "openrouter", "gateway": "portkey"},
        {"model": "ds-flash", "provider": "openrouter", "gateway": "bifrost"},
        {"model": "qwen", "provider": "lmstudio", "gateway": "openai"},
    ]
    for options in cases:
        enabled = engine.resolve({**options, "think": True})
        disabled = engine.resolve({**options, "think": False})
        mode = "litellm" if options["gateway"] == "litellm" else None
        enabled_payload = _openai_payload(_runtime(enabled, mode), spec=enabled, messages=[])
        disabled_payload = _openai_payload(_runtime(disabled, mode), spec=disabled, messages=[])
        assert enabled_payload["extra_body"]["reasoning"] == {"enabled": True}, options
        assert disabled_payload["extra_body"]["reasoning"] == {"enabled": False}, options

    bifrost = engine.resolve({"model": "ds-flash", "provider": "openrouter", "gateway": "bifrost", "think": True, "reasoning_effort": "high"})
    assert _openai_payload(_runtime(bifrost), spec=bifrost, messages=[])["extra_body"]["reasoning"] == {"enabled": True, "effort": "high"}

    # Claude through LiteLLM takes Anthropic's native thinking setting, which LiteLLM forwards as given.
    litellm_think = engine.resolve({"model": "sonnet-4.6", "provider": "anthropic", "gateway": "litellm", "think": True})
    litellm_think_payload = _openai_payload(_runtime(litellm_think, "litellm"), spec=litellm_think, messages=[])
    assert litellm_think_payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "extra_body" not in litellm_think_payload
    # Claude before 4.6 has no adaptive thinking: think=True sends the smallest manual budget.
    litellm_haiku = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "litellm", "think": True})
    litellm_haiku_payload = _openai_payload(_runtime(litellm_haiku, "litellm"), spec=litellm_haiku, messages=[])
    assert litellm_haiku_payload["thinking"] == {"type": "enabled", "budget_tokens": 1024, "display": "summarized"}
    litellm_off = engine.resolve({"model": "haiku", "provider": "anthropic", "gateway": "litellm", "think": False})
    assert "thinking" not in _openai_payload(_runtime(litellm_off, "litellm"), spec=litellm_off, messages=[])

    litellm_budget = engine.resolve({"model": "sonnet-4.6", "provider": "anthropic", "gateway": "litellm", "reasoning_max_tokens": 2048, "max_tokens": 4096})
    litellm_budget_payload = _openai_payload(_runtime(litellm_budget, "litellm"), spec=litellm_budget, messages=[])
    assert litellm_budget_payload["thinking"] == {"type": "enabled", "budget_tokens": 2048, "display": "summarized"}

    local = engine.resolve({"model": "qwen", "provider": "lmstudio", "gateway": "openai", "think": False})
    local_payload = _openai_payload(_runtime(local), spec=local, messages=[])
    assert local_payload["extra_body"]["reasoning"] == {"enabled": False}

    zai = engine.resolve({"model": "glm-5.2", "provider": "zai", "gateway": "openai", "think": False})
    zai_payload = _openai_payload(_runtime(zai), spec=zai, messages=[])
    assert zai_payload["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning" not in zai_payload["extra_body"]

    native = engine.resolve({"model": "sonnet", "provider": "anthropic", "gateway": "anthropic", "think": True})
    native_payload = _anthropic_payload(_runtime(native), native, [{"role": "user", "content": "hi"}])
    assert native_payload["thinking"] == {"type": "adaptive", "display": "summarized"}

    native_effort = native.with_args({"think": True, "reasoning_effort": "high"})
    native_effort_payload = _anthropic_payload(_runtime(native_effort), native_effort, [{"role": "user", "content": "hi"}])
    assert native_effort_payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert native_effort_payload["output_config"] == {"effort": "high"}
    assert "effort" not in native_effort_payload

    legacy_native = engine.resolve({"model": "sonnet-4.6", "provider": "anthropic", "gateway": "anthropic", "think": True})

    native_budget = legacy_native.with_args({"reasoning_max_tokens": 2048, "max_tokens": 4096})
    native_budget_payload = _anthropic_payload(_runtime(native_budget), native_budget, [{"role": "user", "content": "hi"}])
    assert native_budget_payload["thinking"] == {"type": "enabled", "budget_tokens": 2048, "display": "summarized"}

    invalid_budget = legacy_native.with_args({"reasoning_max_tokens": 2048, "max_tokens": 2048})
    with pytest.raises(ValueError, match="budget must be lower than max_tokens"):
        _anthropic_payload(_runtime(invalid_budget), invalid_budget, [{"role": "user", "content": "hi"}])

    native_disabled = native.with_args({"think": False})
    native_disabled_payload = _anthropic_payload(_runtime(native_disabled), native_disabled, [{"role": "user", "content": "hi"}])
    assert "thinking" not in native_disabled_payload

    opus_budget = engine.resolve({"model": "opus", "provider": "anthropic", "gateway": "anthropic", "reasoning_max_tokens": 2048})
    with pytest.raises(ValueError, match="does not support manual thinking budgets"):
        _anthropic_payload(_runtime(opus_budget), opus_budget, [{"role": "user", "content": "hi"}])

    sonnet_budget = engine.resolve({"model": "sonnet", "provider": "anthropic", "gateway": "anthropic", "reasoning_max_tokens": 2048})
    with pytest.raises(ValueError, match="does not support manual thinking budgets"):
        _anthropic_payload(_runtime(sonnet_budget), sonnet_budget, [{"role": "user", "content": "hi"}])

    fable_budget = engine.resolve({"model": "fable", "provider": "anthropic", "gateway": "anthropic", "reasoning_max_tokens": 2048})
    with pytest.raises(ValueError, match="does not support manual thinking budgets"):
        _anthropic_payload(_runtime(fable_budget), fable_budget, [{"role": "user", "content": "hi"}])


def test_embedding_dim_falls_back_to_test_embed(monkeypatch):
    def fake_embed(spec, inputs, **kwargs):
        return {"data": [{"embedding": [1.0, 2.0, 3.0, 4.0]}], "usage": {}}

    monkeypatch.setattr(llm_base, "embed_complete", fake_embed)
    llm = hb.LLM(preset="mock")
    llm.spec = llm.spec.with_args({"dim": None})

    assert llm.dim == 4


@pytest.mark.detail
def test_gateway_base_url_env_overrides_default(monkeypatch):
    monkeypatch.setenv("BIFROST_BASE_URL", "http://localhost:8080/v1")
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="bifrost")

    runtime = _runtime(llm.spec)

    assert runtime["base_url"] == "http://localhost:8080/v1"


@pytest.mark.detail
def test_openai_payload_omits_client_args():
    payload = _openai_payload(
        {
            "model": "m",
            "args": {
                "timeout": 120,
                "max_retries": 3,
                "batch_size": 256,
                "embedding_batch_size": 256,
                "embedding_max_workers": 8,
                "http_proxy": "",
                "https_proxy": "",
                "stream_chat": True,
                "structured_stream": False,
                "enforce_non_stream_structured": True,
                "base_url": "http://example.test/v1",
                "api_key": "secret",
                "name": "runtime-label",
                "temperature": 0,
            },
        },
        max_tokens=8,
    )

    assert payload == {"model": "m", "temperature": 0, "max_tokens": 8}


@pytest.mark.detail
def test_openai_client_uses_timeout_and_max_retries(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setattr(adapters, "_OPENAI_CLIENTS", {})
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    _openai_client(llm.spec, "openai")

    assert captured["timeout"] == 120
    assert captured["max_retries"] == 3


@pytest.mark.detail
def test_llm_spec_hash_key_and_client_key_are_stable():
    direct = hb.LLM(model="deepseek-v4-flash", provider="deepseek", gateway="openai")
    alias = hb.LLM(model="ds-flash", provider="deepseek", gateway="openai")
    other = hb.LLM(model="ds-pro", provider="deepseek", gateway="openai")

    assert direct.spec.hash_key() == alias.spec.hash_key()
    assert direct.spec.hash_key() != other.spec.hash_key()
    assert _route(direct.spec, "litellm").hash_key() != direct.spec.hash_key()
    assert direct.spec.client_key() == other.spec.client_key()
    assert isinstance(_route(direct.spec, "litellm").client_key(), str)


@pytest.mark.detail
def test_openai_compatible_client_cache_reuses_instances(monkeypatch):
    created = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setattr(adapters, "_OPENAI_CLIENTS", {})
    direct = hb.LLM(model="deepseek-v4-flash", provider="deepseek", gateway="openai")
    alias = hb.LLM(model="ds-flash", provider="deepseek", gateway="openai")

    first, _ = _openai_client(direct.spec, "openai")
    second, _ = _openai_client(alias.spec, "openai")

    assert first is second
    assert len(created) == 1


@pytest.mark.detail
def test_openai_compatible_gateway_client_keys_are_distinct():
    openai = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")
    portkey = hb.LLM(model="dsv4", provider="deepseek", gateway="portkey")
    bifrost = hb.LLM(model="dsv4", provider="deepseek", gateway="bifrost")

    assert len({openai.spec.client_key(), portkey.spec.client_key(), bifrost.spec.client_key()}) == 3


def test_chat_include_projection(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        assert messages == [{"role": "user", "content": "hello"}]
        yield {"choices": [{"delta": {"role": "assistant", "reasoning_content": "thinking"}}]}
        yield {"choices": [{"delta": {"content": "hi"}}], "usage": {"total_tokens": 3}}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", think=True, cache=False)

    assert llm.chat("hello") == "hi"
    assert llm.chat("hello", include=["text", "think", "usage"], reduce=False) == {"text": "hi", "think": "thinking", "usage": {"total_tokens": 3}}


def test_think_false_suppresses_returned_reasoning_in_all_chat_fields(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert kwargs["think"] is False
        assert stream is True
        yield {"choices": [{"delta": {"role": "assistant", "reasoning_content": "hidden stream"}}]}
        yield {"choices": [{"delta": {"content": "hi"}}], "usage": {"total_tokens": 3}}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", think=False)
    result = llm.chat("hello", include=["text", "think", "content", "message", "messages", "delta", "raw", "usage"], reduce=False)

    assert result["text"] == "hi"
    assert result["think"] == ""
    assert result["content"] == "hi"
    assert result["message"] == {"role": "assistant", "content": "hi"}
    assert result["messages"] == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    assert result["delta"] == [{"role": "assistant", "content": "hi"}]
    assert result["usage"] == {"total_tokens": 3}
    assert "hidden stream" not in dumps_json(result["raw"])


def test_think_false_suppresses_returned_reasoning_for_non_stream_chat(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert kwargs["think"] is False
        assert stream is False
        return {"choices": [{"message": {"role": "assistant", "content": "hi", "reasoning_content": "hidden non-stream"}}], "usage": {"total_tokens": 3}}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", think=False, stream_chat=False)
    result = llm.chat("hello", include=["text", "think", "content", "message", "raw"], reduce=False)

    assert result["text"] == "hi"
    assert result["think"] == ""
    assert result["content"] == "hi"
    assert result["message"] == {"role": "assistant", "content": "hi"}
    assert "hidden non-stream" not in dumps_json(result["raw"])


def test_think_false_suppresses_returned_reasoning_in_stream_projection(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert kwargs["think"] is False
        assert stream is True
        yield {"choices": [{"delta": {"reasoning": "hidden stream"}}]}
        yield {"choices": [{"delta": {"content": "answer"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", think=False)

    assert list(llm.stream("hello", include="content")) == ["answer"]


def test_streaming_chat_merges_usage_chunks(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        yield {"choices": [{"delta": {"content": "hi"}}], "usage": {"prompt_tokens": 2}}
        yield {"choices": [{"delta": {}}], "usage": {"completion_tokens": 1, "total_tokens": 3}}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    assert llm.chat("hello", include="usage") == {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}


def test_chat_and_achat_are_canonical_text_methods():
    assert callable(hb.LLM.chat)
    assert callable(hb.LLM.achat)


def test_stream_content_interleaves_thinking(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        yield {"choices": [{"delta": {"reasoning": "step 1"}}]}
        yield {"choices": [{"delta": {"reasoning": " step 2"}}]}
        yield {"choices": [{"delta": {"content": "answer"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", think=True, cache=False)

    assert list(llm.stream("hello", include="content")) == ["<think>\nstep 1", " step 2", "\n</think>\nanswer"]


def test_message_format_accepts_message_like_objects():
    class JsonMessage:
        def json(self):
            return '{"role":"assistant","content":"json"}'

    class AttrMessage:
        role = "user"
        content = "attr"

    class DumpMessage:
        def model_dump(self):
            return {"role": "tool", "content": "dump", "tool_call_id": "call_1"}

    messages = llm_base.format_messages(["plain", JsonMessage(), AttrMessage(), DumpMessage(), {"role": "assistant", "content": "dict"}])

    assert messages == [
        {"role": "user", "content": "plain"},
        {"role": "assistant", "content": "json"},
        {"role": "user", "content": "attr"},
        {"role": "tool", "content": "dump", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "dict"},
    ]


def test_stream_include_projection(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        yield {"choices": [{"delta": {"content": "a"}}]}
        yield {"choices": [{"delta": {"content": "b"}}], "usage": {"total_tokens": 2}}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    assert list(llm.stream("hello", include=["text", "usage"], reduce=False)) == [
        {"text": "a", "usage": {}},
        {"text": "b", "usage": {"total_tokens": 2}},
    ]


def test_chat_delta_and_messages_include_full_history(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        yield {"choices": [{"delta": {"content": "Use "}}]}
        yield {
            "choices": [
                {
                    "delta": {
                        "content": "tools",
                        "tool_calls": [{"index": 0, "id": "call_lookup", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
                    }
                }
            ],
            "usage": {"total_tokens": 7},
        }

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = llm.chat("use tools", include=["message", "delta", "messages", "usage"], reduce=False, tools=[{"type": "function"}])

    assert result["message"] == {
        "role": "assistant",
        "content": "Use tools",
        "tool_calls": [{"id": "call_lookup", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }
    assert result["delta"] == [result["message"]]
    assert result["messages"] == [{"role": "user", "content": "use tools"}, result["message"]]
    assert result["usage"] == {"total_tokens": 7}


def test_stream_delta_is_final_message_chunk(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        yield {"choices": [{"delta": {"content": "a"}}]}
        yield {"choices": [{"delta": {"content": "b"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    assert list(llm.stream("hello", include=["text", "delta"], reduce=False)) == [
        {"text": "a", "delta": []},
        {"text": "b", "delta": []},
        {"text": "", "delta": [{"role": "assistant", "content": "ab"}]},
    ]


def test_mock_preset_and_mock_text_override():
    assert hb.LLM(preset="mock").chat("hello") == "mock response"
    assert hb.LLM(mock="instance response").chat("hello") == "instance response"
    assert hb.LLM(preset="mock").chat("hello", mock="call response") == "call response"


def test_mock_preset_can_be_configured_with_script():
    llm = hb.LLM(
        preset="mock",
        mock={"text": "done", "think": "plan", "pause": [0, 0], "chunk_chars": 2, "structured": {"ok": True}, "embed_dim": 4},
    )

    assert list(llm.stream("hello", include="content")) == ["<think>\npl", "an", "\n</think>\ndo", "ne", ""]
    assert llm.chat("json", include="structured", response_format={"type": "json_object"}) == {"ok": True}
    assert llm.embed("abc", include="dim") == 4


@pytest.mark.detail
def test_custom_provider_requires_runtime_base_url_and_model():
    with pytest.raises(ValueError, match="base_url"):
        hb.LLM(preset="custom")

    with pytest.raises(ValueError, match="concrete model"):
        hb.LLM(preset="custom", base_url="http://localhost:9999/v1")

    llm = hb.LLM(preset="custom", base_url="http://localhost:9999/v1", model="third-party-model")
    runtime = _runtime(llm.spec, "openai")

    assert llm.spec.model == "third-party-model"
    assert runtime["base_url"] == "http://localhost:9999/v1"
    assert runtime["model"] == "third-party-model"


def test_embed_include_projection(monkeypatch):
    def fake_embed(spec, inputs, **kwargs):
        assert inputs == ["a", "b"]
        assert "embedding_batch_size" not in kwargs
        assert "embedding_max_workers" not in kwargs
        return {"data": [{"embedding": [1.0, 0.0]}, {"embedding": [0.0, 1.0]}], "usage": {"total_tokens": 2}}

    monkeypatch.setattr(llm_base, "embed_complete", fake_embed)
    llm = hb.LLM(preset="embed")

    result = llm.embed(["a", "b"], include=["embeddings", "dim", "usage"], reduce=False)

    assert result["embeddings"] == [[1.0, 0.0], [0.0, 1.0]]
    assert result["dim"] == 2  # runtime vector length takes priority over configured dim
    assert result["usage"] == {"total_tokens": 2}


def test_embedding_payload_omits_chat_defaults():
    spec = hb.LLM(preset="embed", embedding_batch_size=64, embedding_max_workers=2).spec
    payload = _embedding_payload(LLM_ENGINE.apply(spec), input=["hello"])

    assert payload == {"model": "openai/text-embedding-3-small", "input": ["hello"]}


@pytest.mark.detail
def test_portkey_openrouter_embedding_raises_explicit_error():
    spec = hb.LLMEngine().resolve({"model": "text-embedding-small", "provider": "openrouter", "gateway": "portkey"})

    with pytest.raises(NotImplementedError, match="Portkey Gateway \\+ OpenRouter embeddings"):
        llm_transport.embed_complete(spec, ["hello"])


@pytest.mark.detail
def test_bifrost_image_raises_explicit_error():
    spec = hb.LLMEngine().resolve({"model": "gpt-image-mini", "provider": "openrouter", "gateway": "bifrost"})

    with pytest.raises(NotImplementedError, match="Bifrost image generation"):
        llm_transport.imagen_complete(spec, "draw")


def test_image_helpers_roundtrip():
    raw = b"image-bytes"
    image = hb.LLMImage.from_bytes(raw, format="jpg")

    assert all(not hasattr(hb.LLMImage, name) for name in ("normalize_format", "format_from_media_type", "looks_b64", "ensure_data"))
    assert image.format == "jpeg"
    assert image.to_bytes() == raw
    assert hb.LLMImage.from_b64(image.to_data_url()).to_bytes() == raw


def test_image_helpers_accept_common_input_sources():
    raw = b"image-bytes"
    assert hb.LLMImage.from_any(bytearray(raw)).to_bytes() == raw
    assert hb.LLMImage.from_any(memoryview(raw)).to_bytes() == raw
    assert hb.LLMImage.from_any({"image_url": {"url": "https://example.test/hb.png"}}).url == "https://example.test/hb.png"


def test_image_helpers_accept_pil_and_ndarray_inputs():
    np = pytest.importorskip("numpy")
    pil_image = pytest.importorskip("PIL.Image")

    image = pil_image.new("RGB", (1, 1), color=(255, 0, 0))
    array = np.zeros((1, 1, 3), dtype=np.uint8)

    assert hb.LLMImage.from_any(image).to_b64()
    assert hb.LLMImage.from_any(array).to_b64()


def test_image_helpers_roundtrip_pil_and_ndarray():
    np = pytest.importorskip("numpy")
    pil_image = pytest.importorskip("PIL.Image")

    original = pil_image.new("RGB", (2, 3), color=(255, 0, 0))
    image = hb.LLMImage.from_pil(original, format="png")

    back = image.to_pil()
    assert back.size == (2, 3)
    assert back.mode == "RGB"

    arr = image.to_array()
    assert arr.shape == (3, 2, 3)
    assert arr.dtype == np.uint8
    assert arr[0, 0, 0] == 255


def test_chat_and_stream_accept_image_inputs(monkeypatch):
    data_url = hb.LLMImage.from_bytes(b"png").to_data_url()
    seen = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        seen.append((stream, messages))
        yield {"choices": [{"delta": {"content": "ok"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    assert llm.chat("describe", images=data_url) == "ok"
    assert list(llm.stream([{"role": "user", "content": "describe"}], images=b"png")) == ["ok"]
    assert seen[0][1] == [{"role": "user", "content": [{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {"url": data_url}}]}]
    assert seen[1][1][0]["content"][0] == {"type": "text", "text": "describe"}
    assert seen[1][1][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_message_content_accepts_inline_llm_images():
    image = hb.LLMImage.from_bytes(b"png")
    messages = llm_base.format_messages({"role": "user", "content": ["describe", image]})

    assert messages[0]["content"][0] == {"type": "text", "text": "describe"}
    assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_imagen_include_projection(monkeypatch):
    def fake_imagen(spec, prompt, images=(), **kwargs):
        assert prompt == "draw"
        return {"data": [{"b64_json": base64.b64encode(b"png").decode("utf-8"), "revised_prompt": "draw!"}]}

    monkeypatch.setattr(llm_base, "imagen_complete", fake_imagen)
    llm = hb.LLM(preset="imagen")

    image = llm.imagen("draw")

    assert isinstance(image, hb.LLMImage)
    assert image.to_bytes() == b"png"
    assert image.revised_prompt == "draw!"


def test_imagen_accepts_single_reference_image(monkeypatch):
    def fake_imagen(spec, prompt, images=(), **kwargs):
        refs = list(images)
        assert prompt == "draw"
        assert len(refs) == 1
        assert refs[0].to_bytes() == b"ref"
        return {"data": [{"b64_json": base64.b64encode(b"png").decode("utf-8")}]}

    monkeypatch.setattr(llm_base, "imagen_complete", fake_imagen)
    llm = hb.LLM(preset="imagen")

    assert llm.imagen("draw", images=b"ref").to_bytes() == b"png"


@pytest.mark.detail
def test_litellm_openrouter_imagen_uses_litellm_completion(monkeypatch):
    seen = {}

    class FakeLiteLLM:
        def completion(self, **payload):
            seen.update(payload)
            return {"choices": [{"message": {"images": [{"url": "https://example.test/hb.png"}]}}], "usage": {}}

    monkeypatch.setattr(adapters, "_litellm", lambda spec: FakeLiteLLM())
    llm = hb.LLM(model="gpt-image-mini", provider="openrouter", gateway="litellm", api_key="or-test")

    raw = llm_transport.imagen_complete(llm.spec, "draw")

    assert raw["data"] == [{"url": "https://example.test/hb.png"}]
    assert seen["model"] == "openrouter/openai/gpt-5-image-mini"
    assert seen["api_key"] == "or-test"
    assert "openrouter.ai" in seen["api_base"]
    assert seen["messages"][0]["content"][0] == {"type": "text", "text": "draw"}
    assert seen["modalities"] == ["image", "text"]


def test_include_mismatch_raises(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        yield {"choices": [{"delta": {"content": "hi"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    with pytest.raises(ValueError, match="include"):
        llm.chat("hello", include="missing_field")


@pytest.mark.detail
def test_litellm_missing_is_lazy(monkeypatch):
    def fail_import(spec):
        raise ImportError("missing")

    monkeypatch.setattr(litellm_gateway, "_litellm", fail_import)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="litellm")

    with pytest.raises(ImportError, match="missing"):
        llm_transport.chat_complete(llm.spec, [{"role": "user", "content": "hello"}])


@pytest.mark.detail
def test_litellm_loader_sets_legacy_compat_defaults(monkeypatch):
    fake = types.SimpleNamespace(drop_params=False)
    monkeypatch.setitem(sys.modules, "litellm", fake)
    monkeypatch.setattr(adapters, "_LITELLM_MODULE", None)
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)

    loaded = adapters._litellm(hb.LLM(preset="mock").spec)

    assert loaded is fake
    assert loaded.drop_params is True
    assert _env("LITELLM_LOCAL_MODEL_COST_MAP") == "True"


@pytest.mark.detail
def test_gateway_selection_does_not_fallback():
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="bifrost")

    assert llm.spec.gateway == "bifrost"


def test_streaming_chat_merges_multiple_tool_calls(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_a", "type": "function", "function": {"name": "add", "arguments": '{"a":'}},
                            {"index": 1, "id": "call_b", "type": "function", "function": {"name": "mul", "arguments": '{"x":'}},
                        ]
                    }
                }
            ]
        }
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '1,"b":2}'}},
                            {"index": 1, "function": {"arguments": '3,"y":4}'}},
                        ]
                    }
                }
            ],
            "usage": {"total_tokens": 12},
        }

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = llm.chat("use tools", include=["message", "tool_calls", "usage"], reduce=False, tools=[{"type": "function"}])

    assert result["message"]["tool_calls"][0]["function"]["arguments"] == '{"a":1,"b":2}'
    assert result["message"]["tool_calls"][1]["function"]["arguments"] == '{"x":3,"y":4}'
    assert "index" not in result["message"]["tool_calls"][0]
    assert "index" not in result["message"]["tool_calls"][1]
    assert result["tool_calls"] == result["message"]["tool_calls"]
    assert result["usage"] == {"total_tokens": 12}


def test_llm_chat_executes_toolkit_calls_in_delta():
    calls = []

    def hb_add(left: int, right: int) -> int:
        return left + right

    toolkit = hb.Toolkit("math-tools", {"hb_add": hb_add}, namespace="demo")

    def fake_chat(spec, messages, stream=False, **kwargs):
        calls.append({"messages": [dict(message) for message in messages], "tools": kwargs.get("tools")})
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_add",
                                    "type": "function",
                                    "function": {"name": "hb_add", "arguments": '{"left":2,"right":3}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 3},
            }
        return {"choices": [{"message": {"role": "assistant", "content": f"sum is {messages[-1]['content']}"}}], "usage": {"total_tokens": 4}}

    llm = hb.LLM(preset="mock", mock_chat=fake_chat, mock_pause=(0, 0), cache=False)
    result = llm.chat("use add", include=["text", "delta", "messages", "tool_calls"], reduce=False, tools=[toolkit])

    assert result["text"] == "sum is 5"
    assert [message["role"] for message in result["delta"]] == ["assistant", "tool", "assistant"]
    assert result["delta"][1]["content"] == "5"
    assert [_tool_call_name(call) for call in result["tool_calls"]] == ["hb_add"]
    assert result["messages"][-3:] == result["delta"]
    assert calls[0]["tools"][0]["function"]["name"] == "hb_add"
    params = calls[0]["tools"][0]["function"]["parameters"]
    assert {"left", "right"} <= set(params["properties"])
    assert {"left", "right"} <= set(params["required"])
    assert calls[1]["messages"][-1]["role"] == "tool"

    session = LLMSession(llm)
    session.add_tool(toolkit)
    final = session.send("use add")

    assert final["content"] == "sum is 5"
    assert [message["role"] for message in session.messages] == ["user", "assistant", "tool", "assistant"]
    assert session.undo() is True
    assert session.messages == []


def test_llm_chat_executes_plain_callable_tools_in_delta():
    calls = []

    def hb_add(left: int, right: int) -> int:
        """Add two integers."""
        return left + right

    def fake_chat(spec, messages, stream=False, **kwargs):
        calls.append({"messages": [dict(message) for message in messages], "tools": kwargs.get("tools")})
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_add",
                                    "type": "function",
                                    "function": {"name": "hb_add", "arguments": '{"left":2,"right":3}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 3},
            }
        return {"choices": [{"message": {"role": "assistant", "content": f"sum is {messages[-1]['content']}"}}], "usage": {"total_tokens": 4}}

    llm = hb.LLM(preset="mock", mock_chat=fake_chat, mock_pause=(0, 0), cache=False)
    result = llm.chat("use add", include=["text", "delta", "messages", "tool_calls"], reduce=False, tools=[hb_add])

    assert result["text"] == "sum is 5"
    assert [message["role"] for message in result["delta"]] == ["assistant", "tool", "assistant"]
    assert result["delta"][1]["content"] == "5"
    assert [_tool_call_name(call) for call in result["tool_calls"]] == ["hb_add"]
    assert calls[0]["tools"][0]["function"]["name"] == "hb_add"
    params = calls[0]["tools"][0]["function"]["parameters"]
    assert params["properties"] == {"left": {"type": "integer"}, "right": {"type": "integer"}}
    assert params["required"] == ["left", "right"]


def test_tool_call_indices_are_internal_for_history_roundtrip(monkeypatch):
    calls = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        calls.append(messages)
        if len(calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "id": "call_mul", "type": "function", "function": {"name": "hb_mul", "arguments": '{"left":4'}},
                                {"index": 0, "id": "call_add", "type": "function", "function": {"name": "hb_add", "arguments": '{"left":2'}},
                            ]
                        }
                    }
                ]
            }
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ',"right":3}'}},
                                {"index": 1, "function": {"arguments": ',"right":5}'}},
                            ]
                        }
                    }
                ],
                "usage": {"total_tokens": 14},
            }
            return
        yield {"choices": [{"delta": {"content": "done"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = llm.chat("call both tools", include=["message", "messages", "tool_calls"], reduce=False, tools=_multi_tool_tools())

    assert [_tool_call_name(call) for call in result["tool_calls"]] == ["hb_add", "hb_mul"]
    assert [_tool_call_args(call) for call in result["tool_calls"]] == [{"left": 2, "right": 3}, {"left": 4, "right": 5}]
    assert all("index" not in call for call in result["tool_calls"])
    assert result["messages"][-1] == result["message"]

    followup = [
        *result["messages"],
        {"role": "tool", "tool_call_id": "call_add", "content": "5"},
        {"role": "tool", "tool_call_id": "call_mul", "content": "20"},
        {"role": "user", "content": "summarize"},
    ]
    assert llm.chat(followup, include="text") == "done"
    assistant = next(message for message in calls[1] if message.get("role") == "assistant" and message.get("tool_calls"))
    assert all("index" not in call for call in assistant["tool_calls"])


def test_nonstream_tool_call_index_is_not_exposed(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is False
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"index": 0, "id": "call_add", "type": "function", "function": {"name": "hb_add", "arguments": {"left": 2, "right": 3}}}
                        ],
                    }
                }
            ]
        }

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = llm.chat("call tool", include=["message", "tool_calls"], reduce=False, stream_chat=False, tools=_multi_tool_tools())

    assert result["tool_calls"][0]["id"] == "call_add"
    assert result["tool_calls"][0]["type"] == "function"
    assert _tool_call_name(result["tool_calls"][0]) == "hb_add"
    assert _tool_call_args(result["tool_calls"][0]) == {"left": 2, "right": 3}
    assert "index" not in result["tool_calls"][0]
    assert result["message"]["tool_calls"] == result["tool_calls"]


def test_streaming_chat_parses_structured_json(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        assert stream is True
        yield {"choices": [{"delta": {"content": '{"ok":'}}]}
        yield {"choices": [{"delta": {"content": "true}"}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = llm.chat("json", include=["text", "structured"], reduce=False, response_format={"type": "json_object"})

    assert result == {"text": '{"ok":true}', "structured": {"ok": True}}


def test_structured_stream_false_uses_non_streaming(monkeypatch):
    seen = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        seen.append((stream, dict(kwargs)))
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = llm.chat(
        "json",
        include=["structured"],
        reduce=True,
        response_format={"type": "json_object"},
        structured_stream=False,
    )

    assert seen[0][0] is False
    assert seen[0][1]["response_format"] == {"type": "json_object"}
    assert "structured_stream" not in seen[0][1]
    assert result == {"ok": True}


def test_enforce_non_stream_structured_yields_single_stream_chunk(monkeypatch):
    seen = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        seen.append(stream)
        return {"choices": [{"message": {"content": '{"ok":true}'}}], "usage": {"total_tokens": 3}}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = list(
        llm.stream(
            "json",
            include=["structured", "usage"],
            reduce=False,
            response_format={"type": "json_object"},
            enforce_non_stream_structured=True,
        )
    )

    assert seen == [False]
    assert result == [{"structured": {"ok": True}, "usage": {"total_tokens": 3}}]


def test_tool_calls_still_stream_when_structured_stream_false(monkeypatch):
    seen = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        seen.append((stream, dict(kwargs)))
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "noop", "arguments": "{}"}}]}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = llm.chat("tool", include=["tool_calls"], reduce=True, tools=[{"type": "function"}], structured_stream=False)

    assert seen[0][0] is True
    assert seen[0][1]["tools"] == [{"type": "function"}]
    assert "structured_stream" not in seen[0][1]
    assert result[0]["function"]["name"] == "noop"


def test_tool_call_repair_is_explicit_and_schema_aware(monkeypatch):
    calls = []

    def fake_chat(spec, messages, stream=False, **kwargs):
        calls.append(dict(kwargs))
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_add", "type": "function", "function": {"name": "add", "arguments": '{"a":1'}}]}}]}
        yield {"choices": [{"delta": {}}], "usage": {"total_tokens": 3}}

    tools = [
        {
            "type": "function",
            "function": {
                "name": "add",
                "parameters": {
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                },
            },
        }
    ]
    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    raw = llm.chat("use tools", include="tool_calls", tools=tools)
    fixed = llm.chat("use tools", include="tool_calls", tools=tools, repair_tool_calls=True)

    assert raw[0]["function"]["arguments"] == '{"a":1'
    assert fixed[0]["function"]["arguments"] == '{"a":1,"b":0}'
    assert "repair_tool_calls" not in calls[-1]


def test_tool_call_repair_escapes_open_strings(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "note", "arguments": '{"text":"line\n'}}]}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    result = llm.chat("note", include="tool_calls", repair_tool_calls=True, tools=[{"type": "function", "function": {"name": "note"}}])

    assert result[0]["function"]["arguments"] == '{"text":"line\\n"}'


def test_tool_call_repair_instance_strict_config_raises(monkeypatch):
    def fake_chat(spec, messages, stream=False, **kwargs):
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "bad", "arguments": "{bad"}}]}}]}

    monkeypatch.setattr(llm_base, "chat_complete", fake_chat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", tool_call_repair={"enabled": True, "strict": True})

    with pytest.raises(ValueError, match="Unable to repair"):
        llm.chat("bad", include="tool_calls", tools=[{"type": "function", "function": {"name": "bad"}}])


def test_concurrent_achat_with_a_proxy_restores_the_proxy_environment(monkeypatch):
    import asyncio
    import os

    monkeypatch.setenv("HTTP_PROXY", "http://corp:8080")
    if os.name != "nt":  # one case-insensitive variable on Windows
        monkeypatch.delenv("http_proxy", raising=False)
    seen = []

    async def fake_achat(spec, messages, stream=False, **kwargs):
        await asyncio.sleep(0.002 * int(messages[-1]["content"]))  # the first call entered returns first: not LIFO
        seen.append(os.environ.get("HTTP_PROXY"))
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(llm_base, "achat_complete", fake_achat)
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai", stream_chat=False)

    async def main(proxy):
        return await asyncio.gather(*(llm.achat(str(index), http_proxy=proxy) for index in range(16)))

    assert asyncio.run(main("http://proxy:3128")) == ["ok"] * 16
    assert seen == ["http://proxy:3128"] * 16 and os.environ["HTTP_PROXY"] == "http://corp:8080"
    seen.clear()
    assert asyncio.run(main("")) == ["ok"] * 16  # the empty proxy that local providers set by default
    assert seen == [None] * 16 and os.environ["HTTP_PROXY"] == "http://corp:8080"


def test_collector_closing_an_abandoned_proxied_stream_does_not_deadlock_another_proxy(tmp_path, home_env):
    # A stream holds its proxy across yields. When an abandoned stream sits in a reference cycle, the cyclic collector closes
    # it on whatever thread allocates next, which can be inside another proxy's locked section. Run it in a fresh
    # interpreter so a deadlock fails this test instead of hanging the suite.
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = """
import faulthandler, gc, os
faulthandler.dump_traceback_later(30, exit=True)
from heavenbase.llm import LLM
from heavenbase.utils.network import NetworkProxy
from heavenbase.workspace.registry import WorkspaceSpec

WorkspaceSpec.setup()  # what ``hb setup`` does, in the fresh home below
gc.disable()  # only the explicit collection below may close the abandoned stream

for name in NetworkProxy.PROXY_VARS:
    os.environ.pop(name, None)
os.environ["HTTP_PROXY"] = "http://old"
llm = LLM(preset="mock", mock_pause=(0, 0), cache=False)


class Collecting(str):
    def __eq__(self, other):  # NetworkProxy compares each setting with "" inside its locked section
        gc.collect()
        return str.__eq__(self, other)

    __hash__ = str.__hash__


def abandon():
    gen = llm.stream("hi", http_proxy="http://stream")
    next(gen)
    assert os.environ["HTTP_PROXY"] == "http://stream"
    loop = [gen]
    loop.append(loop)


abandon()
with NetworkProxy(http_proxy=Collecting("http://outer")):
    assert os.environ["HTTP_PROXY"] == "http://outer"
assert os.environ["HTTP_PROXY"] == "http://old" and not NetworkProxy._held
print("STREAM-OK")
"""
    env = home_env(tmp_path)
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0 and "STREAM-OK" in result.stdout, result.stdout + result.stderr


@pytest.mark.full
@pytest.mark.anyio
async def test_async_llm_methods_share_cache_and_batching(monkeypatch):
    chat_calls = []
    embed_calls = []
    image_calls = []

    async def fake_achat(spec, messages, stream=False, **kwargs):
        chat_calls.append((stream, list(messages)))
        if stream:

            async def chunks():
                yield {"choices": [{"delta": {"content": f"answer-{len(chat_calls)}"}}]}
                yield {"choices": [{"delta": {}}], "usage": {"total_tokens": 1}}

            return chunks()
        return {"choices": [{"message": {"content": f"answer-{len(chat_calls)}"}}], "usage": {"total_tokens": 1}}

    async def fake_aembed(spec, inputs, **kwargs):
        embed_calls.append(list(inputs))
        return {"data": [{"index": index, "embedding": [float(len(text))]} for index, text in enumerate(inputs)], "usage": {"total_tokens": len(inputs)}}

    async def fake_aimagen(spec, prompt, images=(), **kwargs):
        image_calls.append(prompt)
        payload = hb.LLMImage.from_bytes(f"image-{len(image_calls)}".encode("utf-8"))
        return {"data": [{"b64_json": payload.data}], "usage": {"image_count": 1}}

    monkeypatch.setattr(llm_base, "achat_complete", fake_achat)
    monkeypatch.setattr(llm_base, "aembed_complete", fake_aembed)
    monkeypatch.setattr(llm_base, "aimagen_complete", fake_aimagen)
    cache = LLMCache.from_dict({"enabled": True, "workspace": "test-llm-async-cache", "backend": {"main": {"type": "inmem"}}})
    llm = hb.LLM(model="dsv4", provider="deepseek", gateway="openai")

    assert await llm.achat("hello", cache=cache) == "answer-1"
    assert await llm.achat("hello", cache=cache) == "answer-1"
    assert [chunk async for chunk in llm.astream("stream", cache=cache)] == ["answer-2", ""]
    assert [chunk async for chunk in llm.astream("stream", cache=cache)] == ["answer-2", ""]

    first = await llm.aembed(["a", "bb", "a"], cache=cache, embedding_batch_size=1, embedding_max_workers=1, include=["embeddings", "raw"], reduce=False)
    second = await llm.aembed(["bb", "a"], cache=cache, embedding_batch_size=1, embedding_max_workers=1, include=["embeddings", "raw"], reduce=False)
    image = await llm.aimagen("draw", cache=cache)
    same_image = await llm.aimagen("draw", cache=cache)

    assert first["embeddings"] == [[1.0], [2.0], [1.0]]
    assert first["raw"]["misses"] == 2
    assert second["embeddings"] == [[2.0], [1.0]]
    assert second["raw"]["cached"] == 2
    assert image.to_bytes() == b"image-1"
    assert same_image.to_bytes() == b"image-1"
    assert len(chat_calls) == 2
    assert embed_calls == [["a"], ["bb"]]
    assert image_calls == ["draw"]


@pytest.mark.anyio
async def test_async_mock_stream_works_without_custom_async_adapter():
    llm = hb.LLM(preset="mock", mock={"text": "abcd", "chunk_chars": 2, "pause": [0, 0]})

    assert [chunk async for chunk in llm.astream("hello")] == ["ab", "cd", ""]


def test_mock_provider_comprehensive_fallback():
    def mock_chat(spec, messages, stream=False, kind=None, **kwargs):
        if kwargs.get("tools"):
            return [
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "id": "call_add", "type": "function", "function": {"name": "add", "arguments": '{"a":'}}]}}
                    ]
                },
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '1,"b":2}'}}]}}], "usage": {"total_tokens": 9}},
            ]
        if kwargs.get("response_format"):
            return [
                {"choices": [{"delta": {"content": '{"ok":'}}]},
                {"choices": [{"delta": {"content": "true}"}}], "usage": {"total_tokens": 5}},
            ]
        return [
            {"choices": [{"delta": {"role": "assistant", "reasoning_content": "brief thought"}}]},
            {"choices": [{"delta": {"content": "mock hello"}}], "usage": {"total_tokens": 4}},
        ]

    def mock_embed(spec, inputs, kind=None, **kwargs):
        return {
            "data": [{"index": index, "embedding": [float(index), float(len(text)), 1.0]} for index, text in enumerate(inputs)],
            "usage": {"total_tokens": sum(len(text) for text in inputs)},
        }

    def mock_imagen(spec, prompt, images=(), kind=None, **kwargs):
        return {"data": [{"b64_json": base64.b64encode(prompt.encode()).decode(), "revised_prompt": f"{prompt}!"}], "usage": {"image_count": 1}}

    llm = hb.LLM(
        provider="mock",
        model="mock",
        think=True,
        cache=False,
        mock_chat=mock_chat,
        mock_embed=mock_embed,
        mock_imagen=mock_imagen,
        mock_pause=(0, 0),
    )
    full = llm.chat(
        "hello",
        include=["text", "think", "content", "message", "usage", "raw", "elapsed", "created_at", "messages", "delta", "structured"],
        reduce=False,
    )

    assert full["text"] == "mock hello"
    assert full["think"] == "brief thought"
    assert full["usage"] == {"total_tokens": 4}
    assert full["elapsed"] >= 0
    assert full["message"] == {"role": "assistant", "content": "mock hello", "reasoning_content": "brief thought"}
    assert full["delta"] == [full["message"]]
    assert full["messages"] == [{"role": "user", "content": "hello"}, full["message"]]
    assert full["structured"] is None

    tool_result = llm.chat("use tools", include=["tool_calls", "usage"], reduce=False, tools=[{"type": "function"}])
    assert tool_result["tool_calls"][0]["function"]["name"] == "add"
    assert tool_result["tool_calls"][0]["function"]["arguments"] == '{"a":1,"b":2}'

    structured = llm.chat("json", include=["structured", "usage"], reduce=False, response_format={"type": "json_object"})
    assert structured == {"structured": {"ok": True}, "usage": {"total_tokens": 5}}

    single_embedding = llm.embed("ab", include=["embeddings", "dim", "usage", "elapsed"], reduce=False)
    batch_embedding = llm.embed(["ab", "abcd"], include=["embeddings", "dim", "usage"], reduce=False)
    assert single_embedding["embeddings"] == [0.0, 2.0, 1.0]
    assert single_embedding["dim"] == 3
    assert batch_embedding["embeddings"] == [[0.0, 2.0, 1.0], [1.0, 4.0, 1.0]]
    assert batch_embedding["usage"] == {"total_tokens": 6}

    image = llm.imagen("mock image", include="images")
    assert image.to_bytes() == b"mock image"
    assert image.revised_prompt == "mock image!"


@pytest.mark.external
@pytest.mark.llm
def test_openrouter_smoke():
    if not _env("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY is not set")
    llm = hb.LLM(model="deepseek-v4-flash", provider="openrouter", gateway="openai", temperature=0, max_tokens=24)

    assert "hb-ok" in llm.chat("Reply exactly hb-ok", include="text").lower()


@pytest.mark.external
@pytest.mark.llm
@pytest.mark.parametrize(
    "model, provider, env_name",
    [
        ("deepseek-v4-flash", "deepseek", "DEEPSEEK_API_KEY"),
        ("deepseek-v4-flash", "openrouter", "OPENROUTER_API_KEY"),
    ],
    ids=["deepseek-flash", "openrouter-deepseek-flash"],
)
def test_external_multi_tool_call_index_smoke(model, provider, env_name):
    if not _env(env_name):
        pytest.skip(f"{env_name} is not set")
    llm = hb.LLM(model=model, provider=provider, gateway="openai", temperature=0, max_tokens=256, timeout=45, max_retries=0)
    prompt = (
        "Call both tools in the same assistant response, exactly once each. "
        "Use hb_add with left=2 and right=3. Use hb_mul with left=4 and right=5. "
        "Do not write prose."
    )
    result = llm.chat(prompt, include=["message", "messages", "tool_calls"], reduce=False, tools=_multi_tool_tools(), tool_choice="auto")

    names = {_tool_call_name(call) for call in result["tool_calls"]}
    assert {"hb_add", "hb_mul"} <= names
    assert all("index" not in call for call in result["tool_calls"])
    assert result["messages"][-1] == result["message"]


# Commented out to save GPT image generation API costs.
# @pytest.mark.external
# @pytest.mark.llm
# @pytest.mark.slow
# def test_openrouter_image_mini_smoke():
#     if not _env("OPENROUTER_API_KEY"):
#         pytest.skip("OPENROUTER_API_KEY is not set")
#     llm = hb.LLM(model="gpt-5-image-mini", provider="openrouter", gateway="openai")
#
#     result = llm.imagen("Generate one simple image: black letters HB on white background.", include=["images", "usage"], reduce=False)
#
#     assert isinstance(result["images"], hb.LLMImage)
#     assert result["images"].data or result["images"].url
#     assert result["usage"]
