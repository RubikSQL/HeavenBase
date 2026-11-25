# SQL text fallback estimate

Status: design estimate, not production telemetry

Date: 2026-07-20

Authority: [ADR 0010](../architecture/adr/0010-text-semantics-parity.md) and the
[boundary register](../architecture/text-semantics-boundaries.md)

## Result

The safe-default implementation now has provider-owned parity slices for 2 of
12 registered SQL dialect identifiers (16.7%): SQLite and MySQL. Registration
identifiers include aliases and specializations, so this is not an engine-market
share metric. New HeavenBase-managed SQLite fields have an enforced physical
contract. MySQL fields qualify only when live evidence proves MySQL (not
MariaDB/OceanBase), `utf8mb4` client/connection/column character sets, a
supported character type, and sufficient declared width.

Under the illustrative daily-operation mix below, the estimated text-operation
fallback ratios are:

| Route policy / field contract | Estimated fallback |
| --- | ---: |
| Managed SQLite, portable default | 21% |
| Evidence-qualified MySQL, portable default | 54% |
| Any currently unproved SQL provider, portable default | 100% |
| Illustrative backend mix, portable default | 53.6% |
| Illustrative backend mix with `unsafe()` | about 1.7% |

The final row is an availability estimate, not a parity claim. It explicitly
allows provider-dependent behavior. Unsupported compilers, structured logical
types, value-specific wildcard guards, split storage, and near-route constraints
can increase its actual fallback rate.

## Assumed daily-operation mix

This model counts text predicate or aggregate-key operations, not queries, rows,
latency, or data volume. It is intended to compare implementation slices until
privacy-preserving route telemetry supplies real workload weights.

| Operation family | Weight | Safe SQLite | Safe MySQL |
| --- | ---: | :---: | :---: |
| Equality | 28% | yes | yes |
| Membership | 14% | yes | yes |
| Inequality | 4% | yes | yes |
| Ordered comparisons (`lt`/`lte`/`gt`/`gte`) | 4% | yes | no |
| Case-sensitive literal `contains` | 15% | yes | no |
| Case-insensitive literal `match` | 12% | no | no |
| `starts_with` | 7% | yes | no |
| `ends_with` | 3% | yes | no |
| `like` / `ilike` | 5% | no | no |
| Wildcard | 2% | no | no |
| Regex | 2% | no | no |
| Group / distinct / text `min` or `max` keys | 4% | yes | no |

The illustrative backend mix is 50% SQLite, 25% PostgreSQL/PgVector, 15%
MySQL, 5% SQL Server, and 5% other SQL providers. With safe fallback rates of
21%, 100%, 54%, 100%, and 100%, respectively, the weighted result is 53.6%.
Changing the workload or backend mix changes the estimate directly.
For the unsafe-routing row, the 5% “other” share uses the StarRocks/Trino
compiler set: `0.50×2% + 0.25×0% + 0.15×2% + 0.05×4% + 0.05×4% = 1.7%`.

## Registered compiler estimate

`unsafe()` bypasses portable proof and static text-safety guards while
still requiring a raw compiler. Applying the same operation weights gives the following
best-case fallback estimate; aggregate-key weight is counted only when that
dialect has a native aggregate compiler.

| Dialect identifier | Safe coverage | `unsafe()` compiler coverage | Estimated unsafe fallback |
| --- | ---: | ---: | ---: |
| `sqlite` | 79% | 98% | 2% |
| `mysql` | 46% | 98% | 2% |
| `postgres`, `postgresql`, `duckdb` | 0% | 100% | 0% |
| `mssql` | 0% | 96% | 4% |
| `pgvector`, generic `sql` | 0% | 92% | 8% |
| `oceanbase` | 0% | 92% | 8% |
| `oracle` | 0% | 96% | 4% |
| `starrocks`, `trino` | 0% | 96% | 4% |

These figures are computed from registered operation metadata. They do not
promise index use, equal results, connector support, or lossless storage.
Oracle's empty-string/null collapse is one example where a low unsafe
fallback estimate can still have a serious correctness boundary.

## Next reductions

1. Measure route choices without logging operands or values, then replace the
   assumed operation and backend weights with observed distributions.
2. Add provider-owned Unicode storage and exact equality evidence for SQL
   Server before enabling any safe operation there.
3. Design a PostgreSQL storage-domain decision for embedded NUL before claiming
   full Python-string parity; deterministic collation alone is insufficient.
4. Extend MySQL ordering or key parity only with differential corpus tests and
   an expression that remains index-friendly.
5. Treat pattern, wildcard, and regex subsets independently; compiler existence
   must not be converted into safe proof.

Do not interpret this report as permission to weaken the portable default.
Every safe-coverage increase still requires provider-owned DDL/evidence,
native-versus-scan differential tests, and accurate `explain()` provenance.
