from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from meshonator.db.models import JobModel, JobResultModel


class JobsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, job_type: str, requested_by: str, source: str, payload: dict) -> JobModel:
        job = JobModel(job_type=job_type, status="pending", requested_by=requested_by, source=source, payload=payload)
        self.db.add(job)
        self.db.commit()
        return job

    def start(self, job_id: UUID) -> JobModel:
        job = self.db.get(JobModel, job_id)
        if job is None:
            raise ValueError("Job not found")
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        self.db.commit()
        return job

    def finish(self, job_id: UUID, success: bool) -> JobModel:
        job = self.db.get(JobModel, job_id)
        if job is None:
            raise ValueError("Job not found")
        job.status = "success" if success else "failed"
        job.finished_at = datetime.now(timezone.utc)
        self.db.commit()
        return job

    def add_result(self, job_id: UUID, status: str, node_id: UUID | None, message: str, details: dict) -> None:
        row = JobResultModel(job_id=job_id, status=status, node_id=node_id, message=message, details=details)
        self.db.add(row)
        self.db.commit()

    def list_jobs(self, limit: int = 100) -> list[JobModel]:
        return list(self.db.scalars(select(JobModel).order_by(JobModel.created_at.desc()).limit(limit)).all())

    def list_results(self, limit: int = 500) -> list[JobResultModel]:
        return list(self.db.scalars(select(JobResultModel).order_by(JobResultModel.created_at.desc()).limit(limit)).all())

    def recover_stale_running_jobs(self, stale_after_minutes: int | None = 15) -> int:
        now = datetime.now(timezone.utc)
        cutoff = None if stale_after_minutes is None else now.timestamp() - (stale_after_minutes * 60)
        recovered = 0
        rows = list(self.db.scalars(select(JobModel).where(JobModel.status.in_(["pending", "running"]))).all())
        for job in rows:
            if cutoff is not None:
                anchor = job.started_at or job.created_at
                if anchor is None:
                    continue
                if anchor.timestamp() >= cutoff:
                    continue
            job.status = "failed"
            job.finished_at = now
            self.db.add(
                JobResultModel(
                    job_id=job.id,
                    status="failed",
                    node_id=None,
                    message="Recovered stale job during startup",
                    details={
                        "reason": "stale_recovery" if stale_after_minutes is not None else "startup_orphan_recovery",
                        "stale_after_minutes": stale_after_minutes,
                    },
                )
            )
            recovered += 1
        if recovered:
            self.db.commit()
        return recovered
