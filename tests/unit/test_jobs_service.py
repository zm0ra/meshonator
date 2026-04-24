from meshonator.jobs.service import JobsService


def test_claim_next_pending_marks_job_running(db):
    svc = JobsService(db)
    first = svc.create(job_type="discovery_scan", requested_by="tester", source="test", payload={})
    _second = svc.create(job_type="sync_all", requested_by="tester", source="test", payload={})

    claimed_id = svc.claim_next_pending_id()
    assert claimed_id is not None
    assert claimed_id == first.id
    claimed = db.get(type(first), claimed_id)
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.started_at is not None


def test_worker_heartbeat_upsert_and_fetch_latest(db):
    svc = JobsService(db)
    svc.touch_worker_heartbeat(
        worker_id="worker-a",
        mode="external",
        host="host-a",
        pid=123,
        status="running",
        processed_jobs=1,
    )
    svc.touch_worker_heartbeat(
        worker_id="worker-a",
        mode="external",
        host="host-a",
        pid=123,
        status="idle",
        processed_jobs=2,
    )
    latest = svc.get_latest_worker_heartbeat()
    assert latest is not None
    assert latest.worker_id == "worker-a"
    assert latest.status == "idle"
    assert latest.processed_jobs == 2
