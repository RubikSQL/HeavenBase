"""Independent business examples exercise real split-storage cached and live queries."""

import json
from statistics import median
from time import perf_counter

import heavenbase as hb
import pytest

from heavenbase.types import cosine_score, euclidean_score, inner_product_score

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.mark.parametrize("provider", ["chroma", "milvus-lite"])
@pytest.mark.parametrize(
    "scenario,metric,scorer",
    [
        ("product-catalog", "cosine", cosine_score),
        ("support-tickets", "l2", euclidean_score),
        ("knowledge-articles", "ip", inner_product_score),
    ],
)
def test_business_examples_match_scalar_reference_and_refresh(tmp_path, provider, scenario, metric, scorer):
    if provider == "milvus-lite":
        pytest.importorskip("pymilvus")
        pytest.importorskip("milvus_lite")
    else:
        pytest.importorskip("chromadb")

    class BusinessDoc(hb.Entity):
        tenant = hb.field(hb.ShortText).store(to="main")
        status = hb.field(hb.ShortText).store(to="main")
        title = hb.field(hb.ShortText).store(to="main")
        embedding = hb.field(hb.Vector[3]).required().store(to="vec")

    config = (
        {"type": provider, "uri": hb.utils.path_to_file_uri(tmp_path / "vectors.db")}
        if provider == "milvus-lite"
        else {"type": "chroma", "path": hb.utils.path_to_file_uri(tmp_path / "chroma")}
    )
    ws = hb.HeavenBase(
        scenario,
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "rows.db")},
            "vec": config,
        },
    )
    rows = [
        {"object_id": "a", "tenant": "acme", "status": "active", "title": scenario + " primary", "embedding": [1.0, 0.0, 0.0]},
        {"object_id": "b", "tenant": "acme", "status": "active", "title": scenario + " alternative", "embedding": [0.0, 1.0, 0.0]},
        {"object_id": "tie", "tenant": "acme", "status": "active", "title": scenario + " duplicate vector", "embedding": [1.0, 0.0, 0.0]},
        {"object_id": "private", "tenant": "other", "status": "active", "title": "other tenant", "embedding": [2.0, 0.0, 0.0]},
        {"object_id": "archived", "tenant": "acme", "status": "archived", "title": "retired item", "embedding": [3.0, 0.0, 0.0]},
    ]
    vector = [1.0, 0.0, 0.0]

    def run(version):
        return (
            ws.query(BusinessDoc)
            .where({"tenant": "acme", "status": "active"})
            .near("embedding", vector, top_k=3, metric=metric, exact=True, cache_version=version)
            .select("object_id", "title", "score")
            .execute()
            .rows()
        )

    try:
        ws.register(BusinessDoc)
        ws.upsert_many(BusinessDoc, rows)
        expected = sorted(
            (row for row in rows if row["tenant"] == "acme" and row["status"] == "active"),
            key=lambda row: (-scorer(row["embedding"], vector), row["object_id"]),
        )
        for version in (None, "published-v1", "published-v1"):
            actual = run(version)
            assert [row["object_id"] for row in actual] == [row["object_id"] for row in expected]
            assert [row["score"] for row in actual] == pytest.approx([scorer(row["embedding"], vector) for row in expected], abs=1e-12)
            assert all("embedding" not in row for row in actual)
        times = {}
        for name, version in (("live_ms", None), ("cached_ms", "published-v1")):
            samples = []
            for _ in range(5):
                start = perf_counter()
                run(version)
                samples.append((perf_counter() - start) * 1000)
            times[name] = median(samples)
        ws.set("a", {"status": "archived"}, entity=BusinessDoc)
        assert [row["object_id"] for row in run("published-v1")] == ["tie", "b"]
        ws.refresh()
        assert run("published-v2") == run(None)
        print(json.dumps({"scenario": scenario, "backend": provider, "expected_ids": [row["object_id"] for row in expected], **times}, sort_keys=True))
    finally:
        ws.release()
