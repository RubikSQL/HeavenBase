import pytest

import heavenbase as hb
from heavenbase.entity.registry import load_entity_artifact_export


def _toolkit_export(module: str, name: str):
    """Resolve one implementation from the installed Toolkit artifact."""
    return load_entity_artifact_export("sys-toolkit", module, name)


def run_for_anthropic(*args, **kwargs):
    """Call the Registry-selected synchronous Anthropic adapter."""
    return _toolkit_export("anthropic", "run_for_anthropic")(*args, **kwargs)


async def arun_for_anthropic(*args, **kwargs):
    """Call the Registry-selected asynchronous Anthropic adapter."""
    return await _toolkit_export("anthropic", "arun_for_anthropic")(*args, **kwargs)


class CustomResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def __str__(self) -> str:
        return f"custom:{self.value}"


def add(left: int, right: int) -> int:
    return left + right


def custom_result(value: int) -> CustomResult:
    return CustomResult(value)


def custom_result_untyped(value: int):
    return CustomResult(value)


def result_dict(value: int) -> dict:
    return {"value": value}


async def async_result(value: int) -> dict:
    return {"value": value}


def custom_value_text(result: CustomResult) -> str:
    return f"value={result.value}"


def exec_sql() -> list[dict[str, object]]:
    return [{"name": "Ada", "score": 10}, {"name": "Lin", "score": 9}]


def rows_to_markdown(rows: list[dict[str, object]]) -> str:
    lines = ["| name | score |", "| --- | --- |"]
    lines.extend(f"| {row['name']} | {row['score']} |" for row in rows)
    return "\n".join(lines)


def external_namespace_transform(*, prefix: str):
    """Build an external test transform through the Registry contract."""
    from fastmcp.server.transforms import Namespace

    return Namespace(f"external-{prefix}")


def test_anthropic_export_includes_allowed_callers_without_sdk():
    toolkit = hb.Toolkit("math", {"add": add}, namespace="demo")

    tools = toolkit.to_anthropic_tools(allowed_callers=["code_execution"])

    assert tools[0]["name"] == "add"
    assert tools[0]["allowed_callers"] == ["code_execution"]
    assert tools[0]["input_schema"]["required"] == ["left", "right"]


def test_agent_run_serializes_without_changing_native_tool_result():
    toolkit = hb.Toolkit("objects", {"custom": custom_result, "result_dict": result_dict}, namespace="demo")

    native = toolkit.run("custom", value=2)

    assert isinstance(native, CustomResult)
    assert str(native) == "custom:2"
    assert toolkit.run_to_str("custom", value=2) == "custom:2"
    assert run_for_anthropic(toolkit.tools["custom"], {"value": 2}) == "custom:2"
    assert toolkit.run_to_str("result_dict", value=2) == '{"value": 2}'

    toolkit.set_serializer("custom", custom_value_text)

    assert toolkit.run_to_str("custom", value=2) == "value=2"
    assert run_for_anthropic(toolkit.tools["custom"], {"value": 2}) == "value=2"


def test_tool_serializer_preserves_raw_table_and_exposes_markdown():
    toolkit = hb.Toolkit("tables", {"exec_sql": exec_sql}, namespace="demo")
    raw = toolkit.run("exec_sql")

    assert raw == [{"name": "Ada", "score": 10}, {"name": "Lin", "score": 9}]
    expected_json = '[{"name": "Ada", "score": 10}, {"name": "Lin", "score": 9}]'
    assert toolkit.run_to_str("exec_sql") == expected_json

    toolkit.set_serializer("exec_sql", rows_to_markdown)

    expected = "| name | score |\n| --- | --- |\n| Ada | 10 |\n| Lin | 9 |"
    assert toolkit.run("exec_sql") == raw
    assert toolkit.to_str("exec_sql", raw) == expected
    assert toolkit.run_to_str("exec_sql") == expected
    assert run_for_anthropic(toolkit.tools["exec_sql"], {}) == expected


def test_anthropic_capability_is_checked_separately_from_mcp():
    toolkit = hb.Toolkit("math", {"add": add}, namespace="demo")
    tool = toolkit.tools["add"]
    tool.capsule.manifest.capabilities["mcp_tool"] = {"ok": False, "reasons": ["mcp-only block"]}
    tool.capsule.manifest.capabilities["anthropic_programmatic"] = {"ok": True, "reasons": []}

    assert toolkit.to_anthropic_tools()[0]["name"] == "add"

    tool.capsule.manifest.capabilities["anthropic_programmatic"] = {"ok": False, "reasons": ["anthropic block"]}
    with pytest.raises(_toolkit_export("errors", "ToolExportError"), match="anthropic block"):
        toolkit.to_anthropic_tools()


@pytest.mark.anyio
async def test_anthropic_async_adapter_returns_string():
    tool = hb.Toolkit("async-tools", {"async_result": async_result}, namespace="demo").tools["async_result"]

    assert await arun_for_anthropic(tool, {"value": 2}) == '{"value": 2}'
    with pytest.raises(TypeError, match="arun_for_anthropic") as err:
        run_for_anthropic(tool, {"value": 2})
    assert isinstance(err.value.__cause__, _toolkit_export("errors", "AsyncToolRequired"))


@pytest.mark.anyio
async def test_fastmcp_keeps_serializer_content_for_opaque_native_result():
    Client = pytest.importorskip("fastmcp").Client
    toolkit = hb.Toolkit("mcp-objects", {"custom": custom_result_untyped}, namespace="demo")
    toolkit.set_serializer("custom", custom_value_text)

    async with Client(toolkit.to_fastmcp()) as client:
        result = await client.call_tool("custom", {"value": 2})

    assert result.structured_content is None
    assert result.data is None
    assert result.content[0].text == "value=2"
    assert isinstance(toolkit.run("custom", value=2), CustomResult)


@pytest.mark.fast
@pytest.mark.anyio
async def test_fastmcp_preserves_explicit_tool_input_schema():
    fastmcp = pytest.importorskip("fastmcp")
    Client = fastmcp.Client
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "oneOf": [
                        {"required": ["family"]},
                        {"required": ["template_id"]},
                    ],
                    "properties": {
                        "family": {"type": "string"},
                        "template_id": {"type": "string"},
                    },
                },
            }
        },
        "required": ["items"],
    }

    def count_items(items: list[dict[str, object]]) -> int:
        """Return the number of supplied items."""

        return len(items)

    toolkit = hb.Toolkit(
        "mcp-schema",
        [
            hb.Tool(
                count_items,
                name="count_items",
                input_schema=input_schema,
            )
        ],
        namespace="demo",
    )

    async with Client(toolkit.to_fastmcp()) as client:
        tools = await client.list_tools()
        result = await client.call_tool("count_items", {"items": [{"family": "idea"}]})

    assert tools[0].input_schema == input_schema
    assert tools[0].output_schema["properties"]["result"] == {"type": "integer"}
    assert tools[0].output_schema["x-fastmcp-wrap-result"] is True
    assert result.structured_content == {"result": 1}
    assert result.data == 1
    assert result.content[0].text == "1"


@pytest.mark.anyio
async def test_fastmcp_uses_tool_serializer_for_table_output():
    Client = pytest.importorskip("fastmcp").Client
    toolkit = hb.Toolkit("mcp-tables", {"exec_sql": exec_sql}, namespace="demo")
    toolkit.set_serializer("exec_sql", rows_to_markdown)

    async with Client(toolkit.to_fastmcp()) as client:
        result = await client.call_tool("exec_sql", {})

    expected = "| name | score |\n| --- | --- |\n| Ada | 10 |\n| Lin | 9 |"
    assert result.structured_content == {"result": exec_sql()}
    assert result.data == exec_sql()
    assert result.content[0].text == expected


@pytest.mark.fast
@pytest.mark.anyio
async def test_fastmcp_cache_retains_native_results_and_serializes_each_hit():
    Client = pytest.importorskip("fastmcp").Client
    calls: list[int] = []
    serializations: list[int] = []

    def cached_value(value: int) -> dict[str, int]:
        calls.append(value)
        return {"value": value, "execution": len(calls)}

    def cache_text(result: dict[str, int]) -> str:
        serializations.append(result["execution"])
        return f"execution={result['execution']}"

    with hb.CM_HVNB.scoped("mcp-native-cache-test"):
        hb.CM_HVNB.set(
            "heavenbase.mcp.cache",
            {
                "enabled": True,
                "tools": ["cached_value"],
                "ttl": None,
                "max_entries": 8,
            },
        )
        toolkit = hb.Toolkit("mcp-cache", {"cached_value": cached_value}, namespace="demo")
        toolkit.set_serializer("cached_value", cache_text)
        server = toolkit.to_fastmcp()

        async with Client(server) as client:
            first = await client.call_tool("cached_value", {"value": 2})
            second = await client.call_tool("cached_value", {"value": 2})

        calls_after_first_lifespan = list(calls)
        serializations_after_first_lifespan = list(serializations)
        async with Client(server) as client:
            reopened = await client.call_tool("cached_value", {"value": 2})

    assert first.data == second.data == {"value": 2, "execution": 1}
    assert first.content[0].text == second.content[0].text == "execution=1"
    assert calls_after_first_lifespan == [2]
    assert serializations_after_first_lifespan == [1, 1]

    assert reopened.data == {"value": 2, "execution": 2}
    assert calls == [2, 2]
    assert serializations == [1, 1, 2]


@pytest.mark.fast
@pytest.mark.anyio
async def test_fastmcp_cache_requires_master_switch_and_explicit_tool_selection():
    Client = pytest.importorskip("fastmcp").Client
    calls: list[int] = []

    def counted(value: int) -> int:
        calls.append(value)
        return len(calls)

    with hb.CM_HVNB.scoped("mcp-cache-opt-in-test"):
        hb.CM_HVNB.set(
            "heavenbase.mcp.cache",
            {
                "enabled": True,
                "tools": [],
                "ttl": 300,
                "max_entries": 8,
            },
        )
        toolkit = hb.Toolkit("mcp-cache-opt-in", {"counted": counted}, namespace="demo")

        async with Client(toolkit.to_fastmcp()) as client:
            first = await client.call_tool("counted", {"value": 2})
            second = await client.call_tool("counted", {"value": 2})

    assert first.data == 1
    assert second.data == 2
    assert calls == [2, 2]


@pytest.mark.fast
@pytest.mark.anyio
async def test_fastmcp_cache_evicts_lru_entries_at_the_configured_bound():
    Client = pytest.importorskip("fastmcp").Client
    calls: list[int] = []

    def counted(value: int) -> dict[str, int]:
        calls.append(value)
        return {"value": value, "execution": len(calls)}

    with hb.CM_HVNB.scoped("mcp-cache-bound-test"):
        hb.CM_HVNB.set(
            "heavenbase.mcp.cache",
            {
                "enabled": True,
                "tools": ["counted"],
                "ttl": None,
                "max_entries": 1,
            },
        )
        toolkit = hb.Toolkit("mcp-cache-bound", {"counted": counted}, namespace="demo")

        async with Client(toolkit.to_fastmcp()) as client:
            first = await client.call_tool("counted", {"value": 1})
            second = await client.call_tool("counted", {"value": 2})
            evicted = await client.call_tool("counted", {"value": 1})

    assert first.data == {"value": 1, "execution": 1}
    assert second.data == {"value": 2, "execution": 2}
    assert evicted.data == {"value": 1, "execution": 3}
    assert calls == [1, 2, 1]


@pytest.mark.fast
def test_fastmcp_cache_rejects_invalid_bounds():
    with hb.CM_HVNB.scoped("mcp-cache-invalid-test"):
        hb.CM_HVNB.set(
            "heavenbase.mcp.cache",
            {
                "enabled": True,
                "tools": ["*"],
                "ttl": 0,
                "max_entries": 0,
            },
        )
        toolkit = hb.Toolkit("mcp-cache-invalid", {"add": add}, namespace="demo")

        with pytest.raises(ValueError, match="cache.ttl"):
            toolkit.to_fastmcp()

        hb.CM_HVNB.set(
            "heavenbase.mcp.cache",
            {
                "enabled": True,
                "tools": ["*"],
                "ttl": 1,
                "max_entries": 0,
            },
        )
        with pytest.raises(ValueError, match="cache.max_entries"):
            toolkit.to_fastmcp()


@pytest.mark.fast
def test_fastmcp_cache_expires_native_results(monkeypatch):
    from heavenbase.toolkit import cache as cache_module

    now = [10.0]
    monkeypatch.setattr(cache_module, "monotonic", lambda: now[0])
    cache = cache_module.MCPToolCache(
        cache_module.MCPToolCacheConfig(
            enabled=True,
            tools=frozenset({"value"}),
            ttl=2.0,
            max_entries=8,
        )
    )
    cache.open()
    cache.set("value", {"key": 1}, {"result": 2})

    assert cache.get("value", {"key": 1}) == (True, {"result": 2})

    now[0] = 12.0

    assert cache.get("value", {"key": 1}) == (False, None)
    assert len(cache) == 0


@pytest.mark.fast
@pytest.mark.anyio
async def test_fastmcp_transforms_can_come_from_heavenbase_config():
    fastmcp = pytest.importorskip("fastmcp")
    with hb.CM_HVNB.scoped("mcp-transform-test"):
        hb.CM_HVNB.set(
            "heavenbase.mcp.transforms",
            [{"name": "namespace", "args": {"prefix": "math"}}],
        )
        toolkit = hb.Toolkit("mcp-transform", {"add": add}, namespace="demo")

        async with fastmcp.Client(toolkit.to_fastmcp()) as client:
            tools = await client.list_tools()
            result = await client.call_tool("math_add", {"left": 2, "right": 3})

    assert [tool.name for tool in tools] == ["math_add"]
    assert result.data == 5


@pytest.mark.fast
@pytest.mark.anyio
async def test_external_mcp_transform_uses_the_same_registry_builder_contract():
    fastmcp = pytest.importorskip("fastmcp")
    service = hb.DEFAULT_CONTEXT.modules()
    receipt = service.register(
        "mcp_transform",
        "external-namespace",
        {
            "name": "External MCP namespace transform",
            "desc": "Test transform registered outside the Toolkit module.",
            "source": "python-import",
            "target": f"{external_namespace_transform.__module__}:{external_namespace_transform.__qualname__}",
            "active": True,
            "meta": {"schema_version": 1, "version": hb.__version__, "tags": {"mcp": True}},
        },
        coordinate="tests/mcp-transform/external-namespace",
    )
    try:
        with hb.CM_HVNB.scoped("mcp-external-transform-test"):
            hb.CM_HVNB.set(
                "heavenbase.mcp.transforms",
                [{"name": "external-namespace", "args": {"prefix": "math"}}],
            )
            toolkit = hb.Toolkit("mcp-external-transform", {"add": add}, namespace="demo")

            async with fastmcp.Client(toolkit.to_fastmcp()) as client:
                tools = await client.list_tools()
                result = await client.call_tool("external-math_add", {"left": 2, "right": 3})

        assert [tool.name for tool in tools] == ["external-math_add"]
        assert result.data == 5
    finally:
        service.uninstall(receipt)
