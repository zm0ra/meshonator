from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

from meshonator.audit.service import AuditService
from meshonator.db.models import JobModel
from meshonator.db.session import SessionLocal
from meshonator.discovery.service import DiscoveryService
from meshonator.jobs.service import JobsService
from meshonator.providers.registry import ProviderRegistry
from meshonator.sync.service import SyncService

executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="meshonator-jobs")


def enqueue_discovery_job(job_id: UUID, registry: ProviderRegistry) -> None:
    executor.submit(_run_discovery_job, job_id, registry)


def enqueue_sync_job(job_id: UUID, registry: ProviderRegistry) -> None:
    executor.submit(_run_sync_job, job_id, registry)


def _run_discovery_job(job_id: UUID, registry: ProviderRegistry) -> None:
    with SessionLocal() as db:
        jobs = JobsService(db)
        audit = AuditService(db)
        job = db.get(JobModel, job_id)
        if job is None:
            return

        jobs.start(job_id)
        payload = job.payload or {}
        try:
            found = DiscoveryService(db, registry).scan(
                provider_name=payload.get("provider", "meshtastic"),
                hosts=payload.get("hosts", []),
                cidrs=payload.get("cidrs", []),
                manual_endpoints=payload.get("manual_endpoints", []),
                port=payload.get("port"),
                source=payload.get("source", "ui"),
            )
            jobs.add_result(
                job_id=job_id,
                status="success",
                node_id=None,
                message=f"Discovery finished with {len(found)} endpoints",
                details={"endpoints": [item.endpoint for item in found]},
            )
            jobs.finish(job_id, success=True)
            audit.log(
                actor=job.requested_by,
                source=job.source,
                action="discovery.scan",
                provider=payload.get("provider"),
                metadata={"queued": True, "completed_at": datetime.now(UTC).isoformat()},
            )
        except Exception as exc:
            jobs.add_result(job_id=job_id, status="failed", node_id=None, message=str(exc), details={})
            jobs.finish(job_id, success=False)


def _run_sync_job(job_id: UUID, registry: ProviderRegistry) -> None:
    with SessionLocal() as db:
        jobs = JobsService(db)
        audit = AuditService(db)
        job = db.get(JobModel, job_id)
        if job is None:
            return

        jobs.start(job_id)
        payload = job.payload or {}
        quick = bool(payload.get("quick", False))
        try:
            results = SyncService(db, registry).sync_all(quick=quick)
            failed = [item for item in results if item.get("status") != "success"]
            jobs.add_result(
                job_id=job_id,
                status="success" if not failed else "failed",
                node_id=None,
                message=f"Sync finished: {len(results)} endpoints, {len(failed)} failed",
                details={"results": results},
            )
            jobs.finish(job_id, success=not failed)
            audit.log(
                actor=job.requested_by,
                source=job.source,
                action="sync.all",
                metadata={
                    "queued": True,
                    "quick": quick,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as exc:
            jobs.add_result(job_id=job_id, status="failed", node_id=None, message=str(exc), details={})
            jobs.finish(job_id, success=False)
