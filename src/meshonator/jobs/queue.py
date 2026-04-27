from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from copy import deepcopy
from uuid import UUID

from meshonator.audit.service import AuditService
from meshonator.config.settings import get_settings
from sqlalchemy import select

from meshonator.db.models import JobModel, ManagedNodeModel, ProviderEndpointModel
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


def enqueue_multi_node_patch_job(job_id: UUID, registry: ProviderRegistry) -> None:
    if settings.job_executor_mode == "local":
        executor.submit(_run_multi_node_patch_job, job_id, registry)


def enqueue_node_nodedb_job(job_id: UUID, registry: ProviderRegistry) -> None:
    if settings.job_executor_mode == "local":
        executor.submit(_run_node_nodedb_job, job_id, registry)


def enqueue_bulk_nodedb_job(job_id: UUID, registry: ProviderRegistry) -> None:
    if settings.job_executor_mode == "local":
        executor.submit(_run_bulk_nodedb_job, job_id, registry)


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
    elif job_type == "multi_node_config_patch":
        _run_multi_node_patch_job(job_id, registry)
    elif job_type == "node_nodedb_mutation":
        _run_node_nodedb_job(job_id, registry)
    elif job_type == "bulk_nodedb_mutation":
        _run_bulk_nodedb_job(job_id, registry)
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


def _run_bulk_nodedb_job(job_id: UUID, registry: ProviderRegistry) -> None:
    with SessionLocal() as db:
        jobs = JobsService(db)
        job = db.get(JobModel, job_id)
        if job is None:
            return
        jobs.start(job_id)
        payload = job.payload or {}
        destination_node_ids = payload.get("destination_node_ids", [])
        action = str(payload.get("action", "")).strip()
        target_node_ids = payload.get("target_node_ids", [])
        dry_run = bool(payload.get("dry_run", True))
        exclude_self = bool(payload.get("exclude_self", True))

        try:
            if not action:
                raise ValueError("Missing action")
            if not isinstance(destination_node_ids, list) or not destination_node_ids:
                raise ValueError("No destination nodes selected")
            if not isinstance(target_node_ids, list) or not target_node_ids:
                raise ValueError("No target nodes selected")
            destinations = [UUID(item) for item in destination_node_ids]
            targets = [str(item).strip() for item in target_node_ids if isinstance(item, str) and str(item).strip()]
            if not targets:
                raise ValueError("No valid target node IDs provided")

            ops = OperationsService(db, registry)
            destination_success = 0
            destination_fail = 0
            for destination_id in destinations:
                destination = db.get(ManagedNodeModel, destination_id)
                if destination is None:
                    jobs.add_result(
                        job_id=job_id,
                        status="failed",
                        node_id=destination_id,
                        message="Destination node not found",
                        details={},
                    )
                    destination_fail += 1
                    continue
                destination_targets = targets
                if exclude_self and destination.provider_node_id:
                    destination_targets = [item for item in targets if item != destination.provider_node_id]
                if not destination_targets:
                    jobs.add_result(
                        job_id=job_id,
                        status="success",
                        node_id=destination_id,
                        message="Skipped: no remaining targets after self exclusion",
                        details={},
                    )
                    destination_success += 1
                    continue
                try:
                    result = ops.mutate_node_db(
                        destination_node_id=destination_id,
                        action=action,
                        target_node_ids=destination_targets,
                        actor=job.requested_by,
                        source=job.source,
                        dry_run=dry_run,
                    )
                    status = "success" if result.get("failed_count", 0) == 0 else "failed"
                    jobs.add_result(
                        job_id=job_id,
                        status=status,
                        node_id=destination_id,
                        message=f"NodeDB {action}: {result.get('applied_count', 0)} ok, {result.get('failed_count', 0)} failed",
                        details={"result": result},
                    )
                    if status == "success":
                        destination_success += 1
                    else:
                        destination_fail += 1
                except Exception as node_exc:
                    db.rollback()
                    jobs.add_result(
                        job_id=job_id,
                        status="failed",
                        node_id=destination_id,
                        message=str(node_exc),
                        details={},
                    )
                    destination_fail += 1

            jobs.finish(job_id, success=(destination_fail == 0))
            AuditService(db).log(
                actor=job.requested_by,
                source=job.source,
                action="node.nodedb.bulk.queued",
                metadata={
                    "dry_run": dry_run,
                    "action": action,
                    "destination_count": len(destinations),
                    "target_count": len(targets),
                    "exclude_self": exclude_self,
                    "success_count": destination_success,
                    "fail_count": destination_fail,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as exc:
            db.rollback()
            jobs.add_result(job_id=job_id, status="failed", node_id=None, message=str(exc), details={})
            jobs.finish(job_id, success=False)


def _run_node_nodedb_job(job_id: UUID, registry: ProviderRegistry) -> None:
    with SessionLocal() as db:
        jobs = JobsService(db)
        job = db.get(JobModel, job_id)
        if job is None:
            return
        jobs.start(job_id)
        payload = job.payload or {}
        destination_node_id = payload.get("destination_node_id")
        action = str(payload.get("action", "")).strip()
        target_node_ids = payload.get("target_node_ids", [])
        dry_run = bool(payload.get("dry_run", True))

        try:
            if destination_node_id is None:
                raise ValueError("Missing destination_node_id")
            if not action:
                raise ValueError("Missing action")
            if not isinstance(target_node_ids, list) or not target_node_ids:
                raise ValueError("No target nodes selected")
            targets = [str(item).strip() for item in target_node_ids if isinstance(item, str) and item.strip()]
            if not targets:
                raise ValueError("No valid target node IDs provided")

            result = OperationsService(db, registry).mutate_node_db(
                destination_node_id=UUID(destination_node_id),
                action=action,
                target_node_ids=targets,
                actor=job.requested_by,
                source=job.source,
                dry_run=dry_run,
            )

            status = "success" if result.get("failed_count", 0) == 0 else "failed"
            jobs.add_result(
                job_id=job_id,
                status=status,
                node_id=UUID(destination_node_id),
                message=f"NodeDB mutation {action} finished ({result['applied_count']} ok, {result['failed_count']} failed)",
                details={"result": result},
            )
            jobs.finish(job_id, success=(result.get("failed_count", 0) == 0))
            AuditService(db).log(
                actor=job.requested_by,
                source=job.source,
                action="node.nodedb.mutation.queued",
                node_id=UUID(destination_node_id),
                metadata={
                    "dry_run": dry_run,
                    "action": action,
                    "target_count": len(targets),
                    "completed_at": datetime.now(UTC).isoformat(),
                    "result": result,
                },
            )
        except Exception as exc:
            db.rollback()
            jobs.add_result(job_id=job_id, status="failed", node_id=None, message=str(exc), details={})
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
            discovery_success = len(found) > 0
            jobs.add_result(
                job_id=job_id,
                status="success" if discovery_success else "failed",
                node_id=None,
                message=(
                    f"Discovery finished with {len(found)} endpoints"
                    if discovery_success
                    else "Discovery found 0 endpoints. Review the target list and probe results before retrying."
                ),
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
            if discovery_success and payload.get("auto_sync", True):
                sync_results = SyncService(db, registry).sync_all(quick=True)
                sync_failed = [item for item in sync_results if item.get("status") != "success"]
                jobs.add_result(
                    job_id=job_id,
                    status="success" if not sync_failed else "failed",
                    node_id=None,
                    message=f"Auto-sync after discovery: {len(sync_results)} endpoints, {len(sync_failed)} failed",
                    details={"sync_results": sync_results},
                )
            jobs.finish(job_id, success=(discovery_success and len(sync_failed) == 0))
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


def _deep_merge_dict(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge_dict(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _merge_channels_patch(base: list[dict], override: list[dict]) -> list[dict]:
    by_index: dict[int, dict] = {}
    for item in base:
        if isinstance(item, dict) and isinstance(item.get("index"), int):
            by_index[item["index"]] = deepcopy(item)
    for item in override:
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            continue
        idx = item["index"]
        current = by_index.get(idx, {"index": idx})
        merged = deepcopy(current)
        for key, value in item.items():
            if key in {"settings", "moduleSettings", "module_settings"} and isinstance(value, dict):
                target_key = "moduleSettings" if key in {"moduleSettings", "module_settings"} else "settings"
                existing = merged.get(target_key, {})
                if not isinstance(existing, dict):
                    existing = {}
                merged[target_key] = _deep_merge_dict(existing, value)
            else:
                merged[key] = deepcopy(value)
        by_index[idx] = merged
    return [by_index[i] for i in sorted(by_index)]


def _sanitize_clone_local_config(local_cfg: dict, include_role: bool) -> dict:
    out = deepcopy(local_cfg)
    if "security" in out:
        out.pop("security", None)
    device_section = out.get("device")
    if isinstance(device_section, dict) and not include_role:
        device_section.pop("role", None)
    return out


def _channels_to_patch(channels: list) -> list[dict]:
    out: list[dict] = []
    for item in channels:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            continue
        row: dict = {"index": index}
        role = item.get("role")
        if isinstance(role, str) and role.strip():
            row["role"] = role.strip().upper()
        settings = item.get("settings")
        if isinstance(settings, dict):
            row["settings"] = settings
        module_settings = item.get("moduleSettings")
        if not isinstance(module_settings, dict):
            module_settings = item.get("module_settings")
        if isinstance(module_settings, dict):
            row["moduleSettings"] = module_settings
        out.append(row)
    return out


def _offset_lat_lon(lat: float, lon: float, distance_m: float, bearing_deg: float) -> tuple[float, float]:
    earth_radius_m = 6371000.0
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    bearing = math.radians(bearing_deg)
    angular_distance = distance_m / earth_radius_m
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), math.degrees(lon2))


def _spread_location(index: int, total: int, center_lat: float, center_lon: float, step_m: float) -> tuple[float, float]:
    if total <= 1 or step_m <= 0:
        return center_lat, center_lon
    ring_capacity = 8
    ring = (index // ring_capacity) + 1
    slot = index % ring_capacity
    bearing = (360.0 / ring_capacity) * slot
    radius = step_m * ring
    return _offset_lat_lon(center_lat, center_lon, radius, bearing)


def _run_multi_node_patch_job(job_id: UUID, registry: ProviderRegistry) -> None:
    with SessionLocal() as db:
        jobs = JobsService(db)
        job = db.get(JobModel, job_id)
        if job is None:
            return
        jobs.start(job_id)
        payload = job.payload or {}
        dry_run = bool(payload.get("dry_run", True))
        node_ids_raw = payload.get("node_ids", [])
        base_patch_payload = payload.get("base_patch", {}) or {}
        clone = payload.get("clone", {}) or {}
        location = payload.get("location_spread", {}) or {}
        include_role = bool(clone.get("include_role", False))
        include_favorite = bool(clone.get("include_favorite", False))

        try:
            node_ids = [UUID(node_id) for node_id in node_ids_raw]
            if not node_ids:
                raise ValueError("No nodes selected")

            merged_base_patch = deepcopy(base_patch_payload)

            source_node_id = clone.get("source_node_id")
            if source_node_id:
                source = db.get(ManagedNodeModel, UUID(source_node_id))
                if source is None:
                    raise ValueError("Clone source node not found")
                raw_meta = source.raw_metadata if isinstance(source.raw_metadata, dict) else {}
                if bool(clone.get("include_local_config", False)):
                    source_local = raw_meta.get("preferences", {})
                    if isinstance(source_local, dict):
                        source_local = _sanitize_clone_local_config(source_local, include_role=include_role)
                        merged_base_patch["local_config_patch"] = _deep_merge_dict(
                            source_local,
                            merged_base_patch.get("local_config_patch", {}) if isinstance(merged_base_patch.get("local_config_patch"), dict) else {},
                        )
                if bool(clone.get("include_module_config", False)):
                    source_module = raw_meta.get("modulePreferences", {})
                    if isinstance(source_module, dict):
                        merged_base_patch["module_config_patch"] = _deep_merge_dict(
                            source_module,
                            merged_base_patch.get("module_config_patch", {}) if isinstance(merged_base_patch.get("module_config_patch"), dict) else {},
                        )
                if bool(clone.get("include_channels", False)):
                    source_channels = raw_meta.get("channels", [])
                    if isinstance(source_channels, list):
                        merged_base_patch["channels_patch"] = _merge_channels_patch(
                            _channels_to_patch(source_channels),
                            merged_base_patch.get("channels_patch", []) if isinstance(merged_base_patch.get("channels_patch"), list) else [],
                        )
                if include_role and isinstance(source.role, str) and source.role.strip():
                    merged_base_patch["role"] = source.role.strip()
                if include_favorite:
                    merged_base_patch["favorite"] = bool(source.favorite)

            use_spread = bool(location.get("enabled", False))
            center_lat = location.get("center_latitude")
            center_lon = location.get("center_longitude")
            center_alt = location.get("center_altitude")
            step_m = float(location.get("step_m", 0) or 0)

            ops = OperationsService(db, registry)
            success_count = 0
            fail_count = 0
            for index, node_id in enumerate(node_ids):
                try:
                    node_patch_payload = deepcopy(merged_base_patch)
                    if use_spread and isinstance(center_lat, (int, float)) and isinstance(center_lon, (int, float)):
                        lat, lon = _spread_location(index=index, total=len(node_ids), center_lat=float(center_lat), center_lon=float(center_lon), step_m=step_m)
                        node_patch_payload["latitude"] = lat
                        node_patch_payload["longitude"] = lon
                        if isinstance(center_alt, (int, float)):
                            node_patch_payload["altitude"] = float(center_alt)
                    patch = ConfigPatch(**node_patch_payload)
                    result = ops.apply_patch(
                        node_id=node_id,
                        patch=patch,
                        actor=job.requested_by,
                        source=job.source,
                        dry_run=dry_run,
                    )
                    jobs.add_result(
                        job_id=job_id,
                        status="success",
                        node_id=node_id,
                        message="Batch patch applied",
                        details={"result": result},
                    )
                    success_count += 1
                except Exception as node_exc:
                    db.rollback()
                    jobs.add_result(
                        job_id=job_id,
                        status="failed",
                        node_id=node_id,
                        message=str(node_exc),
                        details={},
                    )
                    fail_count += 1

            jobs.finish(job_id, success=fail_count == 0)
            AuditService(db).log(
                actor=job.requested_by,
                source=job.source,
                action="node.batch_patch.queued",
                metadata={
                    "dry_run": dry_run,
                    "selected_nodes": len(node_ids),
                    "success_count": success_count,
                    "fail_count": fail_count,
                    "clone": clone,
                    "location_spread": location,
                    "base_patch": json.loads(json.dumps(base_patch_payload)),
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as exc:
            db.rollback()
            jobs.add_result(job_id=job_id, status="failed", node_id=None, message=str(exc), details={})
            jobs.finish(job_id, success=False)
