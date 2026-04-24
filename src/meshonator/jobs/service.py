from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from meshonator.db.models import JobModel, JobResultModel, WorkerHeartbeatModel


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

    def touch_worker_heartbeat(
        self,
        worker_id: str,
        mode: str,
        host: str,
        pid: int,
        status: str = "running",
        last_claimed_job_id: UUID | None = None,
        last_completed_job_id: UUID | None = None,
        processed_jobs: int | None = None,
        details: dict | None = None,
    ) -> WorkerHeartbeatModel:
        now = datetime.now(timezone.utc)
        row = self.db.get(WorkerHeartbeatModel, worker_id)
        if row is None:
            row = WorkerHeartbeatModel(
                worker_id=worker_id,
                mode=mode,
                host=host,
                pid=pid,
                status=status,
                started_at=now,
                last_heartbeat_at=now,
                last_claimed_job_id=last_claimed_job_id,
                last_completed_job_id=last_completed_job_id,
                processed_jobs=processed_jobs or 0,
                details=details or {},
            )
            self.db.add(row)
        else:
            row.mode = mode
            row.host = host
            row.pid = pid
            row.status = status
            row.last_heartbeat_at = now
            if last_claimed_job_id is not None:
                row.last_claimed_job_id = last_claimed_job_id
            if last_completed_job_id is not None:
                row.last_completed_job_id = last_completed_job_id
            if processed_jobs is not None:
                row.processed_jobs = processed_jobs
            if details is not None:
                row.details = details
        self.db.commit()
        return row

    def get_latest_worker_heartbeat(self) -> WorkerHeartbeatModel | None:
        return self.db.scalar(
            select(WorkerHeartbeatModel).order_by(WorkerHeartbeatModel.last_heartbeat_at.desc()).limit(1)
        )

    def claim_next_pending_id(self) -> UUID | None:
        candidate_ids = list(
            self.db.scalars(
                select(JobModel.id).where(JobModel.status == "pending").order_by(JobModel.created_at.asc()).limit(20)
            ).all()
        )
        for candidate_id in candidate_ids:
            now = datetime.now(timezone.utc)
            result = self.db.execute(
                update(JobModel)
                .where(JobModel.id == candidate_id, JobModel.status == "pending")
                .values(status="running", started_at=now)
            )
            self.db.commit()
            if result.rowcount == 1:
                return candidate_id
        return None

    def recover_stale_running_jobs(self, stale_after_minutes: int | None = 15, include_pending: bool = True) -> int:
        now = datetime.now(timezone.utc)
        cutoff = None if stale_after_minutes is None else now.timestamp() - (stale_after_minutes * 60)
        recovered = 0
        statuses = ["running", "pending"] if include_pending else ["running"]
        rows = list(self.db.scalars(select(JobModel).where(JobModel.status.in_(statuses))).all())
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
