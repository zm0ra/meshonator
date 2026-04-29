from __future__ import annotations

from meshonator.db.models import JobModel, JobResultModel, ManagedNodeModel, NodeEndpointModel
from meshonator.jobs.queue import _run_bulk_nodedb_job
from meshonator.jobs.service import JobsService
from meshonator.providers.registry import ProviderRegistry


def test_bulk_nodedb_refresh_triggers_quick_sync(db, monkeypatch):
    destination = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!dest-a",
        short_name="DEST-A",
        reachable=True,
        raw_metadata={"nodesInMesh": {}},
    )
    db.add(destination)
    db.flush()
    db.add(
        NodeEndpointModel(
            node_id=destination.id,
            endpoint="tcp://172.30.0.10:4403",
            host="172.30.0.10",
            port=4403,
            source="test",
            is_primary=True,
        )
    )
    db.commit()

    sync_calls: list[bool] = []

    class FakeOperationsService:
        def __init__(self, db_session, registry):
            self.db_session = db_session
            self.registry = registry

        def mutate_node_db(self, **kwargs):
            return {
                "destination_node_id": "!dest-a",
                "action": kwargs["action"],
                "target_count": len(kwargs["target_node_ids"]),
                "applied_count": len(kwargs["target_node_ids"]),
                "failed_count": 0,
                "applied": [],
                "failures": [],
                "dry_run": kwargs["dry_run"],
            }

    class FakeSyncService:
        def __init__(self, db_session, registry):
            self.db_session = db_session
            self.registry = registry

        def sync_all(self, quick=True):
            sync_calls.append(quick)
            return [{"status": "success", "provider": "meshtastic"}]

    monkeypatch.setattr("meshonator.jobs.queue.OperationsService", FakeOperationsService)
    monkeypatch.setattr("meshonator.jobs.queue.SyncService", FakeSyncService)

    job = JobsService(db).create(
        job_type="bulk_nodedb_mutation",
        requested_by="tester",
        source="test",
        payload={
            "destination_node_ids": [str(destination.id)],
            "target_node_ids": ["!peer-1", "!peer-2"],
            "action": "set_favorite",
            "dry_run": False,
            "exclude_self": True,
            "refresh_mode": True,
            "source_max_hops": 0,
        },
    )

    _run_bulk_nodedb_job(job.id, ProviderRegistry())
    db.expire_all()

    stored_job = db.get(JobModel, job.id)
    assert stored_job is not None
    assert stored_job.status == "success"
    assert sync_calls == [True]

    results = (
        db.query(JobResultModel)
        .filter(JobResultModel.job_id == job.id)
        .order_by(JobResultModel.created_at.asc())
        .all()
    )
    assert any("Auto-sync after favorite refresh" in result.message for result in results)