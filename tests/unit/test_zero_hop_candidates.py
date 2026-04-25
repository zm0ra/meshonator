from __future__ import annotations

from datetime import datetime, timezone

from meshonator.api.app import _filter_zero_hop_candidates, _find_managed_nodes_for_candidates
from meshonator.db.models import ManagedNodeModel


def test_filter_zero_hop_candidates_by_role_and_hops() -> None:
    raw = {
        "nodesInMesh": {
            "!a": {"hopsAway": 0, "user": {"id": "!a", "role": "ROUTER", "shortName": "A"}},
            "!b": {"hopsAway": 0, "user": {"id": "!b", "role": "CLIENT_MUTE", "shortName": "B"}},
            "!c": {"hopsAway": 2, "user": {"id": "!c", "role": "ROUTER", "shortName": "C"}},
        }
    }
    out = _filter_zero_hop_candidates(raw, max_hops=0, allowed_roles={"ROUTER", "CLIENT_BASE"})
    assert [item["id"] for item in out] == ["!a"]


def test_find_managed_nodes_for_candidates_filters_by_provider(db) -> None:
    mesh_node = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!a",
        short_name="A",
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        reachable=True,
    )
    other_provider_node = ManagedNodeModel(
        provider="meshcore",
        provider_node_id="!a",
        short_name="A2",
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        reachable=True,
    )
    db.add(mesh_node)
    db.add(other_provider_node)
    db.commit()

    matches = _find_managed_nodes_for_candidates(
        db=db,
        provider="meshtastic",
        candidates=[{"id": "!a"}, {"id": "!missing"}],
    )
    assert len(matches) == 1
    assert matches[0].provider == "meshtastic"
    assert matches[0].provider_node_id == "!a"
