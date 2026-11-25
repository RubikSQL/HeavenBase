"""Real SQL exact-query memory scales with batches, not candidate count."""

import gc
import json
import tracemalloc
from time import perf_counter

import heavenbase as hb
import pytest

from heavenbase.execution.contracts import RowOp

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.mark.slow
def test_exact_sql_peak_memory_is_bounded(tmp_path):
    class MemoryDoc(hb.Entity):
        scope = hb.field(hb.ShortText)
        payload = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[64]).store(to="main")

    ws = hb.HeavenBase(
        "bounded-near-memory", detached=True, backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "memory.db")}}
    )
    measurements = []
    try:
        ws.register(MemoryDoc)
        backend = ws.backends.get("main")
        q = ws.query(MemoryDoc).where({"scope": "own"}).near("embedding", [1.0] * 64, top_k=10, exact=True).select("object_id", "score")
        previous = 0
        for count in (1024, 8192):
            for start in range(previous, count, 256):
                backend.upsert(
                    [
                        RowOp(MemoryDoc.schema().entity_id, f"r{i:06}", {"scope": "own", "payload": "p" * 512, "embedding": [float(i % 7)] * 64})
                        for i in range(start, min(start + 256, count))
                    ]
                )
            previous = count
            q.execute()  # exclude import/config warmup from peak measurements
            gc.collect()
            tracemalloc.start()
            started = perf_counter()
            hits = q.execute().rows()
            elapsed = perf_counter() - started
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            assert len(hits) == 10
            measurements.append({"rows": count, "peak_bytes": peak, "traced_seconds": elapsed})
        # 8x data growth must not become 8x Python working-memory growth.
        assert measurements[1]["peak_bytes"] < max(4_000_000, measurements[0]["peak_bytes"] * 2)
        assert ws._router._near.cache.bytes == 0
        print(json.dumps({"exact_stream_memory": measurements}, sort_keys=True))
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        ws.release()
