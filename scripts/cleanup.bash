#!/bin/bash
# Cleanup test artifacts + ephemeral state.

echo "[cleanup] local cache directories..."
rm -rf ./.pytest_cache/
rm -rf ./.coverage
rm -rf ./.hvnb/
rm -rf ./.chromadb/
rm -rf ./.ruff_cache/
rm -rf ./coverage/
rm -rf ./htmlcov/
rm -rf ./heavenbase/
rm -rf ./tmp/
rm -rf ./__temp__/
rm -rf ./scratch/
rm -rf ./.temp/
find . -type d -name __pycache__ -prune -exec rm -rf {} +
find . -type d -name "*.egg-info" -prune -exec rm -rf {} +

# Only touch docker DBs when the containers are up.
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^postgres$'; then
    echo "[cleanup] removing heavenbase test databases (postgres)..."
    docker exec -e PGPASSWORD="password" postgres psql -U rubik -d postgres -tAc \
        "SELECT datname FROM pg_database WHERE datname LIKE 'hvnb_%' OR datname LIKE '%test%';" \
        | while read -r dbname; do
            dbname=$(echo "$dbname" | tr -d '[:space:]')
            if [ -n "$dbname" ]; then
                echo "  drop $dbname"
                docker exec -e PGPASSWORD="password" postgres psql -U rubik -d postgres -c \
                    "DROP DATABASE \"$dbname\";" >/dev/null
            fi
        done
fi

if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^mysql$'; then
    echo "[cleanup] removing heavenbase test schemas (mysql)..."
    docker exec -e MYSQL_PWD="password" mysql mysql -u root -s -N -e \
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'hvnb_%' OR schema_name LIKE '%test%';" \
        | while read -r dbname; do
            dbname=$(echo "$dbname" | tr -d '[:space:]')
            if [ -n "$dbname" ]; then
                echo "  drop $dbname"
                docker exec -e MYSQL_PWD="password" mysql mysql -u root -e \
                    "DROP DATABASE \`$dbname\`;" >/dev/null
            fi
        done
fi

if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^redis$'; then
    echo "[cleanup] removing heavenbase test keys and indexes (redis)..."
    docker exec redis redis-cli FT._LIST 2>/dev/null \
        | tr -d '\r' \
        | grep '^idx:docker-' \
        | while read -r index_name; do
            [ -n "$index_name" ] || continue
            echo "  drop index $index_name"
            docker exec redis redis-cli FT.DROPINDEX "$index_name" DD >/dev/null 2>&1 || true
        done

    for pattern in 'docker-*' 'idx:docker-*' '*:docker-*'; do
        docker exec redis redis-cli --scan --pattern "$pattern" 2>/dev/null \
            | tr -d '\r' \
            | while read -r key; do
                [ -n "$key" ] || continue
                docker exec redis redis-cli DEL "$key" >/dev/null 2>&1 || true
            done
    done
fi

echo "[cleanup] done."
