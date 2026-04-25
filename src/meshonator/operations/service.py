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


def _merge_dict(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def _merge_channels(current: list, patch: list[dict]) -> list[dict]:
    by_index: dict[int, dict] = {}
    for idx, item in enumerate(current):
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            index = idx
        by_index[index] = dict(item)
    for item in patch:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            continue
        row = dict(by_index.get(index, {"index": index}))
        for key, value in item.items():
            if key in {"settings", "moduleSettings", "module_settings"} and isinstance(value, dict):
                target_key = "moduleSettings" if key in {"moduleSettings", "module_settings"} else "settings"
                existing = row.get(target_key, {})
                if not isinstance(existing, dict):
                    existing = {}
                row[target_key] = _merge_dict(existing, value)
            else:
                row[key] = value
        by_index[index] = row
    return [by_index[i] for i in sorted(by_index.keys())]


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

        remote_update_requested = any(
            [
                patch.short_name is not None,
                patch.long_name is not None,
                patch.role is not None,
                patch.latitude is not None and patch.longitude is not None,
                bool(patch.local_config_patch),
                bool(patch.module_config_patch),
                bool(patch.channels_patch),
            ]
        )

        before = {
            "short_name": node.short_name,
            "long_name": node.long_name,
            "role": node.role,
            "favorite": node.favorite,
            "location": {"lat": node.latitude, "lon": node.longitude, "alt": node.altitude},
        }
        if not remote_update_requested:
            if not dry_run and patch.favorite is not None:
                node.favorite = patch.favorite
                node.last_successful_sync = datetime.now(timezone.utc)
                self.db.commit()
            result = {
                "provider_node_id": node.provider_node_id,
                "mode": "local_only",
                "applied": {"favorite": patch.favorite} if patch.favorite is not None else {},
                "supported": True,
            }
        else:
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
                raw = json.loads(json.dumps(node.raw_metadata)) if isinstance(node.raw_metadata, dict) else {}
                if patch.local_config_patch:
                    prefs = raw.get("preferences", {})
                    if not isinstance(prefs, dict):
                        prefs = {}
                    raw["preferences"] = _merge_dict(prefs, patch.local_config_patch)
                if patch.module_config_patch:
                    module_prefs = raw.get("modulePreferences", {})
                    if not isinstance(module_prefs, dict):
                        module_prefs = {}
                    raw["modulePreferences"] = _merge_dict(module_prefs, patch.module_config_patch)
                if patch.channels_patch:
                    channels = raw.get("channels", [])
                    if not isinstance(channels, list):
                        channels = []
                    raw["channels"] = _merge_channels(channels, patch.channels_patch)
                node.raw_metadata = raw
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
