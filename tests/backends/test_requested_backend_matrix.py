"""Acceptance contracts for the complete requested backend selector matrix."""

from __future__ import annotations

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast


@pytest.mark.parametrize(
    ("selector", "canonical"),
    (
        ("sqlite", "sqlite"),
        ("postgres", "postgres"),
        ("oracle", "oracle"),
        ("mysql", "mysql"),
        ("sqlserver", "mssql"),
        ("mssql", "mssql"),
        ("oceanbase", "oceanbase"),
        ("seekdb", "seekdb"),
        ("duckdb", "duckdb"),
        ("starrocks", "starrocks"),
        ("supabase", "supabase"),
        ("trino", "trino"),
        ("surrealdb", "surreal"),
        ("neo4j", "neo4j"),
        ("elasticsearch", "elasticsearch"),
        ("opensearch", "opensearch"),
        ("milvus", "milvus"),
        ("milvus-lite", "milvus-lite"),
        ("milvus_lite", "milvus-lite"),
        ("lancedb", "lance"),
        ("chroma", "chroma"),
        ("pinecone", "pinecone"),
        ("qdrant", "qdrant"),
        ("weaviate", "weaviate"),
        ("pgvector", "pgvector"),
        ("redis", "redis"),
        ("redisearch", "redisearch"),
        ("mongodb", "mongodb"),
        ("tinydb", "tinydb"),
        ("clickhouse", "clickhouse"),
        ("dolt", "dolt"),
    ),
)
def test_requested_backend_selector_resolves(selector: str, canonical: str) -> None:
    """Every documented product name resolves through Registry authority."""
    assert hb.BackendType.identifier_of(selector) == canonical


def test_new_provider_classes_expose_their_canonical_identifiers() -> None:
    """Public provider classes retain the selector identity declared by their module."""
    expected = {
        "ClickHouseBackend": "clickhouse",
        "DoltBackend": "dolt",
        "MilvusLiteBackend": "milvus-lite",
        "MongoDBBackend": "mongodb",
        "OpenSearchBackend": "opensearch",
        "QdrantBackend": "qdrant",
        "SeekDBBackend": "seekdb",
        "SupabaseBackend": "supabase",
        "TinyDBBackend": "tinydb",
        "WeaviateBackend": "weaviate",
    }

    for export_name, identifier in expected.items():
        backend_type = getattr(hb.backends, export_name)
        assert backend_type.identifier == identifier
