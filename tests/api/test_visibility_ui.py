from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import ManagedNodeModel


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert response.status_code in (302, 303)
    return response.cookies


def test_visibility_page_shows_mutual_links_and_favorite_gaps(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    node_a = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!a",
        short_name="NODEA",
        long_name="Node A",
        reachable=True,
        favorite=True,
        raw_metadata={
            "nodesInMesh": {
                "!b": {
                    "hopsAway": 0,
                    "isFavorite": True,
                    "snr": 8.4,
                    "user": {"id": "!b", "shortName": "NODEB", "longName": "Node B", "role": "ROUTER"},
                },
                "!c": {
                    "hopsAway": 0,
                    "isFavorite": False,
                    "snr": 5.1,
                    "user": {"id": "!c", "shortName": "NODEC", "longName": "Node C", "role": "CLIENT_BASE"},
                },
                "!x": {
                    "hopsAway": 0,
                    "isFavorite": False,
                    "user": {"id": "!x", "shortName": "UNMAN", "longName": "Unmanaged", "role": "ROUTER"},
                },
            }
        },
    )
    node_b = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!b",
        short_name="NODEB",
        long_name="Node B",
        reachable=True,
        favorite=False,
        raw_metadata={
            "nodesInMesh": {
                "!a": {
                    "hopsAway": 0,
                    "isFavorite": True,
                    "snr": 7.7,
                    "user": {"id": "!a", "shortName": "NODEA", "longName": "Node A", "role": "ROUTER"},
                }
            }
        },
    )
    node_c = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!c",
        short_name="NODEC",
        long_name="Node C",
        reachable=False,
        favorite=False,
        raw_metadata={"nodesInMesh": {}},
    )
    db.add_all([node_a, node_b, node_c])
    db.commit()

    cookies = _login(client)
    response = client.get("/visibility", cookies=cookies)

    assert response.status_code == 200
    body = response.text
    assert "0-hop visibility and favorite coverage" in body
    assert "NODEA" in body
    assert "NODEB" in body
    assert "NODEC" in body
    assert "Has favorites for:" in body
    assert "Missing favorites for:" in body
    assert "Has favorites for:</strong> NODEB" in body
    assert "Missing favorites for:</strong> NODEC" in body
    assert "All direct peers favorite" in body
    assert "1 direct peer missing favorite" in body
    assert "Mutual 0-hop pairs" in body
    assert "One-way visibility" in body
    assert "UNMAN" in body


def test_visibility_page_filters_to_missing_favorites(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    db.add_all(
        [
            ManagedNodeModel(
                provider="meshtastic",
                provider_node_id="!d",
                short_name="NODED",
                long_name="Node D",
                reachable=True,
                raw_metadata={
                    "nodesInMesh": {
                        "!e": {
                            "hopsAway": 0,
                            "isFavorite": False,
                            "user": {"id": "!e", "shortName": "NODEE", "longName": "Node E", "role": "ROUTER"},
                        }
                    }
                },
            ),
            ManagedNodeModel(
                provider="meshtastic",
                provider_node_id="!e",
                short_name="NODEE",
                long_name="Node E",
                reachable=True,
                raw_metadata={
                    "nodesInMesh": {
                        "!d": {
                            "hopsAway": 0,
                            "isFavorite": True,
                            "user": {"id": "!d", "shortName": "NODED", "longName": "Node D", "role": "ROUTER"},
                        }
                    }
                },
            ),
        ]
    )
    db.commit()

    cookies = _login(client)
    response = client.get("/visibility?q=NODED&source_filter=with_gaps&relation_filter=missing_favorite", cookies=cookies)

    assert response.status_code == 200
    body = response.text
    assert "Only favorite gaps" in body
    assert "Missing favorite only" in body
    assert "1 direct peer missing favorite" in body
    assert "NODED" in body
    assert "NODEE" in body
    assert "No one-way 0-hop visibility gaps found in current managed snapshots." in body
    assert "Align missing favorites" in body
    assert 'name="selected_node_ids"' in body