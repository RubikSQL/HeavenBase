# HeavenBase Benchmark Report

> **STALE**: this report predates the `0.1.2.1` train. It was produced on
> 2026-05-08 on a different machine (Windows/AMD64) and has not been
> regenerated since, because repo policy commits an updated `BENCHMARK.md`
> only when fresh results are part of the change. Two figures below are known
> to be wrong in a way that has since been fixed: a stage a failed repeat never
> executed was averaged in as `0.000`, making the failure look fast, and its
> bucket total silently omitted the operation. Read the numbers as a historical
> record of that run, not as current performance.
>
> Regenerate with `bash scripts/benchmark.bash` on the machine whose baseline
> you intend to compare against.

- **Generated**: 2026-05-08 10:24:46
- **Scenario**: product
- **System**: platform=Windows, machine=AMD64, processor=Intel64 Family 6 Model 198 Stepping 2, GenuineIntel, python=3.12.13, ram_gb=63.4
- **Scales**: 1x, 10x
- **Repeats per scale**: 5
- **Seed**: 42
- **Wall time**: 2387.5s
- **Stage errors**: 90

## Summary

| Combination | Type | Scale | Rows | Repeats | Build (s) | Query (s) | Edit (s) | Removal (s) | Mem Build Delta (MB) | Disk Build Delta (MB) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| chroma_all | pure-vector | 1x | 1,000 | 5 | 1.003 +/- 0.024 | 1.883 +/- 0.017 | 1.138 +/- 0.010 | 0.254 +/- 0.011 | 10.1 | 9.988 |
| chroma_all | pure-vector | 10x | 10,000 | 5 | 7.078 +/- 0.383 | 10.228 +/- 0.196 | 1.647 +/- 0.126 | 1.644 +/- 0.061 | 65.1 | 58.5 |
| inmem_all | pure-inmem | 1x | 1,000 | 5 | 0.133 +/- 0.012 | 0.362 +/- 0.004 | 0.089 +/- 0.004 | 0.010 +/- 0.002 | 19.1 | 0.000 |
| inmem_all | pure-inmem | 10x | 10,000 | 5 | 0.593 +/- 0.022 | 3.331 +/- 0.048 | 0.091 +/- 0.003 | 0.079 +/- 0.007 | 100.1 | 0.000 |
| inmem_sqlite_vec | row+sql-vector | 1x | 1,000 | 5 | 1.535 +/- 0.014 | 4.029 +/- 0.022 | 1.324 +/- 0.019 | 0.039 +/- 0.002 | 6.227 | 33.0 |
| inmem_sqlite_vec | row+sql-vector | 10x | 10,000 | 5 | 10.095 +/- 0.253 | 34.982 +/- 0.383 | 1.383 +/- 0.094 | 2.844 +/- 0.073 | 34.6 | 261.8 |
| lance_all | pure-vector | 1x | 1,000 | 5 | 0.450 +/- 0.014 | 3.660 +/- 0.039 | 0.186 +/- 0.020 | 0.077 +/- 0.004 | 1.643 | 4.067 |
| lance_all | pure-vector | 10x | 10,000 | 5 | 1.116 +/- 0.041 | 4.905 +/- 0.091 | 0.186 +/- 0.010 | 0.315 +/- 0.009 | 7.562 | 40.3 |
| milvus_all | pure-vector | 1x | 1,000 | 5 | 1.803 +/- 0.276 | 2.900 +/- 0.236 | 0.236 +/- 0.042 | 0.096 +/- 0.035 | -0.500 | 0.000 |
| milvus_all | pure-vector | 10x | 10,000 | 5 | 2.607 +/- 0.281 | 7.872 +/- 0.100 | 0.257 +/- 0.014 | 0.722 +/- 0.022 | -0.343 | 0.000 |
| mysql_all | pure-sql | 1x | 1,000 | 5 | 4.231 +/- 0.180 | 5.525 +/- 0.130 | 2.497 +/- 0.082 | 0.081 +/- 0.003 | 14.4 | 0.000 |
| mysql_all | pure-sql | 10x | 10,000 | 5 | 21.601 +/- 0.495 | 61.329 +/- 0.687 | 2.590 +/- 0.118 | 5.223 +/- 0.181 | 55.3 | 0.000 |
| pgvector_all | pure-sql-vector | 1x | 1,000 | 5 | 3.209 +/- 0.088 | 3.121 +/- 0.071 | 2.114 +/- 0.066 | 0.067 +/- 0.003 | 3.015 | 0.000 |
| pgvector_all | pure-sql-vector | 10x | 10,000 | 5 | 20.449 +/- 0.795 | 15.127 +/- 0.202 | 2.098 +/- 0.068 | 1.452 +/- 0.018 | 40.2 | 0.000 |
| sqlite_all | pure-sql | 1x | 1,000 | 5 | 1.646 +/- 0.060 | 4.238 +/- 0.058 | 1.376 +/- 0.122 | 0.042 +/- 0.007 | 9.366 | 33.1 |
| sqlite_all | pure-sql | 10x | 10,000 | 5 | 9.692 +/- 0.716 | 33.406 +/- 0.361 | 1.440 +/- 0.021 | 2.868 +/- 0.035 | 33.7 | 263.1 |
| sqlite_chroma_vec | sql+vector | 1x | 1,000 | 5 | 1.581 +/- 0.306 | 2.514 +/- 0.031 | 0.976 +/- 0.016 | 0.205 +/- 0.016 | 6.189 | 18.7 |
| sqlite_chroma_vec | sql+vector | 10x | 10,000 | 5 | 6.009 +/- 0.136 | 11.601 +/- 0.130 | 1.420 +/- 0.054 | 1.308 +/- 0.010 | 85.4 | 60.5 |
| sqlite_inmem_vec | sql+vector | 1x | 1,000 | 5 | 0.674 +/- 0.014 | 0.797 +/- 0.015 | 0.129 +/- 0.004 | 0.020 +/- 0.001 | 4.497 | 9.687 |
| sqlite_inmem_vec | sql+vector | 10x | 10,000 | 5 | 1.342 +/- 0.023 | 4.872 +/- 0.090 | 0.147 +/- 0.007 | 0.165 +/- 0.007 | 87.1 | 11.6 |
| sqlite_lance_vec | sql+vector | 1x | 1,000 | 5 | 1.213 +/- 0.611 | 3.800 +/- 0.106 | 0.201 +/- 0.010 | 0.081 +/- 0.012 | 14.9 | 13.7 |
| sqlite_lance_vec | sql+vector | 10x | 10,000 | 5 | 2.022 +/- 0.094 | 7.074 +/- 0.142 | 0.246 +/- 0.012 | 0.412 +/- 0.008 | 15.2 | 51.1 |
| sqlite_milvus_vec | sql+vector | 1x | 1,000 | 5 | 2.346 +/- 0.270 | 3.889 +/- 0.164 | 0.282 +/- 0.025 | 0.136 +/- 0.031 | 0.505 | 9.656 |
| sqlite_milvus_vec | sql+vector | 10x | 10,000 | 5 | 3.496 +/- 0.135 | 11.167 +/- 0.205 | 0.306 +/- 0.029 | 0.930 +/- 0.131 | 2.682 | 11.6 |

---

## chroma_all (`pure-vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 4.278 +/- 0.033s
- **Backends**: main=inmem, vec=chroma

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,244 | 0.000 |  |
| after_build | 1,254 | 9.988 |  |
| after_edit | 1,265 | 10.3 |  |
| after_removal | 1,265 | 10.2 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.183 | 0.003 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.820 | 0.024 | 1,000 | 1,220 r/s | 0.820 ms | upsert_many batch=100 |
| query | batch_get_many | 0.121 | 0.002 | 1,000 | 8,276 r/s | 0.121 ms | get_many (1000) |
| query | batch_exists_many | 0.006 | 0.000 | 1,000 | 170,480 r/s | 0.006 ms | exists_many (1000) |
| query | point_lookups | 0.197 | 0.005 | 100 | 506.8 r/s | 1.973 ms | get sample=100 |
| query | point_get_entity | 0.188 | 0.007 | 100 | 530.9 r/s | 1.884 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.168 | 0.010 | 100 | 597.0 r/s | 1.675 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.191 | 0.003 | 100 | 522.7 r/s | 1.913 ms | ws[id] sample=100 |
| query | operator_contains | 0.160 | 0.006 | 100 | 626.6 r/s | 1.596 ms | id in ws sample=100 |
| query | workspace_count | 0.002 | 0.000 | 1,000 | 436,319 r/s | 0.002 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 1,000 | 476,104 r/s | 0.002 ms | len(ws) |
| query | iter_workspace | 0.117 | 0.002 | 1,000 | 8,535 r/s | 0.117 ms | list(iter(ws)) |
| query | ids_entity | 0.114 | 0.001 | 1,000 | 8,766 r/s | 0.114 ms | ws.ids(entity) |
| query | rows_entity | 0.118 | 0.004 | 1,000 | 8,440 r/s | 0.118 ms | ws.rows(entity) |
| query | items_entity | 0.114 | 0.003 | 1,000 | 8,759 r/s | 0.114 ms | ws.items(entity) |
| query | filter_gt | 0.087 | 0.002 | 1,000 | 11,523 r/s | 0.087 ms | price > 50 |
| query | filter_range | 0.049 | 0.000 | 1,000 | 20,283 r/s | 0.049 ms | price in [50,150] |
| query | filter_text | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Chroma cannot execute fragment 'match'; r2 NotImplementedError: NotImplementedError: Chroma cannot execute fragment 'match'; r3 NotImplementedError: NotImplementedError: Chroma cannot execute fragment 'match'; +2 more |
| query | filter_bool_tree | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; +2 more |
| query | complex_query | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; +2 more |
| query | vector_near | 0.122 | 0.003 | 1,000 | 8,182 r/s | 0.122 ms | near top_k=10 |
| query | hybrid_query | 0.009 | 0.000 | 1,000 | 109,438 r/s | 0.009 ms | filter + near top_k=10 |
| query | count_all | 0.116 | 0.005 | 1,000 | 8,604 r/s | 0.116 ms | count |
| edit | batch_set_many | 1.138 | 0.010 | 1,000 | 878.8 r/s | 1.138 ms | set_many (1000) |
| removal | batch_delete_many | 0.252 | 0.011 | 1,000 | 3,972 r/s | 0.252 ms | delete_many (1000) |
| removal | count_after_delete | 0.002 | 0.000 | 1,000 | 441,630 r/s | 0.002 ms | count after delete |

---

## chroma_all (`pure-vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 20.597 +/- 0.452s
- **Backends**: main=inmem, vec=chroma

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,534 | 0.000 |  |
| after_build | 1,599 | 58.5 |  |
| after_edit | 1,689 | 58.8 |  |
| after_removal | 1,689 | 58.8 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.170 | 0.007 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 6.908 | 0.383 | 10,000 | 1,448 r/s | 0.691 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.122 | 0.010 | 1,000 | 8,193 r/s | 0.122 ms | get_many (1000) |
| query | batch_exists_many | 0.007 | 0.001 | 1,000 | 150,735 r/s | 0.007 ms | exists_many (1000) |
| query | point_lookups | 0.190 | 0.026 | 100 | 525.0 r/s | 1.905 ms | get sample=100 |
| query | point_get_entity | 0.189 | 0.027 | 100 | 528.1 r/s | 1.893 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.157 | 0.015 | 100 | 637.3 r/s | 1.569 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.183 | 0.019 | 100 | 545.0 r/s | 1.835 ms | ws[id] sample=100 |
| query | operator_contains | 0.159 | 0.014 | 100 | 629.3 r/s | 1.589 ms | id in ws sample=100 |
| query | workspace_count | 0.003 | 0.001 | 10,000 | 3,552,070 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.003 | 0.000 | 10,000 | 3,895,689 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 1.289 | 0.056 | 10,000 | 7,758 r/s | 0.129 ms | list(iter(ws)) |
| query | ids_entity | 1.250 | 0.042 | 10,000 | 7,997 r/s | 0.125 ms | ws.ids(entity) |
| query | rows_entity | 1.258 | 0.038 | 10,000 | 7,949 r/s | 0.126 ms | ws.rows(entity) |
| query | items_entity | 1.301 | 0.069 | 10,000 | 7,684 r/s | 0.130 ms | ws.items(entity) |
| query | filter_gt | 0.983 | 0.071 | 10,000 | 10,176 r/s | 0.098 ms | price > 50 |
| query | filter_range | 0.516 | 0.034 | 10,000 | 19,373 r/s | 0.052 ms | price in [50,150] |
| query | filter_text | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Chroma cannot execute fragment 'match'; r2 NotImplementedError: NotImplementedError: Chroma cannot execute fragment 'match'; r3 NotImplementedError: NotImplementedError: Chroma cannot execute fragment 'match'; +2 more |
| query | filter_bool_tree | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; +2 more |
| query | complex_query | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: Chroma cannot execute non-native filter fragment 'and'; +2 more |
| query | vector_near | 1.282 | 0.101 | 10,000 | 7,798 r/s | 0.128 ms | near top_k=10 |
| query | hybrid_query | 0.031 | 0.004 | 10,000 | 318,459 r/s | 0.003 ms | filter + near top_k=10 |
| query | count_all | 1.303 | 0.092 | 10,000 | 7,673 r/s | 0.130 ms | count |
| edit | batch_set_many | 1.647 | 0.126 | 1,000 | 607.2 r/s | 1.647 ms | set_many (1000) |
| removal | batch_delete_many | 0.482 | 0.043 | 1,000 | 2,073 r/s | 0.482 ms | delete_many (1000) |
| removal | count_after_delete | 1.161 | 0.043 | 10,000 | 8,610 r/s | 0.116 ms | count after delete |

---

## inmem_all (`pure-inmem`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 0.594 +/- 0.013s
- **Backends**: main=inmem

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 122.6 | 0.000 |  |
| after_build | 141.6 | 0.000 |  |
| after_edit | 143.1 | 0.000 |  |
| after_removal | 133.6 | 0.000 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.093 | 0.011 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.040 | 0.002 | 1,000 | 24,734 r/s | 0.040 ms | upsert_many batch=100 |
| query | batch_get_many | 0.005 | 0.001 | 1,000 | 193,211 r/s | 0.005 ms | get_many (1000) |
| query | batch_exists_many | 0.001 | 0.000 | 1,000 | 1,090,013 r/s | 0.001 ms | exists_many (1000) |
| query | point_lookups | 0.001 | 0.000 | 100 | 74,155 r/s | 0.013 ms | get sample=100 |
| query | point_get_entity | 0.001 | 0.000 | 100 | 96,775 r/s | 0.010 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.001 | 0.000 | 100 | 182,123 r/s | 0.005 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.001 | 0.000 | 100 | 67,250 r/s | 0.015 ms | ws[id] sample=100 |
| query | operator_contains | 0.001 | 0.000 | 100 | 121,672 r/s | 0.008 ms | id in ws sample=100 |
| query | workspace_count | 0.000 | 0.000 | 1,000 | 17,035,775 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.000 | 0.000 | 1,000 | 14,192,450 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 0.005 | 0.001 | 1,000 | 216,146 r/s | 0.005 ms | list(iter(ws)) |
| query | ids_entity | 0.004 | 0.001 | 1,000 | 230,626 r/s | 0.004 ms | ws.ids(entity) |
| query | rows_entity | 0.005 | 0.001 | 1,000 | 201,641 r/s | 0.005 ms | ws.rows(entity) |
| query | items_entity | 0.006 | 0.001 | 1,000 | 177,649 r/s | 0.006 ms | ws.items(entity) |
| query | filter_gt | 0.005 | 0.000 | 1,000 | 201,908 r/s | 0.005 ms | price > 50 |
| query | filter_range | 0.006 | 0.001 | 1,000 | 175,063 r/s | 0.006 ms | price in [50,150] |
| query | filter_text | 0.003 | 0.000 | 1,000 | 380,214 r/s | 0.003 ms | body.match(audio) |
| query | filter_bool_tree | 0.010 | 0.001 | 1,000 | 96,740 r/s | 0.010 ms | bool tree |
| query | complex_query | 0.020 | 0.001 | 1,000 | 51,126 r/s | 0.020 ms | 10+ filter complex |
| query | vector_near | 0.161 | 0.003 | 1,000 | 6,208 r/s | 0.161 ms | near top_k=10 |
| query | hybrid_query | 0.121 | 0.001 | 1,000 | 8,286 r/s | 0.121 ms | filter + near top_k=10 |
| query | count_all | 0.006 | 0.000 | 1,000 | 171,970 r/s | 0.006 ms | count |
| edit | batch_set_many | 0.089 | 0.004 | 1,000 | 11,286 r/s | 0.089 ms | set_many (1000) |
| removal | batch_delete_many | 0.010 | 0.002 | 1,000 | 97,763 r/s | 0.010 ms | delete_many (1000) |
| removal | count_after_delete | 0.000 | 0.000 | 1,000 | 12,397,719 r/s | 0.000 ms | count after delete |

---

## inmem_all (`pure-inmem`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 4.093 +/- 0.053s
- **Backends**: main=inmem

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 482.1 | 0.000 |  |
| after_build | 582.2 | 0.000 |  |
| after_edit | 598.1 | 0.000 |  |
| after_removal | 598.7 | 0.000 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.103 | 0.005 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.490 | 0.022 | 10,000 | 20,394 r/s | 0.049 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.006 | 0.001 | 1,000 | 157,133 r/s | 0.006 ms | get_many (1000) |
| query | batch_exists_many | 0.001 | 0.000 | 1,000 | 747,943 r/s | 0.001 ms | exists_many (1000) |
| query | point_lookups | 0.002 | 0.000 | 100 | 60,070 r/s | 0.017 ms | get sample=100 |
| query | point_get_entity | 0.001 | 0.000 | 100 | 79,655 r/s | 0.013 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.001 | 0.000 | 100 | 154,212 r/s | 0.006 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.002 | 0.000 | 100 | 59,474 r/s | 0.017 ms | ws[id] sample=100 |
| query | operator_contains | 0.001 | 0.000 | 100 | 105,214 r/s | 0.010 ms | id in ws sample=100 |
| query | workspace_count | 0.000 | 0.000 | 10,000 | 154,273,373 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.000 | 0.000 | 10,000 | 134,989,201 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 0.058 | 0.006 | 10,000 | 173,617 r/s | 0.006 ms | list(iter(ws)) |
| query | ids_entity | 0.054 | 0.005 | 10,000 | 184,232 r/s | 0.005 ms | ws.ids(entity) |
| query | rows_entity | 0.061 | 0.006 | 10,000 | 164,178 r/s | 0.006 ms | ws.rows(entity) |
| query | items_entity | 0.067 | 0.007 | 10,000 | 149,624 r/s | 0.007 ms | ws.items(entity) |
| query | filter_gt | 0.048 | 0.010 | 10,000 | 208,974 r/s | 0.005 ms | price > 50 |
| query | filter_range | 0.041 | 0.005 | 10,000 | 244,999 r/s | 0.004 ms | price in [50,150] |
| query | filter_text | 0.015 | 0.003 | 10,000 | 659,742 r/s | 0.002 ms | body.match(audio) |
| query | filter_bool_tree | 0.079 | 0.009 | 10,000 | 127,338 r/s | 0.008 ms | bool tree |
| query | complex_query | 0.074 | 0.004 | 10,000 | 135,109 r/s | 0.007 ms | 10+ filter complex |
| query | vector_near | 1.581 | 0.033 | 10,000 | 6,325 r/s | 0.158 ms | near top_k=10 |
| query | hybrid_query | 1.174 | 0.028 | 10,000 | 8,517 r/s | 0.117 ms | filter + near top_k=10 |
| query | count_all | 0.065 | 0.009 | 10,000 | 153,336 r/s | 0.007 ms | count |
| edit | batch_set_many | 0.091 | 0.003 | 1,000 | 10,974 r/s | 0.091 ms | set_many (1000) |
| removal | batch_delete_many | 0.011 | 0.001 | 1,000 | 90,549 r/s | 0.011 ms | delete_many (1000) |
| removal | count_after_delete | 0.067 | 0.007 | 10,000 | 148,183 r/s | 0.007 ms | count after delete |

---

## inmem_sqlite_vec (`row+sql-vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 6.927 +/- 0.032s
- **Backends**: main=inmem, vec=sqlite

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 412.1 | 0.000 |  |
| after_build | 418.3 | 33.0 |  |
| after_edit | 426.7 | 50.2 |  |
| after_removal | 426.8 | 50.2 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.608 | 0.013 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.927 | 0.004 | 1,000 | 1,079 r/s | 0.927 ms | upsert_many batch=100 |
| query | batch_get_many | 0.298 | 0.005 | 1,000 | 3,357 r/s | 0.298 ms | get_many (1000) |
| query | batch_exists_many | 0.005 | 0.001 | 1,000 | 197,995 r/s | 0.005 ms | exists_many (1000) |
| query | point_lookups | 0.112 | 0.005 | 100 | 894.8 r/s | 1.118 ms | get sample=100 |
| query | point_get_entity | 0.059 | 0.004 | 100 | 1,697 r/s | 0.589 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.021 | 0.001 | 100 | 4,713 r/s | 0.212 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.110 | 0.002 | 100 | 906.8 r/s | 1.103 ms | ws[id] sample=100 |
| query | operator_contains | 0.057 | 0.003 | 100 | 1,749 r/s | 0.572 ms | id in ws sample=100 |
| query | workspace_count | 0.001 | 0.000 | 1,000 | 1,235,453 r/s | 0.001 ms | ws.count(entity) |
| query | workspace_len | 0.001 | 0.000 | 1,000 | 1,693,136 r/s | 0.001 ms | len(ws) |
| query | iter_workspace | 0.296 | 0.010 | 1,000 | 3,374 r/s | 0.296 ms | list(iter(ws)) |
| query | ids_entity | 0.289 | 0.003 | 1,000 | 3,459 r/s | 0.289 ms | ws.ids(entity) |
| query | rows_entity | 0.291 | 0.006 | 1,000 | 3,435 r/s | 0.291 ms | ws.rows(entity) |
| query | items_entity | 0.291 | 0.003 | 1,000 | 3,434 r/s | 0.291 ms | ws.items(entity) |
| query | filter_gt | 0.220 | 0.004 | 1,000 | 4,538 r/s | 0.220 ms | price > 50 |
| query | filter_range | 0.122 | 0.003 | 1,000 | 8,208 r/s | 0.122 ms | price in [50,150] |
| query | filter_text | 0.040 | 0.001 | 1,000 | 25,151 r/s | 0.040 ms | body.match(audio) |
| query | filter_bool_tree | 0.315 | 0.011 | 1,000 | 3,176 r/s | 0.315 ms | bool tree |
| query | complex_query | 0.020 | 0.001 | 1,000 | 50,261 r/s | 0.020 ms | 10+ filter complex |
| query | vector_near | 0.748 | 0.007 | 1,000 | 1,336 r/s | 0.748 ms | near top_k=10 |
| query | hybrid_query | 0.442 | 0.005 | 1,000 | 2,261 r/s | 0.442 ms | filter + near top_k=10 |
| query | count_all | 0.290 | 0.005 | 1,000 | 3,452 r/s | 0.290 ms | count |
| edit | batch_set_many | 1.324 | 0.019 | 1,000 | 755.0 r/s | 1.324 ms | set_many (1000) |
| removal | batch_delete_many | 0.039 | 0.002 | 1,000 | 25,930 r/s | 0.039 ms | delete_many (1000) |
| removal | count_after_delete | 0.000 | 0.000 | 1,000 | 2,144,726 r/s | 0.000 ms | count after delete |

---

## inmem_sqlite_vec (`row+sql-vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 49.303 +/- 0.474s
- **Backends**: main=inmem, vec=sqlite

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 530.9 | 0.000 |  |
| after_build | 565.5 | 261.8 |  |
| after_edit | 632.1 | 268.7 |  |
| after_removal | 618.6 | 268.7 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.682 | 0.034 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 9.413 | 0.250 | 10,000 | 1,062 r/s | 0.941 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.314 | 0.017 | 1,000 | 3,180 r/s | 0.314 ms | get_many (1000) |
| query | batch_exists_many | 0.006 | 0.001 | 1,000 | 166,280 r/s | 0.006 ms | exists_many (1000) |
| query | point_lookups | 0.117 | 0.004 | 100 | 853.3 r/s | 1.172 ms | get sample=100 |
| query | point_get_entity | 0.062 | 0.006 | 100 | 1,618 r/s | 0.618 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.023 | 0.003 | 100 | 4,419 r/s | 0.226 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.114 | 0.003 | 100 | 874.5 r/s | 1.143 ms | ws[id] sample=100 |
| query | operator_contains | 0.057 | 0.001 | 100 | 1,769 r/s | 0.565 ms | id in ws sample=100 |
| query | workspace_count | 0.001 | 0.000 | 10,000 | 8,063,346 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.001 | 0.000 | 10,000 | 16,807,853 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 3.135 | 0.052 | 10,000 | 3,190 r/s | 0.313 ms | list(iter(ws)) |
| query | ids_entity | 3.050 | 0.051 | 10,000 | 3,278 r/s | 0.305 ms | ws.ids(entity) |
| query | rows_entity | 2.999 | 0.089 | 10,000 | 3,335 r/s | 0.300 ms | ws.rows(entity) |
| query | items_entity | 2.983 | 0.076 | 10,000 | 3,353 r/s | 0.298 ms | ws.items(entity) |
| query | filter_gt | 2.294 | 0.075 | 10,000 | 4,358 r/s | 0.229 ms | price > 50 |
| query | filter_range | 1.201 | 0.057 | 10,000 | 8,324 r/s | 0.120 ms | price in [50,150] |
| query | filter_text | 0.381 | 0.032 | 10,000 | 26,280 r/s | 0.038 ms | body.match(audio) |
| query | filter_bool_tree | 3.191 | 0.137 | 10,000 | 3,134 r/s | 0.319 ms | bool tree |
| query | complex_query | 0.072 | 0.002 | 10,000 | 138,694 r/s | 0.007 ms | 10+ filter complex |
| query | vector_near | 7.666 | 0.222 | 10,000 | 1,304 r/s | 0.767 ms | near top_k=10 |
| query | hybrid_query | 4.376 | 0.100 | 10,000 | 2,285 r/s | 0.438 ms | filter + near top_k=10 |
| query | count_all | 2.940 | 0.198 | 10,000 | 3,402 r/s | 0.294 ms | count |
| edit | batch_set_many | 1.383 | 0.094 | 1,000 | 723.1 r/s | 1.383 ms | set_many (1000) |
| removal | batch_delete_many | 0.153 | 0.005 | 1,000 | 6,553 r/s | 0.153 ms | delete_many (1000) |
| removal | count_after_delete | 2.691 | 0.073 | 10,000 | 3,716 r/s | 0.269 ms | count after delete |

---

## lance_all (`pure-vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 4.374 +/- 0.046s
- **Backends**: main=inmem, vec=lance

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 684.0 | 0.000 |  |
| after_build | 685.6 | 4.067 |  |
| after_edit | 697.9 | 8.098 |  |
| after_removal | 697.3 | 8.126 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.128 | 0.007 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.322 | 0.012 | 1,000 | 3,105 r/s | 0.322 ms | upsert_many batch=100 |
| query | batch_get_many | 0.053 | 0.002 | 1,000 | 18,797 r/s | 0.053 ms | get_many (1000) |
| query | batch_exists_many | 0.032 | 0.000 | 1,000 | 31,515 r/s | 0.032 ms | exists_many (1000) |
| query | point_lookups | 0.659 | 0.025 | 100 | 151.7 r/s | 6.590 ms | get sample=100 |
| query | point_get_entity | 0.670 | 0.007 | 100 | 149.3 r/s | 6.698 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.666 | 0.017 | 100 | 150.2 r/s | 6.660 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.677 | 0.021 | 100 | 147.7 r/s | 6.772 ms | ws[id] sample=100 |
| query | operator_contains | 0.668 | 0.009 | 100 | 149.7 r/s | 6.680 ms | id in ws sample=100 |
| query | workspace_count | 0.002 | 0.000 | 1,000 | 550,412 r/s | 0.002 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 1,000 | 518,140 r/s | 0.002 ms | len(ws) |
| query | iter_workspace | 0.029 | 0.002 | 1,000 | 34,275 r/s | 0.029 ms | list(iter(ws)) |
| query | ids_entity | 0.027 | 0.001 | 1,000 | 37,287 r/s | 0.027 ms | ws.ids(entity) |
| query | rows_entity | 0.027 | 0.002 | 1,000 | 36,737 r/s | 0.027 ms | ws.rows(entity) |
| query | items_entity | 0.027 | 0.001 | 1,000 | 36,449 r/s | 0.027 ms | ws.items(entity) |
| query | filter_gt | 0.023 | 0.001 | 1,000 | 43,771 r/s | 0.023 ms | price > 50 |
| query | filter_range | 0.020 | 0.001 | 1,000 | 50,098 r/s | 0.020 ms | price in [50,150] |
| query | filter_text | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: LanceDB cannot execute fragment 'match'; r2 NotImplementedError: NotImplementedError: LanceDB cannot execute fragment 'match'; r3 NotImplementedError: NotImplementedError: LanceDB cannot execute fragment 'match'; +2 more |
| query | filter_bool_tree | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; +2 more |
| query | complex_query | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; +2 more |
| query | vector_near | 0.036 | 0.003 | 1,000 | 27,506 r/s | 0.036 ms | near top_k=10 |
| query | hybrid_query | 0.015 | 0.000 | 1,000 | 68,701 r/s | 0.015 ms | filter + near top_k=10 |
| query | count_all | 0.027 | 0.004 | 1,000 | 36,663 r/s | 0.027 ms | count |
| edit | batch_set_many | 0.186 | 0.020 | 1,000 | 5,365 r/s | 0.186 ms | set_many (1000) |
| removal | batch_delete_many | 0.068 | 0.004 | 1,000 | 14,779 r/s | 0.068 ms | delete_many (1000) |
| removal | count_after_delete | 0.009 | 0.001 | 1,000 | 108,969 r/s | 0.009 ms | count after delete |

---

## lance_all (`pure-vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 6.522 +/- 0.101s
- **Backends**: main=inmem, vec=lance

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 794.9 | 0.000 |  |
| after_build | 802.4 | 40.3 |  |
| after_edit | 814.2 | 44.4 |  |
| after_removal | 815.3 | 44.4 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.119 | 0.013 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.997 | 0.039 | 10,000 | 10,027 r/s | 0.100 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.053 | 0.002 | 1,000 | 18,945 r/s | 0.053 ms | get_many (1000) |
| query | batch_exists_many | 0.037 | 0.002 | 1,000 | 26,968 r/s | 0.037 ms | exists_many (1000) |
| query | point_lookups | 0.671 | 0.023 | 100 | 149.0 r/s | 6.713 ms | get sample=100 |
| query | point_get_entity | 0.667 | 0.043 | 100 | 149.8 r/s | 6.674 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.674 | 0.030 | 100 | 148.4 r/s | 6.740 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.669 | 0.038 | 100 | 149.6 r/s | 6.686 ms | ws[id] sample=100 |
| query | operator_contains | 0.671 | 0.047 | 100 | 149.0 r/s | 6.711 ms | id in ws sample=100 |
| query | workspace_count | 0.002 | 0.000 | 10,000 | 5,646,272 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 10,000 | 5,118,912 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 0.184 | 0.011 | 10,000 | 54,298 r/s | 0.018 ms | list(iter(ws)) |
| query | ids_entity | 0.183 | 0.009 | 10,000 | 54,712 r/s | 0.018 ms | ws.ids(entity) |
| query | rows_entity | 0.200 | 0.020 | 10,000 | 50,079 r/s | 0.020 ms | ws.rows(entity) |
| query | items_entity | 0.192 | 0.013 | 10,000 | 52,072 r/s | 0.019 ms | ws.items(entity) |
| query | filter_gt | 0.152 | 0.010 | 10,000 | 65,971 r/s | 0.015 ms | price > 50 |
| query | filter_range | 0.104 | 0.003 | 10,000 | 96,575 r/s | 0.010 ms | price in [50,150] |
| query | filter_text | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: LanceDB cannot execute fragment 'match'; r2 NotImplementedError: NotImplementedError: LanceDB cannot execute fragment 'match'; r3 NotImplementedError: NotImplementedError: LanceDB cannot execute fragment 'match'; +2 more |
| query | filter_bool_tree | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; +2 more |
| query | complex_query | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: LanceDB cannot execute non-native filter fragment 'and'; +2 more |
| query | vector_near | 0.206 | 0.019 | 10,000 | 48,615 r/s | 0.021 ms | near top_k=10 |
| query | hybrid_query | 0.036 | 0.001 | 10,000 | 279,540 r/s | 0.004 ms | filter + near top_k=10 |
| query | count_all | 0.204 | 0.011 | 10,000 | 49,045 r/s | 0.020 ms | count |
| edit | batch_set_many | 0.186 | 0.010 | 1,000 | 5,382 r/s | 0.186 ms | set_many (1000) |
| removal | batch_delete_many | 0.102 | 0.004 | 1,000 | 9,842 r/s | 0.102 ms | delete_many (1000) |
| removal | count_after_delete | 0.213 | 0.007 | 10,000 | 46,860 r/s | 0.021 ms | count after delete |

---

## milvus_all (`pure-vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 5.034 +/- 0.367s
- **Backends**: main=inmem, vec=milvus

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,865 | 0.000 |  |
| after_build | 1,865 | 0.000 |  |
| after_edit | 1,865 | 0.000 |  |
| after_removal | 1,865 | 0.000 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 1.448 | 0.266 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.355 | 0.075 | 1,000 | 2,817 r/s | 0.355 ms | upsert_many batch=100 |
| query | batch_get_many | 0.071 | 0.013 | 1,000 | 14,025 r/s | 0.071 ms | get_many (1000) |
| query | batch_exists_many | 0.063 | 0.004 | 1,000 | 15,930 r/s | 0.063 ms | exists_many (1000) |
| query | point_lookups | 0.146 | 0.024 | 100 | 684.1 r/s | 1.462 ms | get sample=100 |
| query | point_get_entity | 0.135 | 0.018 | 100 | 743.0 r/s | 1.346 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.133 | 0.021 | 100 | 750.5 r/s | 1.332 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.188 | 0.050 | 100 | 530.7 r/s | 1.884 ms | ws[id] sample=100 |
| query | operator_contains | 0.133 | 0.027 | 100 | 751.5 r/s | 1.331 ms | id in ws sample=100 |
| query | workspace_count | 1.411 | 0.224 | 1,000 | 708.5 r/s | 1.411 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 1,000 | 495,457 r/s | 0.002 ms | len(ws) |
| query | iter_workspace | 0.095 | 0.007 | 1,000 | 10,538 r/s | 0.095 ms | list(iter(ws)) |
| query | ids_entity | 0.085 | 0.004 | 1,000 | 11,763 r/s | 0.085 ms | ws.ids(entity) |
| query | rows_entity | 0.085 | 0.002 | 1,000 | 11,712 r/s | 0.085 ms | ws.rows(entity) |
| query | items_entity | 0.086 | 0.012 | 1,000 | 11,569 r/s | 0.086 ms | ws.items(entity) |
| query | filter_gt | 0.062 | 0.011 | 1,000 | 16,089 r/s | 0.062 ms | price > 50 |
| query | filter_range | 0.041 | 0.010 | 1,000 | 24,380 r/s | 0.041 ms | price in [50,150] |
| query | filter_text | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Milvus cannot execute fragment 'match'; r2 NotImplementedError: NotImplementedError: Milvus cannot execute fragment 'match'; r3 NotImplementedError: NotImplementedError: Milvus cannot execute fragment 'match'; +2 more |
| query | filter_bool_tree | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; +2 more |
| query | complex_query | 0.000 | 0.000 | 1,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; +2 more |
| query | vector_near | 0.080 | 0.016 | 1,000 | 12,514 r/s | 0.080 ms | near top_k=10 |
| query | hybrid_query | 0.008 | 0.001 | 1,000 | 117,723 r/s | 0.008 ms | filter + near top_k=10 |
| query | count_all | 0.073 | 0.016 | 1,000 | 13,623 r/s | 0.073 ms | count |
| edit | batch_set_many | 0.236 | 0.042 | 1,000 | 4,242 r/s | 0.236 ms | set_many (1000) |
| removal | batch_delete_many | 0.067 | 0.004 | 1,000 | 14,889 r/s | 0.067 ms | delete_many (1000) |
| removal | count_after_delete | 0.029 | 0.035 | 1,000 | 34,993 r/s | 0.029 ms | count after delete |

---

## milvus_all (`pure-vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 11.459 +/- 0.299s
- **Backends**: main=inmem, vec=milvus

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,885 | 0.000 |  |
| after_build | 1,885 | 0.000 |  |
| after_edit | 1,895 | 0.000 |  |
| after_removal | 1,897 | 0.000 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 1.399 | 0.276 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 1.209 | 0.053 | 10,000 | 8,274 r/s | 0.121 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.072 | 0.007 | 1,000 | 13,938 r/s | 0.072 ms | get_many (1000) |
| query | batch_exists_many | 0.067 | 0.007 | 1,000 | 14,932 r/s | 0.067 ms | exists_many (1000) |
| query | point_lookups | 0.167 | 0.036 | 100 | 597.4 r/s | 1.674 ms | get sample=100 |
| query | point_get_entity | 0.147 | 0.020 | 100 | 679.0 r/s | 1.473 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.125 | 0.011 | 100 | 803.1 r/s | 1.245 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.137 | 0.007 | 100 | 729.6 r/s | 1.371 ms | ws[id] sample=100 |
| query | operator_contains | 0.133 | 0.015 | 100 | 749.7 r/s | 1.334 ms | id in ws sample=100 |
| query | workspace_count | 1.512 | 0.001 | 10,000 | 6,615 r/s | 0.151 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 10,000 | 4,697,570 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 0.820 | 0.040 | 10,000 | 12,192 r/s | 0.082 ms | list(iter(ws)) |
| query | ids_entity | 0.770 | 0.031 | 10,000 | 12,993 r/s | 0.077 ms | ws.ids(entity) |
| query | rows_entity | 0.791 | 0.031 | 10,000 | 12,636 r/s | 0.079 ms | ws.rows(entity) |
| query | items_entity | 0.773 | 0.020 | 10,000 | 12,943 r/s | 0.077 ms | ws.items(entity) |
| query | filter_gt | 0.602 | 0.018 | 10,000 | 16,611 r/s | 0.060 ms | price > 50 |
| query | filter_range | 0.310 | 0.017 | 10,000 | 32,220 r/s | 0.031 ms | price in [50,150] |
| query | filter_text | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Milvus cannot execute fragment 'match'; r2 NotImplementedError: NotImplementedError: Milvus cannot execute fragment 'match'; r3 NotImplementedError: NotImplementedError: Milvus cannot execute fragment 'match'; +2 more |
| query | filter_bool_tree | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; +2 more |
| query | complex_query | 0.000 | 0.000 | 10,000 | - | - | ERROR repeats: r1 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; r2 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; r3 NotImplementedError: NotImplementedError: Milvus cannot execute non-native filter fragment 'and'; +2 more |
| query | vector_near | 0.718 | 0.028 | 10,000 | 13,932 r/s | 0.072 ms | near top_k=10 |
| query | hybrid_query | 0.007 | 0.001 | 10,000 | 1,453,898 r/s | 0.001 ms | filter + near top_k=10 |
| query | count_all | 0.719 | 0.051 | 10,000 | 13,906 r/s | 0.072 ms | count |
| edit | batch_set_many | 0.257 | 0.014 | 1,000 | 3,899 r/s | 0.257 ms | set_many (1000) |
| removal | batch_delete_many | 0.073 | 0.006 | 1,000 | 13,613 r/s | 0.073 ms | delete_many (1000) |
| removal | count_after_delete | 0.649 | 0.021 | 10,000 | 15,408 r/s | 0.065 ms | count after delete |

---

## mysql_all (`pure-sql`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 12.335 +/- 0.237s
- **Backends**: main=mysql

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,789 | 0.000 |  |
| after_build | 1,804 | 0.000 |  |
| after_edit | 1,817 | 0.000 |  |
| after_removal | 1,817 | 0.000 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 2.264 | 0.127 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 1.967 | 0.127 | 1,000 | 508.3 r/s | 1.967 ms | upsert_many batch=100 |
| query | batch_get_many | 0.351 | 0.038 | 1,000 | 2,848 r/s | 0.351 ms | get_many (1000) |
| query | batch_exists_many | 0.014 | 0.001 | 1,000 | 71,044 r/s | 0.014 ms | exists_many (1000) |
| query | point_lookups | 0.488 | 0.048 | 100 | 204.9 r/s | 4.881 ms | get sample=100 |
| query | point_get_entity | 0.202 | 0.020 | 100 | 495.5 r/s | 2.018 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.136 | 0.020 | 100 | 733.7 r/s | 1.363 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.473 | 0.053 | 100 | 211.6 r/s | 4.725 ms | ws[id] sample=100 |
| query | operator_contains | 0.406 | 0.060 | 100 | 246.5 r/s | 4.057 ms | id in ws sample=100 |
| query | workspace_count | 0.003 | 0.000 | 1,000 | 348,216 r/s | 0.003 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 1,000 | 402,007 r/s | 0.002 ms | len(ws) |
| query | iter_workspace | 0.304 | 0.019 | 1,000 | 3,293 r/s | 0.304 ms | list(iter(ws)) |
| query | ids_entity | 0.303 | 0.029 | 1,000 | 3,302 r/s | 0.303 ms | ws.ids(entity) |
| query | rows_entity | 0.309 | 0.029 | 1,000 | 3,233 r/s | 0.309 ms | ws.rows(entity) |
| query | items_entity | 0.298 | 0.012 | 1,000 | 3,358 r/s | 0.298 ms | ws.items(entity) |
| query | filter_gt | 0.221 | 0.011 | 1,000 | 4,533 r/s | 0.221 ms | price > 50 |
| query | filter_range | 0.123 | 0.012 | 1,000 | 8,103 r/s | 0.123 ms | price in [50,150] |
| query | filter_text | 0.045 | 0.008 | 1,000 | 21,983 r/s | 0.045 ms | body.match(audio) |
| query | filter_bool_tree | 0.296 | 0.022 | 1,000 | 3,376 r/s | 0.296 ms | bool tree |
| query | complex_query | 0.017 | 0.001 | 1,000 | 57,218 r/s | 0.017 ms | 10+ filter complex |
| query | vector_near | 0.758 | 0.041 | 1,000 | 1,319 r/s | 0.758 ms | near top_k=10 |
| query | hybrid_query | 0.409 | 0.020 | 1,000 | 2,443 r/s | 0.409 ms | filter + near top_k=10 |
| query | count_all | 0.366 | 0.026 | 1,000 | 2,732 r/s | 0.366 ms | count |
| edit | batch_set_many | 2.497 | 0.082 | 1,000 | 400.5 r/s | 2.497 ms | set_many (1000) |
| removal | batch_delete_many | 0.079 | 0.003 | 1,000 | 12,690 r/s | 0.079 ms | delete_many (1000) |
| removal | count_after_delete | 0.002 | 0.000 | 1,000 | 415,559 r/s | 0.002 ms | count after delete |

---

## mysql_all (`pure-sql`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 90.743 +/- 0.873s
- **Backends**: main=mysql

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,820 | 0.000 |  |
| after_build | 1,875 | 0.000 |  |
| after_edit | 1,890 | 0.000 |  |
| after_removal | 1,857 | 0.000 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 2.128 | 0.083 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 19.474 | 0.488 | 10,000 | 513.5 r/s | 1.947 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.568 | 0.041 | 1,000 | 1,762 r/s | 0.568 ms | get_many (1000) |
| query | batch_exists_many | 0.016 | 0.001 | 1,000 | 61,781 r/s | 0.016 ms | exists_many (1000) |
| query | point_lookups | 0.556 | 0.057 | 100 | 179.9 r/s | 5.559 ms | get sample=100 |
| query | point_get_entity | 0.184 | 0.014 | 100 | 543.3 r/s | 1.840 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.140 | 0.003 | 100 | 716.4 r/s | 1.396 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.511 | 0.030 | 100 | 195.7 r/s | 5.111 ms | ws[id] sample=100 |
| query | operator_contains | 0.393 | 0.013 | 100 | 254.7 r/s | 3.927 ms | id in ws sample=100 |
| query | workspace_count | 0.003 | 0.000 | 10,000 | 3,196,849 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.003 | 0.001 | 10,000 | 3,215,703 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 5.053 | 0.220 | 10,000 | 1,979 r/s | 0.505 ms | list(iter(ws)) |
| query | ids_entity | 4.968 | 0.097 | 10,000 | 2,013 r/s | 0.497 ms | ws.ids(entity) |
| query | rows_entity | 5.167 | 0.243 | 10,000 | 1,935 r/s | 0.517 ms | ws.rows(entity) |
| query | items_entity | 5.124 | 0.193 | 10,000 | 1,952 r/s | 0.512 ms | ws.items(entity) |
| query | filter_gt | 4.501 | 0.134 | 10,000 | 2,222 r/s | 0.450 ms | price > 50 |
| query | filter_range | 2.282 | 0.114 | 10,000 | 4,382 r/s | 0.228 ms | price in [50,150] |
| query | filter_text | 2.533 | 0.147 | 10,000 | 3,949 r/s | 0.253 ms | body.match(audio) |
| query | filter_bool_tree | 5.964 | 0.221 | 10,000 | 1,677 r/s | 0.596 ms | bool tree |
| query | complex_query | 0.596 | 0.089 | 10,000 | 16,767 r/s | 0.060 ms | 10+ filter complex |
| query | vector_near | 11.494 | 0.378 | 10,000 | 870.0 r/s | 1.149 ms | near top_k=10 |
| query | hybrid_query | 6.242 | 0.128 | 10,000 | 1,602 r/s | 0.624 ms | filter + near top_k=10 |
| query | count_all | 5.031 | 0.206 | 10,000 | 1,988 r/s | 0.503 ms | count |
| edit | batch_set_many | 2.590 | 0.118 | 1,000 | 386.1 r/s | 2.590 ms | set_many (1000) |
| removal | batch_delete_many | 0.517 | 0.032 | 1,000 | 1,934 r/s | 0.517 ms | delete_many (1000) |
| removal | count_after_delete | 4.706 | 0.178 | 10,000 | 2,125 r/s | 0.471 ms | count after delete |

---

## pgvector_all (`pure-sql-vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 8.511 +/- 0.131s
- **Backends**: main=pgvector

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,936 | 0.000 |  |
| after_build | 1,939 | 0.000 |  |
| after_edit | 1,963 | 0.000 |  |
| after_removal | 1,963 | 0.000 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 1.755 | 0.063 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 1.455 | 0.061 | 1,000 | 687.3 r/s | 1.455 ms | upsert_many batch=100 |
| query | batch_get_many | 0.166 | 0.004 | 1,000 | 6,018 r/s | 0.166 ms | get_many (1000) |
| query | batch_exists_many | 0.007 | 0.001 | 1,000 | 143,723 r/s | 0.007 ms | exists_many (1000) |
| query | point_lookups | 0.434 | 0.023 | 100 | 230.2 r/s | 4.345 ms | get sample=100 |
| query | point_get_entity | 0.166 | 0.022 | 100 | 603.1 r/s | 1.658 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.127 | 0.018 | 100 | 784.3 r/s | 1.275 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.430 | 0.026 | 100 | 232.4 r/s | 4.303 ms | ws[id] sample=100 |
| query | operator_contains | 0.384 | 0.049 | 100 | 260.2 r/s | 3.844 ms | id in ws sample=100 |
| query | workspace_count | 0.003 | 0.001 | 1,000 | 369,899 r/s | 0.003 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 1,000 | 415,797 r/s | 0.002 ms | len(ws) |
| query | iter_workspace | 0.162 | 0.009 | 1,000 | 6,172 r/s | 0.162 ms | list(iter(ws)) |
| query | ids_entity | 0.160 | 0.006 | 1,000 | 6,267 r/s | 0.160 ms | ws.ids(entity) |
| query | rows_entity | 0.163 | 0.004 | 1,000 | 6,153 r/s | 0.163 ms | ws.rows(entity) |
| query | items_entity | 0.163 | 0.007 | 1,000 | 6,146 r/s | 0.163 ms | ws.items(entity) |
| query | filter_gt | 0.123 | 0.013 | 1,000 | 8,126 r/s | 0.123 ms | price > 50 |
| query | filter_range | 0.070 | 0.003 | 1,000 | 14,299 r/s | 0.070 ms | price in [50,150] |
| query | filter_text | 0.025 | 0.001 | 1,000 | 39,549 r/s | 0.025 ms | body.match(audio) |
| query | filter_bool_tree | 0.171 | 0.009 | 1,000 | 5,862 r/s | 0.171 ms | bool tree |
| query | complex_query | 0.017 | 0.002 | 1,000 | 59,313 r/s | 0.017 ms | 10+ filter complex |
| query | vector_near | 0.173 | 0.006 | 1,000 | 5,777 r/s | 0.173 ms | near top_k=10 |
| query | hybrid_query | 0.010 | 0.001 | 1,000 | 103,315 r/s | 0.010 ms | filter + near top_k=10 |
| query | count_all | 0.165 | 0.010 | 1,000 | 6,070 r/s | 0.165 ms | count |
| edit | batch_set_many | 2.114 | 0.066 | 1,000 | 473.0 r/s | 2.114 ms | set_many (1000) |
| removal | batch_delete_many | 0.064 | 0.003 | 1,000 | 15,601 r/s | 0.064 ms | delete_many (1000) |
| removal | count_after_delete | 0.002 | 0.000 | 1,000 | 401,197 r/s | 0.002 ms | count after delete |

---

## pgvector_all (`pure-sql-vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 39.126 +/- 0.824s
- **Backends**: main=pgvector

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,842 | 0.000 |  |
| after_build | 1,882 | 0.000 |  |
| after_edit | 1,969 | 0.000 |  |
| after_removal | 1,954 | 0.000 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 1.542 | 0.045 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 18.907 | 0.794 | 10,000 | 528.9 r/s | 1.891 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.168 | 0.004 | 1,000 | 5,946 r/s | 0.168 ms | get_many (1000) |
| query | batch_exists_many | 0.009 | 0.000 | 1,000 | 116,227 r/s | 0.009 ms | exists_many (1000) |
| query | point_lookups | 0.416 | 0.038 | 100 | 240.3 r/s | 4.161 ms | get sample=100 |
| query | point_get_entity | 0.157 | 0.012 | 100 | 638.0 r/s | 1.567 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.134 | 0.043 | 100 | 749.0 r/s | 1.335 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.439 | 0.022 | 100 | 227.7 r/s | 4.391 ms | ws[id] sample=100 |
| query | operator_contains | 0.402 | 0.079 | 100 | 248.5 r/s | 4.024 ms | id in ws sample=100 |
| query | workspace_count | 0.003 | 0.001 | 10,000 | 3,421,213 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 10,000 | 4,152,858 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 1.660 | 0.151 | 10,000 | 6,024 r/s | 0.166 ms | list(iter(ws)) |
| query | ids_entity | 1.596 | 0.038 | 10,000 | 6,265 r/s | 0.160 ms | ws.ids(entity) |
| query | rows_entity | 1.605 | 0.046 | 10,000 | 6,231 r/s | 0.160 ms | ws.rows(entity) |
| query | items_entity | 1.656 | 0.034 | 10,000 | 6,039 r/s | 0.166 ms | ws.items(entity) |
| query | filter_gt | 1.197 | 0.015 | 10,000 | 8,353 r/s | 0.120 ms | price > 50 |
| query | filter_range | 0.640 | 0.030 | 10,000 | 15,625 r/s | 0.064 ms | price in [50,150] |
| query | filter_text | 0.205 | 0.007 | 10,000 | 48,812 r/s | 0.020 ms | body.match(audio) |
| query | filter_bool_tree | 1.586 | 0.016 | 10,000 | 6,304 r/s | 0.159 ms | bool tree |
| query | complex_query | 0.019 | 0.001 | 10,000 | 522,441 r/s | 0.002 ms | 10+ filter complex |
| query | vector_near | 1.608 | 0.038 | 10,000 | 6,218 r/s | 0.161 ms | near top_k=10 |
| query | hybrid_query | 0.023 | 0.000 | 10,000 | 438,018 r/s | 0.002 ms | filter + near top_k=10 |
| query | count_all | 1.602 | 0.018 | 10,000 | 6,243 r/s | 0.160 ms | count |
| edit | batch_set_many | 2.098 | 0.068 | 1,000 | 476.7 r/s | 2.098 ms | set_many (1000) |
| removal | batch_delete_many | 0.068 | 0.002 | 1,000 | 14,715 r/s | 0.068 ms | delete_many (1000) |
| removal | count_after_delete | 1.384 | 0.018 | 10,000 | 7,226 r/s | 0.138 ms | count after delete |

---

## sqlite_all (`pure-sql`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 7.301 +/- 0.148s
- **Backends**: main=sqlite

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 187.3 | 0.000 |  |
| after_build | 196.7 | 33.1 |  |
| after_edit | 199.6 | 50.4 |  |
| after_removal | 199.9 | 50.4 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.650 | 0.052 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.996 | 0.030 | 1,000 | 1,004 r/s | 0.996 ms | upsert_many batch=100 |
| query | batch_get_many | 0.320 | 0.005 | 1,000 | 3,123 r/s | 0.320 ms | get_many (1000) |
| query | batch_exists_many | 0.005 | 0.000 | 1,000 | 218,113 r/s | 0.005 ms | exists_many (1000) |
| query | point_lookups | 0.127 | 0.012 | 100 | 788.7 r/s | 1.268 ms | get sample=100 |
| query | point_get_entity | 0.068 | 0.005 | 100 | 1,463 r/s | 0.684 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.022 | 0.002 | 100 | 4,447 r/s | 0.225 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.123 | 0.014 | 100 | 810.1 r/s | 1.234 ms | ws[id] sample=100 |
| query | operator_contains | 0.066 | 0.009 | 100 | 1,509 r/s | 0.663 ms | id in ws sample=100 |
| query | workspace_count | 0.001 | 0.000 | 1,000 | 917,111 r/s | 0.001 ms | ws.count(entity) |
| query | workspace_len | 0.001 | 0.000 | 1,000 | 1,722,950 r/s | 0.001 ms | len(ws) |
| query | iter_workspace | 0.315 | 0.004 | 1,000 | 3,176 r/s | 0.315 ms | list(iter(ws)) |
| query | ids_entity | 0.315 | 0.005 | 1,000 | 3,179 r/s | 0.315 ms | ws.ids(entity) |
| query | rows_entity | 0.311 | 0.009 | 1,000 | 3,221 r/s | 0.311 ms | ws.rows(entity) |
| query | items_entity | 0.305 | 0.012 | 1,000 | 3,274 r/s | 0.305 ms | ws.items(entity) |
| query | filter_gt | 0.234 | 0.022 | 1,000 | 4,276 r/s | 0.234 ms | price > 50 |
| query | filter_range | 0.155 | 0.009 | 1,000 | 6,442 r/s | 0.155 ms | price in [50,150] |
| query | filter_text | 0.043 | 0.002 | 1,000 | 23,346 r/s | 0.043 ms | body.match(audio) |
| query | filter_bool_tree | 0.310 | 0.020 | 1,000 | 3,223 r/s | 0.310 ms | bool tree |
| query | complex_query | 0.019 | 0.002 | 1,000 | 52,884 r/s | 0.019 ms | 10+ filter complex |
| query | vector_near | 0.764 | 0.037 | 1,000 | 1,309 r/s | 0.764 ms | near top_k=10 |
| query | hybrid_query | 0.423 | 0.015 | 1,000 | 2,364 r/s | 0.423 ms | filter + near top_k=10 |
| query | count_all | 0.311 | 0.008 | 1,000 | 3,215 r/s | 0.311 ms | count |
| edit | batch_set_many | 1.376 | 0.122 | 1,000 | 726.9 r/s | 1.376 ms | set_many (1000) |
| removal | batch_delete_many | 0.041 | 0.007 | 1,000 | 24,179 r/s | 0.041 ms | delete_many (1000) |
| removal | count_after_delete | 0.001 | 0.000 | 1,000 | 1,967,807 r/s | 0.001 ms | count after delete |

---

## sqlite_all (`pure-sql`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 47.406 +/- 0.803s
- **Backends**: main=sqlite

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 496.3 | 0.000 |  |
| after_build | 530.0 | 263.1 |  |
| after_edit | 587.9 | 270.7 |  |
| after_removal | 593.0 | 270.7 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.635 | 0.045 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 9.057 | 0.715 | 10,000 | 1,104 r/s | 0.906 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.296 | 0.013 | 1,000 | 3,382 r/s | 0.296 ms | get_many (1000) |
| query | batch_exists_many | 0.004 | 0.001 | 1,000 | 262,226 r/s | 0.004 ms | exists_many (1000) |
| query | point_lookups | 0.111 | 0.010 | 100 | 901.7 r/s | 1.109 ms | get sample=100 |
| query | point_get_entity | 0.057 | 0.006 | 100 | 1,752 r/s | 0.571 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.021 | 0.003 | 100 | 4,827 r/s | 0.207 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.108 | 0.008 | 100 | 928.3 r/s | 1.077 ms | ws[id] sample=100 |
| query | operator_contains | 0.056 | 0.006 | 100 | 1,770 r/s | 0.565 ms | id in ws sample=100 |
| query | workspace_count | 0.001 | 0.000 | 10,000 | 10,020,643 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.000 | 0.000 | 10,000 | 20,506,090 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 2.992 | 0.156 | 10,000 | 3,342 r/s | 0.299 ms | list(iter(ws)) |
| query | ids_entity | 2.962 | 0.144 | 10,000 | 3,376 r/s | 0.296 ms | ws.ids(entity) |
| query | rows_entity | 2.913 | 0.017 | 10,000 | 3,433 r/s | 0.291 ms | ws.rows(entity) |
| query | items_entity | 2.898 | 0.055 | 10,000 | 3,451 r/s | 0.290 ms | ws.items(entity) |
| query | filter_gt | 2.150 | 0.051 | 10,000 | 4,651 r/s | 0.215 ms | price > 50 |
| query | filter_range | 1.501 | 0.087 | 10,000 | 6,664 r/s | 0.150 ms | price in [50,150] |
| query | filter_text | 0.374 | 0.006 | 10,000 | 26,730 r/s | 0.037 ms | body.match(audio) |
| query | filter_bool_tree | 2.865 | 0.081 | 10,000 | 3,491 r/s | 0.286 ms | bool tree |
| query | complex_query | 0.032 | 0.012 | 10,000 | 316,913 r/s | 0.003 ms | 10+ filter complex |
| query | vector_near | 7.191 | 0.236 | 10,000 | 1,391 r/s | 0.719 ms | near top_k=10 |
| query | hybrid_query | 3.985 | 0.082 | 10,000 | 2,510 r/s | 0.398 ms | filter + near top_k=10 |
| query | count_all | 2.890 | 0.046 | 10,000 | 3,460 r/s | 0.289 ms | count |
| edit | batch_set_many | 1.440 | 0.021 | 1,000 | 694.5 r/s | 1.440 ms | set_many (1000) |
| removal | batch_delete_many | 0.179 | 0.003 | 1,000 | 5,590 r/s | 0.179 ms | delete_many (1000) |
| removal | count_after_delete | 2.689 | 0.035 | 10,000 | 3,719 r/s | 0.269 ms | count after delete |

---

## sqlite_chroma_vec (`sql+vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 5.276 +/- 0.308s
- **Backends**: main=sqlite, vec=chroma

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 759.7 | 0.000 |  |
| after_build | 765.9 | 18.7 |  |
| after_edit | 780.0 | 18.7 |  |
| after_removal | 780.2 | 18.7 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.948 | 0.305 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.632 | 0.013 | 1,000 | 1,582 r/s | 0.632 ms | upsert_many batch=100 |
| query | batch_get_many | 0.115 | 0.006 | 1,000 | 8,694 r/s | 0.115 ms | get_many (1000) |
| query | batch_exists_many | 0.010 | 0.001 | 1,000 | 99,509 r/s | 0.010 ms | exists_many (1000) |
| query | point_lookups | 0.352 | 0.017 | 100 | 283.9 r/s | 3.522 ms | get sample=100 |
| query | point_get_entity | 0.261 | 0.008 | 100 | 382.6 r/s | 2.614 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.216 | 0.018 | 100 | 463.7 r/s | 2.157 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.342 | 0.007 | 100 | 292.5 r/s | 3.419 ms | ws[id] sample=100 |
| query | operator_contains | 0.281 | 0.012 | 100 | 356.3 r/s | 2.807 ms | id in ws sample=100 |
| query | workspace_count | 0.003 | 0.000 | 1,000 | 346,337 r/s | 0.003 ms | ws.count(entity) |
| query | workspace_len | 0.003 | 0.000 | 1,000 | 397,652 r/s | 0.003 ms | len(ws) |
| query | iter_workspace | 0.102 | 0.003 | 1,000 | 9,795 r/s | 0.102 ms | list(iter(ws)) |
| query | ids_entity | 0.098 | 0.002 | 1,000 | 10,238 r/s | 0.098 ms | ws.ids(entity) |
| query | rows_entity | 0.099 | 0.001 | 1,000 | 10,097 r/s | 0.099 ms | ws.rows(entity) |
| query | items_entity | 0.101 | 0.003 | 1,000 | 9,884 r/s | 0.101 ms | ws.items(entity) |
| query | filter_gt | 0.089 | 0.003 | 1,000 | 11,195 r/s | 0.089 ms | price > 50 |
| query | filter_range | 0.051 | 0.001 | 1,000 | 19,672 r/s | 0.051 ms | price in [50,150] |
| query | filter_text | 0.019 | 0.002 | 1,000 | 51,525 r/s | 0.019 ms | body.match(audio) |
| query | filter_bool_tree | 0.130 | 0.001 | 1,000 | 7,713 r/s | 0.130 ms | bool tree |
| query | complex_query | 0.017 | 0.001 | 1,000 | 59,658 r/s | 0.017 ms | 10+ filter complex |
| query | vector_near | 0.108 | 0.002 | 1,000 | 9,241 r/s | 0.108 ms | near top_k=10 |
| query | hybrid_query | 0.014 | 0.001 | 1,000 | 73,384 r/s | 0.014 ms | filter + near top_k=10 |
| query | count_all | 0.104 | 0.004 | 1,000 | 9,625 r/s | 0.104 ms | count |
| edit | batch_set_many | 0.976 | 0.016 | 1,000 | 1,025 r/s | 0.976 ms | set_many (1000) |
| removal | batch_delete_many | 0.203 | 0.016 | 1,000 | 4,933 r/s | 0.203 ms | delete_many (1000) |
| removal | count_after_delete | 0.003 | 0.000 | 1,000 | 375,954 r/s | 0.003 ms | count after delete |

---

## sqlite_chroma_vec (`sql+vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 20.338 +/- 0.196s
- **Backends**: main=sqlite, vec=chroma

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,007 | 0.000 |  |
| after_build | 1,093 | 60.5 |  |
| after_edit | 1,178 | 61.2 |  |
| after_removal | 1,175 | 61.2 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.878 | 0.029 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 5.131 | 0.133 | 10,000 | 1,949 r/s | 0.513 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.122 | 0.008 | 1,000 | 8,184 r/s | 0.122 ms | get_many (1000) |
| query | batch_exists_many | 0.011 | 0.001 | 1,000 | 88,372 r/s | 0.011 ms | exists_many (1000) |
| query | point_lookups | 0.342 | 0.019 | 100 | 292.5 r/s | 3.418 ms | get sample=100 |
| query | point_get_entity | 0.268 | 0.008 | 100 | 373.3 r/s | 2.679 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.224 | 0.007 | 100 | 445.9 r/s | 2.242 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.335 | 0.049 | 100 | 298.1 r/s | 3.354 ms | ws[id] sample=100 |
| query | operator_contains | 0.285 | 0.003 | 100 | 351.5 r/s | 2.845 ms | id in ws sample=100 |
| query | workspace_count | 0.003 | 0.000 | 10,000 | 2,917,817 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.003 | 0.000 | 10,000 | 3,193,133 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 1.141 | 0.069 | 10,000 | 8,767 r/s | 0.114 ms | list(iter(ws)) |
| query | ids_entity | 1.102 | 0.021 | 10,000 | 9,071 r/s | 0.110 ms | ws.ids(entity) |
| query | rows_entity | 1.128 | 0.025 | 10,000 | 8,868 r/s | 0.113 ms | ws.rows(entity) |
| query | items_entity | 1.126 | 0.025 | 10,000 | 8,881 r/s | 0.113 ms | ws.items(entity) |
| query | filter_gt | 0.948 | 0.029 | 10,000 | 10,550 r/s | 0.095 ms | price > 50 |
| query | filter_range | 0.507 | 0.025 | 10,000 | 19,723 r/s | 0.051 ms | price in [50,150] |
| query | filter_text | 0.159 | 0.007 | 10,000 | 62,950 r/s | 0.016 ms | body.match(audio) |
| query | filter_bool_tree | 1.567 | 0.069 | 10,000 | 6,383 r/s | 0.157 ms | bool tree |
| query | complex_query | 0.019 | 0.000 | 10,000 | 529,183 r/s | 0.002 ms | 10+ filter complex |
| query | vector_near | 1.110 | 0.019 | 10,000 | 9,005 r/s | 0.111 ms | near top_k=10 |
| query | hybrid_query | 0.072 | 0.006 | 10,000 | 138,068 r/s | 0.007 ms | filter + near top_k=10 |
| query | count_all | 1.128 | 0.025 | 10,000 | 8,869 r/s | 0.113 ms | count |
| edit | batch_set_many | 1.420 | 0.054 | 1,000 | 704.1 r/s | 1.420 ms | set_many (1000) |
| removal | batch_delete_many | 0.298 | 0.006 | 1,000 | 3,352 r/s | 0.298 ms | delete_many (1000) |
| removal | count_after_delete | 1.009 | 0.008 | 10,000 | 9,909 r/s | 0.101 ms | count after delete |

---

## sqlite_inmem_vec (`sql+vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 1.620 +/- 0.021s
- **Backends**: main=sqlite, vec=inmem

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 347.5 | 0.000 |  |
| after_build | 352.0 | 9.687 |  |
| after_edit | 357.5 | 9.687 |  |
| after_removal | 357.5 | 9.687 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.606 | 0.014 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.067 | 0.003 | 1,000 | 14,834 r/s | 0.067 ms | upsert_many batch=100 |
| query | batch_get_many | 0.017 | 0.001 | 1,000 | 57,918 r/s | 0.017 ms | get_many (1000) |
| query | batch_exists_many | 0.004 | 0.001 | 1,000 | 223,318 r/s | 0.004 ms | exists_many (1000) |
| query | point_lookups | 0.079 | 0.003 | 100 | 1,269 r/s | 0.788 ms | get sample=100 |
| query | point_get_entity | 0.029 | 0.007 | 100 | 3,445 r/s | 0.290 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.022 | 0.003 | 100 | 4,576 r/s | 0.219 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.079 | 0.007 | 100 | 1,272 r/s | 0.786 ms | ws[id] sample=100 |
| query | operator_contains | 0.059 | 0.003 | 100 | 1,692 r/s | 0.591 ms | id in ws sample=100 |
| query | workspace_count | 0.001 | 0.000 | 1,000 | 1,135,280 r/s | 0.001 ms | ws.count(entity) |
| query | workspace_len | 0.001 | 0.000 | 1,000 | 1,976,363 r/s | 0.001 ms | len(ws) |
| query | iter_workspace | 0.013 | 0.002 | 1,000 | 74,262 r/s | 0.013 ms | list(iter(ws)) |
| query | ids_entity | 0.014 | 0.001 | 1,000 | 69,780 r/s | 0.014 ms | ws.ids(entity) |
| query | rows_entity | 0.013 | 0.003 | 1,000 | 74,820 r/s | 0.013 ms | ws.rows(entity) |
| query | items_entity | 0.014 | 0.001 | 1,000 | 69,435 r/s | 0.014 ms | ws.items(entity) |
| query | filter_gt | 0.022 | 0.002 | 1,000 | 45,300 r/s | 0.022 ms | price > 50 |
| query | filter_range | 0.013 | 0.001 | 1,000 | 74,854 r/s | 0.013 ms | price in [50,150] |
| query | filter_text | 0.005 | 0.001 | 1,000 | 187,007 r/s | 0.005 ms | body.match(audio) |
| query | filter_bool_tree | 0.029 | 0.003 | 1,000 | 35,082 r/s | 0.029 ms | bool tree |
| query | complex_query | 0.017 | 0.002 | 1,000 | 58,478 r/s | 0.017 ms | 10+ filter complex |
| query | vector_near | 0.179 | 0.007 | 1,000 | 5,593 r/s | 0.179 ms | near top_k=10 |
| query | hybrid_query | 0.172 | 0.004 | 1,000 | 5,822 r/s | 0.172 ms | filter + near top_k=10 |
| query | count_all | 0.014 | 0.001 | 1,000 | 70,619 r/s | 0.014 ms | count |
| edit | batch_set_many | 0.129 | 0.004 | 1,000 | 7,739 r/s | 0.129 ms | set_many (1000) |
| removal | batch_delete_many | 0.019 | 0.001 | 1,000 | 52,779 r/s | 0.019 ms | delete_many (1000) |
| removal | count_after_delete | 0.001 | 0.000 | 1,000 | 1,480,341 r/s | 0.001 ms | count after delete |

---

## sqlite_inmem_vec (`sql+vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 6.526 +/- 0.094s
- **Backends**: main=sqlite, vec=inmem

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 515.3 | 0.000 |  |
| after_build | 602.4 | 11.6 |  |
| after_edit | 621.4 | 12.3 |  |
| after_removal | 621.3 | 12.3 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.662 | 0.016 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.679 | 0.017 | 10,000 | 14,723 r/s | 0.068 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.018 | 0.002 | 1,000 | 56,290 r/s | 0.018 ms | get_many (1000) |
| query | batch_exists_many | 0.005 | 0.000 | 1,000 | 192,296 r/s | 0.005 ms | exists_many (1000) |
| query | point_lookups | 0.079 | 0.005 | 100 | 1,266 r/s | 0.790 ms | get sample=100 |
| query | point_get_entity | 0.027 | 0.002 | 100 | 3,668 r/s | 0.273 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.024 | 0.006 | 100 | 4,187 r/s | 0.239 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.077 | 0.007 | 100 | 1,292 r/s | 0.774 ms | ws[id] sample=100 |
| query | operator_contains | 0.061 | 0.008 | 100 | 1,649 r/s | 0.606 ms | id in ws sample=100 |
| query | workspace_count | 0.001 | 0.000 | 10,000 | 11,811,675 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.000 | 0.000 | 10,000 | 20,952,940 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 0.138 | 0.003 | 10,000 | 72,616 r/s | 0.014 ms | list(iter(ws)) |
| query | ids_entity | 0.132 | 0.004 | 10,000 | 75,687 r/s | 0.013 ms | ws.ids(entity) |
| query | rows_entity | 0.142 | 0.007 | 10,000 | 70,544 r/s | 0.014 ms | ws.rows(entity) |
| query | items_entity | 0.145 | 0.006 | 10,000 | 69,121 r/s | 0.014 ms | ws.items(entity) |
| query | filter_gt | 0.198 | 0.014 | 10,000 | 50,597 r/s | 0.020 ms | price > 50 |
| query | filter_range | 0.103 | 0.004 | 10,000 | 97,288 r/s | 0.010 ms | price in [50,150] |
| query | filter_text | 0.035 | 0.003 | 10,000 | 283,865 r/s | 0.004 ms | body.match(audio) |
| query | filter_bool_tree | 0.269 | 0.012 | 10,000 | 37,108 r/s | 0.027 ms | bool tree |
| query | complex_query | 0.017 | 0.002 | 10,000 | 574,762 r/s | 0.002 ms | 10+ filter complex |
| query | vector_near | 1.678 | 0.062 | 10,000 | 5,961 r/s | 0.168 ms | near top_k=10 |
| query | hybrid_query | 1.584 | 0.059 | 10,000 | 6,312 r/s | 0.158 ms | filter + near top_k=10 |
| query | count_all | 0.139 | 0.008 | 10,000 | 72,055 r/s | 0.014 ms | count |
| edit | batch_set_many | 0.147 | 0.007 | 1,000 | 6,800 r/s | 0.147 ms | set_many (1000) |
| removal | batch_delete_many | 0.031 | 0.002 | 1,000 | 32,366 r/s | 0.031 ms | delete_many (1000) |
| removal | count_after_delete | 0.134 | 0.006 | 10,000 | 74,621 r/s | 0.013 ms | count after delete |

---

## sqlite_lance_vec (`sql+vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 5.294 +/- 0.621s
- **Backends**: main=sqlite, vec=lance

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 610.7 | 0.000 |  |
| after_build | 625.7 | 13.7 |  |
| after_edit | 645.6 | 17.6 |  |
| after_removal | 644.3 | 17.6 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.880 | 0.611 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.332 | 0.008 | 1,000 | 3,008 r/s | 0.332 ms | upsert_many batch=100 |
| query | batch_get_many | 0.065 | 0.007 | 1,000 | 15,420 r/s | 0.065 ms | get_many (1000) |
| query | batch_exists_many | 0.031 | 0.002 | 1,000 | 31,908 r/s | 0.031 ms | exists_many (1000) |
| query | point_lookups | 0.716 | 0.054 | 100 | 139.6 r/s | 7.162 ms | get sample=100 |
| query | point_get_entity | 0.657 | 0.038 | 100 | 152.3 r/s | 6.566 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.634 | 0.032 | 100 | 157.6 r/s | 6.345 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.686 | 0.061 | 100 | 145.8 r/s | 6.860 ms | ws[id] sample=100 |
| query | operator_contains | 0.654 | 0.045 | 100 | 152.8 r/s | 6.543 ms | id in ws sample=100 |
| query | workspace_count | 0.002 | 0.000 | 1,000 | 444,951 r/s | 0.002 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 1,000 | 446,464 r/s | 0.002 ms | len(ws) |
| query | iter_workspace | 0.029 | 0.001 | 1,000 | 34,758 r/s | 0.029 ms | list(iter(ws)) |
| query | ids_entity | 0.026 | 0.001 | 1,000 | 37,837 r/s | 0.026 ms | ws.ids(entity) |
| query | rows_entity | 0.028 | 0.002 | 1,000 | 36,242 r/s | 0.028 ms | ws.rows(entity) |
| query | items_entity | 0.027 | 0.002 | 1,000 | 37,111 r/s | 0.027 ms | ws.items(entity) |
| query | filter_gt | 0.039 | 0.003 | 1,000 | 25,901 r/s | 0.039 ms | price > 50 |
| query | filter_range | 0.025 | 0.002 | 1,000 | 39,325 r/s | 0.025 ms | price in [50,150] |
| query | filter_text | 0.015 | 0.001 | 1,000 | 65,519 r/s | 0.015 ms | body.match(audio) |
| query | filter_bool_tree | 0.050 | 0.005 | 1,000 | 19,984 r/s | 0.050 ms | bool tree |
| query | complex_query | 0.015 | 0.001 | 1,000 | 66,487 r/s | 0.015 ms | 10+ filter complex |
| query | vector_near | 0.044 | 0.003 | 1,000 | 22,665 r/s | 0.044 ms | near top_k=10 |
| query | hybrid_query | 0.025 | 0.001 | 1,000 | 39,624 r/s | 0.025 ms | filter + near top_k=10 |
| query | count_all | 0.028 | 0.001 | 1,000 | 35,673 r/s | 0.028 ms | count |
| edit | batch_set_many | 0.201 | 0.010 | 1,000 | 4,987 r/s | 0.201 ms | set_many (1000) |
| removal | batch_delete_many | 0.071 | 0.012 | 1,000 | 14,030 r/s | 0.071 ms | delete_many (1000) |
| removal | count_after_delete | 0.010 | 0.001 | 1,000 | 100,327 r/s | 0.010 ms | count after delete |

---

## sqlite_lance_vec (`sql+vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 9.753 +/- 0.171s
- **Backends**: main=sqlite, vec=lance

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 735.0 | 0.000 |  |
| after_build | 750.2 | 51.1 |  |
| after_edit | 758.7 | 55.7 |  |
| after_removal | 758.8 | 55.7 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 0.802 | 0.090 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 1.220 | 0.025 | 10,000 | 8,196 r/s | 0.122 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.066 | 0.003 | 1,000 | 15,101 r/s | 0.066 ms | get_many (1000) |
| query | batch_exists_many | 0.039 | 0.002 | 1,000 | 25,551 r/s | 0.039 ms | exists_many (1000) |
| query | point_lookups | 0.831 | 0.054 | 100 | 120.3 r/s | 8.311 ms | get sample=100 |
| query | point_get_entity | 0.732 | 0.040 | 100 | 136.7 r/s | 7.316 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.719 | 0.052 | 100 | 139.0 r/s | 7.193 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.837 | 0.071 | 100 | 119.5 r/s | 8.368 ms | ws[id] sample=100 |
| query | operator_contains | 0.791 | 0.068 | 100 | 126.5 r/s | 7.905 ms | id in ws sample=100 |
| query | workspace_count | 0.003 | 0.000 | 10,000 | 3,620,670 r/s | 0.000 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 10,000 | 4,367,804 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 0.252 | 0.023 | 10,000 | 39,719 r/s | 0.025 ms | list(iter(ws)) |
| query | ids_entity | 0.250 | 0.023 | 10,000 | 39,969 r/s | 0.025 ms | ws.ids(entity) |
| query | rows_entity | 0.257 | 0.018 | 10,000 | 38,975 r/s | 0.026 ms | ws.rows(entity) |
| query | items_entity | 0.262 | 0.023 | 10,000 | 38,114 r/s | 0.026 ms | ws.items(entity) |
| query | filter_gt | 0.417 | 0.027 | 10,000 | 23,966 r/s | 0.042 ms | price > 50 |
| query | filter_range | 0.258 | 0.010 | 10,000 | 38,717 r/s | 0.026 ms | price in [50,150] |
| query | filter_text | 0.086 | 0.008 | 10,000 | 116,447 r/s | 0.009 ms | body.match(audio) |
| query | filter_bool_tree | 0.549 | 0.015 | 10,000 | 18,209 r/s | 0.055 ms | bool tree |
| query | complex_query | 0.019 | 0.001 | 10,000 | 522,129 r/s | 0.002 ms | 10+ filter complex |
| query | vector_near | 0.278 | 0.012 | 10,000 | 36,026 r/s | 0.028 ms | near top_k=10 |
| query | hybrid_query | 0.139 | 0.003 | 10,000 | 72,135 r/s | 0.014 ms | filter + near top_k=10 |
| query | count_all | 0.287 | 0.010 | 10,000 | 34,854 r/s | 0.029 ms | count |
| edit | batch_set_many | 0.246 | 0.012 | 1,000 | 4,068 r/s | 0.246 ms | set_many (1000) |
| removal | batch_delete_many | 0.123 | 0.003 | 1,000 | 8,147 r/s | 0.123 ms | delete_many (1000) |
| removal | count_after_delete | 0.289 | 0.007 | 10,000 | 34,617 r/s | 0.029 ms | count after delete |

---

## sqlite_milvus_vec (`sql+vector`) - scale=1x

- **Rows**: 1,000
- **Repeats**: 5
- **Total time**: 6.652 +/- 0.319s
- **Backends**: main=sqlite, vec=milvus

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,842 | 0.000 |  |
| after_build | 1,843 | 9.656 |  |
| after_edit | 1,843 | 9.656 |  |
| after_removal | 1,843 | 9.656 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 2.121 | 0.265 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 0.224 | 0.054 | 1,000 | 4,456 r/s | 0.224 ms | upsert_many batch=100 |
| query | batch_get_many | 0.072 | 0.012 | 1,000 | 13,916 r/s | 0.072 ms | get_many (1000) |
| query | batch_exists_many | 0.074 | 0.014 | 1,000 | 13,513 r/s | 0.074 ms | exists_many (1000) |
| query | point_lookups | 0.305 | 0.034 | 100 | 327.8 r/s | 3.050 ms | get sample=100 |
| query | point_get_entity | 0.219 | 0.034 | 100 | 457.5 r/s | 2.186 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.228 | 0.026 | 100 | 438.0 r/s | 2.283 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.344 | 0.051 | 100 | 291.0 r/s | 3.437 ms | ws[id] sample=100 |
| query | operator_contains | 0.317 | 0.141 | 100 | 315.2 r/s | 3.173 ms | id in ws sample=100 |
| query | workspace_count | 1.514 | 0.002 | 1,000 | 660.5 r/s | 1.514 ms | ws.count(entity) |
| query | workspace_len | 0.003 | 0.000 | 1,000 | 368,403 r/s | 0.003 ms | len(ws) |
| query | iter_workspace | 0.094 | 0.004 | 1,000 | 10,593 r/s | 0.094 ms | list(iter(ws)) |
| query | ids_entity | 0.097 | 0.009 | 1,000 | 10,320 r/s | 0.097 ms | ws.ids(entity) |
| query | rows_entity | 0.086 | 0.012 | 1,000 | 11,638 r/s | 0.086 ms | ws.rows(entity) |
| query | items_entity | 0.092 | 0.016 | 1,000 | 10,881 r/s | 0.092 ms | ws.items(entity) |
| query | filter_gt | 0.071 | 0.007 | 1,000 | 14,143 r/s | 0.071 ms | price > 50 |
| query | filter_range | 0.041 | 0.003 | 1,000 | 24,548 r/s | 0.041 ms | price in [50,150] |
| query | filter_text | 0.017 | 0.002 | 1,000 | 57,415 r/s | 0.017 ms | body.match(audio) |
| query | filter_bool_tree | 0.103 | 0.010 | 1,000 | 9,745 r/s | 0.103 ms | bool tree |
| query | complex_query | 0.015 | 0.001 | 1,000 | 65,097 r/s | 0.015 ms | 10+ filter complex |
| query | vector_near | 0.101 | 0.015 | 1,000 | 9,867 r/s | 0.101 ms | near top_k=10 |
| query | hybrid_query | 0.016 | 0.001 | 1,000 | 61,511 r/s | 0.016 ms | filter + near top_k=10 |
| query | count_all | 0.080 | 0.014 | 1,000 | 12,478 r/s | 0.080 ms | count |
| edit | batch_set_many | 0.282 | 0.025 | 1,000 | 3,549 r/s | 0.282 ms | set_many (1000) |
| removal | batch_delete_many | 0.081 | 0.005 | 1,000 | 12,286 r/s | 0.081 ms | delete_many (1000) |
| removal | count_after_delete | 0.054 | 0.031 | 1,000 | 18,427 r/s | 0.054 ms | count after delete |

---

## sqlite_milvus_vec (`sql+vector`) - scale=10x

- **Rows**: 10,000
- **Repeats**: 5
- **Total time**: 15.899 +/- 0.279s
- **Backends**: main=sqlite, vec=milvus

### Resources

| Sample | Memory (MB) | Disk (MB) | Notes |
|---|---:|---:|---|
| before_build | 1,843 | 0.000 |  |
| after_build | 1,846 | 11.6 |  |
| after_edit | 1,876 | 12.3 |  |
| after_removal | 1,877 | 12.3 |  |

### Stages

| Bucket | Stage | Mean (s) | Std (s) | Rows | Throughput | Latency (ms) | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| build | setup | 1.961 | 0.093 | - | - | - | workspace + entity registration |
| build | bulk_upsert | 1.535 | 0.097 | 10,000 | 6,516 r/s | 0.153 ms | upsert_many batch=1000 |
| query | batch_get_many | 0.087 | 0.011 | 1,000 | 11,537 r/s | 0.087 ms | get_many (1000) |
| query | batch_exists_many | 0.069 | 0.006 | 1,000 | 14,452 r/s | 0.069 ms | exists_many (1000) |
| query | point_lookups | 0.310 | 0.022 | 100 | 322.3 r/s | 3.102 ms | get sample=100 |
| query | point_get_entity | 0.230 | 0.035 | 100 | 434.9 r/s | 2.299 ms | get(entity=...) sample=100 |
| query | point_exists_entity | 0.236 | 0.014 | 100 | 424.0 r/s | 2.359 ms | exists(entity=...) sample=100 |
| query | operator_getitem | 0.313 | 0.048 | 100 | 319.5 r/s | 3.130 ms | ws[id] sample=100 |
| query | operator_contains | 0.254 | 0.011 | 100 | 393.7 r/s | 2.540 ms | id in ws sample=100 |
| query | workspace_count | 1.521 | 0.020 | 10,000 | 6,574 r/s | 0.152 ms | ws.count(entity) |
| query | workspace_len | 0.002 | 0.000 | 10,000 | 4,043,835 r/s | 0.000 ms | len(ws) |
| query | iter_workspace | 0.958 | 0.067 | 10,000 | 10,442 r/s | 0.096 ms | list(iter(ws)) |
| query | ids_entity | 0.881 | 0.058 | 10,000 | 11,355 r/s | 0.088 ms | ws.ids(entity) |
| query | rows_entity | 0.873 | 0.032 | 10,000 | 11,448 r/s | 0.087 ms | ws.rows(entity) |
| query | items_entity | 1.129 | 0.088 | 10,000 | 8,854 r/s | 0.113 ms | ws.items(entity) |
| query | filter_gt | 0.774 | 0.053 | 10,000 | 12,921 r/s | 0.077 ms | price > 50 |
| query | filter_range | 0.401 | 0.026 | 10,000 | 24,913 r/s | 0.040 ms | price in [50,150] |
| query | filter_text | 0.122 | 0.012 | 10,000 | 81,999 r/s | 0.012 ms | body.match(audio) |
| query | filter_bool_tree | 1.224 | 0.085 | 10,000 | 8,167 r/s | 0.122 ms | bool tree |
| query | complex_query | 0.017 | 0.001 | 10,000 | 589,749 r/s | 0.002 ms | 10+ filter complex |
| query | vector_near | 0.821 | 0.078 | 10,000 | 12,179 r/s | 0.082 ms | near top_k=10 |
| query | hybrid_query | 0.099 | 0.005 | 10,000 | 101,021 r/s | 0.010 ms | filter + near top_k=10 |
| query | count_all | 0.845 | 0.057 | 10,000 | 11,841 r/s | 0.084 ms | count |
| edit | batch_set_many | 0.306 | 0.029 | 1,000 | 3,264 r/s | 0.306 ms | set_many (1000) |
| removal | batch_delete_many | 0.093 | 0.006 | 1,000 | 10,804 r/s | 0.093 ms | delete_many (1000) |
| removal | count_after_delete | 0.837 | 0.131 | 10,000 | 11,945 r/s | 0.084 ms | count after delete |

---

## How to read this report

- **Build**: workspace/backend setup plus initial bulk upsert.
- **Query**: read, lookup, scalar filter, boolean filter, vector near, and near+filter operations.
- **Edit**: data patch/update operations such as `set_many`.
- **Removal**: deletion and post-delete validation stages.
- **Resource samples**: process RSS and backend path size sampled before build, after build, after edit, and after removal.
- **Stage errors**: failed repeats are excluded from stage means and listed in the notes.

## Running the benchmark

```bash
bash scripts/benchmark.bash
HB_BENCH_SCALES=1 HB_BENCH_REPEATS=1 bash scripts/benchmark.bash
HB_BENCH_COMBINATIONS=inmem_all,sqlite_all bash scripts/benchmark.bash
HB_BENCH_ARTIFACT=result.json HB_BENCH_REPORT=REPORT.md bash scripts/benchmark.bash
HB_BENCH_ALLOW_ERRORS=1 bash scripts/benchmark.bash
```
