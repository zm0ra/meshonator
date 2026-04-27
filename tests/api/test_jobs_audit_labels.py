from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import AuditLogModel, JobModel


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert response.status_code in (302, 303)
    return response.cookies


def test_jobs_and_audit_use_operator_language(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    db.add(JobModel(job_type="sync_all", status="success", requested_by="tester", source="ui", payload={}))
    db.add(AuditLogModel(actor="tester", source="ui", action="discovery.scan", provider="meshtastic", metadata={}))
    db.commit()

    cookies = _login(client)

    jobs_response = client.get("/jobs", cookies=cookies)
    assert jobs_response.status_code == 200
    assert '<meta http-equiv="refresh" content="3">' in jobs_response.text
    assert "Fleet sync" in jobs_response.text

    audit_response = client.get("/audit", cookies=cookies)
    assert audit_response.status_code == 200
    assert "Discovery sweep" in audit_response.text
    assert "discovery.scan" not in audit_response.text