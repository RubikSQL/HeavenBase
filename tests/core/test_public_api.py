import ast
from concurrent.futures import ThreadPoolExecutor
import importlib
from pathlib import Path
import sys

import pytest

import heavenbase as hb
import heavenbase.capsule as capsule
import heavenbase.entity as entity_api
import heavenbase.ext as ext
from heavenbase.utils import cmd, load_txt, loads_json, validate_json
from heavenbase.utils.files import pj

ROOT_EXPORTS = [
    "__version__",
    "configure_explicit_context",
    "Array",
    "Artifact",
    "Aggregate",
    "Catalog",
    "capabilities",
    "backends",
    "BackendType",
    "Boolean",
    "Categorical",
    "Date",
    "Duration",
    "Entity",
    "ExternalRef",
    "Float",
    "GraphEdge",
    "GraphDeleteRestrictedError",
    "HeavenBase",
    "HyperG",
    "Identifier",
    "InlineColumn",
    "Integer",
    "index",
    "InvertedIndex",
    "Json",
    "JsonField",
    "LogicalType",
    "LongText",
    "MediumText",
    "MetaSchema",
    "MemoryNote",
    "Message",
    "Prompt",
    "Query",
    "QuerySpec",
    "Registry",
    "RegistryConflictError",
    "RegistryIntegrityError",
    "ResultFrame",
    "ShortText",
    "SideTable",
    "SparseGramIndex",
    "Session",
    "Skill",
    "StorageStrategy",
    "Timestamp",
    "Translation",
    "Vector",
    "VectorIndex",
    "unique",
    "WorkspaceManifest",
    "WorkspaceSkill",
    "field",
    "filters",
    "CM_HVNB",
    "ConfigEngine",
    "ConfigManager",
    "ConfigSnapshot",
    "ConfigSpec",
    "Context",
    "InterpolationPolicy",
    "resolve_interpolations",
    "Database",
    "DBEngine",
    "DBSpec",
    "DB_ENGINE",
    "DatabaseEngineRegistry",
    "SQLResponse",
    "StableRNG",
    "dumps_json",
    "loads_json",
    "md_section",
    "LLM",
    "LLMCache",
    "LLMEmbeddingEntry",
    "LLMEngine",
    "LLMGateway",
    "LLMImage",
    "LLMImageEntry",
    "LLMResponse",
    "LLMSession",
    "LLMSpec",
    "LLMTextEntry",
    "LLMToolCallRepair",
    "Agent",
    "Capsule",
    "capsule_registry",
    "Tool",
    "Toolkit",
    "InteropIssue",
    "InteropReport",
    "InteropSet",
    "InteropTypeInferer",
    "from_daft",
    "from_numpy",
    "from_pandas",
    "from_pyarrow",
    "from_pydantic",
    "from_sql_result",
    "registered_interop_type_inferers",
    "register_interop_type_inferer",
    "ext",
]

EXT_EXPORTS = [
    "Backend",
    "BackendFactory",
    "BackendRequest",
    "ScalarIndexSpec",
    "BackendMCP",
    "BackendMCPProfile",
    "BackendMCPTool",
    "BackendRegistry",
    "BackendType",
    "QueryFragment",
    "HandlerContext",
    "RowOp",
    "StrategyAdapter",
    "ResolvedTagValue",
    "TagDefinition",
    "TagView",
    "tag",
    "tag_definition",
    "tags",
    "AggregateCompileResult",
    "AggregateCompiler",
    "Handler",
    "HandlerCompiler",
    "HandlerKey",
    "HandlerPlugin",
    "HandlerRegistry",
    "HandlerSeedContext",
    "Predicate",
    "handler_plugins",
    "backend_aliases",
    "register_sql_dialect",
    "registered_sql_dialects",
    "SQL_JSON_AGG_STYLE_ARRAYAGG",
    "SQL_JSON_AGG_STYLE_GROUP_ARRAY",
    "SQL_JSON_AGG_STYLE_POSTGRES_ORDERED",
    "SQL_JSON_OBJECT_STYLE_BUILD_OBJECT",
    "SQL_JSON_OBJECT_STYLE_OBJECT",
    "SQL_REGEX_STYLE_DUCKDB_TILDE_SEARCH",
    "SQL_REGEX_STYLE_POSTGRES_TILDE",
    "SQL_REGEX_STYLE_REGEXP_LIKE",
    "SQL_REGEX_STYLE_REGEXP_LIKE_CASE_SENSITIVE",
    "SQL_WILDCARD_STYLE_GLOB",
    "SQL_WILDCARD_STYLE_LIKE",
    "SQLDialectSpec",
    "sql_dialect_spec",
    "DatabaseDialectSpec",
    "MYSQL_TEXT_MIN_LEN",
    "ORACLE_VARCHAR_MAX",
    "SQL_TYPE_CLOB",
    "SQL_TYPE_FLOAT64_BLOB",
    "SQL_TYPE_FLOAT_ARRAY",
    "SQL_TYPE_JSON",
    "SQL_TYPE_JSON_TEXT",
    "SQL_TYPE_MYSQL_TEXT",
    "SQL_TYPE_POSTGRESQL_JSONB",
    "SQL_TYPE_STRING",
    "SQL_TYPE_TEXT",
    "database_dialects",
    "llm_schema_text",
    "register_schema_text",
    "registered_schema_texts",
    "resolve_schema_text",
    "register_gram_normalizer",
    "register_gram_tokenizer",
    "resolve_gram_normalizer",
    "resolve_gram_tokenizer",
    "register_compute",
    "register_query_compute",
    "registered_computes",
    "registered_query_computes",
    "get_logical_type",
    "logical_type_aliases",
    "register_logical_type",
    "AggregateReducer",
    "aggregate_reducers",
    "register_aggregate_reducer",
    "ExternalRef",
    "GraphEdge",
    "SparseGramIndex",
    "register_strategy",
    "StorageStrategy",
    "strategy_aliases",
    "normalize_op",
    "op_aliases",
    "register_op",
    "registered_ops",
    "registered_storage_profiles",
    "StorageBinding",
    "IndexBinding",
    "IndexRule",
    "StoragePlan",
    "StorageProfile",
    "StorageResolver",
    "StorageRule",
    "UniqueBinding",
    "UniqueRule",
    "AGENT_EXTENSION_ID",
    "CAPSULE_EXTENSION_ID",
    "DATABASE_EXTENSION_ID",
    "Extension",
    "MEMORY_EXTENSION_ID",
    "PROMPT_EXTENSION_ID",
    "TOOLKIT_EXTENSION_ID",
    "CapsuleLayer",
    "CapsuleLayerBuildContext",
    "register_capsule_layer",
    "registered_capsule_layers",
    "ProfileSpec",
    "profile_spec_from_mapping",
    "register_profile",
    "registered_profiles",
    "registered_profile_tool_names",
    "WorkspaceToolkitFamilySpec",
    "register_workspace_toolkit_family",
    "registered_workspace_toolkit_families",
    "registered_workspace_tool_names",
    "ToolSerializer",
    "ingest_database",
]

EXTENSION_EXPORTS = [
    "Extension",
    "AGENT_EXTENSION_ID",
    "CAPSULE_EXTENSION_ID",
    "DATABASE_EXTENSION_ID",
    "MEMORY_EXTENSION_ID",
    "PROMPT_EXTENSION_ID",
    "TOOLKIT_EXTENSION_ID",
]


def test_backend_roles_have_one_module_owner():
    from heavenbase.backends import base, capabilities, mcp, scanning
    from heavenbase.execution import contracts

    assert base.__all__ == ["Backend", "backend_locked"]
    assert capabilities.__all__ == ["DEFAULT_INDEXABLE_TYPES", "ScalarIndexSpec"]
    assert contracts.__all__ == ["LOGICAL_OPS", "HandlerContext", "QueryFragment", "RowOp"]
    assert mcp.__all__ == ["BackendMCP", "BackendMCPProfile", "BackendMCPTool"]
    assert scanning.__all__ == ["execute_scan"]


@pytest.mark.parametrize(
    "module_name",
    (
        "heavenbase.backends.context",
        "heavenbase.backends.contracts",
        "heavenbase.discovery",
        "heavenbase.frame",
        "heavenbase.handlers",
        "heavenbase.mcp_runtime",
        "heavenbase.resources.prompts",
        "heavenbase.resources.sql",
        "heavenbase.storage",
        "heavenbase.system_entities",
        "heavenbase.utils.llm",
        "heavenbase.utils.llm.base",
        "heavenbase.utils.llm.cache",
        "heavenbase.utils.llm.session",
    ),
)
def test_retired_package_owners_have_no_compatibility_imports(module_name: str):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_validate_json_is_available_from_the_public_utils_namespace():
    validate_json({"items": [1, True, None]})
    with pytest.raises(TypeError, match="keys must be strings"):
        validate_json({1: "not-json"})
    with pytest.raises(ValueError):
        validate_json({"value": float("nan")})


def test_root_public_exports_are_user_facing():
    assert hb.__all__ == ROOT_EXPORTS
    assert hb.utils is importlib.import_module("heavenbase.utils")
    assert "utils" not in hb.__all__
    assert hb.backends.SQLiteBackend.__name__ == "SQLiteBackend"
    assert "open" not in hb.__all__
    assert "QueryFragment" not in hb.__all__
    assert "HandlerContext" not in hb.__all__
    assert "BackendRegistry" not in hb.__all__
    hidden = [
        "Backend",
        "BackendCapabilities",
        "BackendMCP",
        "BackendMCPProfile",
        "BackendMCPTool",
        "BackendRegistry",
        "ChromaBackend",
        "ClickHouseBackend",
        "DoltBackend",
        "DuckDBBackend",
        "ElasticsearchBackend",
        "FileBackend",
        "InMemBackend",
        "JsonFileBackend",
        "LanceBackend",
        "MilvusBackend",
        "MilvusLiteBackend",
        "MongoDBBackend",
        "MsSQLBackend",
        "MySQLBackend",
        "OceanBaseBackend",
        "OpenSearchBackend",
        "OracleBackend",
        "PgVectorBackend",
        "PickleFileBackend",
        "PineconeBackend",
        "PostgresBackend",
        "QdrantBackend",
        "SeekDBBackend",
        "SQLBackend",
        "SQLiteBackend",
        "StarRocksBackend",
        "SupabaseBackend",
        "TinyDBBackend",
        "TrinoBackend",
        "WeaviateBackend",
        "backend_families",
        "backend_aliases",
        "backend_identifier_in_family",
        "backend_identifiers_for",
        "BenchmarkReport",
        "BenchmarkRunner",
        "BenchmarkScenario",
        "build_backend",
        "DefaultProductScenario",
        "derive",
        "DeriveSpec",
        "FieldRef",
        "FieldSpec",
        "Filter",
        "default_workspace",
        "entity_from_dict",
        "get_backend_builder",
        "get_workspace",
        "get_logical_type",
        "HandlerContext",
        "HandlerRegistry",
        "load_configured_backends",
        "logical_type_aliases",
        "logical_type_families",
        "logical_type_in_family",
        "logical_types_for",
        "normalize_op",
        "op_aliases",
        "op_families",
        "ops_for",
        "QueryFragment",
        "register_backend_builder",
        "register_sql_dialect",
        "register_logical_type",
        "register_op",
        "register_strategy",
        "registered_backend_identifiers",
        "registered_sql_dialects",
        "registered_ops",
        "registered_storage_profiles",
        "RowOp",
        "run_benchmark",
        "refresh_workspace",
        "StorageBinding",
        "StoragePlan",
        "StorageProfile",
        "StorageResolver",
        "StorageRule",
        "SQLDialectSpec",
        "sql_dialect_spec",
        "strategies_for",
        "strategy_aliases",
        "strategy_families",
        "strategy_in_family",
        "hash_id",
        "table_display",
    ]
    assert [name for name in hidden if hasattr(hb, name)] == []


def test_root_import_keeps_heavy_support_modules_lazy():
    script = """
import json
import sys

import heavenbase as hb

heavy_roots = {
    "heavenbase.backends",
    "heavenbase.capabilities",
    "heavenbase.ext",
    "heavenbase.execution.handlers",
    "heavenbase.interop",
    "heavenbase.llm",
    "heavenbase.extensions.agent",
    "heavenbase.capsule",
    "heavenbase.extensions.database",
    "heavenbase.extensions.memory",
    "heavenbase.extensions.prompt",
    "heavenbase.toolkit",
    "heavenbase.toolkit.prompts",
    "heavenbase.database.resources.sql",
    "heavenbase.database",
    "heavenbase.utils.llm",
    "heavenbase.workspace",
}
heavy_prefixes = tuple(f"{name}." for name in heavy_roots)
loaded = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
bound = {
    name: name in hb.__dict__
    for name in (
        "Agent",
        "BackendType",
        "Database",
        "HeavenBase",
        "InteropSet",
        "LLM",
        "Toolkit",
        "WorkspaceManifest",
        "backends",
        "capabilities",
        "ext",
        "from_pandas",
    )
}
print(json.dumps({"bound": bound, "loaded": loaded}, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["loaded"] == []
    assert data["bound"] == {
        "Agent": False,
        "BackendType": False,
        "Database": False,
        "HeavenBase": False,
        "InteropSet": False,
        "LLM": False,
        "Toolkit": False,
        "WorkspaceManifest": False,
        "backends": False,
        "capabilities": False,
        "ext": False,
        "from_pandas": False,
    }


def test_root_import_settles_the_asyncio_first_import_race():
    # typing_extensions.deprecated (pydantic, mcp, openai) imports
    # asyncio.coroutines on first use, and Python takes a submodule's import
    # lock before its package's, so a thread doing that while another thread's
    # lazy first use of HeavenBase first imported asyncio deadlocked with
    # _DeadlockError. Importing heavenbase now imports asyncio first. The race
    # itself is forced with a 1 us switch interval in fresh interpreters,
    # where it deadlocked about one run in three without asyncio imported.
    script = """
import sys
import threading

import heavenbase

assert "asyncio" in sys.modules, "importing heavenbase left asyncio unimported"
sys.setswitchinterval(1e-6)
errors = []


def first_import(name):
    try:
        __import__(name)
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")


threads = [threading.Thread(target=first_import, args=(name,)) for name in ("asyncio.coroutines", "asyncio")]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(60)
print(errors[0] if errors else "ok")
"""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cmd([sys.executable, "-c", script], include="result"), range(8)))

    assert [(result.ok, result.out or result.err[-300:]) for result in results] == [(True, "ok")] * 8


def test_root_import_runs_its_asyncio_import_again_when_python_fails_it_on_an_import_lock_cycle():
    # The early asyncio import can itself meet a thread that first imports
    # asyncio.coroutines; when Python picks the heavenbase import as the one
    # that fails with _DeadlockError, `import heavenbase` failed with it. A
    # finder raising _DeadlockError on the first asyncio lookup stands in for
    # that thread, which keeps the race deterministic.
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys

sys.path.insert(0, sys.argv[1])
assert "asyncio" not in sys.modules
_DeadlockError = type("_DeadlockError", (RuntimeError,), {})
calls = []


class Deadlocking:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name == "asyncio":
            calls.append(name)
            if len(calls) == 1:
                raise _DeadlockError("deadlock detected by _ModuleLock('asyncio.coroutines')")
        return None


sys.meta_path.insert(0, Deadlocking)
import heavenbase

assert calls == ["asyncio", "asyncio"], calls
assert "asyncio" in sys.modules and hasattr(sys.modules["asyncio"], "get_running_loop")
assert not hasattr(heavenbase, "asyncio")
print("ok")
"""
    result = cmd([sys.executable, "-I", "-c", script, str(source)], include="result")

    assert result.ok, result.err
    assert result.out == "ok"


@pytest.mark.parametrize(
    ("module_name", "expected"),
    (
        ("heavenbase", ["heavenbase", "heavenbase._lazy"]),
        (
            "heavenbase.context.bootstrap",
            ["heavenbase", "heavenbase._lazy", "heavenbase.context", "heavenbase.context.bootstrap"],
        ),
        ("heavenbase.utils", ["heavenbase", "heavenbase._lazy", "heavenbase.utils"]),
        ("heavenbase.llm", ["heavenbase", "heavenbase._lazy", "heavenbase.llm"]),
        ("heavenbase.workspace", ["heavenbase", "heavenbase._lazy", "heavenbase.workspace"]),
    ),
)
def test_foundation_facades_initialize_only_their_lazy_loader(module_name, expected):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
importlib.import_module(sys.argv[2])
print(json.dumps(sorted(name for name in sys.modules if name == "heavenbase" or name.startswith("heavenbase."))))
"""
    result = cmd([sys.executable, "-I", "-c", script, str(source), module_name], include="out", check=True)

    assert loads_json(result) == expected


@pytest.mark.parametrize("config_first", (True, False))
def test_default_context_and_public_config_identity_is_access_order_independent(config_first, tmp_path, home_env):
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb

if sys.argv[2] == "config":
    first = hb.CM_HVNB
    second = hb.DEFAULT_CONTEXT.config
else:
    first = hb.DEFAULT_CONTEXT.config
    second = hb.CM_HVNB
assert first is second
"""

    cmd([sys.executable, "-I", "-c", script, str(source), "config" if config_first else "context"], check=True, env=home_env(tmp_path))


def test_ext_import_keeps_heavy_support_modules_lazy():
    script = """
import json
import sys

import heavenbase.ext as ext

heavy_roots = {
    "heavenbase.backends",
    "heavenbase.capabilities",
    "heavenbase.execution.handlers",
    "heavenbase.workspace",
    "heavenbase.extensions.agent",
    "heavenbase.capsule",
    "heavenbase.extensions.database",
    "heavenbase.extensions.memory",
    "heavenbase.extensions.prompt",
    "heavenbase.toolkit",
    "heavenbase.llm",
    "heavenbase.database",
    "heavenbase.utils.llm",
}
heavy_prefixes = tuple(f"{name}." for name in heavy_roots)
loaded = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
bound = {
    name: name in ext.__dict__
    for name in (
        "Backend",
        "TagView",
        "BackendMCP",
        "BackendRegistry",
        "CapsuleLayer",
        "HandlerRegistry",
        "ProfileSpec",
        "ToolSerializer",
        "ingest_database",
    )
}
ids = {
    "agent": ext.AGENT_EXTENSION_ID,
    "capsule": ext.CAPSULE_EXTENSION_ID,
    "database": ext.DATABASE_EXTENSION_ID,
    "memory": ext.MEMORY_EXTENSION_ID,
    "prompt": ext.PROMPT_EXTENSION_ID,
    "toolkit": ext.TOOLKIT_EXTENSION_ID,
}
print(json.dumps({"bound": bound, "ids": ids, "loaded": loaded}, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["loaded"] == []
    assert data["bound"] == {
        "Backend": False,
        "TagView": False,
        "BackendMCP": False,
        "BackendRegistry": False,
        "CapsuleLayer": False,
        "HandlerRegistry": False,
        "ProfileSpec": False,
        "ToolSerializer": False,
        "ingest_database": False,
    }
    assert data["ids"] == {
        "agent": "agent",
        "capsule": "capsule",
        "database": "database",
        "memory": "memory",
        "prompt": "prompt",
        "toolkit": "toolkit",
    }


def test_discovery_import_keeps_index_lazy():
    script = """
import json
import sys

import heavenbase.capabilities as discovery

heavy_roots = {
    "heavenbase.backends",
    "heavenbase.capabilities.index",
    "heavenbase.capabilities.options",
    "heavenbase.execution.handlers",
}
heavy_prefixes = tuple(f"{name}." for name in heavy_roots)
loaded = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
bound = {
    name: name in discovery.__dict__
    for name in (
        "BackendOption",
        "CapabilityIndex",
        "CapabilityOption",
        "LogicalTypeOption",
        "StrategyOption",
        "capabilities",
        "logical_types",
        "strategies",
        "backends",
        "ops",
        "supports",
    )
}
print(json.dumps({"bound": bound, "loaded": loaded}, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["loaded"] == []
    assert data["bound"] == {
        "BackendOption": False,
        "CapabilityIndex": False,
        "CapabilityOption": False,
        "LogicalTypeOption": False,
        "StrategyOption": False,
        "capabilities": True,
        "logical_types": True,
        "strategies": True,
        "backends": True,
        "ops": True,
        "supports": True,
    }


def test_resources_package_contains_only_package_wide_assets():
    script = """
import json

import heavenbase.resources as resources
from heavenbase.database.resources.sql import SqlTemplate
from heavenbase.toolkit.prompts import PromptSpec

print(
    json.dumps(
        {
            "exports": resources.__all__,
            "prompt": PromptSpec.__name__,
            "prompt_module": PromptSpec.__module__,
            "sql": SqlTemplate.__name__,
            "sql_module": SqlTemplate.__module__,
        },
        sort_keys=True,
    )
)
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["exports"] == []
    assert data["prompt"] == "PromptSpec"
    assert data["prompt_module"] == "heavenbase.toolkit.prompts"
    assert data["sql"] == "SqlTemplate"
    assert data["sql_module"] == "heavenbase.database.resources.sql.base"


def test_modules_import_keeps_lifecycle_implementations_lazy():
    script = """
import json
import sys

import heavenbase.modules as modules

heavy_roots = {
    "heavenbase.modules.discovery",
    "heavenbase.modules.manifest",
    "heavenbase.modules.service",
}
heavy_prefixes = tuple(f"{name}." for name in heavy_roots)
loaded = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
bound = {
    name: name in modules.__dict__
    for name in (
        "InstallReceipt",
        "ModuleService",
    )
}
receipt_cls = modules.InstallReceipt
after_receipt = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
service_cls = modules.ModuleService
after_service = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
print(
    json.dumps(
        {
            "after_receipt": after_receipt,
            "after_service": after_service,
            "bound": bound,
            "loaded": loaded,
            "receipt": receipt_cls.__name__,
            "service": service_cls.__name__,
        },
        sort_keys=True,
    )
)
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["loaded"] == []
    assert data["bound"] == {"InstallReceipt": False, "ModuleService": False}
    assert data["receipt"] == "InstallReceipt"
    assert data["service"] == "ModuleService"
    assert data["after_receipt"] == ["heavenbase.modules.manifest"]
    assert data["after_service"] == [
        "heavenbase.modules.discovery",
        "heavenbase.modules.manifest",
        "heavenbase.modules.service",
    ]


def test_root_workspace_class_access_keeps_provider_modules_lazy():
    script = """
import json
import sys

import heavenbase as hb

workspace_cls = hb.HeavenBase
heavy_roots = {
    "heavenbase.backends.elasticsearch",
    "heavenbase.backends.file",
    "heavenbase.backends.inmem",
    "heavenbase.backends.mongodb",
    "heavenbase.backends.opensearch",
    "heavenbase.backends.redis",
    "heavenbase.backends.sql.base",
    "heavenbase.backends.sql.duckdb",
    "heavenbase.backends.sql.mssql",
    "heavenbase.backends.sql.mysql",
    "heavenbase.backends.sql.oceanbase",
    "heavenbase.backends.sql.oracle",
    "heavenbase.backends.sql.postgres",
    "heavenbase.backends.sql.sqlite",
    "heavenbase.backends.sql.starrocks",
    "heavenbase.backends.sql.trino",
    "heavenbase.backends.tinydb",
    "heavenbase.backends.vector",
    "heavenbase.execution.handlers.filters",
    "heavenbase.execution.handlers.seed",
    "heavenbase.backends.sql.handler",
    "heavenbase.backends.vector.handler",
    "heavenbase.database",
}
heavy_prefixes = tuple(f"{name}." for name in heavy_roots)
loaded = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
print(json.dumps({"loaded": loaded, "workspace": workspace_cls.__name__}, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["workspace"] == "HeavenBase"
    assert data["loaded"] == []


def test_backend_package_access_keeps_provider_modules_lazy():
    script = """
import json
import sys

import heavenbase as hb

backend_module = hb.backends
heavy_roots = {
    "heavenbase.backends.elasticsearch",
    "heavenbase.backends.file",
    "heavenbase.backends.inmem",
    "heavenbase.backends.mongodb",
    "heavenbase.backends.opensearch",
    "heavenbase.backends.redis",
    "heavenbase.backends.sql",
    "heavenbase.backends.tinydb",
    "heavenbase.backends.vector",
    "heavenbase.execution.handlers",
    "heavenbase.database",
}
heavy_prefixes = tuple(f"{name}." for name in heavy_roots)
loaded = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
print(json.dumps({"backend_all": list(backend_module.__all__), "loaded": loaded}, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["loaded"] == []
    assert "SQLiteBackend" in data["backend_all"]
    assert "InMemBackend" in data["backend_all"]


@pytest.mark.parametrize(
    ("export_name", "provider_root", "optional_client"),
    (
        ("MongoDBBackend", "heavenbase.backends.mongodb", "pymongo"),
        ("OpenSearchBackend", "heavenbase.backends.opensearch", "opensearchpy"),
        ("TinyDBBackend", "heavenbase.backends.tinydb", "tinydb"),
    ),
)
def test_document_provider_class_access_keeps_optional_clients_lazy(export_name, provider_root, optional_client):
    """Resolving a provider class loads its adapter, never its optional client."""
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import json
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase as hb

client_root = sys.argv[4]
client_prefix = f"{client_root}."
client_before = sorted(name for name in sys.modules if name == client_root or name.startswith(client_prefix))
backend_cls = getattr(hb.backends, sys.argv[2])
client_after = sorted(name for name in sys.modules if name == client_root or name.startswith(client_prefix))
provider_root = sys.argv[3]
provider_prefix = f"{provider_root}."
provider_loaded = sorted(name for name in sys.modules if name == provider_root or name.startswith(provider_prefix))
print(
    json.dumps(
        {
            "client_after": client_after,
            "client_before": client_before,
            "provider_class": backend_cls.__name__,
            "provider_loaded": provider_loaded,
        },
        sort_keys=True,
    )
)
"""
    result = cmd(
        [sys.executable, "-I", "-c", script, str(source), export_name, provider_root, optional_client],
        include="out",
        check=True,
    )
    data = loads_json(result)

    assert data["client_before"] == []
    assert data["client_after"] == []
    assert data["provider_class"] == export_name
    assert provider_root in data["provider_loaded"]


def test_extension_package_exposes_contracts_without_loading_implementations():
    script = """
import json
import sys

import heavenbase.extensions as extensions

implementation_roots = {
    "heavenbase.capsule",
    "heavenbase.extensions.agent",
    "heavenbase.extensions.database",
    "heavenbase.extensions.memory",
    "heavenbase.extensions.prompt",
    "heavenbase.toolkit",
}
implementation_prefixes = tuple(f"{name}." for name in implementation_roots)
loaded = sorted(name for name in sys.modules if name in implementation_roots or name.startswith(implementation_prefixes))
bound = {name: name in extensions.__dict__ for name in ("Agent", "Capsule", "DatabaseAPI", "MemoryNote", "Prompt", "Toolkit")}
ids = {
    "agent": extensions.AGENT_EXTENSION_ID,
    "capsule": extensions.CAPSULE_EXTENSION_ID,
    "database": extensions.DATABASE_EXTENSION_ID,
    "memory": extensions.MEMORY_EXTENSION_ID,
    "prompt": extensions.PROMPT_EXTENSION_ID,
    "toolkit": extensions.TOOLKIT_EXTENSION_ID,
}
print(json.dumps({"bound": bound, "exports": list(extensions.__all__), "ids": ids, "loaded": loaded}, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["loaded"] == []
    assert data["bound"] == {
        "Agent": False,
        "Capsule": False,
        "DatabaseAPI": False,
        "MemoryNote": False,
        "Prompt": False,
        "Toolkit": False,
    }
    assert data["exports"] == EXTENSION_EXPORTS
    assert data["ids"] == {
        "agent": "agent",
        "capsule": "capsule",
        "database": "database",
        "memory": "memory",
        "prompt": "prompt",
        "toolkit": "toolkit",
    }


def test_discovery_package_keeps_registry_readers_lazy():
    script = """
import json
import sys

import heavenbase.capabilities as discovery

heavy_roots = {
    "heavenbase.capabilities.index",
    "heavenbase.capabilities.options",
    "heavenbase.execution.handlers",
    "heavenbase.backends",
}
heavy_prefixes = tuple(f"{name}." for name in heavy_roots)
loaded = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
bound = {name: name in discovery.__dict__ for name in discovery.__all__}
print(json.dumps({"bound": bound, "loaded": loaded}, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["loaded"] == []
    assert data["bound"] == {
        "BackendOption": False,
        "CapabilityIndex": False,
        "CapabilityOption": False,
        "LogicalTypeOption": False,
        "StrategyOption": False,
        "capabilities": True,
        "logical_types": True,
        "strategies": True,
        "backends": True,
        "ops": True,
        "supports": True,
    }


def test_utils_import_excludes_database_and_llm_domain_exports():
    script = """
import json
import sys

import heavenbase.utils as utils

heavy_roots = {"heavenbase.database", "heavenbase.llm"}
heavy_prefixes = tuple(f"{name}." for name in heavy_roots)
before = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
bound_before = {name: name in utils.__dict__ for name in ("Database", "LLMEngine")}
database_exported = "Database" in utils.__all__ or hasattr(utils, "Database")
llm_exported = "LLMEngine" in utils.__all__ or hasattr(utils, "LLMEngine")
after = sorted(name for name in sys.modules if name in heavy_roots or name.startswith(heavy_prefixes))
print(json.dumps({"after": after, "bound_before": bound_before, "database_exported": database_exported, "llm_exported": llm_exported, "before": before}, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    data = loads_json(result)

    assert data["before"] == []
    assert data["bound_before"] == {"Database": False, "LLMEngine": False}
    assert data["database_exported"] is False
    assert data["llm_exported"] is False
    assert not any(name == "heavenbase.database" or name.startswith("heavenbase.database.") for name in data["after"])
    assert not any(name == "heavenbase.llm" or name.startswith("heavenbase.llm.") for name in data["after"])


@pytest.mark.parametrize("module_name", ("heavenbase.utils.db", "heavenbase.utils.dialect"))
def test_retired_database_utility_packages_are_not_importable(module_name):
    assert importlib.util.find_spec(module_name) is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_root_lazy_exports_resolve_on_access():
    assert hb.BackendType.__name__ == "BackendType"
    assert hb.Database.__name__ == "Database"
    assert hb.HeavenBase.__name__ == "HeavenBase"
    assert hb.GraphDeleteRestrictedError.__name__ == "GraphDeleteRestrictedError"
    assert hb.GraphDeleteRestrictedError.__module__ == "heavenbase.workspace.graph.errors"
    assert hb.LLM.__name__ == "LLM"
    assert hb.Capsule is capsule.Capsule
    assert hb.Capsule.__module__ == "heavenbase.capsule.runtime"
    assert hb.Toolkit.__name__ == "Toolkit"
    assert hb.Toolkit.__module__.partition(".")[0].startswith("_heavenbase_artifact_")
    assert hb.WorkspaceManifest.__name__ == "WorkspaceManifest"
    assert hb.Agent.__name__ == "Agent"
    assert hb.backends.SQLiteBackend.__name__ == "SQLiteBackend"
    assert hb.capabilities is importlib.import_module("heavenbase.capabilities")
    assert hb.ext is ext


def test_dynamic_extension_facades_resolve_one_registry_artifact_generation():
    prompt_api = importlib.import_module("heavenbase.extensions.prompt")
    agent_api = importlib.import_module("heavenbase.extensions.agent")
    memory_api = importlib.import_module("heavenbase.extensions.memory")
    database_api = importlib.import_module("heavenbase.extensions.database")
    toolkit_api = importlib.import_module("heavenbase.toolkit")

    artifact_roots = {}
    for module in (prompt_api, agent_api, memory_api, database_api, toolkit_api):
        exports = (getattr(module, name) for name in module.__all__ if not name.endswith("_EXTENSION_ID"))
        roots = {value.__module__.partition(".")[0] for value in exports}
        assert len(roots) == 1, module.__name__
        artifact_root = roots.pop()
        assert artifact_root.startswith("_heavenbase_artifact_"), module.__name__
        artifact_roots[module.__name__] = artifact_root

    assert prompt_api.Prompt is hb.Prompt
    assert prompt_api.Translation is hb.Translation
    assert agent_api.Agent is hb.Agent
    assert agent_api.Message is hb.Message
    assert agent_api.Session is hb.Session
    assert agent_api.Skill is hb.Skill
    assert memory_api.MemoryNote is hb.MemoryNote
    assert toolkit_api.Toolkit is hb.Toolkit
    assert toolkit_api.Tool is hb.Tool
    assert ext.ToolSerializer.__module__.partition(".")[0] == artifact_roots[toolkit_api.__name__]
    assert ext.ingest_database is database_api.ingest_database


def test_capsule_facade_uses_its_fixed_package_owner():
    assert capsule.Capsule is hb.Capsule
    assert capsule.CapsuleLayer is ext.CapsuleLayer
    assert capsule.Capsule.__module__ == "heavenbase.capsule.runtime"
    assert capsule.CapsuleLayer.__module__ == "heavenbase.capsule.layer"


def _type_stub_exports(*path_parts):
    source = load_txt(pj(*path_parts))
    tree = ast.parse(source)
    imported = {alias.asname for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names if alias.asname == alias.name}
    declared = {node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")}
    return imported | declared


def _assert_type_stub_matches_module(module_name, *stub_path_parts):
    module = importlib.import_module(module_name)

    assert _type_stub_exports(*stub_path_parts) == set(module.__all__)


def test_lazy_facades_have_explicit_type_stubs():
    stub_specs = (
        ("heavenbase", ("src", "heavenbase", "__init__.pyi")),
        ("heavenbase.context", ("src", "heavenbase", "context", "__init__.pyi")),
        ("heavenbase.ext", ("src", "heavenbase", "ext.pyi")),
        ("heavenbase.backends", ("src", "heavenbase", "backends", "__init__.pyi")),
        ("heavenbase.backends.elasticsearch", ("src", "heavenbase", "backends", "elasticsearch", "__init__.pyi")),
        ("heavenbase.backends.file", ("src", "heavenbase", "backends", "file", "__init__.pyi")),
        ("heavenbase.backends.inmem", ("src", "heavenbase", "backends", "inmem", "__init__.pyi")),
        ("heavenbase.backends.neo4j", ("src", "heavenbase", "backends", "neo4j", "__init__.pyi")),
        ("heavenbase.backends.redis", ("src", "heavenbase", "backends", "redis", "__init__.pyi")),
        ("heavenbase.backends.sql", ("src", "heavenbase", "backends", "sql", "__init__.pyi")),
        ("heavenbase.backends.sql.strategies", ("src", "heavenbase", "backends", "sql", "strategies", "__init__.pyi")),
        (
            "heavenbase.backends.sql.strategies.gram",
            ("src", "heavenbase", "backends", "sql", "strategies", "gram", "__init__.pyi"),
        ),
        ("heavenbase.backends.surreal", ("src", "heavenbase", "backends", "surreal", "__init__.pyi")),
        ("heavenbase.backends.vector", ("src", "heavenbase", "backends", "vector", "__init__.pyi")),
        ("heavenbase.benchmark", ("src", "heavenbase", "benchmark", "__init__.pyi")),
        ("heavenbase.cli", ("src", "heavenbase", "cli", "__init__.pyi")),
        ("heavenbase.cli.backends", ("src", "heavenbase", "cli", "backends", "__init__.pyi")),
        ("heavenbase.cli.groups", ("src", "heavenbase", "cli", "groups", "__init__.pyi")),
        ("heavenbase.extensions", ("src", "heavenbase", "extensions", "__init__.pyi")),
        ("heavenbase.extensions.agent", ("src", "heavenbase", "extensions", "agent", "__init__.pyi")),
        ("heavenbase.extensions.agent.toolkit", ("src", "heavenbase", "extensions", "agent", "toolkit", "__init__.pyi")),
        ("heavenbase.extensions.database", ("src", "heavenbase", "extensions", "database", "__init__.pyi")),
        (
            "heavenbase.extensions.database.toolkit",
            ("src", "heavenbase", "extensions", "database", "toolkit", "__init__.pyi"),
        ),
        (
            "heavenbase.extensions.memory.toolkit",
            ("src", "heavenbase", "extensions", "memory", "toolkit", "__init__.pyi"),
        ),
        ("heavenbase.extensions.memory", ("src", "heavenbase", "extensions", "memory", "__init__.pyi")),
        ("heavenbase.extensions.prompt", ("src", "heavenbase", "extensions", "prompt", "__init__.pyi")),
        ("heavenbase.capsule", ("src", "heavenbase", "capsule", "__init__.pyi")),
        ("heavenbase.toolkit", ("src", "heavenbase", "toolkit", "__init__.pyi")),
        (
            "heavenbase.toolkit.families.workspace",
            ("src", "heavenbase", "toolkit", "families", "workspace", "__init__.pyi"),
        ),
        ("heavenbase.execution", ("src", "heavenbase", "execution", "__init__.pyi")),
        ("heavenbase.execution.handlers", ("src", "heavenbase", "execution", "handlers", "__init__.pyi")),
        ("heavenbase.capabilities", ("src", "heavenbase", "capabilities", "__init__.pyi")),
        ("heavenbase.llm", ("src", "heavenbase", "llm", "__init__.pyi")),
        ("heavenbase.modules", ("src", "heavenbase", "modules", "__init__.pyi")),
        ("heavenbase.resources", ("src", "heavenbase", "resources", "__init__.pyi")),
        ("heavenbase.utils", ("src", "heavenbase", "utils", "__init__.pyi")),
        ("heavenbase.database", ("src", "heavenbase", "database", "__init__.pyi")),
        ("heavenbase.database.dialect", ("src", "heavenbase", "database", "dialect", "__init__.pyi")),
        ("heavenbase.workspace", ("src", "heavenbase", "workspace", "__init__.pyi")),
        ("heavenbase.entity", ("src", "heavenbase", "entity", "__init__.pyi")),
        ("heavenbase.execution.frame", ("src", "heavenbase", "execution", "frame", "__init__.pyi")),
        ("heavenbase.filters", ("src", "heavenbase", "filters", "__init__.pyi")),
        ("heavenbase.interop", ("src", "heavenbase", "interop", "__init__.pyi")),
        ("heavenbase.query", ("src", "heavenbase", "query", "__init__.pyi")),
        ("heavenbase.schema", ("src", "heavenbase", "schema", "__init__.pyi")),
        ("heavenbase.strategies", ("src", "heavenbase", "strategies", "__init__.pyi")),
        ("heavenbase.toolkit.families", ("src", "heavenbase", "toolkit", "families", "__init__.pyi")),
        ("heavenbase.types", ("src", "heavenbase", "types", "__init__.pyi")),
        ("heavenbase.utils.config", ("src", "heavenbase", "utils", "config", "__init__.pyi")),
        ("heavenbase.workspace.catalog", ("src", "heavenbase", "workspace", "catalog", "__init__.pyi")),
        ("heavenbase.workspace.graph", ("src", "heavenbase", "workspace", "graph", "__init__.pyi")),
        ("heavenbase.workspace.mcp", ("src", "heavenbase", "workspace", "mcp", "__init__.pyi")),
        ("heavenbase.workspace.metaschema", ("src", "heavenbase", "workspace", "metaschema", "__init__.pyi")),
        ("heavenbase.workspace.schema", ("src", "heavenbase", "workspace", "schema", "__init__.pyi")),
        ("heavenbase.placement", ("src", "heavenbase", "placement", "__init__.pyi")),
        ("heavenbase.utils.registry", ("src", "heavenbase", "utils", "registry", "__init__.pyi")),
        ("heavenbase.database.resources.sql", ("src", "heavenbase", "database", "resources", "sql", "__init__.pyi")),
        ("heavenbase.extensions.python", ("src", "heavenbase", "extensions", "python", "__init__.pyi")),
        ("heavenbase.extensions.python.toolkit", ("src", "heavenbase", "extensions", "python", "toolkit", "__init__.pyi")),
        ("heavenbase.cli.groups.config", ("src", "heavenbase", "cli", "groups", "config", "__init__.pyi")),
        ("heavenbase.cli.groups.llm", ("src", "heavenbase", "cli", "groups", "llm", "__init__.pyi")),
        ("heavenbase.cli.groups.mcp", ("src", "heavenbase", "cli", "groups", "mcp", "__init__.pyi")),
        ("heavenbase.cli.groups.prompt", ("src", "heavenbase", "cli", "groups", "prompt", "__init__.pyi")),
        ("heavenbase.cli.groups.ws", ("src", "heavenbase", "cli", "groups", "ws", "__init__.pyi")),
        ("heavenbase.backends.elasticsearch.backend", ("src", "heavenbase", "backends", "elasticsearch", "backend", "__init__.pyi")),
        ("heavenbase.backends.elasticsearch.handler", ("src", "heavenbase", "backends", "elasticsearch", "handler", "__init__.pyi")),
        ("heavenbase.backends.inmem.handler", ("src", "heavenbase", "backends", "inmem", "handler", "__init__.pyi")),
        ("heavenbase.backends.mongodb.backend", ("src", "heavenbase", "backends", "mongodb", "backend", "__init__.pyi")),
        ("heavenbase.backends.neo4j.handler", ("src", "heavenbase", "backends", "neo4j", "handler", "__init__.pyi")),
        ("heavenbase.backends.opensearch.backend", ("src", "heavenbase", "backends", "opensearch", "backend", "__init__.pyi")),
        ("heavenbase.backends.opensearch.handler", ("src", "heavenbase", "backends", "opensearch", "handler", "__init__.pyi")),
        ("heavenbase.backends.redis.handler", ("src", "heavenbase", "backends", "redis", "handler", "__init__.pyi")),
        ("heavenbase.backends.sql.handler", ("src", "heavenbase", "backends", "sql", "handler", "__init__.pyi")),
        ("heavenbase.backends.surreal.handler", ("src", "heavenbase", "backends", "surreal", "handler", "__init__.pyi")),
        ("heavenbase.backends.tinydb.backend", ("src", "heavenbase", "backends", "tinydb", "backend", "__init__.pyi")),
    )

    for module_name, stub_path_parts in stub_specs:
        _assert_type_stub_matches_module(module_name, *stub_path_parts)


def test_lazy_facades_are_discoverable_and_resolve_declared_exports():
    modules = [
        "heavenbase",
        "heavenbase.context",
        "heavenbase.ext",
        "heavenbase.backends",
        "heavenbase.backends.elasticsearch",
        "heavenbase.backends.file",
        "heavenbase.backends.inmem",
        "heavenbase.backends.neo4j",
        "heavenbase.backends.redis",
        "heavenbase.backends.sql",
        "heavenbase.backends.sql.strategies",
        "heavenbase.backends.sql.strategies.gram",
        "heavenbase.backends.surreal",
        "heavenbase.backends.vector",
        "heavenbase.benchmark",
        "heavenbase.cli",
        "heavenbase.cli.backends",
        "heavenbase.cli.groups",
        "heavenbase.capabilities",
        "heavenbase.extensions",
        "heavenbase.extensions.agent",
        "heavenbase.extensions.agent.toolkit",
        "heavenbase.extensions.database",
        "heavenbase.extensions.database.toolkit",
        "heavenbase.extensions.memory",
        "heavenbase.extensions.memory.toolkit",
        "heavenbase.extensions.prompt",
        "heavenbase.capsule",
        "heavenbase.toolkit",
        "heavenbase.toolkit.families.workspace",
        "heavenbase.execution.handlers",
        "heavenbase.llm",
        "heavenbase.modules",
        "heavenbase.resources",
        "heavenbase.utils",
        "heavenbase.database",
        "heavenbase.database.dialect",
        "heavenbase.workspace",
        "heavenbase.entity",
        "heavenbase.execution.frame",
        "heavenbase.filters",
        "heavenbase.interop",
        "heavenbase.query",
        "heavenbase.schema",
        "heavenbase.strategies",
        "heavenbase.toolkit.families",
        "heavenbase.types",
        "heavenbase.utils.config",
        "heavenbase.workspace.catalog",
        "heavenbase.workspace.graph",
        "heavenbase.workspace.mcp",
        "heavenbase.workspace.metaschema",
        "heavenbase.workspace.schema",
        "heavenbase.placement",
        "heavenbase.utils.registry",
        "heavenbase.database.resources.sql",
        "heavenbase.extensions.python",
        "heavenbase.extensions.python.toolkit",
        "heavenbase.cli.groups.config",
        "heavenbase.cli.groups.llm",
        "heavenbase.cli.groups.mcp",
        "heavenbase.cli.groups.prompt",
        "heavenbase.cli.groups.ws",
        "heavenbase.backends.elasticsearch.backend",
        "heavenbase.backends.elasticsearch.handler",
        "heavenbase.backends.inmem.handler",
        "heavenbase.backends.mongodb.backend",
        "heavenbase.backends.neo4j.handler",
        "heavenbase.backends.opensearch.backend",
        "heavenbase.backends.opensearch.handler",
        "heavenbase.backends.redis.handler",
        "heavenbase.backends.sql.handler",
        "heavenbase.backends.surreal.handler",
        "heavenbase.backends.tinydb.backend",
    ]

    for module_name in modules:
        module = importlib.import_module(module_name)
        assert set(module.__all__).issubset(dir(module)), module_name
        for name in module.__all__:
            assert getattr(module, name) is not None, f"{module_name}.{name}"


# Submodules that 0.1.2.2 bound as attributes right after ``import <package>``,
# because its ``__init__`` (or its parent package's, for the python toolkit's
# ``tools``) imported them; the facades are lazy now.
_SUBMODULES_BOUND_ON_IMPORT_IN_0_1_2_2 = {
    "heavenbase.backends": ["_construction", "base", "capabilities", "mcp", "strategy"],
    "heavenbase.backends.elasticsearch.backend": ["runtime"],
    "heavenbase.backends.elasticsearch.handler": ["runtime"],
    "heavenbase.backends.inmem.handler": ["runtime"],
    "heavenbase.backends.mongodb.backend": ["runtime"],
    "heavenbase.backends.neo4j.handler": ["runtime"],
    "heavenbase.backends.opensearch.backend": ["runtime"],
    "heavenbase.backends.opensearch.handler": ["runtime"],
    "heavenbase.backends.redis.handler": ["runtime"],
    "heavenbase.backends.sql.handler": ["base", "json", "text"],
    "heavenbase.backends.surreal.handler": ["runtime"],
    "heavenbase.backends.tinydb.backend": ["runtime"],
    "heavenbase.cli": ["base", "context", "output", "registry", "spec"],
    "heavenbase.cli.groups.config": ["group", "maintain", "read", "version", "write"],
    "heavenbase.cli.groups.llm": ["catalog", "chat", "embed", "group", "image", "mcp", "render", "runtime", "session"],
    "heavenbase.cli.groups.mcp": ["commands", "group", "registry"],
    "heavenbase.cli.groups.prompt": ["group", "read", "runtime", "translate", "write"],
    "heavenbase.cli.groups.ws": ["group", "manifest", "presets", "registry", "runtime"],
    "heavenbase.database.resources.sql": ["base", "db", "starrocks"],
    "heavenbase.entity": ["assembly", "base", "computes", "fields", "ref"],
    "heavenbase.execution.frame": ["merge", "result"],
    "heavenbase.extensions": ["base", "ids"],
    "heavenbase.extensions.python": ["guide", "namespace", "runner"],
    "heavenbase.extensions.python.toolkit": ["factory", "tools"],
    "heavenbase.extensions.python.toolkit.tools": ["python_guide", "run_python"],
    "heavenbase.filters": ["ops", "spec"],
    "heavenbase.interop": ["_loaders", "base", "mapping", "rows"],
    "heavenbase.placement": ["binding", "profile", "profiles", "resolver", "rule", "scalar"],
    "heavenbase.query": ["aggregate", "json_path", "spec", "value"],
    "heavenbase.schema": ["model", "text"],
    "heavenbase.strategies": [
        "base",
        "external_ref",
        "graph_edge",
        "inline_column",
        "inverted_index",
        "json_field",
        "registry",
        "side_table",
        "sparse_gram",
        "vector_index",
    ],
    "heavenbase.toolkit.families": ["family", "registry"],
    "heavenbase.types": [
        "array",
        "artifact",
        "base",
        "boolean",
        "categorical",
        "date",
        "descriptor",
        "duration",
        "filtering",
        "float",
        "hyperg",
        "identifier",
        "integer",
        "json",
        "limits",
        "long_text",
        "medium_text",
        "registry",
        "short_text",
        "timestamp",
        "vector",
    ],
    "heavenbase.utils.config": ["data", "engine", "interpolation", "manager", "snapshot", "spec"],
    "heavenbase.utils.registry": ["codec", "errors", "identity", "registry", "store", "targets"],
    "heavenbase.workspace.catalog": ["entity", "rows"],
    "heavenbase.workspace.graph": ["errors"],
    "heavenbase.workspace.mcp": ["service", "skill", "support"],
    "heavenbase.workspace.metaschema": ["entity"],
}

# Submodules that 0.1.2.2 already exported lazily as module attributes; they
# must stay the same submodules.
_LAZY_SUBMODULE_EXPORTS_IN_0_1_2_2 = {
    "heavenbase": ["backends", "capabilities", "ext", "filters"],
    "heavenbase.utils": ["parsers", "strings"],
}

_SUBMODULE_ATTRIBUTES = {**_SUBMODULES_BOUND_ON_IMPORT_IN_0_1_2_2, **_LAZY_SUBMODULE_EXPORTS_IN_0_1_2_2}

_SUBMODULE_ATTRIBUTE_SCRIPT = """
import importlib
import json
import sys
import types

sys.path.insert(0, sys.argv[1])
package_name = sys.argv[2]
names = sys.argv[3:]
package = importlib.import_module(package_name)
listed = set(dir(package))
result = {"unlisted": [name for name in names if name not in listed], "wrong": [], "missing": []}
for name in names:
    try:
        value = getattr(package, name)
    except AttributeError as error:
        cause = error.__cause__
        if isinstance(cause, ModuleNotFoundError) and not (cause.name or "").startswith("heavenbase"):
            result["missing"].append(f"{name} ({cause.name})")
            continue
        raise
    if not isinstance(value, types.ModuleType) or value is not sys.modules.get(f"{package_name}.{name}"):
        result["wrong"].append(name)
print(json.dumps(result))
"""


def _assert_submodule_attributes(source: Path, package_name: str, names: list[str]) -> None:
    out = cmd(
        [sys.executable, "-I", "-c", _SUBMODULE_ATTRIBUTE_SCRIPT, str(source), package_name, *names],
        include="out",
        check=True,
    )

    result = loads_json(out)
    missing = result.pop("missing")
    assert result == {"unlisted": [], "wrong": []}, f"{package_name}: {result}"
    if missing:
        pytest.skip(f"{package_name} needs optional dependencies for {', '.join(missing)}")


@pytest.mark.slow
@pytest.mark.parametrize("package_name", sorted(_SUBMODULE_ATTRIBUTES))
def test_lazy_facades_bind_their_submodules_on_attribute_access(package_name):
    # A fresh interpreter, so nothing else has imported the submodules yet:
    # ``import heavenbase.placement as p; p.scalar`` keeps working as in 0.1.2.2.
    source = Path(__file__).resolve().parents[2] / "src"
    _assert_submodule_attributes(source, package_name, _SUBMODULE_ATTRIBUTES[package_name])


def test_lazy_facade_reports_broken_siblings_when_an_optional_dependency_is_missing(tmp_path):
    package = tmp_path / "test_facade"
    package.mkdir()
    (package / "__init__.py").write_text(
        """\
from heavenbase._lazy import _attach_lazy_exports
import types as wrong_binding

__getattr__, actual_dir = _attach_lazy_exports(__name__, globals(), {}, [])

def __dir__():
    return [name for name in actual_dir() if name != "hidden"]
""",
        encoding="utf-8",
    )
    (package / "hidden.py").touch()
    (package / "available.py").touch()
    (package / "optional.py").write_text("import _heavenbase_test_missing_optional_dependency\n", encoding="utf-8")

    with pytest.raises(pytest.skip.Exception, match=r"optional \(_heavenbase_test_missing_optional_dependency\)"):
        _assert_submodule_attributes(tmp_path, package.name, ["optional", "available"])

    try:
        with pytest.raises(AssertionError) as error:
            _assert_submodule_attributes(tmp_path, package.name, ["optional", "wrong_binding", "hidden", "available"])
    except pytest.skip.Exception:
        pytest.fail("A missing optional dependency hid defects in available sibling exports")
    assert "'unlisted': ['hidden']" in str(error.value)
    assert "'wrong': ['wrong_binding']" in str(error.value)


def test_lazy_facade_attribute_access_does_not_invent_submodules():
    script = """
import sys

sys.path.insert(0, sys.argv[1])
import heavenbase.placement as placement
import heavenbase.utils as utils
import heavenbase.utils.registry as registry
import heavenbase.workspace.mcp as mcp
from heavenbase.workspace.mcp.service import _MCPService

assert callable(placement.index) and placement.index is sys.modules["heavenbase.placement.index"].index
for module, name in ((placement, "missing"), (registry, "missing"), (utils, "Database"), (placement, "__wrapped__")):
    assert not hasattr(module, name), (module.__name__, name)
assert mcp._MCPService is _MCPService
print("ok")
"""
    source = Path(__file__).resolve().parents[2] / "src"

    out = cmd([sys.executable, "-I", "-c", script, str(source)], include="out", check=True)

    assert out == "ok"


def test_filter_expr_from_dict_parses_generic_filters():
    from heavenbase.filters import Expr

    expr = Expr.from_dict({"$and": [{"name": {"$match": "desk"}}, {"price": {"$gte": 10}}]})

    assert expr is not None
    assert expr.to_dict() == {
        "and": [
            {"filter": {"field": "name", "op": "match", "value": "desk"}},
            {"filter": {"field": "price", "op": "gte", "value": 10}},
        ]
    }


def test_system_entities_are_root_exports_not_entity_layer_exports():
    assert hb.Catalog.schema().entity_id == "sys-catalog"
    assert hb.MetaSchema.schema().entity_id == "sys-metaschema"
    assert "Catalog" not in entity_api.__all__
    assert "MetaSchema" not in entity_api.__all__


def test_extension_exports_are_developer_facing():
    from heavenbase.backends.sql.dialect import SQLDialectSpec as SQLDialectSpecContract
    from heavenbase.database.dialect.spec import DatabaseDialectSpec as DatabaseDialectSpecContract

    assert ext.__all__ == EXT_EXPORTS
    assert hb.ext.TagView is ext.TagView
    assert hb.ext.TagDefinition is ext.TagDefinition
    assert hb.ext.ResolvedTagValue is ext.ResolvedTagValue
    assert hb.ext.BackendMCP is ext.BackendMCP
    assert hb.ext.BackendMCPProfile is ext.BackendMCPProfile
    assert hb.ext.BackendMCPTool is ext.BackendMCPTool
    assert hb.ext.QueryFragment is ext.QueryFragment
    assert hb.ext.HandlerContext is ext.HandlerContext
    assert hb.ext.Predicate is ext.Predicate
    assert hb.ext.StrategyAdapter is ext.StrategyAdapter
    assert hb.ext.llm_schema_text is ext.llm_schema_text
    assert hb.ext.resolve_schema_text is ext.resolve_schema_text
    assert hb.ext.profile_spec_from_mapping is ext.profile_spec_from_mapping
    assert hb.ext.registered_profile_tool_names is ext.registered_profile_tool_names
    assert hb.ext.WorkspaceToolkitFamilySpec is ext.WorkspaceToolkitFamilySpec
    assert hb.ext.register_workspace_toolkit_family is ext.register_workspace_toolkit_family
    assert hb.ext.registered_workspace_toolkit_families is ext.registered_workspace_toolkit_families
    assert hb.ext.registered_workspace_tool_names is ext.registered_workspace_tool_names
    assert ext.SQLDialectSpec is SQLDialectSpecContract
    assert hb.ext.SQLDialectSpec is ext.SQLDialectSpec
    assert hb.backends.SQLDialectSpec is SQLDialectSpecContract
    assert hb.backends.sql.SQLDialectSpec is SQLDialectSpecContract
    assert hb.ext.register_sql_dialect is ext.register_sql_dialect
    assert hb.ext.registered_sql_dialects is ext.registered_sql_dialects
    assert hb.ext.sql_dialect_spec is ext.sql_dialect_spec
    assert hb.ext.SQL_JSON_AGG_STYLE_ARRAYAGG == "json_arrayagg"
    assert ext.DatabaseDialectSpec is DatabaseDialectSpecContract
    assert hb.ext.DatabaseDialectSpec is ext.DatabaseDialectSpec
    assert hb.ext.database_dialects is ext.database_dialects
    assert hb.ext.ToolSerializer is ext.ToolSerializer
    assert hb.ext.register_capsule_layer is ext.register_capsule_layer
    assert hb.ext.registered_capsule_layers is ext.registered_capsule_layers


def test_registry_convergence_has_no_replaced_authorities_or_provider_selectors():
    """Central composition surfaces retain no HB-001 authority or provider inventory."""
    backend_registry = importlib.import_module("heavenbase.backends.registry")
    extension_registry = importlib.import_module("heavenbase.extensions.registry")
    replaced_backend_names = {
        "_BACKEND_BUILDERS",
        "_BACKEND_TYPES",
        "_BUILTIN_BACKEND_MODULES",
        "get_backend_builder",
        "register_backend_builder",
        "register_backend_class",
        "registered_backend_identifiers",
    }
    replaced_extension_names = {
        "ExtensionRegistry",
        "extension_registry",
        "get_extension",
        "load_extension",
        "register_extension",
        "registered_extensions",
    }

    assert replaced_backend_names.isdisjoint(vars(backend_registry))
    assert replaced_extension_names.isdisjoint(vars(extension_registry))

    provider_identifiers = {
        "chroma",
        "clickhouse",
        "duckdb",
        "elasticsearch",
        "lance",
        "milvus",
        "mssql",
        "mysql",
        "neo4j",
        "oracle",
        "pgvector",
        "pinecone",
        "postgres",
        "redis",
        "redisearch",
        "sqlite",
        "starrocks",
        "surreal",
        "trino",
    }
    composition_paths = (
        ("src", "heavenbase", "backends", "_construction.py"),
        ("src", "heavenbase", "execution", "handlers", "seed.py"),
        ("src", "heavenbase", "placement", "resolver.py"),
        ("src", "heavenbase", "workspace", "query", "aggregate.py"),
        ("src", "heavenbase", "workspace", "setup", "backends.py"),
        ("src", "heavenbase", "workspace", "query", "filter.py"),
    )
    for path_parts in composition_paths:
        tree = ast.parse(load_txt(pj(*path_parts)))
        literals = {node.value.lower() for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        assert provider_identifiers.isdisjoint(literals), pj(*path_parts)


def test_handler_registers_on_detached_workspace():
    class HandlerNote(hb.Entity):
        identifier = "public-handler-note"

        title = hb.field(hb.ShortText)

    def compile_endswith(field_schema, value, ctx):
        def payload(row):
            return str(row.get(field_schema.name, "")).endswith(str(value))

        return hb.ext.QueryFragment(ctx.backend, "ends_with_public", field_schema.name, payload)

    ws = hb.HeavenBase("public-handler-test", detached=True, backends={"main": {"type": "inmem"}})
    hb.ext.register_op("ends_with_public")
    hb.ext.Handler(hb.ShortText, "ends_with_public", "inmem", hb.InlineColumn, compile_endswith).register(ws.handlers)
    ws.register(HandlerNote)
    ws.upsert(HandlerNote.from_dict({"object_id": "n1", "title": "Search Index"}))

    frame = ws.query(HandlerNote).where(HandlerNote.title.op("ends_with_public", "Index")).execute()

    assert frame.ids == ["n1"]


def test_capsule_entrypoint_hides_legacy_callable_serialization_helpers():
    hidden = [
        "CallableSerialization",
        "CaptureOptions",
        "callable_ref",
        "capture_function",
        "deserialize_callable",
        "restore_callable",
        "serialize_callable",
        "verify_manifest",
    ]
    assert [name for name in hidden if hasattr(capsule, name)] == []
