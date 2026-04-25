from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import JobModel, ManagedNodeModel


def _login(client):
    r = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    return r.cookies


def test_queue_favorite_all_creates_batch_job(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    n1 = ManagedNodeModel(provider="meshtastic", provider_node_id="!n1", short_name="N1", favorite=False, reachable=True)
    n2 = ManagedNodeModel(provider="meshtastic", provider_node_id="!n2", short_name="N2", favorite=False, reachable=True)
    db.add(n1)
    db.add(n2)
    db.commit()
    expected_ids = {str(n1.id), str(n2.id)}

    cookies = _login(client)
    response = client.post("/ui/nodes/favorites/all", data={}, cookies=cookies, follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["location"].startswith("/nodes?message=")

    job = db.query(JobModel).order_by(JobModel.created_at.desc()).first()
    assert job is not None
    assert job.job_type == "multi_node_config_patch"
    assert job.payload["base_patch"]["favorite"] is True
    queued_ids = set(job.payload["node_ids"])
    assert expected_ids.issubset(queued_ids)
