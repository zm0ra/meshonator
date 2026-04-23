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
