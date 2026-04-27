from __future__ import annotations

from urllib.parse import unquote_plus

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import JobModel, ManagedNodeModel


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert response.status_code in (302, 303)
    return response.cookies


def test_batch_configure_requires_confirmation(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    node = ManagedNodeModel(provider="meshtastic", provider_node_id="!batch-confirm-1", short_name="NODE1", reachable=True)
    db.add(node)
    db.commit()
    initial_job_count = db.query(JobModel).count()

    cookies = _login(client)
    response = client.post(
        "/ui/nodes/batch-configure",
        data={"selected_node_ids": [str(node.id)]},
        cookies=cookies,
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    location = unquote_plus(response.headers["location"])
    assert "Review the preflight summary and confirm the batch change before queueing" in location
    assert db.query(JobModel).count() == initial_job_count


def test_batch_configure_returns_operator_safe_json_error(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    node = ManagedNodeModel(provider="meshtastic", provider_node_id="!batch-json-2", short_name="NODE2", reachable=True)
    db.add(node)
    db.commit()

    cookies = _login(client)
    response = client.post(
        "/ui/nodes/batch-configure",
        data={
            "selected_node_ids": [str(node.id)],
            "confirm_batch_changes": "true",
            "local_config_patch_json": "{broken}",
        },
        cookies=cookies,
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    location = unquote_plus(response.headers["location"])
    assert "Invalid JSON in Local config patch" in location
    assert "Expecting property name enclosed in double quotes" not in location