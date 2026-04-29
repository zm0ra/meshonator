from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import ManagedNodeModel


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert response.status_code in (302, 303)
    return response.cookies


def test_ui_nodes_compare_get_redirects_to_nodes(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    cookies = _login(client)

    response = client.get("/ui/nodes/compare", cookies=cookies, follow_redirects=False)

    assert response.status_code in (302, 303)
    assert response.headers["location"].startswith("/nodes?error=")


def test_ui_nodes_compare_renders_nodes_page_with_results(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    source = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!cmp-src",
        short_name="CMP-SRC",
        long_name="Compare Source",
        reachable=True,
        raw_metadata={
            "preferences": {"device": {"role": "ROUTER"}},
            "modulePreferences": {"mqtt": {"enabled": True}},
            "channels": [{"index": 0, "settings": {"name": "mesh-a"}}],
        },
    )
    target = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!cmp-dst",
        short_name="CMP-DST",
        long_name="Compare Target",
        reachable=True,
        raw_metadata={
            "preferences": {"device": {"role": "CLIENT"}},
            "modulePreferences": {"mqtt": {"enabled": False}},
            "channels": [{"index": 0, "settings": {"name": "mesh-b"}}],
        },
    )
    db.add_all([source, target])
    db.commit()

    cookies = _login(client)
    response = client.post(
        "/ui/nodes/compare",
        data={
            "source_node_id": str(source.id),
            "target_node_ids": [str(target.id)],
            "use_alignment_profile": "true",
        },
        cookies=cookies,
    )

    assert response.status_code == 200
    body = response.text
    assert "Compared 1 target nodes" in body
    assert "Compare result" in body
    assert "CMP-SRC" in body
    assert "CMP-DST" in body