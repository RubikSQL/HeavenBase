#!/usr/bin/env bash
# Test Runner with automatic artifact cleanup and Docker auto-start.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${ROOT}"
source "${ROOT}/scripts/_env.bash"

# Keep the FastMCP 4 protocol-model migration honest in local and CI runs.
export FASTMCP_MCP_CAMELCASE_COMPAT="${FASTMCP_MCP_CAMELCASE_COMPAT:-false}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== HeavenBase Test Runner ===${NC}"

# ── Docker auto-start for external tests ──

_ensure_docker_services() {
    # Only proceed if docker is available
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    if ! docker info >/dev/null 2>&1; then
        echo -e "${YELLOW}Docker daemon not available; skipping auto-start.${NC}"
        return 0
    fi

    # Discover compose file
    local compose_file="${COMPOSE_FILE:-}"
    if [[ -z "${compose_file}" ]]; then
        for candidate in \
            "${DATABASE_DIR:-}/docker-compose.yml" \
            "D:/databases/docker-compose.yml" \
            "/d/databases/docker-compose.yml" \
            "/mnt/d/databases/docker-compose.yml" \
            "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/../databases/docker-compose.yml"
        do
            if [[ -f "${candidate}" ]]; then
                compose_file="${candidate}"
                break
            fi
        done
    fi

    if [[ -z "${compose_file}" ]] || [[ ! -f "${compose_file}" ]]; then
        echo -e "${YELLOW}Docker compose file not found; skipping auto-start.${NC}"
        return 0
    fi

    local compose_dir
    compose_dir="$(cd -- "$(dirname -- "${compose_file}")" && pwd -P)"

    # Check if any required services are already healthy
    local ps_json
    ps_json=$(docker compose --project-directory "${compose_dir}" -f "${compose_file}" ps --format json 2>/dev/null || true)

    if [[ -z "${ps_json}" ]]; then
        echo -e "${YELLOW}No Docker containers found; will start services.${NC}"
    else
        local desired_count running_count
        desired_count=$(docker compose --project-directory "${compose_dir}" -f "${compose_file}" config --services 2>/dev/null | sed '/^$/d' | wc -l | tr -d ' ')
        running_count=$(printf '%s\n' "${ps_json}" | run_python -c "
import sys
print(sum(1 for line in sys.stdin.read().strip().split('\\n') if line))
" 2>/dev/null || echo "0")
        if [[ "${desired_count}" != "" ]] && (( running_count < desired_count )); then
            echo -e "${YELLOW}${running_count}/${desired_count} Docker service(s) found; will start missing services.${NC}"
            echo -e "${YELLOW}Starting Docker services from ${compose_file}...${NC}"
            docker compose --project-directory "${compose_dir}" -f "${compose_file}" up -d
            ps_json=$(docker compose --project-directory "${compose_dir}" -f "${compose_file}" ps --format json 2>/dev/null || true)
        fi

        local unhealthy
        unhealthy=$(printf '%s\n' "${ps_json}" \
            | run_python -c "
import json, sys
lines = sys.stdin.read().strip().split('\n')
count = 0
for line in lines:
    if not line:
        continue
    obj = json.loads(line)
    health = obj.get('Health', obj.get('State', ''))
    # Containers without a healthcheck report running state; treat as healthy.
    if health and health not in ('healthy', 'running'):
        count += 1
print(count)
" 2>/dev/null || echo "?")
        if [[ "${unhealthy}" == "0" ]]; then
            echo -e "${GREEN}Docker services are already healthy.${NC}"
            return 0
        fi
        echo -e "${YELLOW}${unhealthy} Docker service(s) not healthy; will restart.${NC}"
    fi

    echo -e "${YELLOW}Starting Docker services from ${compose_file}...${NC}"
    docker compose --project-directory "${compose_dir}" -f "${compose_file}" up -d

    echo -e "${YELLOW}Waiting for containers to become healthy (up to 5 min)...${NC}"
    local elapsed=0
    while true; do
        unhealthy=$(docker compose --project-directory "${compose_dir}" -f "${compose_file}" ps --format json 2>/dev/null \
            | run_python -c "
import json, sys
lines = sys.stdin.read().strip().split('\n')
count = 0
for line in lines:
    if not line:
        continue
    obj = json.loads(line)
    health = obj.get('Health', obj.get('State', ''))
    # Containers without a healthcheck report running state; treat as healthy.
    if health and health not in ('healthy', 'running'):
        count += 1
print(count)
" 2>/dev/null || echo "?")

        if [[ "${unhealthy}" == "0" ]]; then
            echo -e "${GREEN}All Docker services healthy!${NC}"
            break
        fi

        if (( elapsed >= 300 )); then
            echo -e "${RED}Timeout after 5 minutes. Some services may still be starting.${NC}"
            docker compose --project-directory "${compose_dir}" -f "${compose_file}" ps
            break
        fi

        echo "  ... ${unhealthy} container(s) not yet healthy (${elapsed}s elapsed)"
        sleep 10
        elapsed=$((elapsed + 10))
    done
}

_marker_expr_excludes_external_tests() {
    local marker_expr="$1"
    if [[ "${marker_expr}" == "full" ]] \
        || [[ "${marker_expr}" == *"not external"* ]] \
        || [[ "${marker_expr}" == *"non external"* ]] \
        || [[ "${marker_expr}" == *"not full"* ]]; then
        return 0
    fi
    if [[ "${marker_expr}" == *"fast"* ]] \
        && [[ "${marker_expr}" != *" or "* ]] \
        && [[ "${marker_expr}" != *"external"* ]] \
        && [[ "${marker_expr}" != *"full"* ]] \
        && [[ "${marker_expr}" != *"llm"* ]] \
        && [[ "${marker_expr}" != *"slow"* ]] \
        && [[ "${marker_expr}" != *"detail"* ]]; then
        return 0
    fi
    return 1
}

_marker_args_exclude_external_tests() {
    local args=("$@")
    local i=0

    while [[ $i -lt ${#args[@]} ]]; do
        local arg="${args[$i]}"
        if [[ "${arg}" == "-m" ]]; then
            local next_idx=$((i + 1))
            if [[ $next_idx -lt ${#args[@]} ]]; then
                if _marker_expr_excludes_external_tests "${args[$next_idx]}"; then
                    return 0
                fi
            fi
        elif [[ "${arg}" == "-m"* ]]; then
            if _marker_expr_excludes_external_tests "${arg#-m}"; then
                return 0
            fi
        fi
        i=$((i + 1))
    done

    return 1
}

# Detect if external tests would run.
_would_run_external_tests() {
    ! _marker_args_exclude_external_tests "$@"
}


# ── /Docker auto-start ──

cleanup() {
    echo -e "${YELLOW}Cleaning up test artifacts...${NC}"
    bash scripts/cleanup.bash > /dev/null 2>&1 || true
}

trap cleanup EXIT

echo -e "${YELLOW}Setting up test environment...${NC}"
mkdir -p ./.pytest_cache/cache

resolve_uv

TEST_ARGS=("$@")
if [[ ${#TEST_ARGS[@]} -eq 0 ]]; then
    TEST_ARGS=(tests -q)
fi

_has_marker_expr() {
    local args=("$@")
    local arg
    for arg in "${args[@]}"; do
        if [[ "${arg}" == "-m" ]] || [[ "${arg}" == "-m"* ]]; then
            return 0
        fi
    done
    return 1
}

_marker_expr_selects_full_tier() {
    local marker_expr="$1"
    local positive_expr="${marker_expr}"
    local marker

    if [[ -z "${marker_expr}" ]]; then
        return 0
    fi

    for marker in full detail slow external llm; do
        positive_expr="${positive_expr//not ${marker}/}"
    done

    for marker in full detail slow external llm; do
        if [[ " ${positive_expr} " =~ (^|[[:space:]()])${marker}([[:space:]()]|$) ]]; then
            return 0
        fi
    done

    return 1
}

_marker_args_select_full_tier() {
    local args=("$@")
    local i=0

    while [[ $i -lt ${#args[@]} ]]; do
        local arg="${args[$i]}"
        if [[ "${arg}" == "-m" ]]; then
            local next_idx=$((i + 1))
            if [[ $next_idx -lt ${#args[@]} ]] && _marker_expr_selects_full_tier "${args[$next_idx]}"; then
                return 0
            fi
        elif [[ "${arg}" == "-m"* ]] && _marker_expr_selects_full_tier "${arg#-m}"; then
            return 0
        fi
        i=$((i + 1))
    done

    return 1
}

DEFAULT_MARKERS="fast and not full"
if ! _has_marker_expr "${TEST_ARGS[@]}"; then
    TEST_ARGS+=(-m "${DEFAULT_MARKERS}")
    echo -e "${YELLOW}Marker filter:${NC} ${DEFAULT_MARKERS}"
else
    echo -e "${YELLOW}Marker filter:${NC} custom (\`-m\` supplied)"
fi

if _would_run_external_tests "${TEST_ARGS[@]}"; then
    _ensure_docker_services
fi

PARALLEL_ARGS=()
PARALLEL_WORKERS="${HB_TEST_WORKERS:-auto}"
PARALLEL_WORKER_DESCRIPTION="${PARALLEL_WORKERS}"
# A scalar, not an array: bash 3.2 under `set -u` rejects expanding an empty one.
PARALLEL_MAX_PROCESSES=""
if [[ -z "${HB_TEST_WORKERS:-}" ]] && ! _marker_args_select_full_tier "${TEST_ARGS[@]}"; then
    PARALLEL_MAX_PROCESSES=10
    PARALLEL_WORKER_DESCRIPTION="auto, capped at 10"
fi
if [[ "${HB_TEST_PARALLEL:-auto}" != "0" ]] && "${UV_BIN}" run --extra dev python - <<'PY'
import importlib.util
import sys

sys.exit(0 if importlib.util.find_spec("xdist") else 1)
PY
then
    PARALLEL_ARGS=(-n "${PARALLEL_WORKERS}" --dist worksteal)
    if [[ -n "${PARALLEL_MAX_PROCESSES}" ]]; then
        PARALLEL_ARGS+=(--maxprocesses "${PARALLEL_MAX_PROCESSES}")
    fi
fi

echo -e "${GREEN}Running tests...${NC}"
if [[ $(array_len PARALLEL_ARGS) -gt 0 ]]; then
    echo -e "${YELLOW}Parallel workers:${NC} ${PARALLEL_WORKER_DESCRIPTION}, work-stealing (set HB_TEST_PARALLEL=0 to disable)"
    PYTEST_RC=0
    "${UV_BIN}" run --extra dev pytest "${PARALLEL_ARGS[@]}" "${TEST_ARGS[@]}" || PYTEST_RC=$?
else
    if [[ "${HB_TEST_PARALLEL:-auto}" != "0" ]]; then
        echo -e "${YELLOW}Parallel workers:${NC} disabled (install pytest-xdist or set HB_TEST_PARALLEL=0 to keep sequential)"
    fi
    PYTEST_RC=0
    "${UV_BIN}" run --extra dev pytest "${TEST_ARGS[@]}" || PYTEST_RC=$?
fi
if [[ "${PYTEST_RC}" -eq 0 ]]; then
    echo -e "${GREEN}=== Test run completed successfully ===${NC}"
else
    echo -e "${RED}=== Test run completed with failures ===${NC}"
    exit 1
fi
