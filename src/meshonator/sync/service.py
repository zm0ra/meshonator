from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from meshonator.audit.service import AuditService
from meshonator.db.models import ManagedNodeModel, NodeSnapshotModel, ProviderEndpointModel
from meshonator.inventory.service import InventoryService
from meshonator.operations.service import OperationsService
from meshonator.providers.base import ProviderConnection
from meshonator.providers.registry import ProviderRegistry


class SyncService:
    def __init__(self, db: Session, registry: ProviderRegistry) -> None:
        self.db = db
        self.registry = registry
        self.inventory = InventoryService(db)
        self.audit = AuditService(db)
        self.ops = OperationsService(db, registry)

    def sync_endpoint(self, endpoint_id: str, quick: bool = False) -> dict:
        endpoint = self.db.get(ProviderEndpointModel, endpoint_id)
        if endpoint is None:
            raise ValueError("Endpoint not found")

        provider = self.registry.get(endpoint.provider_name)
        conn = provider.connect(ProviderConnection(endpoint=endpoint.endpoint, host=endpoint.host, port=endpoint.port))
        nodes = provider.fetch_nodes(conn)
        saved = self.inventory.upsert_nodes(nodes, endpoint.endpoint, endpoint.host, endpoint.port, endpoint.source)

        snapshot_count = 0
        if not quick:
            for db_node in saved:
                cfg = provider.fetch_config(conn, db_node.provider_node_id)
                self.ops.save_config_snapshot(db_node.id, "provider_config", cfg)
                self.db.add(NodeSnapshotModel(node_id=db_node.id, snapshot_type="full_sync", payload=cfg))
                snapshot_count += 1
            self.db.commit()

        return {
            "endpoint": endpoint.endpoint,
            "nodes": len(saved),
            "snapshots": snapshot_count,
            "quick": quick,
        }

    def sync_all(self, quick: bool = False) -> list[dict]:
        out: list[dict] = []
        endpoints = list(self.db.scalars(select(ProviderEndpointModel).where(ProviderEndpointModel.reachable.is_(True))).all())
        for endpoint in endpoints:
            try:
                result = self.sync_endpoint(str(endpoint.id), quick=quick)
                out.append({"status": "success", **result})
            except Exception as exc:
                out.append({"status": "failed", "endpoint": endpoint.endpoint, "error": str(exc)})

        self.inventory.stale_mark(stale_minutes=30)
        self.audit.log(
            actor="scheduler",
            source="scheduler",
            action="sync.all",
            metadata={"quick": quick, "results": out, "executed_at": datetime.now(timezone.utc).isoformat()},
        )
        return out

    def sync_node(self, node_id: str, quick: bool = False) -> dict:
        node = self.db.scalar(
            select(ManagedNodeModel)
            .where(ManagedNodeModel.id == node_id)
            .options(selectinload(ManagedNodeModel.endpoints))
        )
        if node is None:
            raise ValueError("Node not found")
        if not node.endpoints:
            raise ValueError("Node has no endpoint")
        endpoint = node.endpoints[0]
        provider = self.registry.get(node.provider)
        conn = provider.connect(
            ProviderConnection(endpoint=endpoint.endpoint, host=endpoint.host, port=endpoint.port)
        )
        nodes = provider.fetch_nodes(conn)
        saved = self.inventory.upsert_nodes(nodes, endpoint.endpoint, endpoint.host, endpoint.port, endpoint.source)
        if not quick:
            cfg = provider.fetch_config(conn, node.provider_node_id)
            self.ops.save_config_snapshot(node.id, "provider_config", cfg)
            self.db.add(NodeSnapshotModel(node_id=node.id, snapshot_type="full_sync", payload=cfg))
            self.db.commit()
        return {"node_id": node_id, "saved_nodes": len(saved), "quick": quick}

    def node_details(self, node_id: str) -> ManagedNodeModel | None:
        stmt = (
            select(ManagedNodeModel)
            .where(ManagedNodeModel.id == node_id)
            .options(selectinload(ManagedNodeModel.endpoints))
        )
        return self.db.scalar(stmt)
