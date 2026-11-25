#!/usr/bin/env python3
"""CLI wrapper for the HeavenBase benchmark package."""

from __future__ import annotations

import argparse
import sys

from heavenbase.benchmark import BenchmarkRunner, DefaultProductScenario
from heavenbase.context import DEFAULT_CONTEXT


def _as_bool(value: object) -> bool:
    """Return a configuration value as a CLI boolean."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_args() -> argparse.Namespace:
    """Parse explicit CLI options without opening runtime authorities."""
    parser = argparse.ArgumentParser(description="Run HeavenBase backend-combination benchmarks.")
    parser.add_argument("--scales", default=argparse.SUPPRESS, help="comma-separated scale factors")
    parser.add_argument("--repeats", type=int, default=argparse.SUPPRESS, help="repetitions per scale")
    parser.add_argument("--seed", type=int, default=argparse.SUPPRESS, help="StableRNG seed")
    parser.add_argument("--combinations", default=argparse.SUPPRESS, help="comma-separated registered combination labels")
    parser.add_argument("--base-rows", type=int, default=argparse.SUPPRESS, help="base row count multiplied by each scale")
    parser.add_argument("--vector-dim", type=int, default=argparse.SUPPRESS, help="vector dimension for the default scenario")
    parser.add_argument("--top-k", type=int, default=argparse.SUPPRESS, help="top_k for vector near stages")
    parser.add_argument("--temp-root", default=argparse.SUPPRESS, help="temporary benchmark artifact root")
    parser.add_argument("--artifact", default=argparse.SUPPRESS, help="optional JSON artifact path")
    parser.add_argument("--include-raw", action="store_true", default=argparse.SUPPRESS, help="include raw repeats in JSON artifact")
    parser.add_argument("--report", default=argparse.SUPPRESS, help="markdown report path")
    parser.add_argument("--allow-errors", action="store_true", default=argparse.SUPPRESS, help="write reports even when stages fail")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    context = DEFAULT_CONTEXT
    options = {
        "scales": (1, 10),
        "repeats": 5,
        "seed": 42,
        "combinations": "",
        "base_rows": 1_000,
        "vector_dim": 1024,
        "top_k": 10,
        "temp_root": ".tmp",
        "artifact": "",
        "include_raw": False,
        "report": "BENCHMARK.md",
        "allow_errors": False,
    }
    options.update(context.config.snapshot().get("heavenbase.benchmark", default={}))
    options.update(vars(args))
    scenario = DefaultProductScenario(
        label="product",
        base_rows=int(options["base_rows"]),
        vector_dim=int(options["vector_dim"]),
        top_k=int(options["top_k"]),
    )
    runner = BenchmarkRunner(
        context,
        scenario=scenario,
        combinations=options["combinations"] or None,
        scales=options["scales"],
        repeats=int(options["repeats"]),
        seed=int(options["seed"]),
        temp_root=str(options["temp_root"]),
    )
    report = runner.run()
    report.print()
    artifact = str(options["artifact"] or "")
    report_path = str(options["report"] or "")
    if artifact:
        report.write_json(artifact, include_raw=_as_bool(options["include_raw"]))
        print(f"[benchmark] JSON artifact written to {artifact}")
    if report_path:
        report.write_markdown(report_path)
        print(f"[benchmark] Markdown report written to {report_path}")
    if report.has_errors() and not _as_bool(options["allow_errors"]):
        print(f"[benchmark] failed: {report.error_count()} stage error(s). Re-run with --allow-errors for diagnostic baselines.")
        sys.exit(1)
    print("[benchmark] complete.")


if __name__ == "__main__":
    main()
