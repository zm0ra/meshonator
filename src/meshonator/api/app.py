from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from meshonator.api.schemas import BatchPatchRequest, ConfigPatchRequest, DiscoveryRequest, GroupCreateRequest
from meshonator.audit.service import AuditService
from meshonator.auth.security import CurrentUser, authenticate_user, bootstrap_admin, require_role, require_session_user
from meshonator.config.settings import get_settings
from meshonator.db.base import Base
from meshonator.db.models import (
    AuditLogModel,
    JobModel,
    JobResultModel,
    ManagedNodeModel,
    NodeGroupModel,
    ProviderEndpointModel,
)
from meshonator.db.session import engine, get_db
from meshonator.discovery.service import DiscoveryService
from meshonator.domain.models import ConfigPatch
from meshonator.groups.service import GroupsService
from meshonator.inventory.service import InventoryService
from meshonator.jobs.service import JobsService
from meshonator.map.service import MapService
from meshonator.jobs.queue import (
    enqueue_discovery_job,
    enqueue_group_patch_job,
    enqueue_multi_node_patch_job,
    enqueue_node_patch_job,
    enqueue_sync_job,
)
from meshonator.operations.service import OperationsService
from meshonator.providers.registry import ProviderRegistry
from meshonator.sync.service import SyncService

try:
    from google.protobuf.descriptor import FieldDescriptor
    from meshtastic.protobuf import channel_pb2, localonly_pb2
except Exception:  # pragma: no cover
    FieldDescriptor = None  # type: ignore[assignment]
    channel_pb2 = None  # type: ignore[assignment]
    localonly_pb2 = None  # type: ignore[assignment]

settings = get_settings()
registry = ProviderRegistry()

app = FastAPI(title="Meshonator", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 8)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="src/meshonator/static"), name="static")
templates = Jinja2Templates(directory="src/meshonator/templates")
templates.env.globals.update(
    app_name=settings.app_name,
    build_version=settings.build_version,
    build_date=settings.build_date,
)


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException):
    accepts = request.headers.get("accept", "")
    wants_html = "text/html" in accepts or "*/*" in accepts
    is_ui_path = not request.url.path.startswith("/api/")
    if exc.status_code == 401 and wants_html and is_ui_path and request.url.path != "/login":
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _parse_multi_value(raw: str) -> list[str]:
    values = []
    for chunk in raw.replace(",", "\n").splitlines():
        item = chunk.strip()
        if item:
            values.append(item)
    return values


def _parse_optional_float(raw: str) -> float | None:
    value = raw.strip()
    if not value:
        return None
    return float(value)


def _parse_json_dict(raw: str) -> dict:
    value = raw.strip()
    if not value:
        return {}
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("Expected JSON object")
    return decoded


def _parse_json_list(raw: str) -> list:
    value = raw.strip()
    if not value:
        return []
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError("Expected JSON array")
    return decoded


def _parse_optional_int(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    return int(value)


def _parse_optional_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if not value or value == "keep":
        return None
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {raw}")


def _parse_optional_text(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    return value


def _parse_uuid_list(values: list[str]) -> list[UUID]:
    out: list[UUID] = []
    for value in values:
        v = value.strip()
        if not v:
            continue
        out.append(UUID(v))
    return out


def _node_label(node: ManagedNodeModel) -> str:
    return node.short_name or node.long_name or node.provider_node_id


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_channels_for_compare(channels: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(channels, list):
        return out
    for position, item in enumerate(channels):
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            index = position
        module_settings = item.get("moduleSettings")
        if not isinstance(module_settings, dict):
            module_settings = item.get("module_settings")
        out[str(index)] = {
            "role": item.get("role"),
            "settings": _safe_dict(item.get("settings")),
            "moduleSettings": _safe_dict(module_settings),
        }
    return out


def _compare_values(source: Any, target: Any, path: str = "") -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    if isinstance(source, dict) and isinstance(target, dict):
        for key in sorted(set(source) | set(target)):
            child_path = f"{path}.{key}" if path else key
            diffs.extend(_compare_values(source.get(key), target.get(key), child_path))
        return diffs
    if isinstance(source, list) and isinstance(target, list):
        if source != target:
            diffs.append({"path": path, "source": source, "target": target})
        return diffs
    if source != target:
        diffs.append({"path": path, "source": source, "target": target})
    return diffs


def _section_for_path(path: str) -> str:
    parts = path.split(".")
    if not parts:
        return "unknown"
    root = parts[0]
    if root in {"local_config", "module_config"} and len(parts) > 1:
        return f"{root}.{parts[1]}"
    if root == "channels" and len(parts) > 1:
        return f"channels.{parts[1]}"
    if root == "location":
        return "location"
    return root


def _is_location_path(path: str) -> bool:
    return path.startswith("location.")


def _build_comparable_snapshot(node: ManagedNodeModel) -> dict[str, Any]:
    raw = node.raw_metadata if isinstance(node.raw_metadata, dict) else {}
    return {
        "local_config": _safe_dict(raw.get("preferences")),
        "module_config": _safe_dict(raw.get("modulePreferences")),
        "channels": _normalize_channels_for_compare(raw.get("channels")),
        "location": {
            "latitude": node.latitude,
            "longitude": node.longitude,
            "altitude": node.altitude,
        },
    }


def _compare_node_configs(
    source_node: ManagedNodeModel,
    target_nodes: list[ManagedNodeModel],
    *,
    ignore_location: bool = True,
) -> list[dict[str, Any]]:
    source_snapshot = _build_comparable_snapshot(source_node)
    results: list[dict[str, Any]] = []
    for target in target_nodes:
        if target.id == source_node.id:
            continue
        if target.provider != source_node.provider:
            results.append(
                {
                    "target": target,
                    "status": "skipped",
                    "reason": "Different provider",
                    "sections": [],
                    "diffs": [],
                    "diff_count": 0,
                }
            )
            continue
        target_snapshot = _build_comparable_snapshot(target)
        diffs = _compare_values(source_snapshot, target_snapshot)
        if ignore_location:
            diffs = [d for d in diffs if not _is_location_path(d["path"])]
        sections = sorted({_section_for_path(d["path"]) for d in diffs})
        results.append(
            {
                "target": target,
                "status": "different" if diffs else "same",
                "reason": "",
                "sections": sections,
                "diffs": diffs,
                "diff_count": len(diffs),
            }
        )
    return results


def _set_nested(dst: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = dst
    for segment in path[:-1]:
        cursor = cursor.setdefault(segment, {})
    cursor[path[-1]] = value


def _merge_patch(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge_patch(out[key], value)
        else:
            out[key] = value
    return out


def _merge_list_patch(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for item in base:
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            continue
        out[item["index"]] = dict(item)
    for item in extra:
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            continue
        idx = item["index"]
        current = out.get(idx, {"index": idx})
        merged = dict(current)
        for key, value in item.items():
            if key in {"settings", "moduleSettings", "module_settings"} and isinstance(value, dict):
                existing = merged.get("moduleSettings" if key in {"moduleSettings", "module_settings"} else "settings", {})
                if not isinstance(existing, dict):
                    existing = {}
                merged["moduleSettings" if key in {"moduleSettings", "module_settings"} else "settings"] = _merge_patch(existing, value)
            else:
                merged[key] = value
        out[idx] = merged
    return [out[i] for i in sorted(out)]


def _build_fleet_baseline_patch(
    *,
    primary_position_precision: str,
    primary_uplink_enabled: str,
    primary_downlink_enabled: str,
    telemetry_device_update_interval: str,
    telemetry_environment_update_interval: str,
    telemetry_air_quality_interval: str,
    telemetry_power_update_interval: str,
    telemetry_health_update_interval: str,
    telemetry_device_enabled: str,
    telemetry_environment_enabled: str,
    telemetry_air_quality_enabled: str,
    telemetry_power_enabled: str,
    telemetry_health_enabled: str,
    mqtt_address: str,
    mqtt_username: str,
    mqtt_password: str,
    mqtt_root: str,
    mqtt_enabled: str,
    mqtt_encryption_enabled: str,
    mqtt_json_enabled: str,
    mqtt_map_reporting_enabled: str,
    mqtt_proxy_to_client_enabled: str,
    mqtt_tls_enabled: str,
    network_rsyslog_server: str,
    network_enabled_protocols: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    local_patch: dict[str, Any] = {}
    module_patch: dict[str, Any] = {}
    channels_patch: list[dict[str, Any]] = []

    network_patch: dict[str, Any] = {}
    if value := _parse_optional_text(network_rsyslog_server):
        network_patch["rsyslogServer"] = value
    value = _parse_optional_int(network_enabled_protocols)
    if value is not None:
        network_patch["enabledProtocols"] = value
    if network_patch:
        local_patch["network"] = network_patch

    telemetry_patch: dict[str, Any] = {}
    telemetry_int_map = {
        "deviceUpdateInterval": telemetry_device_update_interval,
        "environmentUpdateInterval": telemetry_environment_update_interval,
        "airQualityInterval": telemetry_air_quality_interval,
        "powerUpdateInterval": telemetry_power_update_interval,
        "healthUpdateInterval": telemetry_health_update_interval,
    }
    for key, raw in telemetry_int_map.items():
        value = _parse_optional_int(raw)
        if value is not None:
            telemetry_patch[key] = value
    telemetry_bool_map = {
        "deviceTelemetryEnabled": telemetry_device_enabled,
        "environmentMeasurementEnabled": telemetry_environment_enabled,
        "airQualityEnabled": telemetry_air_quality_enabled,
        "powerMeasurementEnabled": telemetry_power_enabled,
        "healthMeasurementEnabled": telemetry_health_enabled,
    }
    for key, raw in telemetry_bool_map.items():
        value = _parse_optional_bool(raw)
        if value is not None:
            telemetry_patch[key] = value
    if telemetry_patch:
        module_patch["telemetry"] = telemetry_patch

    mqtt_patch: dict[str, Any] = {}
    mqtt_text_map = {
        "address": mqtt_address,
        "username": mqtt_username,
        "password": mqtt_password,
        "root": mqtt_root,
    }
    for key, raw in mqtt_text_map.items():
        if value := _parse_optional_text(raw):
            mqtt_patch[key] = value
    mqtt_bool_map = {
        "enabled": mqtt_enabled,
        "encryptionEnabled": mqtt_encryption_enabled,
        "jsonEnabled": mqtt_json_enabled,
        "mapReportingEnabled": mqtt_map_reporting_enabled,
        "proxyToClientEnabled": mqtt_proxy_to_client_enabled,
        "tlsEnabled": mqtt_tls_enabled,
    }
    for key, raw in mqtt_bool_map.items():
        value = _parse_optional_bool(raw)
        if value is not None:
            mqtt_patch[key] = value
    if mqtt_patch:
        module_patch["mqtt"] = mqtt_patch

    channel_settings_patch: dict[str, Any] = {}
    channel_module_patch: dict[str, Any] = {}
    value = _parse_optional_int(primary_position_precision)
    if value is not None:
        channel_module_patch["positionPrecision"] = value
    for key, raw in {
        "uplinkEnabled": primary_uplink_enabled,
        "downlinkEnabled": primary_downlink_enabled,
    }.items():
        value = _parse_optional_bool(raw)
        if value is not None:
            channel_settings_patch[key] = value
    if channel_settings_patch or channel_module_patch:
        row: dict[str, Any] = {"index": 0}
        if channel_settings_patch:
            row["settings"] = channel_settings_patch
        if channel_module_patch:
            row["moduleSettings"] = channel_module_patch
        channels_patch.append(row)

    return local_patch, module_patch, channels_patch


def _normalize_roles(raw: str) -> list[str]:
    roles = []
    for item in _parse_multi_value(raw):
        roles.append(item.strip().upper())
    return [r for r in roles if r]


def _extract_zero_hop_candidates(raw_metadata: dict[str, Any], max_hops: int = 0) -> list[dict[str, Any]]:
    nodes = raw_metadata.get("nodesInMesh", {})
    if not isinstance(nodes, dict):
        return []
    out: list[dict[str, Any]] = []
    for node_id, payload in nodes.items():
        if not isinstance(payload, dict):
            continue
        hops_away = payload.get("hopsAway")
        if not isinstance(hops_away, int):
            continue
        if hops_away > max_hops:
            continue
        user = payload.get("user", {}) if isinstance(payload.get("user"), dict) else {}
        out.append(
            {
                "id": user.get("id") or node_id,
                "short_name": user.get("shortName"),
                "long_name": user.get("longName"),
                "role": user.get("role"),
                "hw_model": user.get("hwModel"),
                "hops_away": hops_away,
                "last_heard": payload.get("lastHeard"),
                "snr": payload.get("snr"),
            }
        )
    out.sort(key=lambda item: (item.get("hops_away", 999), (item.get("short_name") or item.get("id") or "")))
    return out


def _filter_zero_hop_candidates(
    raw_metadata: dict[str, Any],
    *,
    max_hops: int,
    allowed_roles: set[str],
) -> list[dict[str, Any]]:
    candidates = _extract_zero_hop_candidates(raw_metadata, max_hops=max_hops)
    if allowed_roles:
        candidates = [item for item in candidates if str(item.get("role", "")).upper() in allowed_roles]
    return candidates


def _find_managed_nodes_for_candidates(
    *,
    db: Session,
    provider: str,
    candidates: list[dict[str, Any]],
) -> list[ManagedNodeModel]:
    candidate_ids = [item["id"] for item in candidates if isinstance(item.get("id"), str)]
    if not candidate_ids:
        return []
    return list(
        db.scalars(
            select(ManagedNodeModel).where(
                ManagedNodeModel.provider == provider,
                ManagedNodeModel.provider_node_id.in_(candidate_ids),
            )
        ).all()
    )


def _channels_to_patch(channels: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in channels:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            continue
        patch: dict[str, Any] = {"index": index}
        if isinstance(item.get("settings"), dict):
            patch["settings"] = item["settings"]
        if isinstance(item.get("moduleSettings"), dict):
            patch["moduleSettings"] = item["moduleSettings"]
        if isinstance(item.get("module_settings"), dict):
            patch["moduleSettings"] = item["module_settings"]
        if isinstance(item.get("role"), str) and item.get("role"):
            patch["role"] = item["role"]
        out.append(patch)
    return out


def _get_nested(src: dict[str, Any], path: list[str]) -> Any:
    current: Any = src
    for segment in path:
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def _field_kind(field: Any) -> str:
    if FieldDescriptor is None:
        return "string"
    is_repeated = bool(getattr(field, "is_repeated", False))
    if not is_repeated and hasattr(field, "label"):
        is_repeated = field.label == FieldDescriptor.LABEL_REPEATED
    if is_repeated:
        return "json"
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        return "message"
    if field.type == FieldDescriptor.TYPE_ENUM:
        return "enum"
    if field.type == FieldDescriptor.TYPE_BOOL:
        return "bool"
    if field.type in {
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_SINT32,
        FieldDescriptor.TYPE_SINT64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_SFIXED32,
        FieldDescriptor.TYPE_SFIXED64,
    }:
        return "int"
    if field.type in {FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE}:
        return "float"
    if field.type == FieldDescriptor.TYPE_BYTES:
        return "bytes"
    return "string"


def _descriptor_to_rows(descriptor: Any, current: dict[str, Any], prefix: list[str] | None = None) -> list[dict[str, Any]]:
    if prefix is None:
        prefix = []
    rows: list[dict[str, Any]] = []
    for field in descriptor.fields:
        field_name = field.json_name
        path = [*prefix, field_name]
        kind = _field_kind(field)
        value = _get_nested(current, path)
        if kind == "message":
            rows.extend(_descriptor_to_rows(field.message_type, current, path))
            continue
        row: dict[str, Any] = {
            "path": ".".join(path),
            "label": ".".join(path),
            "kind": kind,
            "value": value,
        }
        if kind == "enum":
            row["options"] = [item.name for item in field.enum_type.values]
            if isinstance(value, str):
                row["value"] = value
            elif value is None and row["options"]:
                row["value"] = row["options"][0]
        elif kind == "json":
            row["value"] = json.dumps(value if value is not None else [], ensure_ascii=False)
        rows.append(row)
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        dedup[row["path"]] = row
    return [dedup[key] for key in sorted(dedup.keys())]


def _build_structured_forms(
    current_local_config: dict[str, Any],
    current_module_config: dict[str, Any],
    current_channels: list[dict[str, Any]],
) -> dict[str, Any]:
    if localonly_pb2 is None or channel_pb2 is None:
        return {"supported": False, "local_rows": [], "module_rows": [], "channel_forms": []}

    local_rows = _descriptor_to_rows(localonly_pb2.LocalConfig.DESCRIPTOR, current_local_config or {})
    module_rows = _descriptor_to_rows(localonly_pb2.LocalModuleConfig.DESCRIPTOR, current_module_config or {})
    channel_forms = []
    for channel in current_channels or []:
        if not isinstance(channel, dict):
            continue
        index = channel.get("index")
        if not isinstance(index, int):
            continue
        rows = _descriptor_to_rows(channel_pb2.Channel.DESCRIPTOR, channel, [])
        channel_forms.append({"index": index, "rows": rows})
    channel_forms.sort(key=lambda item: item["index"])
    return {
        "supported": True,
        "local_rows": local_rows,
        "module_rows": module_rows,
        "channel_forms": channel_forms,
    }


def _coerce_structured_value(kind: str, raw: str) -> Any:
    if kind == "bool":
        return raw.strip().lower() in {"true", "1", "yes", "on"}
    if kind == "int":
        return int(raw.strip())
    if kind == "float":
        return float(raw.strip())
    if kind == "json":
        return json.loads(raw.strip() or "[]")
    if kind == "bytes":
        return raw.encode("utf-8")
    return raw


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        bootstrap_admin(
            db,
            username=settings.bootstrap_admin_username,
            password=settings.bootstrap_admin_password,
            role=settings.bootstrap_admin_role,
        )
        JobsService(db).recover_stale_running_jobs(stale_after_minutes=None, include_pending=False)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "providers": [p.health().model_dump() for p in registry.all()],
    }


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = authenticate_user(db, username, password)
    if user is None:
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid credentials"}, status_code=401)
    request.session["user"] = {"username": user.username, "role": user.role}
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_session_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    total_nodes = db.scalar(select(func.count()).select_from(ManagedNodeModel)) or 0
    online_nodes = db.scalar(select(func.count()).select_from(ManagedNodeModel).where(ManagedNodeModel.reachable.is_(True))) or 0
    stale_nodes = db.scalar(select(func.count()).select_from(ManagedNodeModel).where(ManagedNodeModel.reachable.is_(False))) or 0
    pending_jobs = db.scalar(select(func.count()).select_from(JobModel).where(JobModel.status.in_(["pending", "running"]))) or 0
    failed_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    failed_jobs = (
        db.scalar(
            select(func.count())
            .select_from(JobModel)
            .where(JobModel.status == "failed", JobModel.finished_at.is_not(None), JobModel.finished_at >= failed_cutoff)
        )
        or 0
    )
    provider_summary = list(
        db.execute(select(ManagedNodeModel.provider, func.count()).group_by(ManagedNodeModel.provider)).all()
    )
    recent_changes = list(db.scalars(select(AuditLogModel).order_by(AuditLogModel.created_at.desc()).limit(10)).all())
    latest_discovery_job = db.scalar(
        select(JobModel).where(JobModel.job_type == "discovery_scan").order_by(JobModel.created_at.desc()).limit(1)
    )
    latest_discovery_progress = None
    if latest_discovery_job is not None:
        latest_discovery_progress = db.scalar(
            select(JobResultModel)
            .where(JobResultModel.job_id == latest_discovery_job.id)
            .order_by(JobResultModel.created_at.desc())
            .limit(1)
        )
    providers = [provider.name for provider in registry.all()]
    worker = JobsService(db).get_latest_worker_heartbeat() if settings.job_executor_mode == "external" else None
    worker_online = False
    if worker is not None and worker.last_heartbeat_at is not None:
        heartbeat = worker.last_heartbeat_at
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        worker_online = (datetime.now(timezone.utc) - heartbeat).total_seconds() <= 20
    ui_message = request.query_params.get("message")
    ui_error = request.query_params.get("error")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "job_executor_mode": settings.job_executor_mode,
            "total_nodes": total_nodes,
            "online_nodes": online_nodes,
            "stale_nodes": stale_nodes,
            "pending_jobs": pending_jobs,
            "failed_jobs": failed_jobs,
            "provider_summary": provider_summary,
            "recent_changes": recent_changes,
            "latest_discovery_job": latest_discovery_job,
            "latest_discovery_progress": latest_discovery_progress,
            "providers": providers,
            "worker": worker,
            "worker_online": worker_online,
            "ui_message": ui_message,
            "ui_error": ui_error,
        },
    )


@app.post("/ui/discovery/scan")
def ui_discovery_scan(
    provider: str = Form("meshtastic"),
    hosts_text: str = Form(""),
    cidrs_text: str = Form(""),
    endpoints_text: str = Form(""),
    port: int | None = Form(default=None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    hosts = _parse_multi_value(hosts_text)
    cidrs = _parse_multi_value(cidrs_text)
    endpoints = _parse_multi_value(endpoints_text)
    try:
        job = JobsService(db).create(
            job_type="discovery_scan",
            requested_by=user.username,
            source="ui",
            payload={
                "provider": provider,
                "hosts": hosts,
                "cidrs": cidrs,
                "manual_endpoints": endpoints,
                "port": port,
                "auto_sync": True,
                "source": "ui",
            },
        )
        enqueue_discovery_job(job.id, registry)
        message = quote_plus(f"Discovery queued. Job ID: {job.id}")
        return RedirectResponse(url=f"/?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Discovery failed: {exc}")
        return RedirectResponse(url=f"/?error={error}", status_code=303)


@app.get("/ui/discovery/scan")
def ui_discovery_scan_get() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=303)


@app.post("/ui/sync/run")
def ui_sync_run(
    quick: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    job = JobsService(db).create(
        job_type="sync_all",
        requested_by=user.username,
        source="ui",
        payload={"quick": quick},
    )
    enqueue_sync_job(job.id, registry)
    message = quote_plus(f"Sync queued. Job ID: {job.id}")
    return RedirectResponse(url=f"/?message={message}", status_code=303)


@app.get("/ui/sync/run")
def ui_sync_run_get() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=303)


@app.get("/nodes", response_class=HTMLResponse)
def nodes_page(
    request: Request,
    provider: str | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
    ) -> HTMLResponse:
    svc = InventoryService(db)
    nodes = svc.list_nodes(provider=provider)
    return templates.TemplateResponse(
        request,
        "nodes.html",
        {
            "user": user,
            "nodes": nodes,
            "provider": provider,
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


@app.post("/ui/nodes/compare", response_class=HTMLResponse)
def ui_nodes_compare(
    request: Request,
    source_node_id: UUID = Form(...),
    target_node_ids: list[str] = Form(default=[]),
    ignore_location: bool = Form(True),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    svc = InventoryService(db)
    nodes = svc.list_nodes()
    nodes_by_id = {str(node.id): node for node in nodes}
    source_node = nodes_by_id.get(str(source_node_id))
    if source_node is None:
        return templates.TemplateResponse(
            request,
            "nodes.html",
            {
                "user": user,
                "nodes": nodes,
                "provider": None,
                "ui_error": "Source node not found",
                "ui_message": None,
            },
            status_code=400,
        )
    parsed_target_ids = [str(node_id) for node_id in _parse_uuid_list(target_node_ids)]
    targets = [nodes_by_id[node_id] for node_id in parsed_target_ids if node_id in nodes_by_id and node_id != str(source_node_id)]
    if not targets:
        return templates.TemplateResponse(
            request,
            "nodes.html",
            {
                "user": user,
                "nodes": nodes,
                "provider": None,
                "ui_error": "Select at least one target node",
                "ui_message": None,
                "compare_source_id": str(source_node_id),
                "compare_target_ids": parsed_target_ids,
                "compare_ignore_location": ignore_location,
            },
            status_code=400,
        )
    results = _compare_node_configs(source_node, targets, ignore_location=ignore_location)
    compared = len(results)
    different = len([r for r in results if r["status"] == "different"])
    same = len([r for r in results if r["status"] == "same"])
    skipped = len([r for r in results if r["status"] == "skipped"])
    return templates.TemplateResponse(
        request,
        "nodes.html",
        {
            "user": user,
            "nodes": nodes,
            "provider": None,
            "ui_message": f"Compared {compared} target nodes (different: {different}, same: {same}, skipped: {skipped})",
            "ui_error": None,
            "compare_source_id": str(source_node_id),
            "compare_target_ids": [str(t.id) for t in targets],
            "compare_ignore_location": ignore_location,
            "compare_results": results,
        },
    )


@app.post("/ui/nodes/batch-configure")
def ui_nodes_batch_configure(
    selected_node_ids: list[str] = Form(default=[]),
    clone_source_node_id: str = Form(""),
    clone_local_config: bool = Form(False),
    clone_module_config: bool = Form(False),
    clone_channels: bool = Form(False),
    clone_role: bool = Form(False),
    clone_favorite: bool = Form(False),
    local_config_patch_json: str = Form(""),
    module_config_patch_json: str = Form(""),
    channels_patch_json: str = Form(""),
    role_override: str = Form(""),
    favorite_state: str = Form("keep"),
    center_latitude: str = Form(""),
    center_longitude: str = Form(""),
    center_altitude: str = Form(""),
    spread_step_m: str = Form("0"),
    primary_position_precision: str = Form(""),
    primary_uplink_enabled: str = Form("keep"),
    primary_downlink_enabled: str = Form("keep"),
    telemetry_device_update_interval: str = Form(""),
    telemetry_environment_update_interval: str = Form(""),
    telemetry_air_quality_interval: str = Form(""),
    telemetry_power_update_interval: str = Form(""),
    telemetry_health_update_interval: str = Form(""),
    telemetry_device_enabled: str = Form("keep"),
    telemetry_environment_enabled: str = Form("keep"),
    telemetry_air_quality_enabled: str = Form("keep"),
    telemetry_power_enabled: str = Form("keep"),
    telemetry_health_enabled: str = Form("keep"),
    mqtt_address: str = Form(""),
    mqtt_username: str = Form(""),
    mqtt_password: str = Form(""),
    mqtt_root: str = Form(""),
    mqtt_enabled: str = Form("keep"),
    mqtt_encryption_enabled: str = Form("keep"),
    mqtt_json_enabled: str = Form("keep"),
    mqtt_map_reporting_enabled: str = Form("keep"),
    mqtt_proxy_to_client_enabled: str = Form("keep"),
    mqtt_tls_enabled: str = Form("keep"),
    network_rsyslog_server: str = Form(""),
    network_enabled_protocols: str = Form(""),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        node_ids = _parse_uuid_list(selected_node_ids)
        if not node_ids:
            return RedirectResponse(url="/nodes?error=Select+at+least+one+node", status_code=303)

        base_patch: dict[str, Any] = {
            "local_config_patch": _parse_json_dict(local_config_patch_json),
            "module_config_patch": _parse_json_dict(module_config_patch_json),
            "channels_patch": _parse_json_list(channels_patch_json),
        }
        if value := role_override.strip():
            base_patch["role"] = value.upper()
        favorite_value = _parse_optional_bool(favorite_state)
        if favorite_value is not None:
            base_patch["favorite"] = favorite_value
        baseline_local_patch, baseline_module_patch, baseline_channels_patch = _build_fleet_baseline_patch(
            primary_position_precision=primary_position_precision,
            primary_uplink_enabled=primary_uplink_enabled,
            primary_downlink_enabled=primary_downlink_enabled,
            telemetry_device_update_interval=telemetry_device_update_interval,
            telemetry_environment_update_interval=telemetry_environment_update_interval,
            telemetry_air_quality_interval=telemetry_air_quality_interval,
            telemetry_power_update_interval=telemetry_power_update_interval,
            telemetry_health_update_interval=telemetry_health_update_interval,
            telemetry_device_enabled=telemetry_device_enabled,
            telemetry_environment_enabled=telemetry_environment_enabled,
            telemetry_air_quality_enabled=telemetry_air_quality_enabled,
            telemetry_power_enabled=telemetry_power_enabled,
            telemetry_health_enabled=telemetry_health_enabled,
            mqtt_address=mqtt_address,
            mqtt_username=mqtt_username,
            mqtt_password=mqtt_password,
            mqtt_root=mqtt_root,
            mqtt_enabled=mqtt_enabled,
            mqtt_encryption_enabled=mqtt_encryption_enabled,
            mqtt_json_enabled=mqtt_json_enabled,
            mqtt_map_reporting_enabled=mqtt_map_reporting_enabled,
            mqtt_proxy_to_client_enabled=mqtt_proxy_to_client_enabled,
            mqtt_tls_enabled=mqtt_tls_enabled,
            network_rsyslog_server=network_rsyslog_server,
            network_enabled_protocols=network_enabled_protocols,
        )
        base_patch["local_config_patch"] = _merge_patch(base_patch["local_config_patch"], baseline_local_patch)
        base_patch["module_config_patch"] = _merge_patch(base_patch["module_config_patch"], baseline_module_patch)
        base_patch["channels_patch"] = _merge_list_patch(base_patch["channels_patch"], baseline_channels_patch)

        location_enabled = False
        location_payload: dict[str, Any] = {
            "enabled": False,
            "center_latitude": None,
            "center_longitude": None,
            "center_altitude": None,
            "step_m": 0,
        }
        if center_latitude.strip() and center_longitude.strip():
            location_enabled = True
            location_payload["enabled"] = True
            location_payload["center_latitude"] = float(center_latitude.strip())
            location_payload["center_longitude"] = float(center_longitude.strip())
            location_payload["center_altitude"] = _parse_optional_float(center_altitude)
            location_payload["step_m"] = max(0.0, float(spread_step_m.strip() or "0"))

        clone_source = clone_source_node_id.strip()
        clone_payload = {
            "source_node_id": clone_source or None,
            "include_local_config": clone_local_config,
            "include_module_config": clone_module_config,
            "include_channels": clone_channels,
            "include_role": clone_role,
            "include_favorite": clone_favorite,
        }

        if clone_source and not any([clone_local_config, clone_module_config, clone_channels, clone_role, clone_favorite]):
            return RedirectResponse(url="/nodes?error=Select+at+least+one+clone+option", status_code=303)
        if not clone_source:
            clone_payload = {}
        if location_enabled and location_payload["step_m"] <= 0 and len(node_ids) > 1:
            return RedirectResponse(url="/nodes?error=Spread+step+must+be+greater+than+0+for+multiple+nodes", status_code=303)

        job = JobsService(db).create(
            job_type="multi_node_config_patch",
            requested_by=user.username,
            source="ui",
            payload={
                "node_ids": [str(node_id) for node_id in node_ids],
                "dry_run": dry_run,
                "base_patch": base_patch,
                "clone": clone_payload,
                "location_spread": location_payload,
            },
        )
        enqueue_multi_node_patch_job(job.id, registry)
        message = quote_plus(f"Batch node patch queued. Job ID: {job.id}")
        return RedirectResponse(url=f"/nodes?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Batch configure failed: {exc}")
        return RedirectResponse(url=f"/nodes?error={error}", status_code=303)


@app.post("/ui/nodes/favorites/all")
def ui_nodes_favorite_all(
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        nodes = InventoryService(db).list_nodes()
        node_ids = [str(node.id) for node in nodes if node.id is not None]
        if not node_ids:
            return RedirectResponse(url="/nodes?error=No+managed+nodes+found", status_code=303)

        job = JobsService(db).create(
            job_type="multi_node_config_patch",
            requested_by=user.username,
            source="ui",
            payload={
                "node_ids": node_ids,
                "dry_run": dry_run,
                "base_patch": {
                    "favorite": True,
                    "local_config_patch": {},
                    "module_config_patch": {},
                    "channels_patch": [],
                },
                "clone": {},
                "location_spread": {"enabled": False},
            },
        )
        enqueue_multi_node_patch_job(job.id, registry)
        message = quote_plus(f"Queued favorite=true for {len(node_ids)} managed nodes. Job ID: {job.id}")
        return RedirectResponse(url=f"/nodes?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Favorite-all failed: {exc}")
        return RedirectResponse(url=f"/nodes?error={error}", status_code=303)


@app.post("/ui/nodes/compare-sync")
def ui_nodes_compare_sync(
    source_node_id: UUID = Form(...),
    target_node_ids: list[str] = Form(default=[]),
    sync_local_config: bool = Form(True),
    sync_module_config: bool = Form(True),
    sync_channels: bool = Form(True),
    sync_location: bool = Form(False),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        node_ids = _parse_uuid_list(target_node_ids)
        if not node_ids:
            return RedirectResponse(url="/nodes?error=Select+at+least+one+target+node", status_code=303)
        if not any([sync_local_config, sync_module_config, sync_channels, sync_location]):
            return RedirectResponse(url="/nodes?error=Select+at+least+one+section+to+synchronize", status_code=303)
        source_node = db.get(ManagedNodeModel, source_node_id)
        if source_node is None:
            return RedirectResponse(url="/nodes?error=Source+node+not+found", status_code=303)
        base_patch: dict[str, Any] = {
            "local_config_patch": {},
            "module_config_patch": {},
            "channels_patch": [],
        }
        if sync_location:
            if source_node.latitude is None or source_node.longitude is None:
                return RedirectResponse(url="/nodes?error=Source+node+has+no+known+location", status_code=303)
            base_patch["latitude"] = source_node.latitude
            base_patch["longitude"] = source_node.longitude
            if source_node.altitude is not None:
                base_patch["altitude"] = source_node.altitude

        job = JobsService(db).create(
            job_type="multi_node_config_patch",
            requested_by=user.username,
            source="ui",
            payload={
                "node_ids": [str(node_id) for node_id in node_ids],
                "dry_run": dry_run,
                "base_patch": base_patch,
                "clone": {
                    "source_node_id": str(source_node_id),
                    "include_local_config": sync_local_config,
                    "include_module_config": sync_module_config,
                    "include_channels": sync_channels,
                    "include_role": False,
                    "include_favorite": False,
                },
                "location_spread": {"enabled": False},
            },
        )
        enqueue_multi_node_patch_job(job.id, registry)
        message = quote_plus(f"Synchronization queued. Job ID: {job.id}")
        return RedirectResponse(url=f"/nodes?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Synchronization failed: {exc}")
        return RedirectResponse(url=f"/nodes?error={error}", status_code=303)


@app.get("/nodes/{node_id}", response_class=HTMLResponse)
def node_details_page(
    request: Request,
    node_id: UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    node = SyncService(db, registry).node_details(str(node_id))
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    audits = list(db.scalars(select(AuditLogModel).where(AuditLogModel.node_id == node.id).order_by(AuditLogModel.created_at.desc()).limit(20)).all())
    zero_hop_candidates = _extract_zero_hop_candidates(node.raw_metadata or {}, max_hops=0)
    managed_matches = _find_managed_nodes_for_candidates(
        db=db,
        provider=node.provider,
        candidates=zero_hop_candidates,
    )
    managed_by_provider_id = {item.provider_node_id: item for item in managed_matches}
    for candidate in zero_hop_candidates:
        provider_node_id = candidate.get("id")
        managed = managed_by_provider_id.get(provider_node_id) if isinstance(provider_node_id, str) else None
        if managed is None:
            candidate["managed_node_id"] = None
            candidate["managed_short_name"] = None
            candidate["managed_favorite"] = None
        else:
            candidate["managed_node_id"] = str(managed.id)
            candidate["managed_short_name"] = managed.short_name
            candidate["managed_favorite"] = managed.favorite
    current_local_config = node.raw_metadata.get("preferences", {}) if isinstance(node.raw_metadata, dict) else {}
    current_module_config = node.raw_metadata.get("modulePreferences", {}) if isinstance(node.raw_metadata, dict) else {}
    current_channels = node.raw_metadata.get("channels", []) if isinstance(node.raw_metadata, dict) else []
    structured_forms = _build_structured_forms(current_local_config, current_module_config, current_channels)
    return templates.TemplateResponse(
        request,
        "node_detail.html",
        {
            "user": user,
            "node": node,
            "audits": audits,
            "zero_hop_candidates": zero_hop_candidates,
            "current_local_config": current_local_config,
            "current_module_config": current_module_config,
            "current_channels": current_channels,
            "structured_forms": structured_forms,
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


@app.post("/ui/nodes/{node_id}/configure")
def ui_node_configure(
    node_id: UUID,
    short_name: str = Form(""),
    long_name: str = Form(""),
    role: str = Form(""),
    favorite_state: str = Form("keep"),
    latitude: str = Form(""),
    longitude: str = Form(""),
    altitude: str = Form(""),
    local_config_patch_json: str = Form(""),
    module_config_patch_json: str = Form(""),
    channels_patch_json: str = Form(""),
    full_local_config_json: str = Form(""),
    full_module_config_json: str = Form(""),
    full_channels_json: str = Form(""),
    position_broadcast_secs: str = Form(""),
    position_broadcast_smart_enabled: str = Form("keep"),
    gps_update_interval: str = Form(""),
    broadcast_smart_min_distance: str = Form(""),
    broadcast_smart_min_interval_secs: str = Form(""),
    gps_enabled: str = Form("keep"),
    fixed_position: str = Form("keep"),
    position_flags: str = Form(""),
    device_role: str = Form(""),
    lora_hop_limit: str = Form(""),
    lora_tx_power: str = Form(""),
    primary_channel_position_precision: str = Form(""),
    telemetry_device_update_interval: str = Form(""),
    telemetry_device_enabled: str = Form("keep"),
    telemetry_environment_update_interval: str = Form(""),
    telemetry_environment_enabled: str = Form("keep"),
    telemetry_power_update_interval: str = Form(""),
    telemetry_power_enabled: str = Form("keep"),
    telemetry_health_update_interval: str = Form(""),
    telemetry_health_enabled: str = Form("keep"),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        favorite_value = None
        if favorite_state == "true":
            favorite_value = True
        elif favorite_state == "false":
            favorite_value = False
        local_patch = _parse_json_dict(local_config_patch_json)
        module_patch = _parse_json_dict(module_config_patch_json)
        channels_patch = _channels_to_patch(_parse_json_list(channels_patch_json))

        if full_local_config_json.strip():
            local_patch = _merge_patch(_parse_json_dict(full_local_config_json), local_patch)
        if full_module_config_json.strip():
            module_patch = _merge_patch(_parse_json_dict(full_module_config_json), module_patch)
        if full_channels_json.strip():
            channels_patch = _merge_list_patch(_channels_to_patch(_parse_json_list(full_channels_json)), channels_patch)

        value = _parse_optional_int(position_broadcast_secs)
        if value is not None:
            _set_nested(local_patch, ["position", "positionBroadcastSecs"], value)
        value = _parse_optional_bool(position_broadcast_smart_enabled)
        if value is not None:
            _set_nested(local_patch, ["position", "positionBroadcastSmartEnabled"], value)
        value = _parse_optional_int(gps_update_interval)
        if value is not None:
            _set_nested(local_patch, ["position", "gpsUpdateInterval"], value)
        value = _parse_optional_int(broadcast_smart_min_distance)
        if value is not None:
            _set_nested(local_patch, ["position", "broadcastSmartMinimumDistance"], value)
        value = _parse_optional_int(broadcast_smart_min_interval_secs)
        if value is not None:
            _set_nested(local_patch, ["position", "broadcastSmartMinimumIntervalSecs"], value)
        value = _parse_optional_bool(gps_enabled)
        if value is not None:
            _set_nested(local_patch, ["position", "gpsEnabled"], value)
        value = _parse_optional_bool(fixed_position)
        if value is not None:
            _set_nested(local_patch, ["position", "fixedPosition"], value)
        value = _parse_optional_int(position_flags)
        if value is not None:
            _set_nested(local_patch, ["position", "positionFlags"], value)
        if value := device_role.strip():
            _set_nested(local_patch, ["device", "role"], value.upper())
        value = _parse_optional_int(lora_hop_limit)
        if value is not None:
            _set_nested(local_patch, ["lora", "hopLimit"], value)
        value = _parse_optional_int(lora_tx_power)
        if value is not None:
            _set_nested(local_patch, ["lora", "txPower"], value)
        value = _parse_optional_int(primary_channel_position_precision)
        if value is not None:
            channels_patch = _merge_list_patch(
                channels_patch,
                [{"index": 0, "moduleSettings": {"positionPrecision": value}}],
            )
        value = _parse_optional_int(telemetry_device_update_interval)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "deviceUpdateInterval"], value)
        value = _parse_optional_bool(telemetry_device_enabled)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "deviceTelemetryEnabled"], value)
        value = _parse_optional_int(telemetry_environment_update_interval)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "environmentUpdateInterval"], value)
        value = _parse_optional_bool(telemetry_environment_enabled)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "environmentMeasurementEnabled"], value)
        value = _parse_optional_int(telemetry_power_update_interval)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "powerUpdateInterval"], value)
        value = _parse_optional_bool(telemetry_power_enabled)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "powerMeasurementEnabled"], value)
        value = _parse_optional_int(telemetry_health_update_interval)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "healthUpdateInterval"], value)
        value = _parse_optional_bool(telemetry_health_enabled)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "healthMeasurementEnabled"], value)

        patch = ConfigPatch(
            short_name=short_name.strip() or None,
            long_name=long_name.strip() or None,
            role=role.strip() or None,
            favorite=favorite_value,
            latitude=_parse_optional_float(latitude),
            longitude=_parse_optional_float(longitude),
            altitude=_parse_optional_float(altitude),
            local_config_patch=local_patch,
            module_config_patch=module_patch,
            channels_patch=channels_patch,
        )
        job = JobsService(db).create(
            job_type="node_config_patch",
            requested_by=user.username,
            source="ui",
            payload={
                "node_id": str(node_id),
                "dry_run": dry_run,
                "patch": patch.model_dump(),
            },
        )
        enqueue_node_patch_job(job.id, registry)
        message = quote_plus(f"Node patch queued. Job ID: {job.id}")
        return RedirectResponse(url=f"/nodes/{node_id}?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Node patch failed: {exc}")
        return RedirectResponse(url=f"/nodes/{node_id}?error={error}", status_code=303)


@app.post("/ui/nodes/{node_id}/configure-structured")
async def ui_node_configure_structured(
    node_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        form = await request.form()
        paths = list(form.getlist("structured_path"))
        scopes = list(form.getlist("structured_scope"))
        kinds = list(form.getlist("structured_kind"))
        values = list(form.getlist("structured_value"))
        dry_run = str(form.get("dry_run", "")).lower() in {"1", "true", "on", "yes"}

        if not (len(paths) == len(scopes) == len(kinds) == len(values)):
            raise ValueError("Structured form payload is inconsistent")

        local_patch: dict[str, Any] = {}
        module_patch: dict[str, Any] = {}
        channel_patches: dict[int, dict[str, Any]] = {}

        for path, scope, kind, raw_value in zip(paths, scopes, kinds, values):
            path = path.strip()
            scope = scope.strip()
            kind = kind.strip()
            if not path or not scope:
                continue
            value = _coerce_structured_value(kind, raw_value)
            segments = path.split(".")
            if scope == "local":
                _set_nested(local_patch, segments, value)
            elif scope == "module":
                _set_nested(module_patch, segments, value)
            elif scope.startswith("channel:"):
                idx = int(scope.split(":", 1)[1])
                entry = channel_patches.setdefault(idx, {"index": idx})
                _set_nested(entry, segments, value)

        patch = ConfigPatch(
            local_config_patch=local_patch,
            module_config_patch=module_patch,
            channels_patch=[channel_patches[index] for index in sorted(channel_patches.keys())],
        )
        job = JobsService(db).create(
            job_type="node_config_patch",
            requested_by=user.username,
            source="ui",
            payload={
                "node_id": str(node_id),
                "dry_run": dry_run,
                "patch": patch.model_dump(),
            },
        )
        enqueue_node_patch_job(job.id, registry)
        message = quote_plus(f"Structured patch queued. Job ID: {job.id}")
        return RedirectResponse(url=f"/nodes/{node_id}?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Structured patch failed: {exc}")
        return RedirectResponse(url=f"/nodes/{node_id}?error={error}", status_code=303)


@app.post("/ui/nodes/{node_id}/zero-hop/import")
def ui_node_zero_hop_import(
    node_id: UUID,
    group_name: str = Form(""),
    allowed_roles_text: str = Form("ROUTER\nROUTER_LATE\nCLIENT_BASE"),
    max_hops: int = Form(0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        node = db.get(ManagedNodeModel, node_id)
        if node is None:
            return RedirectResponse(url=f"/nodes/{node_id}?error=Node+not+found", status_code=303)
        allowed_roles = set(_normalize_roles(allowed_roles_text))
        candidates = _filter_zero_hop_candidates(
            node.raw_metadata or {},
            max_hops=max_hops,
            allowed_roles=allowed_roles,
        )
        candidate_ids = [item["id"] for item in candidates if isinstance(item.get("id"), str)]
        if not candidate_ids:
            return RedirectResponse(url=f"/nodes/{node_id}?error=No+matching+0-hop+nodes", status_code=303)

        matched_nodes = _find_managed_nodes_for_candidates(
            db=db,
            provider=node.provider,
            candidates=candidates,
        )
        if not matched_nodes:
            return RedirectResponse(
                url=f"/nodes/{node_id}?error=No+matching+managed+nodes+for+selected+0-hop+entries",
                status_code=303,
            )

        service = GroupsService(db)
        effective_group_name = group_name.strip() or f"zero-hop-{node.short_name or node.provider_node_id}"
        group = service.get_group_by_name(effective_group_name)
        if group is None:
            group = service.create_group(
                name=effective_group_name,
                description=f"Imported from {node.provider_node_id} zero-hop view",
                dynamic_filter={},
                desired_config_template={},
            )
        assigned_count = service.assign_nodes(group.id, [item.id for item in matched_nodes])
        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="node.zero_hop.import_to_group",
            provider=node.provider,
            node_id=node.id,
            group_id=group.id,
            metadata={
                "max_hops": max_hops,
                "allowed_roles": sorted(allowed_roles),
                "candidate_count": len(candidate_ids),
                "matched_managed_count": len(matched_nodes),
                "assigned_count": assigned_count,
            },
        )
        message = quote_plus(
            f"Imported {assigned_count} nodes into group {group.name} (matched managed: {len(matched_nodes)} / candidates: {len(candidate_ids)})"
        )
        return RedirectResponse(url=f"/nodes/{node_id}?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"0-hop import failed: {exc}")
        return RedirectResponse(url=f"/nodes/{node_id}?error={error}", status_code=303)


@app.post("/ui/nodes/{node_id}/zero-hop/favorites")
def ui_node_zero_hop_favorites(
    node_id: UUID,
    allowed_roles_text: str = Form("ROUTER\nROUTER_LATE\nCLIENT_BASE"),
    max_hops: int = Form(0),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        node = db.get(ManagedNodeModel, node_id)
        if node is None:
            return RedirectResponse(url=f"/nodes/{node_id}?error=Node+not+found", status_code=303)

        allowed_roles = set(_normalize_roles(allowed_roles_text))
        candidates = _filter_zero_hop_candidates(
            node.raw_metadata or {},
            max_hops=max_hops,
            allowed_roles=allowed_roles,
        )
        matched_nodes = _find_managed_nodes_for_candidates(
            db=db,
            provider=node.provider,
            candidates=candidates,
        )
        if not matched_nodes:
            return RedirectResponse(
                url=f"/nodes/{node_id}?error=No+managed+nodes+matched+the+0-hop+selection",
                status_code=303,
            )

        job = JobsService(db).create(
            job_type="multi_node_config_patch",
            requested_by=user.username,
            source="ui",
            payload={
                "node_ids": [str(item.id) for item in matched_nodes],
                "dry_run": dry_run,
                "base_patch": {
                    "favorite": True,
                    "local_config_patch": {},
                    "module_config_patch": {},
                    "channels_patch": [],
                },
                "clone": {},
                "location_spread": {"enabled": False},
            },
        )
        enqueue_multi_node_patch_job(job.id, registry)

        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="node.zero_hop.favorite_all",
            provider=node.provider,
            node_id=node.id,
            metadata={
                "max_hops": max_hops,
                "allowed_roles": sorted(allowed_roles),
                "candidate_count": len(candidates),
                "matched_managed_count": len(matched_nodes),
                "dry_run": dry_run,
                "job_id": str(job.id),
            },
        )
        message = quote_plus(
            f"Queued favorite sync for {len(matched_nodes)} managed nodes from 0-hop view. Job ID: {job.id}"
        )
        return RedirectResponse(url=f"/nodes/{node_id}?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"0-hop favorite sync failed: {exc}")
        return RedirectResponse(url=f"/nodes/{node_id}?error={error}", status_code=303)


@app.get("/map", response_class=HTMLResponse)
def map_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    markers = MapService(db).markers()
    known_nodes = InventoryService(db).list_nodes()
    mapped_ids = {item["id"] for item in markers}
    without_location = [node for node in known_nodes if str(node.id) not in mapped_ids]
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "user": user,
            "markers": markers,
            "known_nodes_count": len(known_nodes),
            "mapped_nodes_count": len(markers),
            "without_location": without_location,
            "map_default_lat": settings.map_default_lat,
            "map_default_lon": settings.map_default_lon,
            "map_default_zoom": settings.map_default_zoom,
        },
    )


@app.get("/groups", response_class=HTMLResponse)
def groups_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    group_service = GroupsService(db)
    groups = group_service.list_groups()
    groups_view = []
    for group in groups:
        assigned = group_service.list_assigned_members(group.id)
        resolved = group_service.resolve_all_members(group)
        groups_view.append({"group": group, "assigned": assigned, "resolved": resolved})
    nodes = InventoryService(db).list_nodes()
    return templates.TemplateResponse(
        request,
        "groups.html",
        {
            "user": user,
            "groups_view": groups_view,
            "nodes": nodes,
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


@app.post("/ui/groups/create")
def ui_group_create(
    name: str = Form(...),
    description: str = Form(""),
    dynamic_filter_json: str = Form("{}"),
    desired_template_json: str = Form("{}"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        group = GroupsService(db).create_group(
            name=name.strip(),
            description=description.strip() or None,
            dynamic_filter=_parse_json_dict(dynamic_filter_json),
            desired_config_template=_parse_json_dict(desired_template_json),
        )
        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="group.create",
            group_id=group.id,
            after_state={
                "name": group.name,
                "description": group.description,
                "dynamic_filter": group.dynamic_filter,
                "desired_config_template": group.desired_config_template,
            },
        )
        message = quote_plus(f"Group created: {group.name}")
        return RedirectResponse(url=f"/groups?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Group create failed: {exc}")
        return RedirectResponse(url=f"/groups?error={error}", status_code=303)


@app.post("/ui/groups/{group_id}/assign-node")
def ui_group_assign_node(
    group_id: UUID,
    node_id: UUID = Form(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        GroupsService(db).assign_node(group_id=group_id, node_id=node_id)
        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="group.assign_node",
            group_id=group_id,
            node_id=node_id,
        )
        return RedirectResponse(url="/groups?message=Node+assigned", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Assign failed: {exc}")
        return RedirectResponse(url=f"/groups?error={error}", status_code=303)


@app.post("/ui/groups/{group_id}/apply-template")
def ui_group_apply_template(
    group_id: UUID,
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("admin")),
) -> RedirectResponse:
    groups = GroupsService(db)
    group = groups.get_group(group_id)
    if group is None:
        return RedirectResponse(url="/groups?error=Group+not+found", status_code=303)

    job = JobsService(db).create(
        job_type="group_apply_template",
        requested_by=user.username,
        source="ui",
        payload={
            "group_id": str(group_id),
            "dry_run": dry_run,
            "patch": group.desired_config_template or {},
        },
    )
    enqueue_group_patch_job(job.id, registry)
    message = quote_plus(f"Group template queued. Job ID: {job.id}")
    return RedirectResponse(url=f"/groups?message={message}", status_code=303)


@app.post("/ui/groups/{group_id}/configure")
def ui_group_configure(
    group_id: UUID,
    description: str = Form(""),
    dynamic_filter_json: str = Form("{}"),
    desired_template_json: str = Form("{}"),
    template_favorite_state: str = Form("keep"),
    template_position_broadcast_secs: str = Form(""),
    template_position_broadcast_smart_enabled: str = Form("keep"),
    template_gps_update_interval: str = Form(""),
    template_gps_enabled: str = Form("keep"),
    template_device_role: str = Form(""),
    template_lora_hop_limit: str = Form(""),
    template_lora_tx_power: str = Form(""),
    template_telemetry_device_update_interval: str = Form(""),
    template_telemetry_device_enabled: str = Form("keep"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        desired_template = _parse_json_dict(desired_template_json)
        local_patch = desired_template.get("local_config_patch", {}) if isinstance(desired_template.get("local_config_patch"), dict) else {}
        module_patch = desired_template.get("module_config_patch", {}) if isinstance(desired_template.get("module_config_patch"), dict) else {}

        favorite_value = _parse_optional_bool(template_favorite_state)
        if favorite_value is not None:
            desired_template["favorite"] = favorite_value
        value = _parse_optional_int(template_position_broadcast_secs)
        if value is not None:
            _set_nested(local_patch, ["position", "positionBroadcastSecs"], value)
        value = _parse_optional_bool(template_position_broadcast_smart_enabled)
        if value is not None:
            _set_nested(local_patch, ["position", "positionBroadcastSmartEnabled"], value)
        value = _parse_optional_int(template_gps_update_interval)
        if value is not None:
            _set_nested(local_patch, ["position", "gpsUpdateInterval"], value)
        value = _parse_optional_bool(template_gps_enabled)
        if value is not None:
            _set_nested(local_patch, ["position", "gpsEnabled"], value)
        if value := template_device_role.strip():
            _set_nested(local_patch, ["device", "role"], value.upper())
        value = _parse_optional_int(template_lora_hop_limit)
        if value is not None:
            _set_nested(local_patch, ["lora", "hopLimit"], value)
        value = _parse_optional_int(template_lora_tx_power)
        if value is not None:
            _set_nested(local_patch, ["lora", "txPower"], value)
        value = _parse_optional_int(template_telemetry_device_update_interval)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "deviceUpdateInterval"], value)
        value = _parse_optional_bool(template_telemetry_device_enabled)
        if value is not None:
            _set_nested(module_patch, ["telemetry", "deviceTelemetryEnabled"], value)
        if local_patch:
            desired_template["local_config_patch"] = local_patch
        if module_patch:
            desired_template["module_config_patch"] = module_patch

        group = GroupsService(db).update_group(
            group_id,
            description=description.strip() or None,
            dynamic_filter=_parse_json_dict(dynamic_filter_json),
            desired_config_template=desired_template,
        )
        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="group.configure",
            group_id=group.id,
            after_state={
                "description": group.description,
                "dynamic_filter": group.dynamic_filter,
                "desired_config_template": group.desired_config_template,
            },
        )
        message = quote_plus(f"Group updated: {group.name}")
        return RedirectResponse(url=f"/groups?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Group update failed: {exc}")
        return RedirectResponse(url=f"/groups?error={error}", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    jobs_service = JobsService(db)
    jobs = jobs_service.list_jobs()
    results = jobs_service.list_results(limit=600)
    results_by_job: dict[str, list] = {}
    for row in results:
        key = str(row.job_id)
        bucket = results_by_job.setdefault(key, [])
        if len(bucket) < 30:
            bucket.append(row)
    auto_refresh = any(job.status in {"pending", "running"} for job in jobs)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "user": user,
            "jobs": jobs,
            "results_by_job": results_by_job,
            "auto_refresh": auto_refresh,
        },
    )


@app.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    entries = AuditService(db).list_recent(limit=300)
    return templates.TemplateResponse(request, "audit.html", {"user": user, "entries": entries})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    providers = [
        {
            "name": p.name,
            "health": p.health().model_dump(),
            "operation_matrix": p.operation_matrix().model_dump(),
            "capabilities": p.capabilities().model_dump(),
            "experimental": p.experimental,
        }
        for p in registry.all()
    ]
    return templates.TemplateResponse(request, "settings.html", {"user": user, "providers": providers})


@app.post("/api/discovery/scan")
def api_discovery_scan(
    payload: DiscoveryRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    found = DiscoveryService(db, registry).scan(
        provider_name=payload.provider,
        hosts=payload.hosts,
        cidrs=payload.cidrs,
        manual_endpoints=payload.manual_endpoints,
        port=payload.port,
        source="api",
    )
    AuditService(db).log(actor=user.username, source="api", action="discovery.scan", provider=payload.provider, metadata=payload.model_dump())
    return {"count": len(found), "endpoints": [e.endpoint for e in found]}


@app.get("/api/nodes")
def api_nodes(
    provider: str | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> list[dict]:
    rows = InventoryService(db).list_nodes(provider=provider)
    return [
        {
            "id": str(r.id),
            "provider": r.provider,
            "provider_node_id": r.provider_node_id,
            "short_name": r.short_name,
            "long_name": r.long_name,
            "firmware": r.firmware_version,
            "hardware": r.hardware_model,
            "role": r.role,
            "favorite": r.favorite,
            "location": {"lat": r.latitude, "lon": r.longitude, "alt": r.altitude},
            "last_seen": r.last_seen,
            "reachable": r.reachable,
            "capabilities": r.capability_matrix,
        }
        for r in rows
    ]


@app.post("/api/sync")
def api_sync_all(
    quick: bool = False,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    jobs = JobsService(db)
    job = jobs.create(job_type="sync_all", requested_by=user.username, source="api", payload={"quick": quick})
    jobs.start(job.id)
    results = SyncService(db, registry).sync_all(quick=quick)
    success = all(r.get("status") == "success" for r in results)
    jobs.finish(job.id, success=success)
    AuditService(db).log(actor=user.username, source="api", action="sync.all", metadata={"results": results})
    return {"job_id": str(job.id), "results": results}


@app.post("/api/nodes/{node_id}/patch")
def api_patch_node(
    node_id: UUID,
    payload: ConfigPatchRequest,
    dry_run: bool = True,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    patch = ConfigPatch(**payload.model_dump())
    node = db.get(ManagedNodeModel, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    operation_matrix = registry.get(node.provider).operation_matrix()
    if operation_matrix.write_config.value == "unsupported_or_restricted":
        raise HTTPException(status_code=400, detail="Provider does not support remote config write")
    return OperationsService(db, registry).apply_patch(node_id, patch, actor=user.username, source="api", dry_run=dry_run)


@app.post("/api/batch/patch")
def api_batch_patch(
    payload: BatchPatchRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    results = []
    ops = OperationsService(db, registry)
    for node_id in payload.node_ids:
        try:
            result = ops.apply_patch(
                node_id=node_id,
                patch=ConfigPatch(**payload.patch.model_dump()),
                actor=user.username,
                source="api",
                dry_run=payload.dry_run,
            )
            results.append({"node_id": str(node_id), "status": "success", "result": result})
        except Exception as exc:
            results.append({"node_id": str(node_id), "status": "failed", "error": str(exc)})
    return {"results": results}


@app.get("/api/map/markers")
def api_map_markers(
    provider: str | None = None,
    favorite: bool | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> list[dict]:
    return MapService(db).markers(provider=provider, favorite=favorite)


@app.get("/api/providers/capabilities")
def api_provider_capabilities(user: CurrentUser = Depends(require_session_user)) -> dict:
    _ = user
    return {
        p.name: {
            "experimental": p.experimental,
            "capabilities": p.capabilities().model_dump(),
            "operation_matrix": p.operation_matrix().model_dump(),
            "health": p.health().model_dump(),
            "transport": "tcp",
        }
        for p in registry.all()
    }


@app.get("/api/jobs")
def api_jobs(db: Session = Depends(get_db), user: CurrentUser = Depends(require_session_user)) -> list[dict]:
    _ = user
    return [
        {
            "id": str(job.id),
            "job_type": job.job_type,
            "status": job.status,
            "requested_by": job.requested_by,
            "source": job.source,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }
        for job in JobsService(db).list_jobs()
    ]


@app.post("/api/groups")
def api_groups_create(
    payload: GroupCreateRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    group = GroupsService(db).create_group(
        name=payload.name,
        description=payload.description,
        dynamic_filter=payload.dynamic_filter,
        desired_config_template=payload.desired_config_template,
    )
    AuditService(db).log(actor=user.username, source="api", action="group.create", group_id=group.id, after_state=payload.model_dump())
    return {
        "id": str(group.id),
        "name": group.name,
        "description": group.description,
        "dynamic_filter": group.dynamic_filter,
        "desired_config_template": group.desired_config_template,
    }
