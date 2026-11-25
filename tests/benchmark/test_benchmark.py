"""Benchmark Context authority and registered-recipe contracts."""

from __future__ import annotations

import pytest

from heavenbase import DEFAULT_CONTEXT, Context, Entity, ShortText, field
from heavenbase.benchmark import (
    MISSING_STAGE_ERROR,
    BackendCombination,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkScenario,
    BenchRun,
    ResourceSample,
    StageResult,
)
from heavenbase.modules import ModuleService
from heavenbase.utils.config import ConfigManager
from heavenbase.utils.registry import Registry

pytestmark = pytest.mark.fast


def _context(root) -> Context:
    """Return an isolated in-memory benchmark authority."""
    return Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {
                "type": "inmem",
                "module": "heavenbase.backends.inmem.backend",
                "name": "system",
            },
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(default={}, setup=False),
    )


def _register_recipe(service: ModuleService, identifier: str, backend_type: str) -> None:
    """Publish one inert benchmark recipe through the normal module protocol."""
    service.register(
        "benchmark_combination",
        identifier,
        {
            "name": identifier,
            "desc": "Test recipe.",
            "source": "inline",
            "target": "definition",
            "active": True,
            "meta": {
                "schema_version": 1,
                "tags": {"benchmark-suites": ["default"]},
                "definition": {
                    "combination_type": "test",
                    "backends": {"main": {"type": backend_type}},
                },
            },
        },
    )


def test_registered_recipes_are_resolver_local_and_inline() -> None:
    first = ModuleService(Registry("system-modules"))
    second = ModuleService(Registry("system-modules"))
    _register_recipe(first, "same-recipe", "inmem")
    _register_recipe(second, "same-recipe", "sqlite")

    first_recipe = BackendCombination.load("same-recipe", resolver=first)
    second_recipe = BackendCombination.load("same-recipe", resolver=second)

    assert first_recipe.backends["main"]["type"] == "inmem"
    assert second_recipe.backends["main"]["type"] == "sqlite"
    assert BackendCombination.list(resolver=first) == (first_recipe,)
    assert BackendCombination.list(resolver=second) == (second_recipe,)


def test_bundled_recipe_catalog_has_a_dependency_light_default_suite(tmp_path) -> None:
    context = _context(tmp_path)
    try:
        context._setup_modules()
        default = BackendCombination.list(resolver=context.modules())
        all_recipes = BackendCombination.list(resolver=context.modules(), suite=None)

        assert tuple(recipe.label for recipe in default) == (
            "inmem_all",
            "sqlite_all",
            "sqlite_inmem_vec",
            "inmem_sqlite_vec",
        )
        assert {"sqlite_lance_vec", "elasticsearch_inmem_vec", "pgvector_all"}.issubset(recipe.label for recipe in all_recipes)
    finally:
        context.close()


def test_combination_rejects_a_route_without_a_backend() -> None:
    with pytest.raises(ValueError, match="absent from backends"):
        BackendCombination(
            label="invalid-route",
            combination_type="test",
            backends={"main": {"type": "inmem"}},
            vector_backend="missing",
        )


def test_combination_snapshots_caller_owned_backend_specs() -> None:
    backends = {"main": {"type": "inmem", "options": {"mode": "first"}}}
    combination = BackendCombination("snapshot", "test", backends)

    backends["main"]["type"] = "sqlite"
    backends["main"]["options"]["mode"] = "second"

    assert combination.backends["main"]["type"] == "inmem"
    assert combination.backends["main"]["options"]["mode"] == "first"
    clone = BackendCombination("snapshot-clone", "test", combination.backends)
    assert clone.backends == combination.backends
    with pytest.raises(TypeError):
        combination.backends["main"]["type"] = "sqlite"  # type: ignore[index]


class _TinyScenario(BenchmarkScenario):
    """Minimal workload proving Context-bound Backend construction."""

    label = "tiny"
    base_rows = 2
    seen_context = None

    def make_workspace(self, ctx, combination, backends):
        """Capture and delegate the Context-owned workspace construction."""
        self.seen_context = ctx.context
        return super().make_workspace(ctx, combination, backends)

    def make_entity(self, ctx, combination):
        """Return a scalar-only Entity routed by the selected recipe."""

        class BenchmarkNote(Entity):
            identifier = "benchmark-note"

            title = field(ShortText).store(to=combination.scalar_backend)

        return BenchmarkNote

    def make_rows(self, n, rng):
        """Return deterministic scalar rows for the tiny scenario."""
        return [{"object_id": f"note-{index}", "title": f"Note {index}"} for index in range(n)]


def test_runner_uses_its_explicit_context_for_workspace_and_backends(tmp_path) -> None:
    context = _context(tmp_path / "context")
    scenario = _TinyScenario()
    combination = BackendCombination(
        label="direct-inmem",
        combination_type="test",
        backends={"main": {"type": "inmem"}},
    )
    try:
        context._setup_modules()
        runner = BenchmarkRunner(
            context,
            scenario=scenario,
            combinations=[combination],
            scales=[1],
            repeats=1,
            seed=7,
            temp_root=str(tmp_path / "runs"),
        )

        report = runner.run()

        assert scenario.seen_context is context
        assert scenario.seen_context is not DEFAULT_CONTEXT
        assert report.runs[0].backend_identifiers == {"main": "inmem"}
        assert report.has_errors() is False
    finally:
        context.close()


def _bench_run(repeat: int, *, aborted: bool = False) -> BenchRun:
    """Build one hand-made repeat, optionally aborting the way `run_one` does at a failed setup."""
    run = BenchRun("sqlite_all", "pure-sql", {"main": "sqlite"}, "product", 1, repeat, 1_000)
    run.resources.append(ResourceSample("before_build", 100.0, 0.0))
    if aborted:
        run.stages.append(StageResult("setup", "build", 0.05, 0, "ERROR: locked", "OSError", "locked"))
        return run
    run.stages.append(StageResult("setup", "build", 0.1, 0, "workspace + entity registration"))
    run.stages.append(StageResult("bulk_upsert", "build", 10.0, 1_000, "upsert_many batch=100"))
    run.resources.append(ResourceSample("after_build", 250.0, 33.0))
    run.stages.append(StageResult("filter_text", "query", 0.0, 1_000, "ERROR: no match", "NotImplementedError", "no match"))
    return run


@pytest.mark.parametrize("aborted_repeat", [1, 3])
def test_aggregate_excludes_an_aborted_repeat_instead_of_averaging_a_zero(aborted_repeat) -> None:
    """A repeat that never reached a stage must not make that stage look fast, wherever it lands in the group."""
    runs = [_bench_run(repeat, aborted=repeat == aborted_repeat) for repeat in range(1, 6)]
    aggregate = BenchmarkReport(runs, meta={"repeats": 5}).aggregated()[0]
    upsert = next(stage for stage in aggregate.stages if stage.name == "bulk_upsert")

    assert upsert.mean_sec == 10.0
    assert (upsert.samples, upsert.repeats, upsert.partial) == (4, 5, True)
    assert upsert.bucket == "build"
    assert upsert.rows == 1_000
    assert [error["type"] for error in upsert.errors] == [MISSING_STAGE_ERROR]
    assert upsert.errors[0]["repeat"] == aborted_repeat
    assert "4/5" in upsert.notes
    assert aggregate.bucket("build").mean_sec == pytest.approx(10.1)
    assert (aggregate.samples, aggregate.expected_samples) == (8, 15)


def test_a_stage_no_repeat_measured_publishes_no_mean_and_shrinks_no_bucket() -> None:
    """A fully failed stage must report `None`, not a zero that reads as instantaneous."""
    aggregate = BenchmarkReport([_bench_run(repeat) for repeat in range(1, 4)], meta={"repeats": 3}).aggregated()[0]
    failed = next(stage for stage in aggregate.stages if stage.name == "filter_text")
    query = aggregate.bucket("query")

    assert (failed.mean_sec, failed.std_sec, failed.samples, failed.throughput, failed.latency_ms) == (None, None, 0, None, None)
    assert failed.to_dict()["mean_sec"] is None
    assert (query.mean_sec, query.stages, query.measured, query.partial) == (None, 1, 0, True)
    assert "-" in BenchmarkReport([_bench_run(1)], meta={"repeats": 1}).to_markdown()


def test_resource_delta_refuses_two_means_taken_over_different_repeats() -> None:
    """`before_build` covering more repeats than `after_build` is a population change, not a phase measurement."""
    clean = BenchmarkReport([_bench_run(repeat) for repeat in range(1, 4)], meta={"repeats": 3}).aggregated()[0]
    mixed = BenchmarkReport([_bench_run(repeat, aborted=repeat == 2) for repeat in range(1, 4)], meta={"repeats": 3}).aggregated()[0]

    assert clean.resource_delta("before_build", "after_build", "memory_mb") == pytest.approx(150.0)
    assert clean.resource_delta("before_build", "after_build", "disk_mb") == pytest.approx(33.0)
    assert mixed.resource_delta("before_build", "after_build", "memory_mb") is None
    assert mixed.resource_delta("before_build", "after_build", "disk_mb") is None
    assert mixed.to_dict()["resource_deltas"]["build_memory_mb"] is None

    after_build = mixed.resource("after_build")
    assert (after_build.samples, after_build.memory_samples, after_build.repeats, after_build.partial) == (2, 2, 3, True)
    assert "partial mean over 2/3 repeats" in after_build.notes
    assert mixed.resource("before_build").samples == 3


def test_markdown_publishes_the_sample_count_beside_every_mean() -> None:
    """A reader must never have to infer how many repeats a published mean covers."""
    report = BenchmarkReport([_bench_run(repeat, aborted=repeat == 2) for repeat in range(1, 6)], meta={"repeats": 5, "scales": [1]})
    markdown = report.to_markdown()

    assert "| Repeats | Samples |" in markdown
    assert "| 5 | 8/15 |" in markdown
    assert "> **WARNING**: 5 stage error(s) in this run." in markdown
    assert "| Mean (s) | Std (s) | Samples |" in markdown
    assert "| build | bulk_upsert | 10.000 | 0.000 | 4/5 | 1,000 |" in markdown
    assert "| query | filter_text | - | - | 0/5 |" in markdown
    assert "| 250.0 | 33.0 | 4/5 | partial mean over 4/5 repeats |" in markdown
    assert "HB_BENCH_" not in markdown


def test_runner_requires_a_context_and_known_registered_recipe() -> None:
    with pytest.raises(TypeError, match="explicit ModuleResolver"):
        BackendCombination.list(resolver=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="explicit ModuleResolver"):
        BackendCombination.load("missing-recipe", resolver=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="explicit Context"):
        BenchmarkRunner(None)  # type: ignore[arg-type]
    with pytest.raises(KeyError, match="Unknown benchmark combination"):
        BenchmarkRunner(DEFAULT_CONTEXT, combinations=["missing-recipe"])
