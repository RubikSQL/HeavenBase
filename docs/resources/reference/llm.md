# Python LLM Class

`hb.LLM` is the HeavenBase Python client for chat, streaming, embeddings, image generation, mocks, and gateway routing. It works without running a HeavenBase service: set the provider API key, create an `LLM`, and call it.

The bundled default is `preset="system"`, served by OpenRouter through the OpenAI-compatible gateway.

```bash
export OPENROUTER_API_KEY="..."
```

```python
import heavenbase as hb

llm = hb.LLM()
text = llm.chat("Reply with exactly: hb-ok")
```

On Windows PowerShell, use `$env:OPENROUTER_API_KEY="..."`.

## Mental Model

An `LLM` resolves five things before it calls a provider:

- `preset`: a named shortcut such as `system`, `chat`, `reason`, `coder`, `embed`, `imagen`, `mock`, or `custom`.
- `model`: a canonical model key or alias such as `ds-flash`, `sonnet`, `gpt`, or `gpt-image-mini`.
- `provider`: where the model is served. The bundled models default to `openrouter`.
- `gateway`: how the request is transported. The default is `openai`; optional gateways include `anthropic`, `portkey`, `litellm`, `bifrost`, and `mock`.
- `endpoint`: the request/response grammar, independently selected and
  validated against the gateway. Bundled routes use `openai` or `anthropic`.

You can pass only what you need:

```python
hb.LLM()                         # preset="system"
hb.LLM(model="ds-flash")
hb.LLM(model="ds-flash", provider="deepseek")
hb.LLM(preset="reason")
hb.LLM(preset="mock")
```

Pass `context=` when gateway records or configuration are composition-specific:

```python
context = hb.Context.load(home_path="bootstrap.yaml")
llm = hb.LLM(preset="chat", context=context)
```

The resulting `LLMSpec`, gateway lookup, client cache, semantic cache, and tool-call
repair policy retain that Context's resolver and configuration authority. Omitting
the authority deliberately selects `hb.DEFAULT_CONTEXT`; an explicitly bound LLM
does not consult it later.

Unknown keywords become provider request arguments, so advanced provider parameters stay available:

```python
llm = hb.LLM(model="ds-flash", max_tokens=256, temperature=0)
```

Default request arguments are `timeout=120`, `max_retries=3`, `temperature=0`, `seed=42`, `http_proxy=None`, `https_proxy=None`, `no_proxy=None`, and `enforce_non_stream_structured=False`. Model, provider, and preset defaults can override them; explicit call kwargs win last.

## Built-In Presets

The default preset is `system`. Presets use persistable model aliases so user configs stay readable.

| Preset | Model alias | Default thinking | Description |
|---|---|---|---|
| `system` | `ds-flash` | disabled | Default lightweight system LLM for short orchestration calls. |
| `tiny` | `gemma` | disabled | Tiny chat model for low-latency offline work. |
| `chat` | `ds-flash` | disabled | General chat preset for fast non-thinking answers. |
| `chat-pro` | `ds-pro` | disabled | Stronger chat preset that still avoids reasoning by default. |
| `reason` | `ds-pro` | enabled | Reasoning preset for harder tasks that should expose thinking when supported. |
| `reason-pro` | `gpt` | enabled (`max`) | Higher-capability reasoning preset backed by the default GPT alias. |
| `worker` | `ds-flash` | disabled | Background worker preset for deterministic non-thinking utility calls. |
| `local` | `qwen3.6-flash` | disabled | Local LM Studio chat preset. |
| `worker-local` | `qwen3.6-flash` | disabled | Local LM Studio worker preset. |
| `coder` | `sonnet` | enabled | Coding preset with thinking enabled for multi-step implementation work. |
| `coder-pro` | `opus` | enabled | Highest-end coding preset for deep implementation and review work. |
| `embed` | `gpt-embedding-small` | n/a | Default embedding preset. |
| `embed-local` | `embeddinggemma` | n/a | Local embedding preset. |
| `imagen` | `gpt-image-mini` | n/a | Default image-generation preset. |
| `imagen-local` | `z-image-turbo` | n/a | Local Ollama image-generation preset. |
| `ocr-local` | `glm-ocr` | disabled | Local Ollama OCR preset. |
| `mock` | `mock` | offline | Non-LLM deterministic mock preset for tests and demos. |
| `custom` | `custom` | runtime supplied | Runtime-supplied OpenAI-compatible provider preset. |

Thinking defaults use the HeavenBase canonical `think` request option. At call time, HeavenBase applies configurable gateway/provider control through `extra_body.reasoning` or `extra_body.thinking.type` for OpenAI-compatible gateways (`openai`, `portkey`, `bifrost`, and `litellm`). Anthropic Messages requests (the native `anthropic` gateway, and Portkey and Bifrost with `endpoint="anthropic"`) never use `extra_body.reasoning`, which no Messages API reads: where that is the configured control, they use the native `thinking` setting instead, and a provider's own control, such as zai's `thinking.type`, is kept. Claude through LiteLLM (`provider="anthropic", gateway="litellm"`) uses the native setting too. The native setting maps `think=True` to adaptive thinking with summarized display; Claude models before 4.6 (such as `haiku`, Claude Haiku 4.5) have no adaptive thinking, so for them `think=True` sends a manual budget of 1024 tokens, which needs `max_tokens` above 1024. On Anthropic Messages routes, a manual budget sets the default limit to the budget plus 1024 tokens. An explicit or configured limit must exceed the budget. A home first used with 0.1.2.2 has `heavenbase.llm.gateways.anthropic.default_args.max_tokens: 1024` saved as a configured limit; `hb config unset heavenbase.llm.gateways.anthropic.default_args.max_tokens` removes it. Messages routes accept `max_tokens` or its alias `max_completion_tokens` at the top level or in `extra_body`. The nested limit takes precedence, and `max_tokens` takes precedence when both names appear at the same level. The Messages request carries one `max_tokens` field. The Messages request defaults to 1024 tokens without a manual budget. Explicit `reasoning_max_tokens` maps to a manual thinking budget when the model supports it; the adaptive-only models (`opus`, `sonnet`, `fable`) refuse a budget. On the Messages routes `reasoning_effort` maps to `output_config.effort`, where an explicit `effort` argument goes as well; Claude models before 4.6 other than Opus 4.5 (such as `haiku`) accept no effort level, so `reasoning_effort` raises `ValueError` for them before any request; an `output_config` in `extra_body` is merged with it, and a different effort there raises `ValueError`. With thinking on, `reasoning_effort` for Claude on `gateway="litellm"` raises `ValueError` before any request is sent, because HeavenBase cannot send the effort level there for every model. With thinking off (the default of most presets), it is passed to LiteLLM, which sends it as `output_config.effort` for some models (such as `sonnet-4.6`) and drops it for others (such as `sonnet` and `opus`); Claude models without an effort setting (such as `haiku`) still raise `ValueError`. HeavenBase also asks LiteLLM to forward `thinking` for Claude models its bundled model list does not mark as thinking-capable, which LiteLLM otherwise drops. With thinking on, Claude requests on every route carry no `temperature` and no `top_k`, top-level or in `extra_body`, because Claude accepts neither with thinking (temperature only at its default of 1). A configured value, such as the packaged default `temperature=0` or one from a model entry or preset, is left out, so it no longer fails these requests. A value you pass yourself, to `hb.LLM(...)` or to the call, top-level or in `extra_body`, raises `ValueError` before any request instead, except `temperature=1`; values in `default_args` given to `hb.LLM(...)` count as configuration and are left out. The Agent extension's `llm_args` are passed to `hb.LLM(...)` as keyword arguments, so they count as the caller's. Without thinking, a request keeps its temperature, except that the `sonnet`, `opus` and `fable` entries set none by default, because those Claude 5 models refuse sampling parameters altogether. Other models on the Messages routes keep their temperature. Anthropic's other thinking rules, a `top_p` between 0.95 and 1 and no forced `tool_choice`, apply to what you pass. Claude on the OpenAI-format routes uses each service's documented thinking field: on OpenRouter (`gateway="openai"`, and Portkey or Bifrost routing to it) the `reasoning` object (`enabled`, `effort` or `max_tokens`); on Anthropic's OpenAI-compatibility endpoint (`provider="anthropic", gateway="openai"`) a top-level `thinking` of type `enabled` with `budget_tokens`, the only type it accepts (it turns it into adaptive thinking on the models that refuse `enabled`); on Portkey's OpenAI format (`provider="anthropic", gateway="portkey"`) the top-level `thinking` that Portkey passes on to Claude, set as on the Messages routes; on Bifrost's OpenAI format (`provider="anthropic", gateway="bifrost"`) `reasoning.max_tokens`, which Bifrost sends as an `enabled` budget. On the last three, `think=True` sends the smallest budget (1024 tokens) where a budget is needed, `reasoning_max_tokens` follows the native budget rules, and the budget must stay below `max_tokens` or, when only that is set, `max_completion_tokens`, and `reasoning_effort` raises `ValueError`, because Anthropic's compatibility endpoint ignores it and the other two have no field that carries it; on Bifrost's OpenAI format the adaptive-only models (`opus`, `sonnet`, `fable`) raise `ValueError` too, since they refuse `enabled` thinking; use `endpoint="anthropic"` there. HeavenBase checks the request bodies it sends, not the services' replies. The removed `thinking_budget_tokens` alias fails with guidance to use `reasoning_max_tokens`. When `think=False`, HeavenBase also suppresses provider-returned reasoning from normalized `think`, `content`, `message`, `delta`, `messages`, and `raw` outputs so providers that ignore the request hint do not leak thinking content. Provider-specific options can still be passed explicitly with `extra_body` when a local server needs them.

You can override it per call:

```python
hb.LLM(preset="chat").chat(
    "Think briefly, then answer.",
    think=True,
    reasoning_effort="medium",
)
```

## Built-In Models

The bundled model catalog is intentionally small. Online production models use `default_provider: openrouter` and list the OpenRouter identifier first. Local-only models, such as `embeddinggemma`, `z-image-turbo`, and `glm-ocr`, keep local providers only.

| Canonical model | Aliases | Default identifier | Other identifiers |
|---|---|---|---|
| `deepseek-v4-flash` | `ds-flash`, `deepseek-flash`, `deepseek-chat` | `deepseek/deepseek-v4-flash-0731` | `deepseek: deepseek-chat` |
| `deepseek-reasoner` | `ds-flash-thinking`, `deepseek-flash-thinking` | `deepseek/deepseek-v4-flash-0731` | `deepseek: deepseek-reasoner` |
| `deepseek-v4-pro` | `ds`, `ds-pro`, `dsv4`, `dsv4-pro`, `deepseek`, `deepseek-v4`, `deepseek-pro` | `deepseek/deepseek-v4-pro` | `deepseek: deepseek-v4-pro` |
| `gpt-5.6-luna` | `gpt-nano`, `gpt-luna`, `5.6-luna`, `luna` | `openai/gpt-5.6-luna` | `openai: gpt-5.6-luna` |
| `gpt-5.6-terra` | `gpt-mini`, `gpt-terra`, `5.6-terra`, `terra` | `openai/gpt-5.6-terra` | `openai: gpt-5.6-terra` |
| `gpt-5.6-sol` | `gpt`, `gpt-sol`, `5.6`, `5.6-sol`, `sol` | `openai/gpt-5.6-sol` | `openai: gpt-5.6-sol` |
| `claude-haiku-4-5` | `haiku`, `haiku-4-5` | `anthropic/claude-haiku-4.5` | `anthropic: claude-haiku-4-5` |
| `claude-sonnet-5` | `sonnet`, `sonnet-5` | `anthropic/claude-sonnet-5` | `anthropic: claude-sonnet-5` |
| `claude-sonnet-4-6` | `sonnet-4.6` | `anthropic/claude-sonnet-4.6` | `anthropic: claude-sonnet-4-6` |
| `claude-opus-5` | `opus`, `opus-5` | `anthropic/claude-opus-5` | `anthropic: claude-opus-5` |
| `claude-fable-5` | `fable`, `fable-5` | `anthropic/claude-fable-5` | `anthropic: claude-fable-5` |
| `grok-4.5` | `grok` | `x-ai/grok-4.5` | `grok: grok-4.5` |
| `gemini-3.5-flash-lite` | `gemini-flash-lite`, `gemini-lite` | `google/gemini-3.5-flash-lite` | `gemini: gemini-3.5-flash-lite` |
| `gemini-3.6-flash` | `gemini-flash` | `google/gemini-3.6-flash` | `gemini: gemini-3.6-flash` |
| `gemini-3.1-pro-preview` | `gemini-pro` | `google/gemini-3.1-pro-preview` | `gemini: gemini-3.1-pro-preview` |
| `kimi-k2.6` | `kimi`, `k2.6` | `moonshotai/kimi-k2.6` | `moonshot: kimi-k2.6` |
| `kimi-k3` | `k3` | `moonshotai/kimi-k3` | `moonshot: kimi-k3` |
| `glm-5.2` | `glm` | `z-ai/glm-5.2` | `zai: glm-5.2` |
| `glm-4.7-flash` | `glm-flash`, `glm-4.7` | `z-ai/glm-4.7-flash` | `zai: glm-4.7-flash` |
| `gemma-4-26b-a4b-it` | `gemma4`, `gemma4-26b`, `gemma4-26b-a4b`, `gemma4-26b-a4b-it`, `gemma`, `gemma-26b`, `gemma-26b-a4b`, `gemma-26b-a4b-it` | `google/gemma-4-26b-a4b-it` | `lmstudio: gemma-4-26b-a4b`, `ollama: gemma4:26b` |
| `qwen3.6-flash` | `qwen3.6`, `qwen3.6-flash`, `qwen3.6-35b`, `qwen3.6-35b-a3b`, `qwen`, `qwen-flash`, `qwen-35b`, `qwen-35b-a3b` | `qwen/qwen3.6-flash` | `lmstudio: qwen3.6-35b-a3b`, `ollama: qwen3.6`, `omlx: qwen3.6-flash`, `vllm: Qwen/Qwen3.6-35B-A3B` |
| `qwen3.7-max` | `qwen-max`, `qwen3.7max` | `dashscope: qwen3.7-max` | n/a |
| `qwen3.7-plus` | `qwen-plus`, `qwen3.7plus` | `dashscope: qwen3.7-plus` | n/a |
| `embeddinggemma` | `embeddinggemma-300m` | `lmstudio: text-embedding-embeddinggemma-300m` | `ollama: embeddinggemma`, `omlx: embeddinggemma` |
| `text-embedding-3-small` | `gpt-embedding`, `gpt-embedding-small`, `text-embedding-small` | `openai/text-embedding-3-small` | `openai: text-embedding-3-small` |
| `embed-v4.0` | `cohere`, `cohere-embedding`, `cohere-embedding-v4`, `embed-v4` | `cohere: embed-v4.0` | n/a |
| `voyage-4-lite` | `voyage`, `voyage-lite` | `voyage: voyage-4-lite` | n/a |
| `gpt-5-image-mini` | `gpt-image-mini`, `image-mini` | `openai/gpt-5-image-mini` | `openai: gpt-5-image-mini` |
| `gpt-5.4-image-2` | `gpt-image`, `gpt-image-2`, `image-2` | `openai/gpt-5.4-image-2` | `openai: gpt-5.4-image-2` |
| `z-image-turbo` | `z-image`, `image-local`, `imagen-local` | `ollama: x/z-image-turbo` | n/a |
| `glm-ocr` | `ocr` | `ollama: glm-ocr` | `omlx: glm-ocr` |

The GPT-5.6 entries set `reasoning_effort` to `high` for Luna and Terra and `medium` for Sol. The `reason-pro` preset overrides Sol to `max`; direct callers replacing a former Pro-model workload can pass `reasoning_effort="max"`. OpenRouter receives this setting as `extra_body.reasoning.effort`.

`mock` and `custom` are utility model entries for offline testing and runtime-supplied OpenAI-compatible providers.

## Chat

`chat` is the main text entry point. `LLM` is callable, so `llm("hello")` is the same as `llm.chat("hello")`.

```python
llm = hb.LLM(model="ds-flash")

text = llm.chat("Summarize HeavenBase in one sentence.")
same = llm("Summarize HeavenBase in one sentence.")
```

Pass a system prompt with `system=...`:

```python
answer = llm.chat("What should I do next?", system="You are concise and practical.")
```

Provider arguments can be passed at construction time or call time:

```python
llm = hb.LLM(model="ds-flash", temperature=0)
answer = llm.chat("Draft a title", max_tokens=24)
```

## Message Inputs

`chat` and `stream` accept a single string, one OpenAI-style message dictionary, or a list of messages:

```python
llm.chat("Hello")

llm.chat({"role": "user", "content": "Hello"})

llm.chat([
    {"role": "system", "content": "Be direct."},
    {"role": "user", "content": "Explain vector search."},
])
```

The formatter also accepts objects with `model_dump()`, `to_dict()`, `dict()`, `json()`, or `role` and `content` attributes. Tool-call arguments supplied as dictionaries or lists are serialized into the OpenAI-compatible `tool_calls` shape.

For multimodal models, pass image inputs with `images=`. HeavenBase normalizes them to `LLMImage` and sends OpenAI-compatible `image_url` content parts on the last user message:

```python
llm = hb.LLM(model="qwen3.6", provider="openrouter")

answer = llm.chat(
    "What is in this image?",
    images=["./sample.png"],
)

for chunk in llm.stream("Describe this image briefly.", images=b"...png bytes..."):
    print(chunk, end="")
```

`images=` accepts `LLMImage`, URLs, data URLs, base64 strings, local file paths, bytes-like objects, binary file objects, provider-style dictionaries, Pillow images, and numpy-compatible ndarrays. `LLMImage` remains the canonical in-memory format and can emit bytes, base64, data URLs, or OpenAI-compatible dictionaries.

## Streaming

Use `stream` when you want deltas as they arrive:

```python
for chunk in llm.stream("Write a short haiku"):
    print(chunk, end="")
```

`chat` uses the same streaming path internally and gathers the final response. This keeps regular chat, reasoning streams, usage accounting, structured outputs, and tool calls on one response pipeline.

## Async Usage

Async callers can use the `a*` methods without changing request arguments or include projection:

```python
llm = hb.LLM(model="ds-flash")

text = await llm.achat("Summarize HeavenBase in one sentence.")

async for chunk in llm.astream("Write a short haiku"):
    print(chunk, end="")
```

Embeddings and image generation have matching async entry points:

```python
vectors = await hb.LLM(preset="embed").aembed(["hello", "world"])
image = await hb.LLM(preset="imagen").aimagen("A clean product render of an HB mug")
```

The async methods use the same cache rows, embedding batching, deduplication, include fields, structured parsing, and tool-call merge path as the sync methods. `achat(..., stream=True)` returns the same async generator as `astream(...)`; `astream` is usually clearer at call sites.

## Include Projection

The `include` argument chooses what part of the response you want. It is a broad response projection design and is intentionally powerful.

Common fields:

- `text`: final assistant text.
- `think`: reasoning or thinking content, when a provider streams it separately.
- `content`: combined thinking plus visible answer, with `<think>` tags around reasoning.
- `message`: OpenAI-format assistant response dictionary. It contains `role="assistant"`, `content`, and optional `tool_calls`; it is not the full conversation history. A Claude thinking turn on an Anthropic Messages route or on `gateway="litellm"` also carries `thinking_blocks` (see the native Anthropic gateway below). OpenRouter and Bifrost Chat Completions can return `reasoning_details` for Claude. Portkey Chat Completions with Anthropic also normalizes signed blocks into `thinking_blocks` and keeps the original `content_blocks` on non-streamed messages, including citations.
- `delta`: new OpenAI-format messages produced by this inference call. Today this is usually `[message]`; once tool execution is wired into the inference path, tool-result messages are appended after the assistant message.
- `messages`: full conversation history, equal to the normalized input messages plus `delta`.
- `tool_calls`: normalized OpenAI `tool_calls` from the assistant response.
- `usage`: provider usage counters for this call. Common keys are `prompt_tokens`, `completion_tokens`, and `total_tokens`; streamed usage chunks are merged by summing numeric counters and keeping the first non-numeric value per key. A response that reports `prompt_tokens` and `completion_tokens` but no `total_tokens` gets their sum as `total_tokens`, whether it was streamed or not (a stream's chunks count together, so counts that arrive in different chunks still give one total), and a tool loop's usage sums every round's total. The Anthropic Messages routes (`gateway="anthropic"`, Portkey and Bifrost) expose the numeric counts from `message_start` and subsequent changes in `message_delta`. Sum these increments to recover the latest counts. If you close a live stream or the SDK raises, the usage already yielded reflects the last counts received and may be partial. A closed iterator cannot report counts from later events.
- `raw`: raw provider response chunks or response object.
- `elapsed`: request elapsed seconds.
- `created_at`: local response creation timestamp from `time.time()`.
- `structured`: parsed structured output.

A single field returns that value directly. Multiple fields return a dictionary. Use `reduce=False` to force dictionary output even for one field:

```python
text = llm.chat("Hello", include="text")

detail = llm.chat(
    "Reply with exactly: hb-ok",
    include=["text", "usage", "elapsed"],
    reduce=False,
    max_tokens=8,
)
```

`stream` supports the same projection:

```python
for event in llm.stream("Think briefly, then answer.", include=["text", "usage"], reduce=False):
    print(event)
```

When `stream` includes `delta` or `messages`, content chunks still arrive progressively and HeavenBase emits a final metadata chunk where `text` and `think` are empty and `delta` contains the completed assistant message.

## Structured Output

Pass OpenAI-compatible `response_format` arguments directly:

```python
data = llm.chat(
    "Return JSON with keys name and score.",
    response_format={"type": "json_object"},
    include="structured",
)
```

Most providers can stream structured output through the normal path. When a model is known to be unreliable for streaming structured JSON, its model defaults can set `structured_stream: false`. You can also force non-streaming structured calls from user code:

```python
data = llm.chat(
    "Return JSON with key ok=true.",
    response_format={"type": "json_object"},
    include="structured",
    enforce_non_stream_structured=True,
)
```

For `stream(..., enforce_non_stream_structured=True)`, HeavenBase performs one non-streaming request and yields a single projected chunk.

## Tools And Tool Calls

Pass OpenAI-compatible tools directly:

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "lookup_user",
            "description": "Look up a user by id",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    }
]

calls = llm.chat("Find user 42", tools=tools, include="tool_calls")
```

When you want HeavenBase to execute tools during the LLM turn, pass plain Python callables, objects with `run_to_str(...)` / `arun_to_str(...)`, or Toolkit-style runners. Plain callables are wrapped locally by the LLM client from their signature and annotations; persisting executable tool bundles still belongs to `hb.Toolkit`.

Tool calls are collected from streaming deltas and returned in the standard `tool_calls` field. Streaming-only `index` values are used internally to merge multiple tool calls in the right slot, then omitted from the final `message`, `delta`, and `messages` fields so follow-up history stays OpenAI-compatible. The optional `LLMToolCallRepair` plugin can repair common provider mistakes after streaming deltas have been merged. It is explicit by default: pass `repair_tool_calls=True` on a call, or configure an instance with `tool_call_repair={"enabled": True}`.

```python
calls = llm.chat(
    "Find user 42",
    tools=tools,
    include="tool_calls",
    repair_tool_calls=True,
)
```

The built-in repair path balances missing JSON brackets, closes open strings, escapes raw control characters inside strings, and fills missing required function parameters from the tool schema with type-aware defaults. Set `tool_call_repair={"enabled": True, "strict": True}` to raise when arguments cannot be repaired.

For multi-turn agents, use `LLMSession` and attach runtime tools once:

```python
llm = hb.LLM(preset="chat", cache=False)
session = hb.LLMSession(llm)

workspace = hb.HeavenBase("agent-memory", preset="debug")
session.add_mcp(workspace.to_mcp(profile="agent").to_fastmcp(), name="agent-memory-client")
session.add_mcp("http://127.0.0.1:7001/mcp", name="external-tools")

print(session.list_tools())
final = session.send("Inspect memory, then use the external tool.", max_tool_turns=8)
```

`add_mcp(...)` accepts a FastMCP server, URL, transport, or other value supported by `fastmcp.Client`; use `Toolkit.from_fastmcp(...)` when you need to inspect or reuse the imported toolkit before attaching it. Registered Capsule Toolkits can be loaded with `hb.Toolkit.load(...)` and attached with `session.add_tool(...)`. Session setup rejects duplicate tool names early, which is usually easier to debug than a provider-time tool schema error.

The `hb llm chat` and `hb llm session` commands keep this runtime path and also persist completed chat/session history into the active workspace's `agent` extension rows. New workspaces include `agent` in the bundled default extension roots; workspaces created with an explicit root set must include or enable it before using these commands. Query `hb.Session` and `hb.Message` rows when you need to inspect prior prompts, assistant deltas, tool calls, tool outputs, or usage.

## Tracing

`chat`, `achat`, `stream`, and `astream` accept a `trace=` callback that observes every provider call made for that one call: the first round, each tool round, and each in-turn compaction summary. It receives `heavenbase.llm.LLMTraceEvent` values (the callback type is `heavenbase.llm.TraceCallback`):

- `kind="request"` is emitted before gateway conversion and provider I/O. Its `payload` contains `messages`, `tools`, `options`, and `route` in HeavenBase's normalized request form. The messages reflect formatting and any compaction already performed. For example, an Anthropic trace keeps the system message in `messages` and records `tool_choice="required"` in `options`. The gateway moves the system instruction to the provider's top-level `system` field and sends `tool_choice={"type": "any"}`.
- `kind="response"` (`{"message", "usage", "served_model", "elapsed"}`) or `kind="error"` (`{"error"}`) follows with the same `seq`. A `request` with no terminal event was interrupted.
- `call` names the origin: `"round"` or `"turn_compaction"`.
- A cache hit emits nothing, because no provider saw a request.

```python
from heavenbase.llm import LLMTraceEvent

events: list[LLMTraceEvent] = []
llm = hb.LLM(preset="chat", cache=False)
answer = llm.chat("Summarize the release notes", trace=events.append)
print([(event.kind, event.seq) for event in events])  # [("request", 0), ("response", 0)]
```

The callback is an observer. An exception inside it is reported once per exception type as a `UserWarning` and never fails the call. With no `trace=`, tracing costs one dictionary lookup per provider call; with one, each request deep-copies its message list.

`options` keeps request-shaping parameters only. Client wiring never enters it: API keys, auth tokens, base URLs, proxies, headers, and mocks. Messages, tool schemas, and the top-level `response_format`, `tool_choice`, `functions`, and `function_call` options are recorded before gateway conversion and bypass trace redaction. A persisted trace may therefore contain sensitive business data. In every other option, at any depth including inside `extra_query` and `extra_body`, a value under a name that looks like a credential (`*key*`, `*token*`, `*secret*`, `*password*`, `*passwd*`, `*credential*`, `auth*`) is replaced by `"***"` unless it is null, a boolean, a number, or a list, tuple, or set made only of those. So `max_tokens` and `stop_token_ids` survive, while text, bytes, byte arrays, mappings (their keys are text), and mixed containers under such a name are replaced whole. OpenAI's `prompt_cache_key` is a request parameter and remains in the normalized options. A schema here is a boolean, or a mapping that is empty or whose every key is a JSON-schema keyword (draft-07 to 2020-12, such as `type`, `$ref`, `anyOf`, or `description`, plus OpenAPI's `discriminator`, `example`, `externalDocs`, `nullable`, and `xml` and Gemini's `propertyOrdering`). In a schema, a key of a `properties`, `patternProperties`, `$defs`, `definitions`, `dependentSchemas`, or `dependencies` mapping whose value is a schema, or of a `dependentRequired` or `dependencies` mapping whose value is a list of strings, is a property name, not a credential name. A mapping with any other key is not a schema, so a credential container under a key named `properties` is still redacted. So a schema sent in Anthropic's `output_config`, vLLM's `guided_json`, or anywhere in `extra_body` keeps properties such as `api_key` or `token_count`. In these other options, every URL inside a string, or inside the `str()` text of a non-JSON value (a `repr` such as `Endpoint(url='...')` included), loses its user info and secret-looking query values, whatever its scheme; a URL joined to another by punctuation or nested in another URL's path or query is redacted on its own. User info is found when it is well formed, and also when it holds a `:` before its first unescaped `/`, `?` or `#` and no `@` before that mark: it then runs to the last `@` of the URL, so `postgres://user:pa/ss@host/db` is stored as `postgres://***:***@host/db`. A `host:port` followed by an `@` anywhere later in the URL, in the path, query or fragment, or such user info followed by an `@` in the path, is redacted through that last `@` too (`https://host:8080/p?email=a@b.com` is stored as `https://***:***@b.com`). User info that holds a `/` before its `:` (`postgres://us/er:pass@host/db`) ends at the last `@` before the query and fragment whenever a `:` comes before that `@`, and so does a path that holds a `:` and then an `@` (`https://h/a:b@c` is stored as `https://***:***@c`), which cannot be told apart from it. When the URL already holds well-formed user info, that user info is redacted and its host kept, unless a `:` and then an `@` follow in the path (`postgres://us@er/x:pass@host/db` is stored as `postgres://***:***@host/db`); so an `@` in an ordinary path keeps the host (`https://user:token@registry/@scope/pkg` is stored as `https://***:***@registry/@scope/pkg`). An `@` with no `:` before it is a path or query value (`https://h/@name` is kept). Two cases are not fully found: a user name with no password that holds an unescaped `/`, `?` or `#`, and a password that holds an `@` and then a `/` with no `:` after it (`postgres://user:p@ss/word@host/db` is stored as `postgres://***:***@ss/word@host/db`), which cannot be told apart from a path `@` after well-formed user info. Redaction runs before JSON conversion: tuples and other sequences are stored as lists, sets as sorted lists, and any other non-JSON value as its `str()` text, which is stored as `"***"` when it mentions a credential-looking name. Provider error text in `error` events is stored as the provider wrote it.

`heavenbase.llm.trace.request_fingerprint(payload)` returns the SHA-256 digest of the trace payload in sorted-key JSON. Object keys are converted to their JSON strings before sorting. When multiple keys encode to the same name, the last value wins, as in a JSON round trip. Lone surrogates are escaped. Requests that yield the same canonical JSON share a fingerprint, including requests that differ only in omitted client wiring, redacted values, or equivalent JSON representations such as a tuple and a list. `forward_trace(...)` renumbers a helper call's events onto an outer trace. The dashboard assistant's opt-in session log is built on this seam (see the dashboard architecture page).

## Embeddings

Use `embed` for embeddings:

```python
llm = hb.LLM(preset="embed")

vector = llm.embed("hello")
vectors = llm.embed(["hello", "world"])
dim = llm.embed("hello", include="dim")
same_dim = llm.dim
```

Known embedding dimensions are stored in config: local-only `embeddinggemma` is `768`, `text-embedding-3-small` is `1536`, `embed-v4.0` (Cohere) is `1536`, and `voyage-4-lite` is `1024`. `llm.dim` reads the configured dimension first and falls back to one test embedding call when no `dim` is configured. `embed` deduplicates inputs before provider calls, reads and writes cache rows per unique input, splits cache misses by `embedding_batch_size`, can run split batches with bounded workers, and broadcasts cached/fresh vectors back to the original order. Global LLM defaults set `embedding_batch_size: 256` and `embedding_max_workers: 8`, so uncached work can fan out to 2048 inputs in flight while cached rows are served directly. DashScope sets `embedding_batch_size: 10` in provider `default_args`.

Local embedding servers are available through OpenAI-compatible providers such as LM Studio, Ollama, and oMLX:

```python
llm = hb.LLM(preset="embed-local", provider="lmstudio")
```

The CLI prints the full raw embedding array by default and the full response object with `--json`:

```bash
hb llm embed "hello"
hb llm embed "hello" --json
```

Use `--preview` when you only want to inspect shape and a few values. Preview output keeps the same JSON keys but replaces `embeddings`
with an abbreviated string under `--json`.

```bash
hb llm embed "hello" --preview
hb llm embed "hello" --json --preview
```

`hb llm chat` and `hb llm embed` also support `--copy` / `-cp` to copy the emitted response explicitly.

## Cache

The global LLM cache is enabled by default under `heavenbase.llm.cache`. It persists plaintext messages and embedding inputs under the package home; set `heavenbase.llm.cache.enabled: false` to turn it off for a deployment, or pass `cache=False` to one client. It stores normalized rows in normal HeavenBase backends with separate text, embedding, and image namespaces. `chat` and `stream` share text rows; embeddings are cached one input at a time; image rows use a separate image namespace.

Text cache keys include normalized messages, including assistant `tool_calls` and `tool` role messages. This makes deterministic replay safe after a tool result is already part of the conversation. Executable tools attached as callables or `Toolkit` objects intentionally bypass text-cache reads and writes for that provider step, so HeavenBase does not skip side effects or replay stale tool output. For live tool debugging, keep `cache=False`; enable cache only for deterministic replay/eval runs where tool side effects are already controlled.

Disable or enable cache for one client:

```python
llm = hb.LLM(preset="system", cache=False)
llm = hb.LLM(preset="system", cache=True)
```

Use a small local cache for temporary batch work:

```python
cache = hb.LLMCache.from_dict(
    {
        "enabled": True,
        "workspace": "tmp-batch-cache",
        "backend": {"main": {"type": "sqlite", "database": "file://%/llm/tmp-batch-cache.db"}},
        "namespaces": {"embedding": {"enabled": True, "touch_hits": False}},
    }
)
llm = hb.LLM(preset="embed", cache=cache, embedding_batch_size=256, embedding_max_workers=8)
```

Embedding execution controls are normal LLM arguments and can be set in root `default_args`, provider `default_args`, constructor kwargs, or call kwargs:

- `embedding_batch_size`: miss split size before provider requests.
- `embedding_max_workers`: maximum concurrent miss batches.
- `heavenbase.llm.cache.namespaces.embedding.touch_hits`: whether cached embedding reads update hit metadata; the default is `false` to keep large cached reads read-only.

At call time, `llm.embed(items, embedding_batch_size=..., embedding_max_workers=...)` overrides constructor and config defaults. These controls are not forwarded to gateways and are excluded from cache keys.

Embedding `include="raw"` returns a stable batching envelope with `cached`, `misses`, `embedding_batch_size`, `embedding_max_workers`, and `batches`. Each item in `batches` is the raw provider response for one miss batch.

Switch the global cache to Postgres by configuring `heavenbase.llm.cache.backend.main.type: postgres` plus the normal Postgres backend connection fields. A per-instance `cache=` or `cache_config=` still overrides the global cache for that client.

`hb.LLMCache.reset(config_manager=context.config)` retires the storage of that Context's cache service, the cache that `hb.LLMCache.current()` returns, also after the cache object was garbage-collected; `drop=True` drops that storage on every route first, and also the storage that the Context's `heavenbase.llm.cache` configuration names when nothing has opened it yet, such as in a new process or a new Context over the same root; a closed Context's rows stay on disk. A Context that a `Context.close()` or `reset_default_local_state()` has marked counts as closed for a drop: configured storage that nothing has opened yet is skipped and the reset returns normally, and a drop of open storage raises `RuntimeError`; if that close or reset gives up, the Context is open again and still serves the rows. The process-wide `hb.LLMCache.reset(drop=True)` does this for every open root Context of the process, the default Context once it exists included, whether or not it used the cache; it never builds a default Context to drop it. A drop opens configured storage only when it may exist: storage whose routes are all local files that do not exist is skipped, so a Context that never created it is not initialized, while storage on a server database is opened. A Context discarded without `close()` stays open until it is garbage-collected, so a process-wide drop still reaches it; close Contexts you no longer use. Without `drop`, the process-wide reset touches only the Contexts that hold a cache store. The next cached call opens new storage. A cache built from a configuration (`LLMCache.from_dict`, `cache_config=`, a `cache=` mapping) owns its storage, which a reset leaves alone. When a drop from any Context removes a database that a cache uses, the cache opens it again on fresh storage at its next call, and its routes on other storage keep their rows; a cache that is opening the database meanwhile waits for the drop to end first. The drop waits for hit bookkeeping the cache already started, so a hit never counts on an entry written after the drop.

Under the deterministic policy, `temperature > 0` with no effective `seed` is never cached, even with `cache_policy="force"`.

## Image Generation

Use `imagen` for image generation responses:

```python
image = hb.LLM(preset="imagen").imagen("A clean product render of a white ceramic mug")
```

Use `imagen-local` when Ollama is serving `x/z-image-turbo` locally:

```python
image = hb.LLM(preset="imagen-local").imagen("A clean product render of a white ceramic mug")
```

The built-in image models are `gpt-5-image-mini`, `gpt-5.4-image-2`, and the Ollama-only `z-image-turbo`:

```python
image = hb.LLM(model="gpt-image-2").imagen("A small product icon for HB")
```

Image responses are normalized into `LLMImage` objects when possible, while raw provider payloads remain available through `include="raw"`.

URL-backed `LLMImage` values fetch lazily only when you call `to_bytes()`, `to_b64()`, `to_data_url()`, or `save()`. The fetch timeout is configured by `heavenbase.llm.image_url_timeout`.

`imagen` accepts the same `images=` input formats as `chat` and `stream` for reference images:

```python
reference = hb.LLMImage.from_any("./style-reference.png")
image = hb.LLM(preset="imagen").imagen("Apply this style to an HB mark", images=reference)
```

## OCR

Use the `ocr-local` preset to run optical character recognition with the local Ollama `glm-ocr` model. OCR needs no separate API: it is a normal `chat` (or `stream`) call whose prompt selects the recognition task and whose `images=` carries the page.

```python
llm = hb.LLM(preset="ocr-local")

text = llm.chat("Text Recognition: ", images=["./page.png"])
```

The `glm-ocr` model recognizes content from a short task prompt. The recognition prompts are:

| Mode | Prompt |
|---|---|
| text | `Text Recognition: ` |
| table | `Table Recognition: ` |
| figure | `Figure Recognition: ` |

```python
table = llm.chat("Table Recognition: ", images=["./table.png"])
```

`images=` accepts the same inputs as regular multimodal chat: paths, URLs, data URLs, base64 strings, bytes-like objects, `LLMImage`, Pillow images, and ndarrays. Streaming produces the same recognized text:

```python
text = "".join(llm.stream("Text Recognition: ", images=["./page.png"]))
```

HeavenBase routes `ocr-local` through the OpenAI-compatible gateway against Ollama's `/v1` endpoint, so recognition runs through the standard chat pipeline rather than a model-specific path.

The matching CLI uses the repeatable `--image` option:

```bash
hb llm chat "Text Recognition: " --image ./page.png --preset ocr-local
hb llm session "Text Recognition: " --image ./page.png --preset ocr-local
```

`hb llm chat` accepts multiple `--image` values on one prompt. For `hb llm session`, the image values attach to the initial prompt and remain in the conversation history for follow-up questions.

## Mocking And Offline Tests

Use the built-in mock preset for tests and demos that should never call a network provider:

```python
hb.LLM(preset="mock").chat("hello")
# "mock response"

hb.LLM(mock="instance response").chat("hello")
hb.LLM(preset="mock").chat("hello", mock="call response")
```

`mock` accepts a string, an OpenAI-style chunk list, or a script dictionary:

```python
mock = hb.LLM(
    preset="mock",
    mock={
        "text": "done",
        "think": "plan",
        "pause": [0, 0],
        "chunk_chars": 2,
        "structured": {"ok": True},
        "embed_dim": 4,
    },
)

assert mock.chat("json", response_format={"type": "json_object"}, include="structured") == {"ok": True}
```

Advanced tests can pass Python callables through `mock_chat`, `mock_embed`, or `mock_imagen` for full control over OpenAI-format payloads.

## Custom OpenAI-Compatible Providers

Use the `custom` preset for a provider that speaks the OpenAI API but is not in the bundled model catalog:

```python
llm = hb.LLM(
    preset="custom",
    base_url="http://localhost:9999/v1",
    model="third-party-model",
    api_key="optional-key",
)
```

The custom provider intentionally requires a runtime `base_url` and a concrete `model`. Calling `hb.LLM(preset="custom")` raises a clear error instead of guessing.

## Gateways

The default gateway is `openai`. It uses the OpenAI Python SDK against the provider's OpenAI-compatible endpoint, so no Portkey, LiteLLM, Bifrost, or native Anthropic SDK process is required.

`endpoint` is a first-class normalized route dimension, separate from
`gateway`. Resolution expands the preset, selects model and provider, then
selects gateway and endpoint. Validate rejects incompatible endpoint/gateway,
provider/model, and operation combinations instead of silently rewriting the
route. The final URL comes from selected bindings plus an explicit runtime
`base_url=...` override.

Anthropic has two supported gateway surfaces. Use `gateway="openai"` for the OpenAI SDK compatibility endpoint:

```python
llm = hb.LLM(model="haiku", provider="anthropic", gateway="openai")
runtime = hb.LLMEngine().apply(llm.spec)
print(runtime["base_url"])
```

Use `gateway="anthropic"` for the official Anthropic Python SDK and the native Messages format:

```python
llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic")
runtime = hb.LLMEngine().apply(llm.spec)
print(runtime["base_url"])
```

`to_client()` and `to_aclient()` return OpenAI SDK clients only. They raise a controlled `ValueError` for `anthropic`, `litellm`, and `mock` gateways instead of returning a client that fails later at request time. Use `to_anthropic_client()` or `to_anthropic_aclient()` when the selected gateway should expose an Anthropic Messages endpoint. These methods support `gateway="anthropic"`, `gateway="portkey"`, and `gateway="bifrost"`; Portkey uses the gateway root URL, while Bifrost maps the OpenAI-compatible `/v1` URL to `/anthropic`.

Anthropic-compatible providers can use the same provider key across gateways. For example, Z.ai GLM routes through the native Messages gateway with:

```python
llm = hb.LLM(model="glm-5.2", provider="zai", gateway="anthropic")
print(llm.chat("Reply exactly: OK"))
```

The native gateway converts HeavenBase's internal OpenAI-style messages and tools to Claude Messages payloads, then normalizes Anthropic responses back to the standard `text`, `think`, `tool_calls`, `message`, `delta`, `messages`, and `usage` include fields. Native thinking controls are translated before the request; manual budget requests must be at least 1024 tokens, lower than `max_tokens`, and compatible with the selected Claude model. The `think` text is the thinking text only; block signatures are never part of it. With thinking on, the Messages API requires an assistant turn that calls tools to be sent back starting with the thinking blocks the model returned in it, unmodified. So the assistant `message` of such a turn (and the matching entries of `delta` and `messages`) also keeps each complete block in `thinking_blocks`, a list of `{"type": "thinking", "thinking": ..., "signature": ...}` and `{"type": "redacted_thinking", "data": ...}` items in response order, streamed or not. A stream delivers each block in fragments (its text, then its signature); the gathered message joins them back into the complete block, leaves out a block that never received its signature, and stream callers never see a chunk that carries only a fragment. The Anthropic Messages routes (`gateway="anthropic"`, Portkey and Bifrost) send these blocks back first in an assistant message that has `tool_calls` whenever the request has thinking on, so a tool loop with `think=True` can finish, and so can a conversation you continue from `messages`. In-turn compaction (`compact_ratio`) of such a loop puts its compacted-memory message after the two rounds it keeps instead of before them: the round that holds the turn's thinking block is gone, so the request ends on the memo and Claude starts a new assistant turn, with fresh thinking. The kept rounds are sent without their thinking blocks: an earlier turn needs none, which Anthropic documents as accepted, and removing the rounds before them changed what those blocks followed, which the history-editing check of newer Claude models (for example Opus 5.5 and Fable 5.2) refuses for a block that is sent back. HeavenBase checks the request it sends for this shape; it was not tested against a live model with that check. `gateway="litellm"` with a Claude model gets the same `thinking_blocks`, streamed or not, and LiteLLM sends them back itself. `think=False` removes `thinking_blocks` and `reasoning_details` with the other reasoning fields. Embeddings and image generation are not part of the native Anthropic Messages API; use `gateway="openai"` or `gateway="litellm"` for those routes. `ANTHROPIC_BASE_URL` can point either to the OpenAI-compatible `/v1` endpoint or to a native Anthropic-compatible base URL. For the native gateway, a trailing `/v1` is stripped before constructing the SDK client.

For Claude tool loops on Chat Completions, HeavenBase gathers and replays OpenRouter's `reasoning_details` and Bifrost's Anthropic `reasoning_details`. Older Bifrost deployments can discard `reasoning_details` when parsing the assistant request. Bifrost's OpenRouter adapter can also drop those details when converting the request to send upstream. Portkey's Anthropic adapter exposes native thinking fragments with strict OpenAI compliance disabled, HeavenBase's default for Portkey. HeavenBase joins those fragments into signed blocks and sends them back as assistant `content_blocks`, including when Claude thinks by default. For blocking responses, it replays the original block sequence, including non-thinking blocks and citations. When the app switches routes, HeavenBase selects the history fields supported by the destination and leaves the recorded messages intact. The direct Anthropic OpenAI-compatibility endpoint does not expose signed thinking history in its documented Chat Completions response. Use an Anthropic Messages route to replay Claude's thinking when that endpoint or your Bifrost deployment cannot carry it. For OpenRouter, select a route that forwards `reasoning_details` to OpenRouter. These replay shapes were tested through the SDK against local responses. [OpenRouter reasoning replay](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#preserving-reasoning-blocks) · [Portkey Anthropic adapter](https://github.com/Portkey-AI/gateway/blob/main/src/providers/anthropic/chatComplete.ts) · [Bifrost assistant history parser](https://github.com/maximhq/bifrost/blob/main/core/providers/openai/types.go#L459) · [Bifrost outbound conversion](https://github.com/maximhq/bifrost/blob/main/core/providers/openai/utils.go#L305) · [Anthropic OpenAI compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk).

Anthropic documents the compatibility base URL as `https://api.anthropic.com/v1/` and notes that the native Claude API is the better long-term surface for full features such as PDF processing, citations, extended thinking, and prompt caching: <https://docs.anthropic.com/en/api/openai-sdk>. Use `LLMEngine.apply(spec, profile="print")` for redacted route inspection before live Anthropic calls.

Endpoint and gateway remain separate even when an upstream service supports
multiple payload families:

- Portkey supports OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages as gateway formats, and its Anthropic integration supports `/messages`, token counting, Anthropic SDK usage, prompt caching, extended thinking, files, and web search: <https://portkey.ai/docs/product/ai-gateway/universal-api> and <https://portkey.ai/docs/integrations/llms/anthropic>.
- LiteLLM supports Anthropic through the `anthropic/` provider route, chat-completions style calls, and `/v1/messages` passthrough: <https://docs.litellm.ai/docs/providers/anthropic>.
- Bifrost supports Anthropic both through OpenAI-compatible chat/responses conversion to `/v1/messages` and through provider-compatible Anthropic SDK endpoints: <https://docs.getbifrost.ai/providers/supported-providers/anthropic> and <https://docs.getbifrost.ai/integrations/anthropic-sdk/overview>.

Each gateway is an ordinary `llm_gateway` module record. Its descriptor publishes
supported endpoints, a versioned compiler contract, and the transport operations it
implements. The creating `LLMEngine` reads those records through its Context-owned
resolver; a route without its required compiler or transport fails explicitly.

Use Portkey when you want gateway-level routing, observability, caching, or policy controls:

```bash
export PORTKEY_API_KEY="..."
```

```python
llm = hb.LLM(model="ds-flash", provider="deepseek", gateway="portkey")
```

Portkey is optional. You can point at a self-hosted or local gateway with `PORTKEY_BASE_URL`. For short live smoke checks, prefer `preset="chat", gateway="portkey"` unless you are intentionally comparing provider routes. Some direct providers may still emit reasoning deltas before visible text even when HeavenBase sends `think=False`; HeavenBase suppresses those deltas from normalized outputs, but you should still give those routes enough `max_tokens` for the final answer.

For GLM tool-call validation, keep the internal tool format as native OpenAI JSON. Start with `glm-flash` through OpenRouter via Portkey when you want a broadly compatible route; validate larger GLM routes intentionally before using them for live tool loops. Treat XML tool prompting as a future adapter only if repeated failures show JSON tool schema is the cause.

Other gateways:

- `anthropic`: uses the official Anthropic Python SDK and native Messages payloads.
- `litellm`: sends provider-prefixed model IDs such as `deepseek/deepseek-v4-flash-0731`.
- `bifrost`: sends provider-prefixed model IDs and uses `BIFROST_BASE_URL`, defaulting to `http://localhost:8080/v1`.
- `mock`: stays offline and uses the built-in mock adapter.

Register a gateway only when its client requires a runtime shape or transport that
the built-ins cannot express. Registration is scoped to one Context:

```python
import heavenbase as hb


def compile_runtime(spec, gateway, override):
    return {
        "gateway": gateway,
        "model": spec.model_id,
        "args": {**spec.args, **override},
    }


def chat(spec, messages, *, stream=False, **kwargs):
    return company_client.complete(messages, stream=stream, **kwargs)


context = hb.Context.load()
hb.LLMGateway(
    "company-proxy",
    compile_runtime,
    chat=chat,
).register(resolver=context.modules())

llm = hb.LLM(preset="custom", gateway="company-proxy", context=context)
```

For a distributable gateway, place the compiler and transport targets beside one
exact `meta.yaml` and install the folder with `context.modules().install(...)`.
Bundled and external folders then use the same resolver, validation, cache, and
dispatch path; no central gateway-name list is updated. Prefer provider and model
configuration when an existing gateway already produces the required runtime
arguments.

Temporary gateway limitations are raised explicitly: `gateway="portkey"` with `provider="openrouter"` for embeddings is blocked until Portkey Gateway support lands ([Portkey PR 1622](https://github.com/Portkey-AI/gateway/pull/1622)), and Bifrost image generation is blocked while upstream image support is unresolved ([Bifrost issue 2367](https://github.com/maximhq/bifrost/issues/2367)).

If a selected gateway or optional dependency is unavailable, resolution or
Apply fails; HeavenBase does not fall back to the `openai` gateway.

## Spec Keys And Client Reuse

Every resolved `LLMSpec` can produce deterministic hash keys:

```python
llm = hb.LLM(model="ds-flash", provider="deepseek")

spec_key = llm.spec.hash_key()
client_key = llm.spec.client_key()
```

`hash_key()` includes canonical semantic config, selected route bindings, and
applied runtime values. Changing gateway requires re-resolving the semantic
config; it cannot be selected as a hash-key argument.

`client_key()` includes only the fields that affect client construction: gateway, API key or bearer auth token, base URL, headers, timeout, and retries. The SDK adapters for `openai`, `portkey`, `bifrost`, and `anthropic` keep an in-memory cache keyed by this value, so duplicated `LLM` instances and repeated calls reuse the SDK client. Sync clients are shared by every thread. An async client binds its connection pool to the event loop it runs on, so async clients are cached per running event loop: each `asyncio.run(...)` call and each thread running its own loop gets its own client, and each loop has its own least-recently-used cache, so one loop never evicts another loop's clients. The cache is stored on the loop object, so its clients live as long as the loop: a loop dropped without being closed releases its clients when the loop object is garbage-collected; an event loop object that does not accept attributes gets a new client for every request. If the cache is active when the loop begins shutting down its async generators, the shutdown hook closes cached clients on their loop after generator cleanup. A generator's `finally` block can still call the model during cleanup. Clients first requested after the cache's shutdown hook finishes keep zero idle connections. A loop closed without that step has its clients dropped the next time any loop requests its first client, or when the closed loop is garbage-collected, and their sockets are released by garbage collection. These clients use the SDK's `DefaultAsyncHttpxClient`, so a dropped client is never closed on a different loop. `to_aclient()`, `to_anthropic_aclient()` and `LLMGateway.client(spec, protocol, asynchronous=True)` called inside a running loop return that loop's cached client; called outside any running loop they return a new client that the caller owns and closes. LiteLLM does not create an SDK client object in this layer, but the module import and runtime key path are compatible with the same resolution model.

Generator cleanup can use cached clients when a task factory wraps its coroutines. On asyncio and uvloop, the first SDK client requested during the loop's async generator shutdown keeps zero idle connections, including requests from exception handlers in generator cleanup. An explicit `aclose()` during normal loop operation preserves connection reuse. The shutdown hook also waits for generator cleanup tasks already scheduled through the loop while its SDK client cache is active. If the loop prevents HeavenBase from wrapping task creation or shutdown, cached SDK clients use zero idle connections.

## Providers And Environment Variables

The curated built-in model catalog uses OpenRouter first, then official or local OpenAI-compatible providers where useful.

| Provider | API key environment | Base URL environment |
|---|---|---|
| OpenRouter | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` |
| OpenAI | `OPENAI_API_KEY` | `OPENAI_BASE_URL` |
| Anthropic | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` |
| xAI / Grok | `XAI_API_KEY` | `XAI_BASE_URL` |
| Google Gemini | `GEMINI_API_KEY` | `GEMINI_BASE_URL` |
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` |
| Moonshot | `MOONSHOT_API_KEY` | `MOONSHOT_BASE_URL` |
| Z.ai | `ZAI_API_KEY` | `ZAI_BASE_URL`, `ZAI_ANTHROPIC_BASE_URL` |
| Minimax | `MINIMAX_API_KEY` | `MINIMAX_BASE_URL` |
| Cohere | `COHERE_API_KEY` | `COHERE_BASE_URL`, `COHERE_LITELLM_BASE_URL` |
| Voyage | `VOYAGE_API_KEY` | `VOYAGE_BASE_URL` |
| DashScope | `DASHSCOPE_API_KEY` | `DASHSCOPE_BASE_URL` |
| Ollama | `OLLAMA_API_KEY` | `OLLAMA_BASE_URL` |
| LM Studio | `LM_STUDIO_API_KEY` | `LM_STUDIO_BASE_URL` |
| oMLX | `OMLX_API_KEY` | `OMLX_BASE_URL` |
| vLLM | `VLLM_API_KEY` | `VLLM_BASE_URL` |
| Mock | none | none |
| Custom | pass `api_key=...` if needed | pass `base_url=...` |

All bundled defaults live in `src/heavenbase/resources/configs/default.yaml`. The LLM section is ordered as `presets`, `models`, `providers`, then `gateways`, so a reader can start with friendly presets and drill down only when needed.

## Inspecting Resolved Runtime Config

Use `spec` when you need to see how an `LLM` resolved:

```python
llm = hb.LLM(model="ds-flash", provider="deepseek")

print(llm.spec.to_dict())
print(hb.LLMEngine().apply(llm.spec, profile="print"))
```

`spec.to_dict()` is an executable persistence envelope and may contain secrets
in `bindings`; it is not a logging API. The `print` profile redacts API keys,
auth tokens, headers, and URL credentials while preserving useful route data.

## Proxies, Timeouts, And Retries

Proxy values can be supplied as regular arguments:

```python
llm = hb.LLM(
    model="ds-flash",
    http_proxy="http://127.0.0.1:7890",
    https_proxy="http://127.0.0.1:7890",
    no_proxy="localhost,127.0.0.1",
)
```

A `base_url` on this machine (`localhost`, a `*.localhost` name, or a loopback
address such as `127.0.0.1` or `::1`) is reached directly on the `openai`, `anthropic`,
`portkey` and `bifrost` gateways (not `litellm`), ignoring the environment's proxy variables and the system
proxy (the Windows registry, macOS System Configuration), unless `http_proxy` or
`https_proxy` is set on the LLM, passed to the call, or set by a `NetworkProxy` block
that is open on the calling thread or task. The proxy options of a stream count as such
a block until the stream ends; after an async stream is closed early (`aclose()` or a
`break`), they can still count for the next calls of that task, until the event loop has
run the stream's finalizer. A block does not reach the worker threads on which `embed`
sends its requests: pass the proxy to `embed` itself. The SDKs read the system proxy but not
its bypass list, so a local model server was otherwise sent through the system proxy.

A client built for such a proxy is kept for requests that name the same proxy settings.
The SDK reads the proxy from the process environment while the client is built, and the
newest open block of any thread or task sets it there. Calls or blocks with different
proxies that overlap can therefore build a client with another one's proxy, and later
requests with the same settings reuse it. This was also in 0.1.2.2, where such a client
also served requests that name no proxy; those are now always sent directly.

Local live checks can use standard proxy exports from `~/.bashrc` (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, plus lowercase variants) together with provider API keys and base URL overrides. Use those exports when VPN/TUN mode changes GLM, Anthropic, or OpenRouter reachability.

`timeout` and `max_retries` are passed to the OpenAI client. Request-only arguments such as `temperature`, `seed`, `top_p`, `max_tokens`, and provider-specific options are sent in the payload after client-only arguments are filtered out.

## Runnable Demo

LLM gateway checks should live in targeted tests or docs examples. Set provider API keys only for explicit `llm`-marked verification.
