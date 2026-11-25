# HeavenBase

> 翻译状态：本页尚未完整同步 `0.1.2.1` 的架构说明；当前 API 与工程事实以 [`README.en.md`](README.en.md) 为准。

HeavenBase 是面向智能体的多后端数据引擎，用于结构化数据管理。
你只需要定义一套逻辑模型，连接已经在使用的存储系统，再把一个统一的 workspace API 暴露给应用和智能体。

它不是数据库替代品。它是一个让智能体理解数据的层：有哪些数据、数据是什么意思、数据在哪里、如何查询，以及可以通过哪些工具安全使用。

```python
import heavenbase as hb
```

## 发布状态

> [!NOTE]
> **归档说明：** HeavenBase (`0.1.2.3`) 是 [AgentHeaven](https://github.com/RubikSQL/AgentHeaven) 的继任项目。当前 Python 版本仓库已过时且不再维护，现以最终版本 `0.1.2.3` 归档。

## 为什么需要 HeavenBase

智能体出错，很多时候不是因为不会推理，而是因为看不见完整的数据环境：

- 不知道某个数据孤岛存在
- 拿到的是过期 schema 或数据源地图
- 没有掌握领域词汇
- 只取回了方便的 top-k 切片
- 不能把数据、手册、prompt、工具和记忆联系起来

HeavenBase 的做法是把数据、元数据、领域知识、prompt、工具和智能体记忆放进同一个 typed workspace。智能体可以通过 `Catalog` 行发现具体对象，通过 `MetaSchema` 行理解结构，并用同一个 API 查询 typed entity。

## 当前包含什么

- `HeavenBase` workspace：管理 schema、后端、路由、数据、Catalog、MetaSchema、audit、repair 和 MCP 暴露。
- `Entity` 类：支持 `hb.field(...)`、逻辑类型、默认值、compute hook、query-compute hook、JSON schema 编译和稳定的 `object_id` 身份。
- Python 表达式和 Mongo 风格 JSON filter 两种查询表面，都会降到 `QuerySpec`。
- 字段级存储放置，策略包括 `InlineColumn`、`JsonField`、`SideTable`、`VectorIndex`、`InvertedIndex`、`GraphEdge` 和 `ExternalRef`。
- 内置后端族：内存、文件 JSON/pickle、SQL、向量存储和搜索。
- 基于 handler 的查询编译，并通过 `Query.explain()` 诊断后端、策略、handler mode、fallback 和 unsupported reason。
- `Catalog` 用于对象发现，`MetaSchema` 用于 workspace 结构发现。
- Workspace MCP profiles：agent、full/admin，以及由 memory 扩展拥有的 memory / memstate 项目记忆工具。
- `hb.LLM` 和 `hb llm`：支持配置化 chat、embedding、图像生成、session、MCP 工具循环、缓存和 provider/gateway 路由。
- `CM_HVNB`、`ConfigEngine`、Prompt 工具、SQL 辅助工具、互操作辅助工具，以及一个小型公共 capability index。

## 安装

包发布后：

```bash
pip install heavenbase
hb setup
```

源码开发推荐使用 `uv`：

```bash
git clone https://github.com/Magolor/HeavenBase-legacy.git
cd HeavenBase-legacy
uv sync --extra dev
uv run hb setup
```

项目通过 `.python-version` 固定本地 `uv` 默认解释器，同时包本身仍支持 Python 3.10 到 3.13。

需要更多 provider 时可以安装 optional extras：

```bash
pip install "heavenbase[full]"
uv sync --extra full
```

## 第一个 Workspace

第一次使用请从 `debug` preset 开始。它不需要 Docker 服务，会创建本地 SQLite 以及内存向量/搜索后端。

```python
import heavenbase as hb


class Product(hb.Entity):
    name = hb.field(hb.ShortText).desc("Display name")
    body = hb.field(hb.LongText).desc("Searchable description")
    price = hb.field(hb.Float).desc("List price")
    tags = hb.field(hb.Array[hb.ShortText]).default([])


ws = hb.HeavenBase("shop", preset="debug")
ws.register(Product)

desk_id = ws.upsert(
    Product,
    {
        "name": "Oak desk",
        "body": "Writing desk with cable tray",
        "price": 129.0,
        "tags": ["office", "furniture"],
    },
)

rows = (
    ws.query(Product)
    .where(Product.price < 150)
    .where(Product.tags.array_contains("office"))
    .select("name", "price")
    .execute()
    .rows()
)

print(desk_id)
print(rows)
```

`register()` 只建立 Entity 类及其存储计划，不会写入 Entity 实例。`upsert`、`set`、
`delete` 等行操作要求该类已经存在于 Workspace 中，也不会把行写入变成隐式的
schema 注册。

每一行都有且只有一个 `object_id`。如果省略它并提供 `name`，HeavenBase 会根据实体 schema 和行名生成确定性的 ID。
Query 是不可变的规格值：每次变换都会返回新 Query，每次 `execute()` 都会重新计算。
`ResultFrame` 只是结果传输容器；应用代码应通过 `rows()`、`scalar()`、`to_pandas()`、
`to_pyarrow()` 等已知格式显式导出，不应直接打印或依赖隐式转换。

## 查询和 Explain

Python 表达式和 JSON filter 共用同一个 planner：

```python
cheap_count = ws.query(Product).where(Product.price < 100).count().execute().scalar()

json_rows = ws.query_json(
    Product,
    {
        "filter": {"body": {"$match": "desk"}},
        "select": ["name", "price"],
        "limit": 5,
    },
).execute().rows()

plan = ws.query(Product).where(Product.body.match("desk")).explain()

print(cheap_count)
print(json_rows)
print(plan["steps"][0]["handler_mode"])
```

结果投影会保留 `object_id`，这样智能体可以展示紧凑行，同时后续仍能用稳定身份调用 `get`、`set`、`delete`、`query` 或 `explain`。

## 智能体发现

HeavenBase 会自动写入系统行：

```python
catalog_rows = ws.query(hb.Catalog).select("target_entity", "target_id", "name").execute().rows()
schema_rows = (
    ws.query(hb.MetaSchema)
    .where(hb.MetaSchema.kind == "field")
    .where(hb.MetaSchema.subject_id == Product.schema().entity_id)
    .select("field", "dtype", "desc")
    .execute()
    .rows()
)

print(catalog_rows)
print(schema_rows)
```

用 `Catalog` 查找具体对象。用 `MetaSchema` 理解实体、字段、存储放置、后端、能力和扩展。

## Workspace MCP

任何 workspace 都可以变成 MCP toolkit：

```python
print(ws.to_mcp_json(name="shop-mcp", profile="agent", transport="http", host="127.0.0.1", port=7001))
ws.serve(name="shop-mcp", profile="agent", transport="http", host="127.0.0.1", port=7001)
```

`agent` profile 暴露一个紧凑的 schema/data 表面：`define_entity`、`list_entities`、`describe_entity`、`upsert`、`get`、`set`、`count`、`query` 和 `explain`。
可信管理流程使用 `profile="full"`。启用可选 `memory` 扩展后，用 `ws.memory.to_mcp(profile="memory")` 做笔记式记忆，用 `ws.memory.to_mcp(profile="memstate")` 做版本化项目记忆。

## CLI

`hb` 命令是同一套 API 之上的薄层：

```bash
hb --help
hb setup
hb ws list
hb ws presets show debug
hb config get heavenbase.workspace.default_preset
hb cfg set heavenbase.llm.default_preset mock
hb llm chat --preset mock --provider mock --gateway mock --no-stream "hello"
hb llm embed "semantic text" --preview
hb prompt create demo.hello --template "Hello, {name}" --tr-key name
hb prompt render demo.hello --args '{"name":"Ada"}'
```

`hb setup` 会初始化全局 HeavenBase 配置和默认 workspace。运行时状态位于 HeavenBase 配置根目录下，不会创建新的项目本地文件夹。

## 后端和 Presets

先从 presets 开始：

| Preset | 后端 | 使用场景 |
| --- | --- | --- |
| `debug` | SQLite `main`、内存 `vec`、内存 `search` | 不需要 Docker 的 demo、测试和首次开发。 |
| `local-lts` | Postgres `main`、LanceDB `vec`、Elasticsearch `search` | 使用仓库服务栈的持久化本地开发。 |
| `web-lts` | Postgres 兼容 `main`、pgvector `vec`、托管搜索 | 托管或共享部署。 |

当部署放置很重要时，使用显式后端映射：

```python
ws = hb.HeavenBase(
    "custom-shop",
    backends={
        "main": {"type": "sqlite", "database": ":memory:"},
        "vec": {"type": "inmem"},
    },
)
```

内置后端族包括 `inmem`、`json`、`pickle`、`sqlite`、`duckdb`、`postgres`、`pgvector`、`mysql`、`oceanbase`、`mssql`、`oracle`、`trino`、`starrocks`、`lance`、`chroma`、`milvus`、`pinecone` 和 `elasticsearch`。可选 provider 需要对应的 Python driver 和可访问的服务。

在代码中检查支持情况：

```python
hb.capabilities.backends(hb.Vector, op="near")
ws.capabilities.ops(hb.ShortText, hb.InlineColumn, backend="main")
```

## LLM 工具

`hb.LLM` 会从 `CM_HVNB` 解析 preset、model alias、provider、gateway、请求默认值和缓存设置。

```python
import heavenbase as hb

llm = hb.LLM(preset="mock")
print(llm.chat("Reply with hb-ok"))
```

默认在线 provider 是通过 OpenAI 兼容 gateway 访问的 OpenRouter。你可以通过 `OPENROUTER_API_KEY` 等环境变量配置凭据，也可以在测试和 demo 中切换到 local/mock provider。

## 开发

使用仓库 wrapper：

```bash
uv sync --extra dev
bash scripts/test.bash tests/config/test_config_spec.py::test_config_spec_is_recursively_immutable_and_behaviorally_equal -q
bash scripts/flake.bash -a
```

连续 benchmark suite：

```bash
bash scripts/benchmark.bash
```

外部数据库测试在服务不可用时会跳过，或使用 Docker stack。直接配置本地服务时运行：

```bash
bash ./scripts/docker-restart.bash /d/databases/
```

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `src/heavenbase/` | 运行时包。 |
| `src/heavenbase/context/`、`modules/` | 机器级组合、Registry 适配与模块生命周期。 |
| `src/heavenbase/workspace/` | Workspace facade，以及按 setup、schema、rows、query、graph、catalog、metaschema 分组的服务。 |
| `src/heavenbase/entity/` | Entity DSL、field specs 与 JSON compiler。 |
| `src/heavenbase/query/` | Query expressions、JSON query parsing、query specs。 |
| `src/heavenbase/backends/` | 内置后端实现和后端注册表。 |
| `src/heavenbase/execution/` | 中立执行契约、ResultFrame 与通用 handler；provider 专用 handler 留在对应 backend 下。 |
| `src/heavenbase/placement/` | 逻辑存储规则、profile 与解析后的 placement plan。 |
| `src/heavenbase/capabilities/` | 跨模块的只读能力检查。 |
| `src/heavenbase/strategies/` | 存储策略标记。 |
| `src/heavenbase/database/` | 底层 SQL 配置、方言与数据库执行。 |
| `src/heavenbase/llm/` | LLM 配置、gateway module、响应、缓存与会话。 |
| `src/heavenbase/extensions/system/` | System entities、Toolkit 和 MCP 表面。 |
| `src/heavenbase/utils/` | 通用 Registry、配置、序列化、路径、哈希、日志和运行时辅助工具。 |
| `docs/` | 当前目标、进度、架构参考、运维参考和生成报告。 |
| `tests/` | Core、backend、interop、LLM、MCP、CLI 和线程安全覆盖。 |

## 边界

- capability 注册不等于 provider 原生下推保证。请用 `Query.explain()` 查看 `handler_mode`、`near_filter_mode` 和 fallback reason。
- 多后端写入由 workspace 协调，但不是分布式事务系统。请保持跨后端不变量简单。
- 文件后端是本地开发工具。pickle 存储只应视为可信本地使用。
- Workspace 持久化由后端驱动；manifest 导入/导出只描述构造、schema 与 extension，不包含行数据。

## 文档

- 包内文档：`docs/`
- Workspace presets：`docs/resources/reference/workspace-presets.md`
- 配置：`docs/resources/reference/config-spec.md`
- 身份规则：`docs/resources/reference/id-semantics.md`
- MCP：`docs/resources/reference/mcp.md`
- LLM：`docs/resources/reference/llm.md`
- 能力矩阵：`docs/resources/reports/capabilities.md`
