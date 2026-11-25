# Test suite

Tests are grouped by the subsystem whose public behavior they protect:

- `agents/`: Capsules, prompts, and agent-facing Toolkit integration.
- `backends/`: backend contracts and provider-specific execution.
- `benchmark/`: Context authority, registered recipes, and runner lifecycle.
- `capabilities/`: registered cross-family capability inspection.
- `config/`: bootstrap, configuration, and migration behavior.
- `core/`: public API, logical types, utilities, and core services.
- `execution/`: neutral fragments, result frames, and portable handler protocols.
- `extensions/`: extension registration and extension-owned behavior.
- `interfaces/`: CLI, dashboard, and MCP adapters.
- `llm/`: LLM runtime, provider routes, semantic caching, and sessions.
- `placement/`: logical storage rules, profiles, plans, and resolution.
- `query/`: query syntax, routing, filtering, aggregation, and indexes.
- `registry/`: in-memory and persistent registry contracts.
- `scripts/`: generated metadata and release-tooling contracts.
- `workspace/`: workspace manifests, presets, registration, and row commands.

Tests also have two validation tiers:

- `fast`: a curated daily integrity suite of stable public contracts, boundary validation, configuration, query syntax, storage protocols, and the active SQL text-semantics regression surface.
- `full`: broader integration, backend, provider, implementation-detail, slow, external-service, and online-LLM validation for major merges and releases.

The daily tier must fit the hosted fast-test job: two CPUs, four workers, and a 45-minute timeout. A `fast` test that takes 3 seconds or more, measured with `--durations=0` while each worker has its own core, is marked `@pytest.mark.slow` and runs in `full`. A module keeps its cheapest test in the daily tier when every other test would leave it and that test takes 5 seconds or less.

Tests are behavioral evidence for **current** architecture claims. Acceptance tests for an accepted **target** should fail or remain task-scoped until the owning implementation lands; an ADR or roadmap item alone must not be treated as shipped behavior. Active test follow-up belongs in `docs/tasks.yaml`, not in this file.

Run tests only through `scripts/test.bash`. Its default marker filter is `fast and not full`, with xdist work-stealing enabled whenever `pytest-xdist` is installed. Local fast-tier runs use xdist's auto-detected worker count capped at 10; full-tier runs leave that count uncapped. Set `HB_TEST_WORKERS` to override either policy (for example, `HB_TEST_WORKERS=8` for a lower-impact run) or `HB_TEST_PARALLEL=0` to diagnose a concurrency-sensitive failure. CI pins its own worker count.

Measured on Linux aarch64 servers for this release: the default xdist suite (10 workers) takes about 5 minutes. On two CPUs with four workers, as on the hosted runner, the daily tier takes about 13 minutes and the complete credential-free `fast or full` suite about 82 minutes; the **Python Tests** full job allows 125 minutes and four workers. With four workers, a worker of the complete suite can hold more than 1,024 files open at once, the usual soft limit (`ulimit -n`); raise it for such a run, as hosted runners allow 65,536. The hosted compatibility matrix runs only the daily tier; the broader tier is a single, explicit **Python Tests** workflow dispatch and is not repeated by PyPI publication.

```bash
rtk bash scripts/test.bash
rtk bash scripts/test.bash tests/query/test_query_spec.py -q
rtk bash scripts/test.bash -m full
rtk bash scripts/test.bash -m external
rtk bash scripts/test.bash -m llm
```

`-m full` is local and credential-free: external tests remain selected for reporting but skip unless the expression explicitly requests `external` or `HB_TEST_EXTERNAL=1` is set.

## Compression rationale

The subsystem layout keeps public contracts, durable regressions, meaningful failure paths, and real provider routes easy to find. The curated module allowlist in `conftest.py` keeps the daily tier compact; explicit `fast` markers can admit focused regressions, while new unclassified tests safely enter `full`. Six repeated SQL scalar-filter tests were merged into one behavioral contract with shared setup. The private registry-reset path test was removed because it asserted one helper's internal path derivation rather than observable registry behavior; persistent registry reset and backend behavior remain covered at their public boundaries. The release pipeline runs all 2,536 daily tests on each interpreter and leaves the 2,904 further tests of the broader tier to the single full job, avoiding four redundant executions of that tier.
