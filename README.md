# Meshonator

Meshonator to self-hosted web fleet manager dla Meshtastic zaprojektowany jako **TCP-only** od pierwszej linii kodu, z architekturą gotową pod drugi provider (MeshCore).

## TCP-only (ważne)

Aktualna wersja Meshonator obsługuje wyłącznie endpointy `tcp://host:port`.
Nie implementuje serial, BLE ani innych lokalnych transportów.

## Stack

- Python 3.12
- FastAPI + Jinja2 + HTMX + Leaflet
- SQLAlchemy 2.x + Alembic
- APScheduler
- PostgreSQL (domyślnie w Docker), SQLite (dev)
- Typer CLI
- pytest

## Architektura

Modular monolith:

- `src/meshonator/api` - API i panel web
- `src/meshonator/domain` - modele domenowe niezależne od protokołu
- `src/meshonator/providers` - provider contract + Meshtastic + MeshCore experimental
- `src/meshonator/discovery` - skan IP/CIDR/host (TCP)
- `src/meshonator/inventory` - lokalny inventory/cache
- `src/meshonator/sync` - full/quick/scheduled sync
- `src/meshonator/operations` - patch config, diff, desired-state
- `src/meshonator/groups` - grupy ręczne i dynamiczne
- `src/meshonator/jobs` - job lifecycle
- `src/meshonator/audit` - audit trail
- `src/meshonator/map` - markery mapy
- `src/meshonator/cli` - CLI pomocnicze

## Providery

### MeshtasticProvider (produkcyjny)

- transport: TCP
- główny backend: oficjalna biblioteka Python `meshtastic`
- capability matrix i operation matrix expose przez API/UI
- CLI fallback jest opakowany adapterem ze structured errors i timeoutami

### MeshCoreProvider (experimental)

- transport: TCP
- capability matrix + operation matrix
- skeleton integracyjny gotowy pod rozszerzenie
- nieobsługiwane operacje oznaczone jawnie jako `unsupported_or_restricted`

## Capability-driven design

UI/API nie zakładają identycznych możliwości providerów.
Każdy provider i node ma capability flags, np.:

- `can_discover_over_tcp`
- `can_remote_read_config`
- `can_remote_write_config`
- `can_batch_write`
- `has_location`
- `has_neighbors`
- `has_firmware_info`
- `has_favorites`
- `has_channels`

## Quick start (local)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
alembic upgrade head
meshonator bootstrap
uvicorn meshonator.api.app:app --reload --host 0.0.0.0 --port 8080
```

Panel: `http://localhost:8080`
Login domyślny: `admin/admin` (zmień w `.env`).

## Quick start (Docker)

```bash
cp .env.example .env
docker compose up --build
```

Serwisy:

- app: `http://localhost:8080`
- postgres: `localhost:5432`

## Discovery i scan targets

API:

```bash
curl -X POST http://localhost:8080/api/discovery/scan \
  -H 'Content-Type: application/json' \
  -d '{"provider":"meshtastic","hosts":["10.10.0.15"],"cidrs":["10.10.0.0/24"]}'
```

CLI:

```bash
meshonator discover-scan --cidr 10.10.0.0/24
meshonator discover-add-host 10.10.0.15
```

## Sync

```bash
meshonator nodes-sync-all
meshonator nodes-sync-all --quick
```

API:

```bash
curl -X POST "http://localhost:8080/api/sync?quick=true"
```

## Grupy i batch

- grupy ręczne/dynamiczne (`node_groups`, `node_group_members`)
- batch patch przez `/api/batch/patch`
- każde działanie zapisywane w `audit_logs`

## CLI helper - przykłady

```bash
meshonator bootstrap
meshonator discover-scan --cidr 10.10.0.0/24
meshonator discover-add-host 10.10.0.15
meshonator providers-test --provider meshtastic --tcp 10.10.0.20
meshonator nodes-sync-all
meshonator nodes-export --format yaml
meshonator jobs-run-sync-group core-west
meshonator db-seed-demo
meshonator providers-capabilities
```

## Bezpieczeństwo

- logowanie użytkownik/hasło (role: `viewer`, `operator`, `admin`)
- RBAC po endpointach write
- audyt zmian (`audit_logs`) i źródło operacji (`ui/api/cli/scheduler`)
- timeouty provider/CLI fallback
- batch write tylko dla roli admin

## Migracje

```bash
alembic upgrade head
```

Początkowa migracja: `alembic/versions/20260423_0001_initial.py`.

## Testy

```bash
pytest
```

Zakres:

- unit (tcp scan, capability matrix, diff)
- API (health, login)
- integration (discovery + fake provider)

## Ograniczenia i jawne statusy

- transport jest **wyłącznie TCP**
- operacje zdalne zależne od ograniczeń protokołu i uprawnień/kluczy
- to, czego provider nie wspiera stabilnie, jest oznaczone jako:
  - `supported_natively`
  - `supported_via_cli_fallback`
  - `unsupported_or_restricted`

## Rozszerzenie o MeshCore

Obecna architektura ma wspólny kontrakt providera (`providers/base.py`) i capability gating.
Dla pełnego MeshCore wystarczy rozszerzyć `src/meshonator/providers/meshcore/provider.py` bez przepisywania domeny, UI i DB.
