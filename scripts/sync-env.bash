#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${ROOT}"
source "${ROOT}/scripts/_env.bash"

CHECK=0
SYNC=1
UV_SYNC_ARGS=(--all-extras)
README_ARGS=()

usage() {
    cat <<'EOF'
Usage: bash scripts/sync-env.bash [OPTIONS] [-- UV_SYNC_ARGS...]

Synchronize package metadata, lockfiles, and generated compatibility files.

src/heavenbase/version.py owns the package version projected into Poetry metadata
and built-in meta.yaml headers. requirements*.txt own dependencies. This script
refreshes those projections, uv.lock, poetry.lock, and environment-*.yml.

Options:
  --check       Verify version projections, locks, and generated files are current.
  --no-sync     Update lock/generated files without running uv sync.
  --readme-target PATH
                Also sync/check README.en.md to PATH.
  --readme-resource-target IMPORT_NAME
                Also sync/check src/IMPORT_NAME/resources/README.md.
  -h, --help    Show this help message.

Examples:
  bash scripts/sync-env.bash
  bash scripts/sync-env.bash --check
  bash scripts/sync-env.bash -- --extra sql
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            CHECK=1
            SYNC=0
            shift
            ;;
        --no-sync)
            SYNC=0
            shift
            ;;
        --readme-target)
            if [[ $# -lt 2 ]]; then
                echo "error: --readme-target requires a value" >&2
                exit 1
            fi
            README_ARGS+=(--target "$2")
            shift 2
            ;;
        --readme-resource-target)
            if [[ $# -lt 2 ]]; then
                echo "error: --readme-resource-target requires a value" >&2
                exit 1
            fi
            README_ARGS+=(--resource-target "$2")
            shift 2
            ;;
        --)
            shift
            UV_SYNC_ARGS+=("$@")
            break
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            UV_SYNC_ARGS+=("$1")
            shift
            ;;
    esac
done

resolve_uv

sync_poetry_lock() {
    "${UV_BIN}" run --with poetry poetry lock
    echo "[sync-env] refreshed poetry.lock"
}

check_poetry_lock() {
    if [[ ! -f poetry.lock ]]; then
        echo "error: poetry.lock is missing; run bash scripts/sync-env.bash" >&2
        exit 1
    fi
    "${UV_BIN}" run --with poetry poetry check
    echo "[sync-env] poetry.lock is current"
}

if [[ "${CHECK}" -eq 1 ]]; then
    UV_BIN="${UV_BIN}" run_uv_python scripts/sync-env.py --check
    "${UV_BIN}" lock --check
    check_poetry_lock
    run_with_array README_ARGS bash scripts/sync-readme.bash --check
    echo "[sync-env] generated files are current"
    exit 0
fi

UV_BIN="${UV_BIN}" run_uv_python scripts/sync-env.py
"${UV_BIN}" lock
sync_poetry_lock
if [[ "${SYNC}" -eq 1 ]]; then
    "${UV_BIN}" sync "${UV_SYNC_ARGS[@]}"
fi
run_with_array README_ARGS bash scripts/sync-readme.bash
