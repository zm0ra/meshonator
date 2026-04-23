# Meshonator

Meshonator is a web fleet manager for Meshtastic with a provider architecture ready for MeshCore.

## Transport

Meshonator currently uses TCP endpoints (`tcp://host:port`).

## Stack

- Python 3.12
- FastAPI + Jinja2 + HTMX + Leaflet
- SQLAlchemy 2.x + Alembic
- APScheduler
- PostgreSQL (Docker default), SQLite (dev mode)
- Typer CLI
- pytest

## Architecture

Modular monolith boundaries:

- `src/meshonator/api` - HTTP API + web UI
- `src/meshonator/domain` - protocol-agnostic domain models
- `src/meshonator/providers` - provider contract + Meshtastic + MeshCore (experimental)
- `src/meshonator/discovery` - host/IP/CIDR discovery
- `src/meshonator/inventory` - local node inventory and cache
- `src/meshonator/sync` - full/quick/scheduled sync
- `src/meshonator/operations` - config patch, desired state, drift diff
- `src/meshonator/groups` - manual and dynamic groups
- `src/meshonator/jobs` - job lifecycle and status
- `src/meshonator/audit` - audit trail
- `src/meshonator/map` - map markers and filtering
- `src/meshonator/cli` - helper CLI

## Providers

### MeshtasticProvider (primary)

- Uses official Meshtastic Python library as the main backend.
- Exposes capability matrix and operation matrix for UI/API gating.
- CLI fallback is wrapped with timeout + structured errors.

### MeshCoreProvider (experimental)

- Experimental provider skeleton with the same contract.
- Capability and operation matrices are explicit.
- Unsupported operations are returned as `unsupported_or_restricted`.

## Capability-driven design

UI/API do not assume equal feature parity between providers.
Capabilities include:

- `can_discover_over_tcp`
- `can_remote_read_config`
- `can_remote_write_config`
- `can_batch_write`
- `has_location`
- `has_neighbors`
- `has_firmware_info`
- `has_favorites`
- `has_channels`

## Quick Start (local)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
alembic upgrade head
meshonator bootstrap
uvicorn meshonator.api.app:app --reload --host 0.0.0.0 --port 8080
```

UI: `http://localhost:8080`
Default credentials: `admin/admin`.

## Quick Start (Docker)

If port `8080` is already used on your machine, set `MESHONATOR_HTTP_PORT`.

```bash
cp .env.example .env
MESHONATOR_HTTP_PORT=8081 docker compose up --build -d
```

UI: `http://localhost:8081`

## UI-first workflow

Dashboard is the primary workflow:

1. Open **Discovery** and submit hosts/CIDR/endpoints.
2. A discovery job is queued immediately.
3. Open **Jobs** to track progress/results.
4. Run **Sync** from Dashboard (also queued).
5. Verify in **Nodes** and **Map**.

## How to add a node for testing

### Option A: CLI (single host)

```bash
meshonator discover-add-host 10.10.0.15 --provider meshtastic
meshonator nodes-sync-all
```

### Option B: CLI (CIDR range)

```bash
meshonator discover-scan --cidr 10.10.0.0/24 --provider meshtastic
meshonator nodes-sync-all --quick
```

### Option C: API (single host / CIDR)

```bash
curl -X POST http://localhost:8081/api/discovery/scan \
  -H 'Content-Type: application/json' \
  -d '{"provider":"meshtastic","hosts":["10.10.0.15"],"cidrs":["10.10.0.0/24"]}'

curl -X POST "http://localhost:8081/api/sync?quick=true"
```

### Option D: import hosts file (YAML/JSON)

Example `targets.yml`:

```yaml
hosts:
  - 10.10.0.15
  - 10.10.0.16
cidrs:
  - 10.10.1.0/24
endpoints:
  - tcp://10.10.2.10:4403
```

Run:

```bash
meshonator discover-import --path targets.yml --provider meshtastic
meshonator nodes-sync-all
```

## Useful CLI commands

```bash
meshonator bootstrap
meshonator providers-test --provider meshtastic --tcp 10.10.0.20
meshonator providers-capabilities
meshonator nodes-export --format yaml
meshonator jobs-run-sync-group core-west
meshonator db-seed-demo
```

## Security model

- Session login (roles: `viewer`, `operator`, `admin`)
- RBAC on write endpoints
- Audit events persisted in `audit_logs`
- Provider/CLI timeout controls

## Migrations

```bash
alembic upgrade head
```

Initial migration: `alembic/versions/20260423_0001_initial.py`.

## Tests

```bash
pytest
```

Current coverage includes:

- unit tests (scan helpers, capability matrix, diff behavior)
- API tests (health, login)
- integration test (discovery with fake provider)

## Health check

```bash
curl -fsS http://localhost:8081/health
```

Example response includes service and provider health status.

## Build footer metadata

The footer displays:

- application name
- build version
- rebuild date

Set with:

- `BUILD_VERSION`
- `BUILD_DATE`

## Extending MeshCore

MeshCore can be extended in `src/meshonator/providers/meshcore/provider.py` without rewriting domain models, UI, or database schema.
