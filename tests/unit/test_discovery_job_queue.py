from __future__ import annotations

from meshonator.db.models import JobModel, JobResultModel
from meshonator.jobs.queue import _run_discovery_job
from meshonator.jobs.service import JobsService
from meshonator.providers.registry import ProviderRegistry


def test_discovery_job_fails_when_no_endpoints_are_found(db, monkeypatch):
    class FakeDiscoveryService:
        def __init__(self, db_session, registry):
            self.db_session = db_session
            self.registry = registry

        def scan(self, **kwargs):
            return []

    class FakeSyncService:
        def __init__(self, db_session, registry):
            self.db_session = db_session
            self.registry = registry

        def sync_all(self, quick=True):
            raise AssertionError("auto-sync should not run when discovery found no endpoints")

    monkeypatch.setattr("meshonator.jobs.queue.DiscoveryService", FakeDiscoveryService)
    monkeypatch.setattr("meshonator.jobs.queue.SyncService", FakeSyncService)

    job = JobsService(db).create(
        job_type="discovery_scan",
        requested_by="tester",
        source="test",
        payload={
            "provider": "meshtastic",
            "hosts": ["badhost.invalid.local"],
            "cidrs": [],
            "manual_endpoints": [],
            "auto_sync": True,
            "source": "test",
        },
    )

    _run_discovery_job(job.id, ProviderRegistry())
    db.expire_all()

    stored_job = db.get(JobModel, job.id)
    assert stored_job is not None
    assert stored_job.status == "failed"

    results = db.query(JobResultModel).filter(JobResultModel.job_id == job.id).order_by(JobResultModel.created_at.asc()).all()
    assert any(result.status == "failed" and "0 endpoints" in result.message for result in results)