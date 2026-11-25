#!/usr/bin/env bash
# Run the Context-bound HeavenBase benchmark CLI.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${ROOT}"
source "${ROOT}/scripts/_env.bash"

run_python scripts/benchmark.py "$@"
