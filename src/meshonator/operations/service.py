from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from meshonator.audit.service import AuditService
from meshonator.db.models import ManagedNodeModel, NodeConfigModel, NodeDesiredStateModel
from meshonator.domain.models import ConfigPatch
from meshonator.inventory.service import InventoryService
from meshonator.providers.base import ProviderConnection
from meshonator.providers.registry import ProviderRegistry


class OperationsService:
    def __init__(self, db: Session, registry: ProviderRegistry) -> None:
        self.db = db
        self.registry = registry
        self.audit = AuditService(db)
        self.inventory = InventoryService(db)

    def apply_patch(
        self,
        node_id: UUID,
        patch: ConfigPatch,
        actor: str,
        source: str,
        dry_run: bool = False,
    ) -> dict:
        node = self.db.get(ManagedNodeModel, node_id)
        if node is None:
            raise ValueError("Node not found")

        provider = self.registry.get(node.provider)

        endpoint_row = node.endpoints[0] if node.endpoints else None
        if endpoint_row is None:
            raise ValueError("Node has no TCP endpoint")

        conn = None
        try:
            conn = provider.connect(
                ProviderConnection(
                    endpoint=endpoint_row.endpoint,
                    host=endpoint_row.host,
                    port=endpoint_row.port,
                )
            )
            before = {
                "short_name": node.short_name,
                "long_name": node.long_name,
                "role": node.role,
                "favorite": node.favorite,
                "location": {"lat": node.latitude, "lon": node.longitude, "alt": node.altitude},
            }
            result = provider.apply_config_patch(conn, node.provider_node_id, patch, dry_run=dry_run)

            if not dry_run:
                if patch.short_name is not None:
                    node.short_name = patch.short_name
                if patch.long_name is not None:
                    node.long_name = patch.long_name
                if patch.role is not None:
                    node.role = patch.role
                if patch.favorite is not None:
                    node.favorite = patch.favorite
                if patch.latitude is not None:
                    node.latitude = patch.latitude
                if patch.longitude is not None:
                    node.longitude = patch.longitude
                if patch.altitude is not None:
                    node.altitude = patch.altitude
                node.last_successful_sync = datetime.now(timezone.utc)
                self.db.commit()
        finally:
            provider.disconnect(conn)

        after = {
            "short_name": patch.short_name if patch.short_name is not None else node.short_name,
            "long_name": patch.long_name if patch.long_name is not None else node.long_name,
            "role": patch.role if patch.role is not None else node.role,
            "favorite": patch.favorite if patch.favorite is not None else node.favorite,
            "location": {
                "lat": patch.latitude if patch.latitude is not None else node.latitude,
                "lon": patch.longitude if patch.longitude is not None else node.longitude,
                "alt": patch.altitude if patch.altitude is not None else node.altitude,
            },
        }

        self.audit.log(
            actor=actor,
            source=source,
            action="node.config_patch",
            provider=node.provider,
            node_id=node.id,
            before_state=before,
            after_state=after,
            metadata={"dry_run": dry_run, "result": result},
        )
        return result

    def save_config_snapshot(self, node_id: UUID, config_type: str, config: dict) -> NodeConfigModel:
        version_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
        row = NodeConfigModel(node_id=node_id, config_type=config_type, config=config, version_hash=version_hash)
        self.db.add(row)
        self.db.commit()
        return row

    def diff_latest_configs(self, node_id: UUID, config_type: str) -> dict:
        stmt = (
            select(NodeConfigModel)
            .where(NodeConfigModel.node_id == node_id, NodeConfigModel.config_type == config_type)
            .order_by(NodeConfigModel.created_at.desc())
            .limit(2)
        )
        rows = list(self.db.scalars(stmt).all())
        if len(rows) < 2:
            return {"status": "unknown", "diff": {}}
        current, previous = rows[0], rows[1]
        changed = {
            k: {"before": previous.config.get(k), "after": current.config.get(k)}
            for k in sorted(set(previous.config) | set(current.config))
            if previous.config.get(k) != current.config.get(k)
        }
        return {"status": "drifted" if changed else "compliant", "diff": changed}

    def set_desired_state(self, node_id: UUID, desired: dict) -> NodeDesiredStateModel:
        row = self.db.scalar(select(NodeDesiredStateModel).where(NodeDesiredStateModel.node_id == node_id))
        if row is None:
            row = NodeDesiredStateModel(node_id=node_id, desired_snapshot=desired, drift_state="unknown")
            self.db.add(row)
        else:
            row.desired_snapshot = desired
        self.db.commit()
        return row
