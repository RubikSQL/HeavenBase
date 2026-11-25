#!/usr/bin/env bash

# Create and seed the registered "demo" workspace for hb dashboard.
# Usage: bash scripts/demo-workspace.bash [--ws-id NAME]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_env.bash
source "${SCRIPT_DIR}/_env.bash"

cd "${SCRIPT_DIR}/.."
resolve_uv
run_python scripts/demo_workspace.py "$@"
