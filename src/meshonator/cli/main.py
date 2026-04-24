from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from sqlalchemy.orm import Session

from meshonator.api.app import registry
from meshonator.auth.security import bootstrap_admin
from meshonator.config.settings import get_settings
from meshonator.db.base import Base
from meshonator.db.models import ManagedNodeModel
from meshonator.db.session import engine
from meshonator.discovery.service import DiscoveryService
from meshonator.inventory.service import InventoryService
from meshonator.jobs.worker import run_worker_loop
from meshonator.sync.service import SyncService

app = typer.Typer(help="Meshonator CLI")
console = Console()
settings = get_settings()


def with_db() -> Session:
    return Session(engine)


@app.command("bootstrap")
def bootstrap() -> None:
    Base.metadata.create_all(engine)
    with with_db() as db:
        bootstrap_admin(db, settings.bootstrap_admin_username, settings.bootstrap_admin_password, settings.bootstrap_admin_role)
    console.print("[green]Database and admin bootstrap completed.[/green]")


@app.command("discover-scan")
def discover_scan(
    cidr: Annotated[list[str], typer.Option("--cidr")] = [],
    host: Annotated[list[str], typer.Option("--host")] = [],
    provider: str = "meshtastic",
    port: int | None = None,
) -> None:
    with with_db() as db:
        found = DiscoveryService(db, registry).scan(provider_name=provider, hosts=host, cidrs=cidr, port=port, source="cli")
    console.print_json(data={"count": len(found), "endpoints": [f.endpoint for f in found], "transport": "tcp"})


@app.command("discover-add-host")
def discover_add_host(host: str, provider: str = "meshtastic", port: int | None = None) -> None:
    with with_db() as db:
        found = DiscoveryService(db, registry).scan(provider_name=provider, hosts=[host], port=port, source="cli")
    console.print_json(data={"count": len(found), "host": host, "transport": "tcp"})


@app.command("discover-import")
def discover_import(path: str, provider: str = "meshtastic", port: int | None = None) -> None:
    content = Path(path).read_text(encoding="utf-8")
    if path.endswith(".json"):
        payload = json.loads(content)
    else:
        payload = yaml.safe_load(content)
    hosts = payload.get("hosts", []) if isinstance(payload, dict) else []
    cidrs = payload.get("cidrs", []) if isinstance(payload, dict) else []
    endpoints = payload.get("endpoints", []) if isinstance(payload, dict) else []
    with with_db() as db:
        found = DiscoveryService(db, registry).scan(
            provider_name=provider,
            hosts=hosts,
            cidrs=cidrs,
            manual_endpoints=endpoints,
            port=port,
            source="cli-import",
        )
    console.print_json(data={"count": len(found), "transport": "tcp", "source": path})


@app.command("providers-test")
def providers_test(provider: str = "meshtastic", tcp: str = "127.0.0.1", port: int | None = None) -> None:
    p = registry.get(provider)
    endpoints = p.discover_endpoints([tcp], port=port)
    console.print_json(
        data={
            "provider": provider,
            "health": p.health().model_dump(),
            "operation_matrix": p.operation_matrix().model_dump(),
            "reachable": bool(endpoints),
            "transport": "tcp",
        }
    )


@app.command("nodes-sync-all")
def nodes_sync_all(quick: bool = False) -> None:
    with with_db() as db:
        result = SyncService(db, registry).sync_all(quick=quick)
    console.print_json(data={"results": result})


@app.command("nodes-export")
def nodes_export(format: str = "yaml", output: str = "-") -> None:
    with with_db() as db:
        rows = InventoryService(db).list_nodes()
    payload = [
        {
            "id": str(r.id),
            "provider": r.provider,
            "provider_node_id": r.provider_node_id,
            "short_name": r.short_name,
            "long_name": r.long_name,
            "firmware": r.firmware_version,
            "hardware": r.hardware_model,
            "role": r.role,
            "favorite": r.favorite,
            "location": {"lat": r.latitude, "lon": r.longitude, "alt": r.altitude},
            "reachable": r.reachable,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            "capability_matrix": r.capability_matrix,
        }
        for r in rows
    ]
    if format == "json":
        text = json.dumps(payload, indent=2)
    else:
        text = yaml.safe_dump(payload, sort_keys=False)
    if output == "-":
        console.print(text)
    else:
        Path(output).write_text(text, encoding="utf-8")
        console.print(f"Exported to {output}")


@app.command("jobs-run-sync-group")
def jobs_run_sync_group(group: str) -> None:
    with with_db() as db:
        from meshonator.db.models import NodeGroupModel
        from meshonator.groups.service import GroupsService

        group_row = db.query(NodeGroupModel).filter(NodeGroupModel.name == group).one_or_none()
        if group_row is None:
            raise typer.Exit(code=1)
        nodes = GroupsService(db).resolve_dynamic_members(group_row)
        synced = SyncService(db, registry)
        results = []
        for node in nodes:
            if not node.endpoints:
                results.append({"node": str(node.id), "status": "skipped", "reason": "no endpoint"})
                continue
            results.append(synced.sync_node(str(node.id), quick=False))
    console.print_json(data={"group": group, "results": results})


@app.command("db-seed-demo")
def db_seed_demo() -> None:
    with with_db() as db:
        exists = db.query(ManagedNodeModel).count()
        if exists > 0:
            console.print("Demo seed skipped: nodes already exist")
            return
        db.add(
            ManagedNodeModel(
                provider="meshtastic",
                provider_node_id="demo-1",
                short_name="D1",
                long_name="Demo Node 1",
                firmware_version="2.5.0",
                hardware_model="T-Echo",
                role="ROUTER",
                favorite=True,
                latitude=52.23,
                longitude=21.01,
                altitude=100,
                location_source="seed",
                reachable=True,
                capability_matrix=registry.get("meshtastic").capabilities().model_dump(),
                raw_metadata={"seed": True},
            )
        )
        db.commit()
    console.print("Demo data seeded")


@app.command("providers-capabilities")
def providers_capabilities() -> None:
    console.print_json(
        data={
            p.name: {
                "health": p.health().model_dump(),
                "capabilities": p.capabilities().model_dump(),
                "operation_matrix": p.operation_matrix().model_dump(),
                "experimental": p.experimental,
            }
            for p in registry.all()
        }
    )


@app.command("jobs-worker")
def jobs_worker(
    poll_interval: Annotated[float, typer.Option("--poll-interval", min=0.2)] = 2.0,
    once: Annotated[bool, typer.Option("--once")] = False,
) -> None:
    processed = run_worker_loop(registry=registry, poll_interval_s=poll_interval, once=once)
    if once:
        console.print_json(data={"processed": processed})


if __name__ == "__main__":
    app()
