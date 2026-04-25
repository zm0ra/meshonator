from __future__ import annotations

from meshonator.domain.models import ManagedNode, ProviderType
from meshonator.inventory.service import InventoryService


def test_upsert_nodes_marks_managed_nodes_as_favorite(db) -> None:
    svc = InventoryService(db)
    saved = svc.upsert_nodes(
        nodes=[
            ManagedNode(
                provider=ProviderType.MESHTASTIC,
                provider_node_id="!fav-a",
                short_name="A",
                reachable=True,
                favorite=False,
            ),
            ManagedNode(
                provider=ProviderType.MESHTASTIC,
                provider_node_id="!fav-b",
                short_name="B",
                reachable=True,
                favorite=False,
            ),
        ],
        endpoint="tcp://172.30.105.36:4403",
        host="172.30.105.36",
        port=4403,
        source="sync",
    )
    assert len(saved) == 2
    assert all(node.favorite is True for node in saved)
