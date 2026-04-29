#!/usr/bin/env bash

set -euo pipefail

PROD_HOST="${MESHONATOR_PROD_HOST:-root@docker}"
PROD_REPO="${MESHONATOR_PROD_REPO:-/data/projects/meshonator}"
ROUTE_CHECK="${1:-}"

ssh -o BatchMode=yes -o ConnectTimeout=10 "${PROD_HOST}" 'bash -s' -- "${PROD_REPO}" "${ROUTE_CHECK}" <<'EOF'
set -euo pipefail

repo_path="$1"
route_check="$2"

cd "$repo_path"

git fetch origin main
git checkout origin/main -- Dockerfile pyproject.toml README.md alembic.ini alembic src

export BUILD_VERSION
BUILD_VERSION="$(git rev-parse --short origin/main)"
export BUILD_DATE
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

docker compose build --no-cache web
docker compose up -d --force-recreate web
docker compose ps web
docker exec meshonator-web-1 sh -lc 'curl -fsS http://127.0.0.1:80/health'

if [[ -n "$route_check" ]]; then
  docker exec meshonator-web-1 sh -lc "PYTHONPATH=/app/src python - \"$route_check\" <<'PY'
import sys
from meshonator.api.app import app

route = sys.argv[1]
found = any(getattr(candidate, 'path', None) == route for candidate in app.routes)
print(f'ROUTE_CHECK={route}:{found}')
raise SystemExit(0 if found else 1)
PY"
fi
EOF