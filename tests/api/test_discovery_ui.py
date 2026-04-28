from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import JobModel


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert response.status_code in (302, 303)
    return response.cookies


def test_discovery_requires_at_least_one_target(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    initial_job_count = db.query(JobModel).count()
    cookies = _login(client)

    response = client.post("/ui/discovery/scan", data={}, cookies=cookies, follow_redirects=False)

    assert response.status_code in (302, 303)
    assert response.headers["location"].startswith("/?error=")
    assert db.query(JobModel).count() == initial_job_count