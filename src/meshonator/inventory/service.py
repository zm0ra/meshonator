from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from meshonator.db.models import ManagedNodeModel, NodeEndpointModel
from meshonator.domain.models import ManagedNode
from meshonator.providers.utils.json_safe import to_json_safe


class InventoryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_nodes(self, nodes: list[ManagedNode], endpoint: str, host: str, port: int, source: str) -> list[ManagedNodeModel]:
        out: list[ManagedNodeModel] = []
        now = datetime.now(timezone.utc)
        for node in nodes:
            db_node = self.db.scalar(
                select(ManagedNodeModel).where(
                    ManagedNodeModel.provider == node.provider.value,
                    ManagedNodeModel.provider_node_id == node.provider_node_id,
                )
            )
            if db_node is None and node.provider_node_id.startswith("local-") and node.provider_node_id != "local-unknown":
                db_node = self.db.scalar(
                    select(ManagedNodeModel)
                    .join(NodeEndpointModel, NodeEndpointModel.node_id == ManagedNodeModel.id)
                    .where(
                        ManagedNodeModel.provider == node.provider.value,
                        ManagedNodeModel.provider_node_id == "local-unknown",
                        NodeEndpointModel.endpoint == endpoint,
                    )
                )
                if db_node is not None:
                    db_node.provider_node_id = node.provider_node_id
            if db_node is None:
                db_node = ManagedNodeModel(
                    provider=node.provider.value,
                    provider_node_id=node.provider_node_id,
                    first_seen=node.first_seen or now,
                )
                self.db.add(db_node)
                self.db.flush()

            db_node.node_num = node.node_num
            db_node.short_name = node.short_name
            db_node.long_name = node.long_name
            db_node.firmware_version = node.firmware.version
            db_node.hardware_model = node.hardware.model
            db_node.role = node.role
            db_node.favorite = node.favorite
            db_node.managed = node.managed
            db_node.latitude = node.location.latitude
            db_node.longitude = node.location.longitude
            db_node.altitude = node.location.altitude
            db_node.location_source = node.location.source
            db_node.last_seen = node.last_seen or now
            db_node.reachable = node.reachable
            db_node.last_successful_sync = now
            db_node.capability_matrix = node.capabilities.model_dump()
            db_node.raw_metadata = to_json_safe(node.raw_metadata)

            endpoint_row = self.db.scalar(
                select(NodeEndpointModel).where(NodeEndpointModel.node_id == db_node.id, NodeEndpointModel.endpoint == endpoint)
            )
            if endpoint_row is None:
                endpoint_row = NodeEndpointModel(
                    node_id=db_node.id,
                    endpoint=endpoint,
                    host=host,
                    port=port,
                    source=source,
                    is_primary=True,
                    last_seen=now,
                )
                self.db.add(endpoint_row)
            else:
                endpoint_row.last_seen = now
                endpoint_row.host = host
                endpoint_row.port = port
                endpoint_row.source = source

            out.append(db_node)

        self.db.commit()
        return out

    def list_nodes(self, provider: str | None = None) -> list[ManagedNodeModel]:
        stmt = select(ManagedNodeModel)
        if provider:
            stmt = stmt.where(ManagedNodeModel.provider == provider)
        return list(self.db.scalars(stmt.order_by(ManagedNodeModel.last_seen.desc().nullslast())).all())

    def get_node(self, node_id: UUID) -> ManagedNodeModel | None:
        return self.db.get(ManagedNodeModel, node_id)

    def stale_mark(self, stale_minutes: int = 30) -> int:
        now = datetime.now(timezone.utc)
        count = 0
        for node in self.db.scalars(select(ManagedNodeModel)).all():
            if node.last_seen is None:
                continue
            delta = now - node.last_seen
            is_reachable = delta.total_seconds() <= stale_minutes * 60
            if node.reachable != is_reachable:
                node.reachable = is_reachable
                count += 1
        self.db.commit()
        return count

    def delete_node_endpoints(self, node_id: UUID) -> None:
        self.db.execute(delete(NodeEndpointModel).where(NodeEndpointModel.node_id == node_id))
        self.db.commit()
