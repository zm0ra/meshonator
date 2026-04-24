from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

from meshonator.audit.service import AuditService
from meshonator.config.settings import get_settings
from sqlalchemy import select

from meshonator.db.models import JobModel, ProviderEndpointModel
from meshonator.db.session import SessionLocal
from meshonator.discovery.service import DiscoveryService
from meshonator.domain.models import ConfigPatch
from meshonator.groups.service import GroupsService
from meshonator.jobs.service import JobsService
from meshonator.operations.service import OperationsService
from meshonator.providers.registry import ProviderRegistry
from meshonator.providers.utils.tcp_scan import expand_targets
from meshonator.sync.service import SyncService

executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="meshonator-jobs")
settings = get_settings()


def enqueue_discovery_job(job_id: UUID, registry: ProviderRegistry) -> None:
    if settings.job_executor_mode == "local":
        executor.submit(_run_discovery_job, job_id, registry)


def enqueue_sync_job(job_id: UUID, registry: ProviderRegistry) -> None:
    if settings.job_executor_mode == "local":
        executor.submit(_run_sync_job, job_id, registry)


def enqueue_group_patch_job(job_id: UUID, registry: ProviderRegistry) -> None:
    if settings.job_executor_mode == "local":
        executor.submit(_run_group_patch_job, job_id, registry)


def enqueue_node_patch_job(job_id: UUID, registry: ProviderRegistry) -> None:
    if settings.job_executor_mode == "local":
        executor.submit(_run_node_patch_job, job_id, registry)


def run_job(job_id: UUID, registry: ProviderRegistry) -> None:
    with SessionLocal() as db:
        job = db.get(JobModel, job_id)
        if job is None:
            return
        job_type = job.job_type

    if job_type == "discovery_scan":
        _run_discovery_job(job_id, registry)
    elif job_type == "sync_all":
        _run_sync_job(job_id, registry)
    elif job_type == "group_apply_template":
        _run_group_patch_job(job_id, registry)
    elif job_type == "node_config_patch":
        _run_node_patch_job(job_id, registry)
    else:
        with SessionLocal() as db:
            jobs = JobsService(db)
            jobs.add_result(
                job_id=job_id,
                status="failed",
                node_id=None,
                message=f"Unsupported job type: {job_type}",
                details={},
            )
            jobs.finish(job_id, success=False)


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
            provider_name = payload.get("provider", "meshtastic")
            hosts = payload.get("hosts", [])
            cidrs = payload.get("cidrs", [])
            manual_endpoints = payload.get("manual_endpoints", [])
            port = payload.get("port")
            expanded_targets = expand_targets(hosts, cidrs)
            for endpoint in manual_endpoints:
                if isinstance(endpoint, str) and endpoint.startswith("tcp://"):
                    expanded_targets.append(endpoint.removeprefix("tcp://").split(":")[0])
            unique_targets = sorted(set(expanded_targets))
            jobs.add_result(
                job_id=job_id,
                status="running",
                node_id=None,
                message="Discovery started",
                details={
                    "provider": provider_name,
                    "hosts": hosts,
                    "cidrs": cidrs,
                    "manual_endpoints": manual_endpoints,
                    "port": port,
                    "expanded_target_count": len(unique_targets),
                    "expanded_targets_sample": unique_targets[:100],
                    "source": payload.get("source", "ui"),
                },
            )
            found_count = 0
            closed_count = 0

            def _progress(scanned: int, total: int, host: str, is_open: bool, probe: dict | None = None) -> None:
                nonlocal found_count, closed_count
                if is_open:
                    found_count += 1
                else:
                    closed_count += 1
                probe_details = probe or {}
                if is_open:
                    message = f"Probe {scanned}/{total}: {host} open"
                else:
                    reason = probe_details.get("reason") or "closed"
                    message = f"Probe {scanned}/{total}: {host} closed ({reason})"
                jobs.add_result(
                    job_id=job_id,
                    status="running",
                    node_id=None,
                    message=message,
                    details={
                        "scanned": scanned,
                        "total": total,
                        "host": host,
                        "reachable": is_open,
                        "found_so_far": found_count,
                        "closed_so_far": closed_count,
                        "probe": probe_details,
                    },
                )

            existing_endpoints = set(
                db.scalars(
                    select(ProviderEndpointModel.endpoint).where(ProviderEndpointModel.provider_name == provider_name)
                ).all()
            )
            found = DiscoveryService(db, registry).scan(
                provider_name=provider_name,
                hosts=hosts,
                cidrs=cidrs,
                manual_endpoints=manual_endpoints,
                port=port,
                source=payload.get("source", "ui"),
                progress_cb=_progress,
            )
            discovered_endpoints = [item.endpoint for item in found]
            new_endpoints = sorted(set(discovered_endpoints) - existing_endpoints)
            existing_reconfirmed = sorted(set(discovered_endpoints) & existing_endpoints)
            jobs.add_result(
                job_id=job_id,
                status="success",
                node_id=None,
                message=f"Discovery finished with {len(found)} endpoints",
                details={
                    "endpoints": discovered_endpoints,
                    "new_endpoints": new_endpoints,
                    "existing_reconfirmed": existing_reconfirmed,
                    "targets_total": len(unique_targets),
                    "open_hosts": found_count,
                    "closed_hosts": closed_count,
                },
            )
            sync_failed = []
            if payload.get("auto_sync", True):
                sync_results = SyncService(db, registry).sync_all(quick=True)
                sync_failed = [item for item in sync_results if item.get("status") != "success"]
                jobs.add_result(
                    job_id=job_id,
                    status="success" if not sync_failed else "failed",
                    node_id=None,
                    message=f"Auto-sync after discovery: {len(sync_results)} endpoints, {len(sync_failed)} failed",
                    details={"sync_results": sync_results},
                )
            jobs.finish(job_id, success=(len(sync_failed) == 0))
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


def _run_group_patch_job(job_id: UUID, registry: ProviderRegistry) -> None:
    with SessionLocal() as db:
        jobs = JobsService(db)
        job = db.get(JobModel, job_id)
        if job is None:
            return
        jobs.start(job_id)
        payload = job.payload or {}
        group_id = payload.get("group_id")
        dry_run = bool(payload.get("dry_run", True))
        patch_payload = payload.get("patch", {})

        try:
            if group_id is None:
                raise ValueError("Missing group_id")
            groups = GroupsService(db)
            group = groups.get_group(UUID(group_id))
            if group is None:
                raise ValueError("Group not found")

            members = groups.resolve_all_members(group)
            ops = OperationsService(db, registry)
            patch = ConfigPatch(**patch_payload)

            success_count = 0
            fail_count = 0
            for node in members:
                try:
                    result = ops.apply_patch(
                        node_id=node.id,
                        patch=patch,
                        actor=job.requested_by,
                        source=job.source,
                        dry_run=dry_run,
                    )
                    jobs.add_result(
                        job_id=job_id,
                        status="success",
                        node_id=node.id,
                        message="Group patch applied",
                        details={"result": result},
                    )
                    success_count += 1
                except Exception as node_exc:
                    db.rollback()
                    jobs.add_result(
                        job_id=job_id,
                        status="failed",
                        node_id=node.id,
                        message=str(node_exc),
                        details={},
                    )
                    fail_count += 1

            jobs.finish(job_id, success=fail_count == 0)
            AuditService(db).log(
                actor=job.requested_by,
                source=job.source,
                action="group.apply_template",
                group_id=UUID(group_id),
                metadata={
                    "dry_run": dry_run,
                    "members": len(members),
                    "success_count": success_count,
                    "fail_count": fail_count,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as exc:
            db.rollback()
            jobs.add_result(job_id=job_id, status="failed", node_id=None, message=str(exc), details={})
            jobs.finish(job_id, success=False)


def _run_node_patch_job(job_id: UUID, registry: ProviderRegistry) -> None:
    with SessionLocal() as db:
        jobs = JobsService(db)
        job = db.get(JobModel, job_id)
        if job is None:
            return
        jobs.start(job_id)
        payload = job.payload or {}
        node_id = payload.get("node_id")
        dry_run = bool(payload.get("dry_run", True))
        patch_payload = payload.get("patch", {})

        try:
            if node_id is None:
                raise ValueError("Missing node_id")
            patch = ConfigPatch(**patch_payload)
            result = OperationsService(db, registry).apply_patch(
                node_id=UUID(node_id),
                patch=patch,
                actor=job.requested_by,
                source=job.source,
                dry_run=dry_run,
            )
            jobs.add_result(
                job_id=job_id,
                status="success",
                node_id=UUID(node_id),
                message="Node patch applied",
                details={"result": result},
            )
            jobs.finish(job_id, success=True)
            AuditService(db).log(
                actor=job.requested_by,
                source=job.source,
                action="node.patch.queued",
                node_id=UUID(node_id),
                metadata={
                    "dry_run": dry_run,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as exc:
            db.rollback()
            jobs.add_result(job_id=job_id, status="failed", node_id=None, message=str(exc), details={})
            jobs.finish(job_id, success=False)
