from __future__ import annotations

import time

from meshonator.db.session import SessionLocal
from meshonator.jobs.queue import run_job
from meshonator.jobs.service import JobsService
from meshonator.providers.registry import ProviderRegistry


def run_worker_loop(registry: ProviderRegistry, poll_interval_s: float = 2.0, once: bool = False) -> int:
    processed = 0
    while True:
        with SessionLocal() as db:
            job = JobsService(db).claim_next_pending()

        if job is None:
            if once:
                return processed
            time.sleep(poll_interval_s)
            continue

        run_job(job.id, registry)
        processed += 1

        if once:
            return processed

