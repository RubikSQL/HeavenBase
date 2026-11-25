#!/usr/bin/env bash
# docker-restart.bash — tear down, pull latest images, and restart all database containers
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SCRIPT_DIR="${ROOT}/scripts"
source "${ROOT}/scripts/_env.bash"

usage() {
    cat <<'EOF'
Usage: docker-restart.bash [DATABASE_DIR]

Restart the Docker Compose stack for the target database directory.

Arguments:
  DATABASE_DIR   Directory that contains docker-compose.yml.
                 Default: DATABASE_DIR if set, otherwise the first compose stack found under
                 D:/databases, /d/databases, /mnt/d/databases, then this script directory.

Environment:
  DATABASE_DIR   Same as the positional argument when no argument is given.
  COMPOSE_FILE   Optional override for the compose file path.

Examples:
  ./docker-restart.bash
  ./docker-restart.bash D:/databases
  ./docker-restart.bash /d/databases
  DATABASE_DIR=d:/databases ./docker-restart.bash
EOF
}

normalize_path() {
    local raw_path="$1"

    if command -v cygpath >/dev/null 2>&1; then
        cygpath -u "$raw_path" 2>/dev/null || printf '%s\n' "$raw_path"
        return
    fi

    printf '%s\n' "$raw_path"
}

default_database_dir() {
    if [[ -n "${DATABASE_DIR:-}" ]]; then
        normalize_path "$DATABASE_DIR"
        return
    fi

    local candidate normalized
    for candidate in "D:/databases" "/d/databases" "/mnt/d/databases" "$SCRIPT_DIR"; do
        normalized="$(normalize_path "$candidate")"
        if [[ -d "$normalized" && -f "$normalized/docker-compose.yml" ]]; then
            printf '%s\n' "$normalized"
            return
        fi
    done

    printf '%s\n' "$SCRIPT_DIR"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

TARGET_DIR_INPUT="${1:-$(default_database_dir)}"
TARGET_DIR_INPUT="$(normalize_path "$TARGET_DIR_INPUT")"

if ! TARGET_DIR="$(cd -- "$TARGET_DIR_INPUT" 2>/dev/null && pwd -P)"; then
    echo "Target directory not found: $TARGET_DIR_INPUT" >&2
    exit 1
fi

COMPOSE_FILE_INPUT="${COMPOSE_FILE:-$TARGET_DIR/docker-compose.yml}"
COMPOSE_FILE_INPUT="$(normalize_path "$COMPOSE_FILE_INPUT")"

if [[ ! -f "$COMPOSE_FILE_INPUT" ]]; then
    echo "Compose file not found: $COMPOSE_FILE_INPUT" >&2
    exit 1
fi

COMPOSE_FILE="$(cd -- "$(dirname -- "$COMPOSE_FILE_INPUT")" && pwd -P)/$(basename -- "$COMPOSE_FILE_INPUT")"

resolve_python

compose() {
    docker compose --project-directory "$TARGET_DIR" -f "$COMPOSE_FILE" "$@"
}

echo "=== Target directory: $TARGET_DIR ==="
echo "=== Compose file: $COMPOSE_FILE ==="

echo "=== Stopping and removing containers, volumes, orphans ==="
compose down -v --remove-orphans

echo "=== Pulling latest images ==="
compose pull

echo "=== Starting containers ==="
compose up -d

echo "=== Waiting for containers to become healthy (up to 15 min) ==="
elapsed=0
while true; do
    unhealthy=$(compose ps --format json 2>/dev/null \
        | "${PYTHON_CMD[@]}" -c "
import json
import sys

data = sys.stdin.read().strip()
if not data:
    print(0)
    sys.exit(0)
# Newer docker compose may emit a JSON array; older versions emit NDJSON.
if data.startswith('['):
    items = json.loads(data)
else:
    items = [json.loads(line) for line in data.split('\n') if line.strip()]
count = 0
for obj in items:
    health = obj.get('Health', obj.get('State', ''))
    if health not in ('healthy', 'running'):
        count += 1
print(count)
" 2>/dev/null || echo "?")

    if [[ "$unhealthy" == "0" ]]; then
        echo "All containers healthy!"
        break
    fi

    if (( elapsed >= 900 )); then
        echo "Timeout after 15 minutes. Current status:"
        compose ps
        exit 1
    fi

    echo "  ... $unhealthy container(s) not yet healthy (${elapsed}s elapsed)"
    sleep 15
    elapsed=$((elapsed + 15))
done

echo ""
compose ps
echo ""
echo "Restart complete."
