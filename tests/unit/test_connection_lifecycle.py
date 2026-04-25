from __future__ import annotations

from datetime import datetime, timezone

from meshonator.domain.models import ConfigPatch, NodeCapability, OperationMatrix, ProviderHealth, ProviderOperationSupport, ProviderType
from meshonator.operations.service import OperationsService
from meshonator.providers.base import ProviderConnection
from meshonator.providers.registry import ProviderRegistry
from meshonator.sync.service import SyncService
from meshonator.db.models import ManagedNodeModel, NodeEndpointModel, ProviderEndpointModel


class _LifecycleProvider:
    name = "meshtastic"
    experimental = False

    def __init__(self) -> None:
        self.disconnect_calls = 0

    def capabilities(self) -> NodeCapability:
        return NodeCapability(can_discover_over_tcp=True)

    def operation_matrix(self) -> OperationMatrix:
        return OperationMatrix(
            rename_node=ProviderOperationSupport.NATIVE,
            update_location=ProviderOperationSupport.NATIVE,
            update_role=ProviderOperationSupport.NATIVE,
            favorite_toggle=ProviderOperationSupport.NATIVE,
            read_config=ProviderOperationSupport.NATIVE,
            write_config=ProviderOperationSupport.NATIVE,
            batch_write=ProviderOperationSupport.NATIVE,
        )

    def discover_endpoints(self, hosts, port=None, progress_cb=None):
        return [ProviderConnection(endpoint=f"tcp://{h}:4403", host=h, port=4403) for h in hosts]

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider=ProviderType.MESHTASTIC, status="ok")

    def connect(self, endpoint):
        return object()

    def disconnect(self, conn) -> None:
        self.disconnect_calls += 1

    def fetch_nodes(self, conn):
        return []

    def fetch_config(self, conn, provider_node_id):
        return {}

    def apply_config_patch(self, conn, provider_node_id, patch, dry_run):
        return {"ok": True}


def test_operations_service_disconnects_after_patch(db) -> None:
    provider = _LifecycleProvider()
    registry = ProviderRegistry()
    registry._providers["meshtastic"] = provider

    node = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!abcd",
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        reachable=True,
    )
    db.add(node)
    db.flush()
    db.add(
        NodeEndpointModel(
            node_id=node.id,
            endpoint="tcp://172.30.105.36:4403",
            host="172.30.105.36",
            port=4403,
            source="test",
            is_primary=True,
            last_seen=datetime.now(timezone.utc),
        )
    )
    db.commit()

    service = OperationsService(db, registry)
    service.apply_patch(
        node_id=node.id,
        patch=ConfigPatch(short_name="TEST"),
        actor="tester",
        source="test",
        dry_run=False,
    )

    assert provider.disconnect_calls == 1


def test_sync_service_disconnects_after_endpoint_sync(db) -> None:
    provider = _LifecycleProvider()
    registry = ProviderRegistry()
    registry._providers["meshtastic"] = provider

    endpoint = ProviderEndpointModel(
        provider_name="meshtastic",
        endpoint="tcp://172.30.105.36:4403",
        host="172.30.105.36",
        port=4403,
        source="test",
        reachable=True,
        last_seen=datetime.now(timezone.utc),
        meta_json={"transport": "tcp"},
    )
    db.add(endpoint)
    db.commit()

    service = SyncService(db, registry)
    service.sync_endpoint(str(endpoint.id), quick=True)

    assert provider.disconnect_calls == 1
