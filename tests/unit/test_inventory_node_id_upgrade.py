from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select

from meshonator.db.models import ManagedNodeModel, NodeEndpointModel
from meshonator.domain.models import ManagedNode, ProviderType
from meshonator.inventory.service import InventoryService


def test_upsert_upgrades_legacy_local_provider_id_by_endpoint(db) -> None:
    db.execute(delete(NodeEndpointModel))
    db.execute(delete(ManagedNodeModel))
    db.commit()

    legacy = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="local-1762761672",
        short_name="STOO",
        first_seen=datetime.now(timezone.utc),
    )
    db.add(legacy)
    db.flush()
    db.add(
        NodeEndpointModel(
            node_id=legacy.id,
            endpoint="tcp://172.30.253.3:4403",
            host="172.30.253.3",
            port=4403,
            source="manual",
            is_primary=True,
            last_seen=datetime.now(timezone.utc),
        )
    )
    db.commit()

    InventoryService(db).upsert_nodes(
        nodes=[
            ManagedNode(
                provider=ProviderType.MESHTASTIC,
                provider_node_id="!f1b126cc",
                short_name="STOO",
                long_name="Szczecin Stolczyn OMNI",
                reachable=True,
            )
        ],
        endpoint="tcp://172.30.253.3:4403",
        host="172.30.253.3",
        port=4403,
        source="sync",
    )

    rows = list(db.scalars(select(ManagedNodeModel).where(ManagedNodeModel.provider == "meshtastic")).all())
    assert len(rows) == 1
    assert rows[0].provider_node_id == "!f1b126cc"

    db.execute(delete(NodeEndpointModel))
    db.execute(delete(ManagedNodeModel))
    db.commit()
