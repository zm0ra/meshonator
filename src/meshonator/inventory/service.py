from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from meshonator.db.models import ManagedNodeModel, NodeEndpointModel, ProviderEndpointModel
from meshonator.domain.models import ManagedNode
from meshonator.providers.utils.tcp_scan import ping_probe
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
            if db_node is None and node.provider_node_id != "local-unknown":
                db_node = self.db.scalar(
                    select(ManagedNodeModel)
                    .join(NodeEndpointModel, NodeEndpointModel.node_id == ManagedNodeModel.id)
                    .where(
                        ManagedNodeModel.provider == node.provider.value,
                        ManagedNodeModel.provider_node_id == "local-unknown",
                        NodeEndpointModel.endpoint == endpoint,
                    )
                )
                if db_node is None:
                    db_node = self.db.scalar(
                        select(ManagedNodeModel)
                        .join(NodeEndpointModel, NodeEndpointModel.node_id == ManagedNodeModel.id)
                        .where(
                            ManagedNodeModel.provider == node.provider.value,
                            ManagedNodeModel.provider_node_id.like("local-%"),
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
            db_node.favorite = True
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
            delta = now - self._as_utc(node.last_seen)
            is_fresh = delta.total_seconds() <= stale_minutes * 60
            raw_metadata = dict(node.raw_metadata) if isinstance(node.raw_metadata, dict) else {}
            current_fresh = raw_metadata.get("fresh")
            if current_fresh != is_fresh:
                raw_metadata["fresh"] = is_fresh
                node.raw_metadata = raw_metadata
                count += 1
        self.db.commit()
        return count

    def refresh_transport_reachability(self, timeout: float = 1.0, failures_before_offline: int = 2) -> dict:
        now = datetime.now(timezone.utc)
        endpoint_rows = list(self.db.scalars(select(ProviderEndpointModel).order_by(ProviderEndpointModel.host.asc())).all())
        endpoint_state: dict[str, bool] = {}
        endpoints_online = 0
        endpoints_offline = 0

        for endpoint_row in endpoint_rows:
            previous_reachable = bool(endpoint_row.reachable)
            probe = ping_probe(endpoint_row.host, timeout=timeout)
            is_open = bool(probe.get("is_online"))
            matching_node_endpoints = list(
                self.db.scalars(select(NodeEndpointModel).where(NodeEndpointModel.endpoint == endpoint_row.endpoint)).all()
            )
            meta_json = dict(endpoint_row.meta_json) if isinstance(endpoint_row.meta_json, dict) else {}
            failures = int(meta_json.get("consecutive_transport_failures", 0) or 0)
            if is_open:
                failures = 0
                effective_reachable = True
            else:
                failures += 1
                effective_reachable = previous_reachable and failures < max(1, failures_before_offline)
            endpoint_row.reachable = effective_reachable
            if is_open:
                endpoint_row.last_seen = now
                for node_endpoint in matching_node_endpoints:
                    node_endpoint.last_seen = now
                endpoints_online += 1
            else:
                for node_endpoint in matching_node_endpoints:
                    node_endpoint.last_seen = now
                if not effective_reachable:
                    endpoints_offline += 1
                else:
                    endpoints_online += 1
            meta_json.update(
                {
                    "last_transport_probe_at": now.isoformat(),
                    "last_transport_probe": probe,
                    "consecutive_transport_failures": failures,
                }
            )
            endpoint_row.meta_json = meta_json
            endpoint_state[endpoint_row.endpoint] = effective_reachable

        node_rows = list(
            self.db.scalars(select(ManagedNodeModel).options(selectinload(ManagedNodeModel.endpoints))).all()
        )
        nodes_online = 0
        nodes_offline = 0
        node_changes = 0
        for node in node_rows:
            is_reachable = any(endpoint_state.get(endpoint.endpoint, False) for endpoint in node.endpoints)
            if node.reachable != is_reachable:
                node.reachable = is_reachable
                node_changes += 1
            if is_reachable:
                nodes_online += 1
            else:
                nodes_offline += 1

        self.db.commit()
        return {
            "checked_at": now.isoformat(),
            "timeout_s": timeout,
            "endpoints_total": len(endpoint_rows),
            "endpoints_online": endpoints_online,
            "endpoints_offline": endpoints_offline,
            "nodes_total": len(node_rows),
            "nodes_online": nodes_online,
            "nodes_offline": nodes_offline,
            "node_changes": node_changes,
        }

    def delete_node_endpoints(self, node_id: UUID) -> None:
        self.db.execute(delete(NodeEndpointModel).where(NodeEndpointModel.node_id == node_id))
        self.db.commit()
    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
