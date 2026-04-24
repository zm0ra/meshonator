from __future__ import annotations

import os
import socket
import time
from datetime import datetime, timezone

from meshonator.db.session import SessionLocal
from meshonator.jobs.queue import run_job
from meshonator.jobs.service import JobsService
from meshonator.providers.registry import ProviderRegistry


def run_worker_loop(registry: ProviderRegistry, poll_interval_s: float = 2.0, once: bool = False) -> int:
    processed = 0
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    host = socket.gethostname()
    pid = os.getpid()
    last_claimed_job_id = None
    with SessionLocal() as db:
        JobsService(db).touch_worker_heartbeat(
            worker_id=worker_id,
            mode="external",
            host=host,
            pid=pid,
            status="running",
            processed_jobs=processed,
            details={"poll_interval_s": poll_interval_s, "once": once, "started_at": datetime.now(timezone.utc).isoformat()},
        )
    while True:
        with SessionLocal() as db:
            jobs = JobsService(db)
            job_id = jobs.claim_next_pending_id()
            jobs.touch_worker_heartbeat(
                worker_id=worker_id,
                mode="external",
                host=host,
                pid=pid,
                status="running",
                last_claimed_job_id=last_claimed_job_id,
                processed_jobs=processed,
            )

        if job_id is None:
            if once:
                with SessionLocal() as db:
                    JobsService(db).touch_worker_heartbeat(
                        worker_id=worker_id,
                        mode="external",
                        host=host,
                        pid=pid,
                        status="idle",
                        last_claimed_job_id=last_claimed_job_id,
                        processed_jobs=processed,
                    )
                return processed
            time.sleep(poll_interval_s)
            continue

        last_claimed_job_id = job_id
        run_job(job_id, registry)
        processed += 1
        with SessionLocal() as db:
            JobsService(db).touch_worker_heartbeat(
                worker_id=worker_id,
                mode="external",
                host=host,
                pid=pid,
                status="running",
                last_claimed_job_id=job_id,
                last_completed_job_id=job_id,
                processed_jobs=processed,
            )

        if once:
            with SessionLocal() as db:
                JobsService(db).touch_worker_heartbeat(
                    worker_id=worker_id,
                    mode="external",
                    host=host,
                    pid=pid,
                    status="idle",
                    last_claimed_job_id=job_id,
                    last_completed_job_id=job_id,
                    processed_jobs=processed,
                )
            return processed
