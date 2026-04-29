from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from meshonator.db.models import ManagedNodeModel, NodeEndpointModel, ProviderEndpointModel
from meshonator.inventory.service import InventoryService


def test_refresh_transport_reachability_updates_endpoints_and_nodes(db, monkeypatch) -> None:
    endpoint = ProviderEndpointModel(
        provider_name="meshtastic",
        endpoint="tcp://172.30.1.10:4403",
        host="172.30.1.10",
        port=4403,
        source="test",
        reachable=False,
        meta_json={},
    )
    node = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!node-a",
        short_name="A",
        reachable=False,
        raw_metadata={},
    )
    db.add_all([endpoint, node])
    db.flush()
    db.add(
        NodeEndpointModel(
            node_id=node.id,
            endpoint=endpoint.endpoint,
            host=endpoint.host,
            port=endpoint.port,
            source="test",
            is_primary=True,
        )
    )
    db.commit()

    monkeypatch.setattr(
        "meshonator.inventory.service.tcp_probe",
        lambda host, port, timeout: {"is_open": True, "latency_ms": 12.5, "timeout_s": timeout},
    )

    result = InventoryService(db).refresh_transport_reachability(timeout=0.75)
    db.expire_all()

    stored_endpoint = db.get(ProviderEndpointModel, endpoint.id)
    stored_node = db.get(ManagedNodeModel, node.id)
    stored_node_endpoint = db.scalar(select(NodeEndpointModel).where(NodeEndpointModel.node_id == node.id))
    assert stored_endpoint is not None
    assert stored_node is not None
    assert stored_node_endpoint is not None
    assert stored_endpoint.reachable is True
    assert stored_endpoint.last_seen is not None
    assert stored_endpoint.meta_json["last_transport_probe"]["is_open"] is True
    assert stored_node.reachable is True
    assert stored_node_endpoint.last_seen is not None
    assert result["endpoints_online"] == 1
    assert result["nodes_online"] == 1
    assert result["node_changes"] == 1


def test_stale_mark_tracks_freshness_without_overwriting_reachability(db) -> None:
    stale_last_seen = datetime.now(timezone.utc) - timedelta(minutes=45)
    node = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!node-b",
        short_name="B",
        reachable=True,
        last_seen=stale_last_seen,
        raw_metadata={},
    )
    db.add(node)
    db.commit()

    changed = InventoryService(db).stale_mark(stale_minutes=30)
    db.expire_all()

    stored_node = db.get(ManagedNodeModel, node.id)
    assert stored_node is not None
    assert changed == 1
    assert stored_node.reachable is True
    assert stored_node.raw_metadata["fresh"] is False


def test_refresh_transport_reachability_requires_two_failures_before_offline(db, monkeypatch) -> None:
    endpoint = ProviderEndpointModel(
        provider_name="meshtastic",
        endpoint="tcp://172.30.1.11:4403",
        host="172.30.1.11",
        port=4403,
        source="test",
        reachable=True,
        meta_json={},
    )
    node = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!node-c",
        short_name="C",
        reachable=True,
        raw_metadata={},
    )
    db.add_all([endpoint, node])
    db.flush()
    db.add(
        NodeEndpointModel(
            node_id=node.id,
            endpoint=endpoint.endpoint,
            host=endpoint.host,
            port=endpoint.port,
            source="test",
            is_primary=True,
        )
    )
    db.commit()

    monkeypatch.setattr(
        "meshonator.inventory.service.tcp_probe",
        lambda host, port, timeout: {"is_open": False, "latency_ms": 500.0, "timeout_s": timeout, "reason": "connect_ex_11"},
    )

    svc = InventoryService(db)
    svc.refresh_transport_reachability(timeout=0.75, failures_before_offline=2)
    db.expire_all()
    stored_after_first = db.get(ManagedNodeModel, node.id)
    stored_provider_after_first = db.get(ProviderEndpointModel, endpoint.id)

    assert stored_after_first is not None
    assert stored_provider_after_first is not None
    assert stored_after_first.reachable is True
    assert stored_provider_after_first.reachable is True
    assert stored_provider_after_first.meta_json["consecutive_transport_failures"] == 1

    svc.refresh_transport_reachability(timeout=0.75, failures_before_offline=2)
    db.expire_all()
    stored_after_second = db.get(ManagedNodeModel, node.id)
    stored_provider_after_second = db.get(ProviderEndpointModel, endpoint.id)

    assert stored_after_second is not None
    assert stored_provider_after_second is not None
    assert stored_after_second.reachable is False
    assert stored_provider_after_second.reachable is False
    assert stored_provider_after_second.meta_json["consecutive_transport_failures"] == 2