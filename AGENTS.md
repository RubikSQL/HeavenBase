# HeavenBase Agent Guide

This repo follows the globally installed `heaven-style` skill. Treat that installation as manually managed external tooling: ordinary HeavenBase work must not locate, clone, synchronize with, or modify Blueprint. Do not track a mirrored `.agents/skills/heaven-style` copy in HeavenBase and do not recreate `.github/skills`, `.codex/skills`, `.cursor/`, `.opencode/`, `.kilo/skills`, or `~/.claude/skills/heaven-style`. If the global skill is missing or stale, report the tooling issue for separate manual maintenance instead of adding a repository copy.

## Environment

Source of truth: **`requirements*.txt`**. `pyproject.toml` reads those files through setuptools dynamic metadata.

Install priority: **uv -> pip (requirements) -> pyproject -> conda -> poetry**.

- Edit `requirements.txt` and optional `requirements-*.txt` files, then run `bash scripts/sync-env.bash`.
- `uv.lock`, `environment-*.yml`, `poetry.lock`, Poetry metadata, and built-in module version headers are synchronized by `scripts/sync-env.bash`; `scripts/sync-env.bash --check` is the CI drift gate.
- Keep the package version only in `src/heavenbase/version.py`. The sync command projects it into every packaged `meta.yaml` as the built-in module version and minimum compatibility bound; review and stage those mixed-content descriptors after a version bump.
- New Bash wrappers must source `scripts/_env.bash` and use `resolve_uv`, `run_python`, or `run_uv_python`.
- Git hooks live in `.githooks`; configure a checkout with `git config core.hooksPath .githooks`. Pre-commit runs env/README sync, validates the docs/task contract, stages standalone generated files, and runs `scripts/flake.bash --all`; generated header edits inside `meta.yaml` remain explicit review-and-stage changes.

## Commands

Prefix commands with `rtk` in local agent sessions that provide it.

```bash
rtk bash scripts/sync-env.bash
rtk uv run python scripts/docs.py tasks --ready
rtk uv run python scripts/docs.py check
rtk bash scripts/flake.bash --ci
rtk bash scripts/test.bash
rtk uv build
rtk bash scripts/release.bash
rtk bash scripts/benchmark.bash
```

### Testing

**Always run tests via `bash scripts/test.bash`.** Never invoke `pytest` directly. Pass paths, `-m`, `-q`, and other pytest-style args to the wrapper; only those args change what runs.

Default `bash scripts/test.bash` runs the fast integrity suite through the wrapper-owned marker filter `fast and not full`. The wrapper uses the dev extra so `pytest-xdist` is available and runs in parallel by default whenever xdist is installed. During iteration, prefer targeted wrapper runs and reserve `-m full` or broad all-marker runs for merge/PR/release checks:

```bash
rtk bash scripts/test.bash tests/query/test_query_spec.py -q
rtk bash scripts/test.bash tests/core/test_core.py -q
```

Opt into heavier or narrower suites with wrapper args: `-m full` for the full opt-in marker set, `-m detail` for provider/implementation-detail contracts, `-m slow`, `-m external`, `-m llm`, or `-m ""` for everything. Set `HB_TEST_PARALLEL=0` to force sequential runs when needed. The wrapper owns marker policy, dev extras, cleanup, and Docker handling.

Tests consume only explicit process environment, named test configuration, and CI secrets. The runner must not source personal shell startup files such as `~/.bashrc`; inject provider credentials deliberately for the external suite that owns them.

- Narrow env sync with `bash scripts/sync-env.bash -- --extra dev` or `uv sync --extra sql` when needed.
- Start database services with `bash scripts/docker-restart.bash /d/databases/` for direct external-backend testing; `scripts/test.bash` auto-starts them when external tests are selected.
- Benchmark docs live at `docs/resources/reports/benchmark.md`; the root `BENCHMARK.md` is the generated latest benchmark artifact.

## Docs and Work Authority

- `README.en.md` is canonical user documentation; `README.md` and the packaged README are generated copies.
- `docs/README.md` defines the four documentation surfaces, authority order, and current engineering map.
- `docs/tasks.yaml` is the only writable queue for active, resumable work. Issues, PRs, ADRs, reports, and chat may link to it but do not own task status.
- `docs/DEVLOG.md` is the rolling, newest-first handoff log. It records concise change and verification evidence, not active task state.
- `docs/scratch/` is an expiring, non-authoritative lane; local-only notes belong under ignored `.temp/notes/`.
- `docs/goals/current.md` distinguishes current behavior, accepted targets, known gaps, operating priorities, and non-goals. `docs/goals/roadmap.md` owns horizon-level direction only.
- `docs/resources/architecture/` and `docs/resources/reference/` own durable engineering contracts. `docs/resources/reports/` holds current generated or deliberately refreshed evidence.

Before starting resumable work, read `docs/tasks.yaml` and use the existing task that owns the outcome. A direct one-session request need not enter the queue if it is completed in that session. Close substantial work with verification, one concise development-log entry, removal of completed queue state, and promotion or deletion of scratch material.

## Architecture Status

- **Current:** the checked-out source is the `0.1.2.3` train. ADR 0011's standalone Registry and Registry-first resolution contracts, ADR 0012's module-folder protocol, Context-local resolution, and the role-based package hierarchy are implemented; read `docs/goals/current.md` and `docs/resources/architecture/mental-model.md` for current ownership boundaries.
- **Target:** Preserve the one-Registry, one-module-protocol baseline while designing remote artifact acquisition and ecosystem trust as separately accepted work.
- **Gap:** no active architecture migration remains. Remote acquisition, signatures, and trust policy are future work; local module roots already use verified content-addressed artifacts.
- **Non-goal:** do not introduce a second planner, a second Registry authority, central provider branches, or permanent compatibility shims.

An accepted ADR is not evidence of shipped behavior. Code, tests, generated artifacts, and current public documentation outrank aspirational prose.

## Standards

- Black + Flake8 clean at every commit. Line length is 160; ignores live in `setup.cfg`.
- Public APIs need type hints and docstrings.
- Use short HeavenBase OOP vocabulary: `to_dict`, `load`, `save`, `get`, `set`, `upsert`, `to_str`.
- Add backends, strategies, and operations by registering handlers/builders, not by hard-coding planner branches.
- Runtime examples should live in docs or tests unless a release explicitly adds a maintained demo; generated local data belongs in `.tmp/`.
- For LLM integration tests, use the real cheapest viable chat preset: `deepseek-v4-flash`. Do not use expensive reasoning models or mocks for those integration tests.
