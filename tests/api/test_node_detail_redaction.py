from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import ManagedNodeModel


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert response.status_code in (302, 303)
    return response.cookies


def test_node_detail_redacts_sensitive_payload_fields(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    node = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!node-1",
        short_name="NODE1",
        reachable=True,
        raw_metadata={
            "owner": {"longName": "Node One", "shortName": "N1"},
            "metadata": {"firmwareVersion": "1.0.0", "hwModel": "TBEAM", "role": "CLIENT"},
            "myInfo": {"myNodeNum": 123},
            "primaryChannelUrl": "super-secret-channel-url",
            "preferences": {"security": {"privateKey": "secret-private", "publicKey": "secret-public"}},
            "modulePreferences": {"mqtt": {"password": "mqtt-secret"}},
            "channels": [{"index": 0, "settings": {"psk": "mesh-secret"}}],
            "nodesInMesh": {},
        },
    )
    db.add(node)
    db.commit()

    cookies = _login(client)
    response = client.get(f"/nodes/{node.id}", cookies=cookies)

    assert response.status_code == 200
    body = response.text
    assert "Redacted Provider Payload" in body
    assert "Hidden in operator view" in body
    assert "super-secret-channel-url" not in body
    assert "secret-private" not in body
    assert "secret-public" not in body
    assert "mqtt-secret" not in body
    assert "mesh-secret" not in body