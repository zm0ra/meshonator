from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus, urlencode
from uuid import UUID

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from meshonator.api.schemas import BatchPatchRequest, ConfigPatchRequest, DiscoveryRequest, GroupCreateRequest, NodeDbMutationRequest
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
    ProviderModel,
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
    enqueue_bulk_nodedb_job,
    enqueue_discovery_job,
    enqueue_group_patch_job,
    enqueue_multi_node_patch_job,
    enqueue_node_nodedb_job,
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
scheduler = BackgroundScheduler(timezone="UTC")
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


def _scheduled_reachability() -> None:
    with Session(engine) as db:
        SyncService(db, registry).refresh_reachability(
            timeout=settings.scheduler_reachability_timeout_s,
            failures_before_offline=settings.scheduler_reachability_failures_before_offline,
        )


def _scheduled_sync() -> None:
    with Session(engine) as db:
        SyncService(db, registry).sync_all(quick=True)


def _ensure_scheduler_started() -> None:
    if not settings.scheduler_enabled or scheduler.running:
        return
    scheduler.add_job(
        _scheduled_reachability,
        CronTrigger.from_crontab(settings.scheduler_reachability_cron),
        id="periodic_reachability_probe",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_sync,
        CronTrigger.from_crontab(settings.scheduler_sync_cron),
        id="periodic_quick_sync",
        replace_existing=True,
    )
    scheduler.start()


def _humanize_identifier(raw: str) -> str:
    return raw.replace("_", " ").replace(".", " ").strip().title()

JOB_TYPE_LABELS = {
    "discovery_scan": "Discovery sweep",
    "sync_all": "Fleet sync",
    "group_apply_template": "Group rollout",
    "node_config_patch": "Single node change",
    "multi_node_config_patch": "Batch node change",
    "node_nodedb_mutation": "NodeDB change",
    "bulk_nodedb_mutation": "Fleet NodeDB change",
}

AUDIT_ACTION_LABELS = {
    "discovery.scan": "Discovery sweep",
    "sync.all": "Fleet sync",
    "node.batch_patch.queued": "Batch node change queued",
    "group.apply_template": "Group rollout",
    "node.config_patch": "Single node change",
    "node.nodedb_mutation": "NodeDB change",
    "bulk.nodedb_mutation": "Fleet NodeDB change",
}

def _humanize_job_type(job_type: str) -> str:
    return JOB_TYPE_LABELS.get(job_type, _humanize_identifier(job_type))

def _humanize_audit_action(action: str) -> str:
    return AUDIT_ACTION_LABELS.get(action, _humanize_identifier(action))

templates.env.globals.update(
    app_name=settings.app_name,
    build_version=settings.build_version,
    build_date=settings.build_date,
    job_type_label=_humanize_job_type,
    audit_action_label=_humanize_audit_action,
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


def _has_discovery_targets(*collections: list[str]) -> bool:
    return any(item.strip() for collection in collections for item in collection if isinstance(item, str))


def _parse_optional_float(raw: str) -> float | None:
    value = raw.strip()
    if not value:
        return None
    return float(value)


def _parse_json_error(label: str, exc: json.JSONDecodeError) -> ValueError:
    return ValueError(
        f"Invalid JSON in {label} at line {exc.lineno}, column {exc.colno}. Check quotes, commas, and braces."
    )


def _parse_json_dict(raw: str, label: str = "JSON input") -> dict:
    value = raw.strip()
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _parse_json_error(label, exc) from None
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return decoded


def _parse_json_list(raw: str, label: str = "JSON input") -> list:
    value = raw.strip()
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _parse_json_error(label, exc) from None
    if not isinstance(decoded, list):
        raise ValueError(f"{label} must be a JSON array")
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


def _format_dashboard_timestamp(value: datetime | None) -> str:
    if value is None:
        return "Never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _normalize_timestamp(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_exact_timestamp(value: datetime | str | None) -> str:
    normalized = _normalize_timestamp(value)
    if normalized is None:
        return "Never"
    return normalized.strftime("%d %b %Y, %H:%M UTC")


def _format_relative_timestamp(value: datetime | str | None) -> str:
    normalized = _normalize_timestamp(value)
    if normalized is None:
        return "Never"
    delta = datetime.now(timezone.utc) - normalized
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    return _format_exact_timestamp(normalized)


def _format_duration(started_at: datetime | None, finished_at: datetime | None) -> str:
    started = _normalize_timestamp(started_at)
    finished = _normalize_timestamp(finished_at) or datetime.now(timezone.utc)
    if started is None:
        return "-"
    delta = max(0, int((finished - started).total_seconds()))
    if delta < 60:
        return f"{delta}s"
    minutes, seconds = divmod(delta, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _node_identity(node: ManagedNodeModel) -> str:
    short_name = str(node.short_name or "").strip()
    short_suffix = f" ({short_name})" if short_name else ""
    return f"{node.provider}: {node.provider_node_id}{short_suffix}"


def _node_display_name(node: ManagedNodeModel) -> str:
    for candidate in [node.long_name, node.short_name, node.provider_node_id]:
        value = str(candidate or "").strip()
        if value:
            return value
    return "Unnamed node"


def _node_operator_label(node: ManagedNodeModel) -> str:
    identity = _node_identity(node)
    display_name = _node_display_name(node)
    if display_name and display_name not in identity:
        return f"{identity} - {display_name}"
    return identity


def _node_label(node: ManagedNodeModel) -> str:
    return _node_display_name(node)


def _humanize_patch_path(path: str) -> str:
    labels = {
        "favorite": "Pin state",
        "role": "Role override",
        "local_config_patch.device.role": "Node role",
        "local_config_patch.lora.hopLimit": "LoRa hop limit",
        "local_config_patch.lora.txPower": "LoRa TX power",
        "local_config_patch.lora.region": "LoRa region",
        "local_config_patch.lora.modemPreset": "LoRa modem preset",
        "local_config_patch.network.rsyslogServer": "Syslog server",
        "local_config_patch.network.enabledProtocols": "Enabled protocols",
        "local_config_patch.position.positionBroadcastSecs": "Position broadcast interval",
        "local_config_patch.position.positionBroadcastSmartEnabled": "Smart position broadcast",
        "local_config_patch.position.broadcastSmartMinimumDistance": "Smart broadcast minimum distance",
        "local_config_patch.position.broadcastSmartMinimumIntervalSecs": "Smart broadcast minimum interval",
        "local_config_patch.position.gpsMode": "GPS mode",
        "local_config_patch.position.gpsUpdateInterval": "GPS update interval",
        "local_config_patch.position.fixedPosition": "Fixed position",
        "local_config_patch.position.positionFlags": "Position flags",
        "module_config_patch.mqtt.address": "MQTT server",
        "module_config_patch.mqtt.username": "MQTT username",
        "module_config_patch.mqtt.password": "MQTT password",
        "module_config_patch.mqtt.root": "MQTT topic root",
        "module_config_patch.mqtt.enabled": "MQTT enabled",
        "module_config_patch.mqtt.encryptionEnabled": "MQTT encryption",
        "module_config_patch.mqtt.jsonEnabled": "MQTT JSON output",
        "module_config_patch.mqtt.mapReportingEnabled": "MQTT map reporting",
        "module_config_patch.mqtt.proxyToClientEnabled": "MQTT proxy to client",
        "module_config_patch.mqtt.tlsEnabled": "MQTT TLS",
        "module_config_patch.telemetry.deviceUpdateInterval": "Telemetry device interval",
        "module_config_patch.telemetry.environmentUpdateInterval": "Telemetry environment interval",
        "module_config_patch.telemetry.airQualityInterval": "Telemetry air quality interval",
        "module_config_patch.telemetry.powerUpdateInterval": "Telemetry power interval",
        "module_config_patch.telemetry.healthUpdateInterval": "Telemetry health interval",
        "module_config_patch.telemetry.deviceTelemetryEnabled": "Device telemetry",
        "module_config_patch.telemetry.environmentTelemetryEnabled": "Environment telemetry",
        "module_config_patch.telemetry.airQualityEnabled": "Air quality telemetry",
        "module_config_patch.telemetry.powerTelemetryEnabled": "Power telemetry",
        "module_config_patch.telemetry.healthTelemetryEnabled": "Health telemetry",
        "channels_patch": "Channel settings",
        "channels_patch.0.settings.uplinkEnabled": "Primary channel uplink",
        "channels_patch.0.settings.downlinkEnabled": "Primary channel downlink",
        "channels_patch.0.moduleSettings.positionPrecision": "Primary channel position precision",
    }
    if path in labels:
        return labels[path]
    cleaned = path.replace("local_config_patch.", "").replace("module_config_patch.", "").replace("channels_patch.", "")
    return _humanize_identifier(cleaned)


def _collect_patch_paths(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, nested in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_collect_patch_paths(nested, child_prefix))
        return paths
    if isinstance(value, list):
        if not value:
            return []
        paths: list[str] = []
        for index, nested in enumerate(value):
            child_prefix = f"{prefix}.{index}" if prefix else str(index)
            paths.extend(_collect_patch_paths(nested, child_prefix))
        return paths or ([prefix] if prefix else [])
    return [prefix] if prefix else []


def _summarize_patch_fields(base_patch: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for path in _collect_patch_paths(base_patch):
        label = _humanize_patch_path(path)
        if label not in seen:
            seen.append(label)
    return seen


def _extract_job_target_ids(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return []
    for key in ["node_ids", "destination_node_ids"]:
        raw = payload.get(key)
        if isinstance(raw, list):
            return [str(value) for value in raw if str(value).strip()]
    return []


def _extract_job_target_count(payload: dict[str, Any], result_rows: list[JobResultModel]) -> int:
    node_ids = _extract_job_target_ids(payload)
    if node_ids:
        return len(node_ids)
    target_ids = [str(row.node_id) for row in result_rows if row.node_id is not None]
    return len(set(target_ids))


def _summarize_job_results(result_rows: list[JobResultModel]) -> dict[str, int]:
    return {
        "success": len([row for row in result_rows if row.status == "success"]),
        "failed": len([row for row in result_rows if row.status == "failed"]),
        "running": len([row for row in result_rows if row.status in {"pending", "running"}]),
    }


def _build_job_result_summary(job: JobModel, result_rows: list[JobResultModel]) -> str:
    counts = _summarize_job_results(result_rows)
    if counts["failed"]:
        return f"{counts['failed']} failed, {counts['success']} succeeded"
    if counts["running"]:
        return f"{counts['running']} still running"
    if counts["success"]:
        return f"{counts['success']} succeeded"
    if job.status == "failed":
        return "Failed before node-level evidence was recorded"
    if job.status in {"pending", "running"}:
        return "Waiting for execution evidence"
    return "No node-level evidence recorded"


def _build_job_evidence_summary(result_rows: list[JobResultModel]) -> list[str]:
    summaries: list[str] = []
    for row in result_rows[:3]:
        message = str(row.message or "").strip() or "No message"
        if message not in summaries:
            summaries.append(message)
    return summaries


templates.env.globals.update(
    node_identity=_node_identity,
    node_display_name=_node_display_name,
    node_operator_label=_node_operator_label,
    exact_timestamp=_format_exact_timestamp,
    relative_timestamp=_format_relative_timestamp,
)


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
    lora_hop_limit: str = "",
    lora_tx_power: str = "",
    lora_modem_preset: str = "",
    lora_region: str = "",
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

    lora_patch: dict[str, Any] = {}
    if value := _parse_optional_int(lora_hop_limit):
        lora_patch["hopLimit"] = value
    if value := _parse_optional_int(lora_tx_power):
        lora_patch["txPower"] = value
    if value := _parse_optional_text(lora_modem_preset):
        lora_patch["modemPreset"] = value
    if value := _parse_optional_text(lora_region):
        lora_patch["region"] = value
    if lora_patch:
        local_patch["lora"] = lora_patch

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


def _build_meshtastic_default_settings_patch(
    *,
    mqtt_address: str,
    mqtt_username: str,
    mqtt_password: str,
    mqtt_root: str,
    mqtt_enabled: str,
    mqtt_encryption_enabled: str,
    mqtt_json_enabled: str,
    mqtt_map_reporting_enabled: str,
    primary_uplink_enabled: str,
    primary_downlink_enabled: str,
    lora_hop_limit: str,
    lora_tx_power: str,
    lora_modem_preset: str,
    lora_region: str,
    lora_tx_enabled: str,
    lora_use_preset: str,
    lora_config_ok_to_mqtt: str,
    network_rsyslog_server: str,
    network_ntp_server: str,
    telemetry_device_update_interval: str,
    position_broadcast_smart_minimum_distance: str,
    position_broadcast_smart_minimum_interval_secs: str,
    position_fixed_position: str,
    position_gps_mode: str,
    position_gps_update_interval: str,
    position_broadcast_secs: str,
    position_broadcast_smart_enabled: str,
    position_flags: str,
) -> dict[str, Any]:
    local_patch: dict[str, Any] = {}
    module_patch: dict[str, Any] = {}
    channels_patch: list[dict[str, Any]] = []

    mqtt_patch: dict[str, Any] = {}
    for key, raw in {
        "address": mqtt_address,
        "username": mqtt_username,
        "password": mqtt_password,
        "root": mqtt_root,
    }.items():
        if value := _parse_optional_text(raw):
            mqtt_patch[key] = value
    for key, raw in {
        "enabled": mqtt_enabled,
        "encryptionEnabled": mqtt_encryption_enabled,
        "jsonEnabled": mqtt_json_enabled,
        "mapReportingEnabled": mqtt_map_reporting_enabled,
    }.items():
        value = _parse_optional_bool(raw)
        if value is not None:
            mqtt_patch[key] = value
    if mqtt_patch:
        module_patch["mqtt"] = mqtt_patch

    channel_settings_patch: dict[str, Any] = {}
    for key, raw in {
        "uplinkEnabled": primary_uplink_enabled,
        "downlinkEnabled": primary_downlink_enabled,
    }.items():
        value = _parse_optional_bool(raw)
        if value is not None:
            channel_settings_patch[key] = value
    if channel_settings_patch:
        channels_patch.append({"index": 0, "settings": channel_settings_patch})

    lora_patch: dict[str, Any] = {}
    value = _parse_optional_int(lora_hop_limit)
    if value is not None:
        lora_patch["hopLimit"] = value
    value = _parse_optional_int(lora_tx_power)
    if value is not None:
        lora_patch["txPower"] = value
    if value := _parse_optional_text(lora_modem_preset):
        lora_patch["modemPreset"] = value.upper()
    if value := _parse_optional_text(lora_region):
        lora_patch["region"] = value.upper()
    value = _parse_optional_bool(lora_tx_enabled)
    if value is not None:
        lora_patch["txEnabled"] = value
    value = _parse_optional_bool(lora_use_preset)
    if value is not None:
        lora_patch["usePreset"] = value
    value = _parse_optional_bool(lora_config_ok_to_mqtt)
    if value is not None:
        lora_patch["configOkToMqtt"] = value
    if lora_patch:
        local_patch["lora"] = lora_patch

    network_patch: dict[str, Any] = {}
    if value := _parse_optional_text(network_rsyslog_server):
        network_patch["rsyslogServer"] = value
    if value := _parse_optional_text(network_ntp_server):
        network_patch["ntpServer"] = value
    if network_patch:
        local_patch["network"] = network_patch

    telemetry_patch: dict[str, Any] = {}
    value = _parse_optional_int(telemetry_device_update_interval)
    if value is not None:
        telemetry_patch["deviceUpdateInterval"] = value
    if telemetry_patch:
        module_patch["telemetry"] = telemetry_patch

    position_patch: dict[str, Any] = {}
    value = _parse_optional_int(position_broadcast_smart_minimum_distance)
    if value is not None:
        position_patch["broadcastSmartMinimumDistance"] = value
    value = _parse_optional_int(position_broadcast_smart_minimum_interval_secs)
    if value is not None:
        position_patch["broadcastSmartMinimumIntervalSecs"] = value
    value = _parse_optional_bool(position_fixed_position)
    if value is not None:
        position_patch["fixedPosition"] = value
    if value := _parse_optional_text(position_gps_mode):
        position_patch["gpsMode"] = value.upper()
    value = _parse_optional_int(position_gps_update_interval)
    if value is not None:
        position_patch["gpsUpdateInterval"] = value
    value = _parse_optional_int(position_broadcast_secs)
    if value is not None:
        position_patch["positionBroadcastSecs"] = value
    value = _parse_optional_bool(position_broadcast_smart_enabled)
    if value is not None:
        position_patch["positionBroadcastSmartEnabled"] = value
    value = _parse_optional_int(position_flags)
    if value is not None:
        position_patch["positionFlags"] = value
    if position_patch:
        local_patch["position"] = position_patch

    return {
        "local_config_patch": local_patch,
        "module_config_patch": module_patch,
        "channels_patch": channels_patch,
    }


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
                "is_favorite": payload.get("isFavorite") is True,
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


def _collect_routing_favorite_targets(
    *,
    db: Session,
    source_nodes: list[ManagedNodeModel],
    allowed_roles: set[str],
    max_hops: int,
) -> list[str]:
    target_ids: set[str] = set()
    for source in source_nodes:
        candidates = _filter_zero_hop_candidates(
            source.raw_metadata or {},
            max_hops=max_hops,
            allowed_roles=allowed_roles,
        )
        for item in candidates:
            node_id = item.get("id")
            if isinstance(node_id, str) and node_id.strip():
                target_ids.add(node_id.strip())
    return sorted(target_ids)


def _collect_current_routing_favorite_targets(
    *,
    source_nodes: list[ManagedNodeModel],
    allowed_roles: set[str],
) -> list[str]:
    target_ids: set[str] = set()
    for source in source_nodes:
        raw_metadata = source.raw_metadata or {}
        nodes_in_mesh = raw_metadata.get("nodesInMesh", {})
        if not isinstance(nodes_in_mesh, dict):
            continue
        for node_id, payload in nodes_in_mesh.items():
            if not isinstance(payload, dict) or payload.get("isFavorite") is not True:
                continue
            user = payload.get("user", {}) if isinstance(payload.get("user"), dict) else {}
            role = str(user.get("role") or "").upper().strip()
            if allowed_roles and role not in allowed_roles:
                continue
            resolved_id = user.get("id") or node_id
            if isinstance(resolved_id, str) and resolved_id.strip():
                target_ids.add(resolved_id.strip())
    return sorted(target_ids)


def _build_visibility_snapshot(nodes: list[ManagedNodeModel]) -> dict[str, Any]:
    managed_by_provider_id = {
        node.provider_node_id: node for node in nodes if isinstance(node.provider_node_id, str) and node.provider_node_id
    }
    seen_by_source: dict[str, set[str]] = {}
    favorite_by_source: dict[str, set[str]] = {}
    source_rows: list[dict[str, Any]] = []

    for node in sorted(nodes, key=lambda item: ((item.short_name or item.provider_node_id or "").lower(), item.provider_node_id)):
        source_id = node.provider_node_id
        zero_hop_candidates = _extract_zero_hop_candidates(node.raw_metadata or {}, max_hops=0)
        managed_peers: list[dict[str, Any]] = []
        unmanaged_peers: list[dict[str, Any]] = []
        visible_managed_ids: set[str] = set()
        favorite_target_ids: set[str] = set()

        for candidate in zero_hop_candidates:
            target_id = candidate.get("id")
            if not isinstance(target_id, str) or not target_id.strip():
                continue
            target_id = target_id.strip()
            if target_id == source_id:
                continue
            target = managed_by_provider_id.get(target_id)
            is_favorite = candidate.get("is_favorite") is True
            peer_row = {
                "provider_node_id": target_id,
                "short_name": candidate.get("short_name"),
                "long_name": candidate.get("long_name"),
                "role": candidate.get("role"),
                "snr": candidate.get("snr"),
                "last_heard": candidate.get("last_heard"),
                "is_favorite": is_favorite,
                "managed_node_id": str(target.id) if target else None,
                "managed_short_name": target.short_name if target else None,
                "managed_long_name": target.long_name if target else None,
                "managed_reachable": target.reachable if target else None,
                "managed_favorite": target.favorite if target else None,
            }
            if target is None:
                unmanaged_peers.append(peer_row)
                continue
            visible_managed_ids.add(target_id)
            if is_favorite:
                favorite_target_ids.add(target_id)
            managed_peers.append(peer_row)

        seen_by_source[source_id] = visible_managed_ids
        favorite_by_source[source_id] = favorite_target_ids
        source_rows.append(
            {
                "node_id": str(node.id),
                "provider_node_id": source_id,
                "short_name": node.short_name,
                "long_name": node.long_name,
                "reachable": node.reachable,
                "managed_peers": managed_peers,
                "unmanaged_peers": unmanaged_peers,
            }
        )

    source_by_provider_id = {row["provider_node_id"]: row for row in source_rows}
    mutual_pairs: list[dict[str, Any]] = []
    one_way_pairs: list[dict[str, Any]] = []
    seen_mutual_pairs: set[tuple[str, str]] = set()

    for row in source_rows:
        source_id = row["provider_node_id"]
        favorite_targets = favorite_by_source.get(source_id, set())
        mutual_count = 0

        for peer in row["managed_peers"]:
            target_id = peer["provider_node_id"]
            target_visible = seen_by_source.get(target_id, set())
            mutual = source_id in target_visible
            peer["mutual_visibility"] = mutual
            if mutual:
                mutual_count += 1
                pair_key = tuple(sorted((source_id, target_id)))
                if pair_key not in seen_mutual_pairs:
                    seen_mutual_pairs.add(pair_key)
                    counterpart = source_by_provider_id.get(target_id)
                    counterpart_node = managed_by_provider_id.get(target_id)
                    source_node = managed_by_provider_id.get(source_id)
                    mutual_pairs.append(
                        {
                            "left_node_id": str(source_node.id) if source_node else None,
                            "left_short_name": source_node.short_name if source_node else row.get("short_name"),
                            "left_provider_node_id": source_id,
                            "right_node_id": str(counterpart_node.id) if counterpart_node else None,
                            "right_short_name": counterpart_node.short_name if counterpart_node else peer.get("managed_short_name"),
                            "right_provider_node_id": target_id,
                            "left_marks_right_favorite": target_id in favorite_targets,
                            "right_marks_left_favorite": source_id in favorite_by_source.get(target_id, set()),
                            "left_reachable": source_node.reachable if source_node else None,
                            "right_reachable": counterpart_node.reachable if counterpart_node else None,
                            "left_long_name": source_node.long_name if source_node else row.get("long_name"),
                            "right_long_name": counterpart_node.long_name if counterpart_node else counterpart.get("long_name") if counterpart else None,
                        }
                    )
            else:
                source_node = managed_by_provider_id.get(source_id)
                target_node = managed_by_provider_id.get(target_id)
                one_way_pairs.append(
                    {
                        "source_node_id": str(source_node.id) if source_node else None,
                        "source_short_name": source_node.short_name if source_node else row.get("short_name"),
                        "source_provider_node_id": source_id,
                        "target_node_id": str(target_node.id) if target_node else None,
                        "target_short_name": target_node.short_name if target_node else peer.get("managed_short_name"),
                        "target_provider_node_id": target_id,
                        "source_marks_target_favorite": target_id in favorite_targets,
                        "source_reachable": source_node.reachable if source_node else None,
                        "target_reachable": target_node.reachable if target_node else None,
                    }
                )

        managed_peer_count = len(row["managed_peers"])
        favorite_count = sum(1 for peer in row["managed_peers"] if peer["is_favorite"])
        row["managed_peer_count"] = managed_peer_count
        row["favorite_peer_count"] = favorite_count
        row["missing_favorite_count"] = managed_peer_count - favorite_count
        row["unmanaged_peer_count"] = len(row["unmanaged_peers"])
        row["mutual_peer_count"] = mutual_count
        row["favorite_coverage_complete"] = managed_peer_count > 0 and favorite_count == managed_peer_count

    source_rows.sort(key=lambda row: ((row.get("short_name") or row["provider_node_id"]).lower(), row["provider_node_id"]))
    mutual_pairs.sort(
        key=lambda row: (
            (row.get("left_short_name") or row["left_provider_node_id"] or "").lower(),
            (row.get("right_short_name") or row["right_provider_node_id"] or "").lower(),
        )
    )
    one_way_pairs.sort(
        key=lambda row: (
            (row.get("source_short_name") or row["source_provider_node_id"] or "").lower(),
            (row.get("target_short_name") or row["target_provider_node_id"] or "").lower(),
        )
    )

    return {
        "source_rows": source_rows,
        "mutual_pairs": mutual_pairs,
        "one_way_pairs": one_way_pairs,
        "metrics": {
            "nodes_total": len(nodes),
            "nodes_with_zero_hop": sum(1 for row in source_rows if row["managed_peer_count"] or row["unmanaged_peer_count"]),
            "managed_directional_links": sum(row["managed_peer_count"] for row in source_rows),
            "mutual_pairs": len(mutual_pairs),
            "favorite_gaps": sum(row["missing_favorite_count"] for row in source_rows),
            "unmanaged_seen": sum(row["unmanaged_peer_count"] for row in source_rows),
        },
    }


def _queue_bulk_nodedb_mutation_job(
    *,
    db: Session,
    requested_by: str,
    destination_node_ids: list[str],
    target_node_ids: list[str],
    action: str,
    dry_run: bool,
    extra_payload: dict[str, Any] | None = None,
) -> JobModel:
    payload = {
        "destination_node_ids": destination_node_ids,
        "target_node_ids": target_node_ids,
        "action": action,
        "exclude_self": True,
        "dry_run": dry_run,
    }
    if extra_payload:
        payload.update(extra_payload)
    job = JobsService(db).create(
        job_type="bulk_nodedb_mutation",
        requested_by=requested_by,
        source="ui",
        payload=payload,
    )
    enqueue_bulk_nodedb_job(job.id, registry)
    return job


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


ALIGNMENT_LOCAL_PATHS: list[list[str]] = [
    ["lora", "hopLimit"],
    ["device", "tzdef"],
    ["network", "ntpServer"],
    ["lora", "ignoreMqtt"],
    ["lora", "spreadFactor"],
    ["network", "enabledProtocols"],
    ["position", "broadcastSmartMinimumIntervalSecs"],
    ["device", "rebroadcastMode"],
    ["device", "role"],
    ["lora", "bandwidth"],
    ["lora", "codingRate"],
    ["position", "gpsMode"],
    ["position", "gpsUpdateInterval"],
    ["position", "positionBroadcastSecs"],
]

ALIGNMENT_MODULE_OMIT_PATHS: list[list[str]] = [
    ["ambientLighting", "blue"],
    ["ambientLighting", "green"],
]


def _remove_nested_key(dst: dict[str, Any], path: list[str]) -> None:
    if not path:
        return
    cursor: Any = dst
    for segment in path[:-1]:
        if not isinstance(cursor, dict):
            return
        next_cursor = cursor.get(segment)
        if not isinstance(next_cursor, dict):
            return
        cursor = next_cursor
    if isinstance(cursor, dict):
        cursor.pop(path[-1], None)


def _is_sensitive_operator_segment(segment: str) -> bool:
    lowered = segment.strip().lower()
    return lowered in {
        "security",
        "privatekey",
        "publickey",
        "password",
        "psk",
        "primarychannelurl",
        "secret",
        "token",
    } or lowered.endswith("password")


def _is_sensitive_operator_path(path: list[str]) -> bool:
    return any(_is_sensitive_operator_segment(segment) for segment in path)


def _redact_operator_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_operator_segment(key):
                out[key] = "[redacted]"
            else:
                out[key] = _redact_operator_metadata(item)
        return out
    if isinstance(value, list):
        return [_redact_operator_metadata(item) for item in value]
    return value


def _build_alignment_local_patch(raw_metadata: dict[str, Any]) -> dict[str, Any]:
    prefs = raw_metadata.get("preferences", {})
    if not isinstance(prefs, dict):
        return {}
    out: dict[str, Any] = {}
    for path in ALIGNMENT_LOCAL_PATHS:
        value = _get_nested(prefs, path)
        if value is not None:
            _set_nested(out, path, value)
    _remove_nested_key(out, ["security", "privateKey"])
    _remove_nested_key(out, ["security", "publicKey"])
    return out


def _build_alignment_module_patch(raw_metadata: dict[str, Any]) -> dict[str, Any]:
    module = raw_metadata.get("modulePreferences", {})
    if not isinstance(module, dict):
        return {}
    out = json.loads(json.dumps(module))
    for path in ALIGNMENT_MODULE_OMIT_PATHS:
        _remove_nested_key(out, path)
    return out


def _is_alignment_compare_path(path: str) -> bool:
    allowed_exact = {
        "local_config.lora.hopLimit",
        "local_config.device.tzdef",
        "local_config.network.ntpServer",
        "local_config.lora.ignoreMqtt",
        "local_config.lora.spreadFactor",
        "local_config.network.enabledProtocols",
        "local_config.position.broadcastSmartMinimumIntervalSecs",
        "local_config.device.rebroadcastMode",
        "local_config.device.role",
        "local_config.lora.bandwidth",
        "local_config.lora.codingRate",
        "local_config.position.gpsMode",
        "local_config.position.gpsUpdateInterval",
        "local_config.position.positionBroadcastSecs",
        "module_config.telemetry.deviceUpdateInterval",
    }
    if path in allowed_exact:
        return True
    return path.endswith(".settings.uplinkEnabled") or path.endswith(".settings.downlinkEnabled")


def _is_ignored_compare_path(path: str) -> bool:
    return path.startswith("local_config.security.") or path.startswith("module_config.ambientLighting.")


def _is_presence_only_alignment_diff(diff: dict[str, Any]) -> bool:
    path = diff.get("path")
    if not isinstance(path, str) or not _is_alignment_compare_path(path):
        return False
    source = diff.get("source")
    target = diff.get("target")
    return (source is None and target is not None) or (source is not None and target is None)


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
        if _is_sensitive_operator_path(path):
            continue
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
    _ensure_scheduler_started()


@app.on_event("shutdown")
def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


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
    favorite_nodes = db.scalar(select(func.count()).select_from(ManagedNodeModel).where(ManagedNodeModel.favorite.is_(True))) or 0
    mapped_nodes = (
        db.scalar(
            select(func.count())
            .select_from(ManagedNodeModel)
            .where(ManagedNodeModel.latitude.is_not(None), ManagedNodeModel.longitude.is_not(None))
        )
        or 0
    )
    unmapped_nodes = max(total_nodes - mapped_nodes, 0)
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
    providers_health = [provider.health().model_dump() for provider in registry.all()]
    provider_summary = list(db.execute(select(ManagedNodeModel.provider, func.count()).group_by(ManagedNodeModel.provider)).all())
    recent_changes = list(db.scalars(select(AuditLogModel).order_by(AuditLogModel.created_at.desc()).limit(6)).all())
    recent_jobs = list(db.scalars(select(JobModel).order_by(JobModel.created_at.desc()).limit(6)).all())
    latest_discovery_job = db.scalar(
        select(JobModel).where(JobModel.job_type == "discovery_scan").order_by(JobModel.created_at.desc()).limit(1)
    )
    latest_sync_job = db.scalar(select(JobModel).where(JobModel.job_type == "sync_all").order_by(JobModel.created_at.desc()).limit(1))
    latest_successful_sync_job = db.scalar(
        select(JobModel)
        .where(JobModel.job_type == "sync_all", JobModel.status == "success")
        .order_by(JobModel.finished_at.desc(), JobModel.created_at.desc())
        .limit(1)
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
    latest_discovery_at = latest_discovery_job.created_at if latest_discovery_job is not None else None
    latest_sync_at = latest_successful_sync_job.finished_at if latest_successful_sync_job is not None else None

    # Build a compact dashboard view model so the template can stay declarative.
    metrics = [
        {
            "label": "Known Nodes",
            "value": total_nodes,
            "tone": "neutral",
            "meta": f"{len(provider_summary)} provider{'s' if len(provider_summary) != 1 else ''} connected",
            "href": "/nodes",
        },
        {
            "label": "Online",
            "value": online_nodes,
            "tone": "success",
            "meta": "TCP reachable during the latest probe",
            "href": "/nodes",
        },
        {
            "label": "Offline",
            "value": stale_nodes,
            "tone": "danger" if stale_nodes else "neutral",
            "meta": "TCP unreachable during the latest probe",
            "href": "/nodes",
        },
        {
            "label": "Pinned Nodes",
            "value": favorite_nodes,
            "tone": "accent",
            "meta": "Managed favorites across the fleet",
            "href": "/nodes",
        },
        {
            "label": "Pending Jobs",
            "value": pending_jobs,
            "tone": "warning" if pending_jobs else "neutral",
            "meta": "Queued or currently running",
            "href": "/jobs",
        },
        {
            "label": "Failed Jobs (24h)",
            "value": failed_jobs,
            "tone": "danger" if failed_jobs else "neutral",
            "meta": "Recent failures needing review",
            "href": "/jobs",
        },
        {
            "label": "Mapped Nodes",
            "value": mapped_nodes,
            "tone": "neutral",
            "meta": f"{unmapped_nodes} without location",
            "href": "/map",
        },
        {
            "label": "Last Successful Sync",
            "value": _format_dashboard_timestamp(latest_sync_at),
            "tone": "neutral",
            "meta": "Freshness of fleet inventory",
            "href": "/jobs",
        },
    ]

    # Attention items surface operational problems before the secondary activity feeds.
    attention_items: list[dict[str, str | int]] = []
    if stale_nodes:
        attention_items.append(
            {
                "tone": "danger",
                "title": "Offline nodes",
                "count": stale_nodes,
                "description": "Transport reachability dropped for part of the fleet. Review affected radios before pushing more changes.",
                "href": "/nodes",
                "cta": "Review nodes",
            }
        )
    if failed_jobs:
        attention_items.append(
            {
                "tone": "danger",
                "title": "Failed jobs in the last 24 hours",
                "count": failed_jobs,
                "description": "Recent jobs ended with errors and should be checked before the next batch operation.",
                "href": "/jobs",
                "cta": "Open jobs",
            }
        )
    if unmapped_nodes:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Nodes missing location",
                "count": unmapped_nodes,
                "description": "Map coverage is incomplete. Add coordinates to improve operational visibility.",
                "href": "/map",
                "cta": "Open map",
            }
        )
    degraded_providers = [provider for provider in providers_health if provider.get("status") != "ok"]
    if degraded_providers:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Provider health degraded",
                "count": len(degraded_providers),
                "description": "At least one provider is not reporting a healthy status. Verify connectivity and capability checks.",
                "href": "/settings",
                "cta": "Inspect providers",
            }
        )
    if settings.job_executor_mode == "external" and not worker_online:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Worker heartbeat missing",
                "count": 1,
                "description": "The external executor is not reporting a fresh heartbeat, so queued jobs may not be processed.",
                "href": "/jobs",
                "cta": "Check worker",
            }
        )
    if not attention_items:
        attention_items.append(
            {
                "tone": "success",
                "title": "Everything looks healthy",
                "count": total_nodes,
                "description": "No critical fleet issues are currently surfaced by the dashboard.",
                "href": "/nodes",
                "cta": "Browse nodes",
            }
        )
    attention_summary = (
        "No critical fleet issues are currently surfaced by the dashboard."
        if attention_items and attention_items[0].get("tone") == "success"
        else f"{len(attention_items)} attention lane{'s' if len(attention_items) != 1 else ''} need review before the next change."
    )

    worker_status = {
        "state": "online" if worker_online else ("offline" if settings.job_executor_mode == "external" else "disabled"),
        "label": (
            "Worker online"
            if worker_online
            else ("Worker offline" if settings.job_executor_mode == "external" else "Internal executor")
        ),
        "detail": (
            f"Last heartbeat {_format_dashboard_timestamp(worker.last_heartbeat_at)}"
            if worker is not None and worker.last_heartbeat_at is not None
            else (
                "No worker heartbeat reported yet"
                if settings.job_executor_mode == "external"
                else "Jobs are executed inside the application process"
            )
        ),
    }
    ui_message = request.query_params.get("message")
    ui_error = request.query_params.get("error")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "job_executor_mode": settings.job_executor_mode,
            "metrics": metrics,
            "attention_items": attention_items,
            "provider_summary": provider_summary,
            "attention_summary": attention_summary,
            "providers_health": providers_health,
            "recent_changes": recent_changes,
            "recent_jobs": recent_jobs,
            "latest_discovery_job": latest_discovery_job,
            "latest_discovery_progress": latest_discovery_progress,
            "latest_discovery_at": _format_dashboard_timestamp(latest_discovery_at),
            "latest_sync_at": _format_dashboard_timestamp(latest_sync_at),
            "latest_sync_job": latest_sync_job,
            "providers": providers,
            "worker": worker,
            "worker_online": worker_online,
            "worker_status": worker_status,
            "fleet_health": {
                "coverage": f"{mapped_nodes}/{total_nodes}" if total_nodes else "0/0",
                "provider_count": len(providers_health),
                "reachable": online_nodes,
                "total_nodes": total_nodes,
            },
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
    if not _has_discovery_targets(hosts, cidrs, endpoints):
        error = quote_plus("Enter at least one host, CIDR range, or manual endpoint before running discovery")
        return RedirectResponse(url=f"/?error={error}", status_code=303)
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


@app.get("/visibility", response_class=HTMLResponse)
def visibility_page(
    request: Request,
    provider: str | None = "meshtastic",
    q: str = "",
    source_filter: str = "all",
    relation_filter: str = "all",
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    nodes = InventoryService(db).list_nodes(provider=provider)
    snapshot = _build_visibility_snapshot(nodes)
    query = q.strip().lower()
    allowed_source_filters = {"all", "with_gaps", "complete", "with_mutual"}
    allowed_relation_filters = {"all", "mutual", "one_way", "favorite_only", "missing_favorite"}
    source_filter = source_filter if source_filter in allowed_source_filters else "all"
    relation_filter = relation_filter if relation_filter in allowed_relation_filters else "all"

    def _peer_matches_query(peer: dict[str, Any]) -> bool:
        if not query:
            return True
        values = [
            str(peer.get("provider_node_id") or "").lower(),
            str(peer.get("short_name") or "").lower(),
            str(peer.get("long_name") or "").lower(),
            str(peer.get("managed_short_name") or "").lower(),
            str(peer.get("managed_long_name") or "").lower(),
            str(peer.get("role") or "").lower(),
        ]
        return any(query in value for value in values)

    def _managed_peer_matches_relation(peer: dict[str, Any]) -> bool:
        if relation_filter == "mutual":
            return peer.get("mutual_visibility") is True
        if relation_filter == "one_way":
            return peer.get("mutual_visibility") is not True
        if relation_filter == "favorite_only":
            return peer.get("is_favorite") is True
        if relation_filter == "missing_favorite":
            return peer.get("is_favorite") is not True
        return True

    filtered_source_rows: list[dict[str, Any]] = []
    for source_row in snapshot["source_rows"]:
        row = copy.deepcopy(source_row)
        source_matches_query = not query or any(
            query in str(value or "").lower()
            for value in [row.get("provider_node_id"), row.get("short_name"), row.get("long_name")]
        )
        original_managed_peers = row["managed_peers"]
        original_unmanaged_peers = row["unmanaged_peers"]
        row["managed_peers"] = [
            peer
            for peer in original_managed_peers
            if _managed_peer_matches_relation(peer) and (source_matches_query or _peer_matches_query(peer))
        ]
        row["unmanaged_peers"] = [
            peer for peer in original_unmanaged_peers if source_matches_query or _peer_matches_query(peer)
        ]
        row["managed_peer_count"] = len(row["managed_peers"])
        row["favorite_peer_count"] = sum(1 for peer in row["managed_peers"] if peer.get("is_favorite") is True)
        row["missing_favorite_count"] = row["managed_peer_count"] - row["favorite_peer_count"]
        row["unmanaged_peer_count"] = len(row["unmanaged_peers"])
        row["mutual_peer_count"] = sum(1 for peer in row["managed_peers"] if peer.get("mutual_visibility") is True)
        row["favorite_coverage_complete"] = row["managed_peer_count"] > 0 and row["favorite_peer_count"] == row["managed_peer_count"]

        if source_filter == "with_gaps" and row["missing_favorite_count"] == 0:
            continue
        if source_filter == "complete" and not row["favorite_coverage_complete"]:
            continue
        if source_filter == "with_mutual" and row["mutual_peer_count"] == 0:
            continue
        if query and not source_matches_query and not row["managed_peers"] and not row["unmanaged_peers"]:
            continue
        filtered_source_rows.append(row)

    visible_source_ids = {row["provider_node_id"] for row in filtered_source_rows}

    def _pair_matches_query(values: list[str | None]) -> bool:
        if not query:
            return True
        return any(query in str(value or "").lower() for value in values)

    filtered_mutual_pairs = [
        pair
        for pair in snapshot["mutual_pairs"]
        if (not visible_source_ids or pair.get("left_provider_node_id") in visible_source_ids or pair.get("right_provider_node_id") in visible_source_ids)
        and _pair_matches_query(
            [
                pair.get("left_provider_node_id"),
                pair.get("left_short_name"),
                pair.get("left_long_name"),
                pair.get("right_provider_node_id"),
                pair.get("right_short_name"),
                pair.get("right_long_name"),
            ]
        )
        and (
            relation_filter in {"all", "mutual"}
            or (relation_filter == "favorite_only" and (pair.get("left_marks_right_favorite") or pair.get("right_marks_left_favorite")))
            or (relation_filter == "missing_favorite" and not (pair.get("left_marks_right_favorite") and pair.get("right_marks_left_favorite")))
        )
    ]
    filtered_one_way_pairs = [
        pair
        for pair in snapshot["one_way_pairs"]
        if pair.get("source_provider_node_id") in visible_source_ids
        and _pair_matches_query(
            [
                pair.get("source_provider_node_id"),
                pair.get("source_short_name"),
                pair.get("target_provider_node_id"),
                pair.get("target_short_name"),
            ]
        )
        and (
            relation_filter in {"all", "one_way"}
            or (relation_filter == "favorite_only" and pair.get("source_marks_target_favorite"))
            or (relation_filter == "missing_favorite" and not pair.get("source_marks_target_favorite"))
        )
    ]

    filtered_metrics = {
        "nodes_total": len(filtered_source_rows),
        "nodes_with_zero_hop": sum(
            1 for row in filtered_source_rows if row["managed_peer_count"] or row["unmanaged_peer_count"]
        ),
        "managed_directional_links": sum(row["managed_peer_count"] for row in filtered_source_rows),
        "mutual_pairs": len(filtered_mutual_pairs),
        "favorite_gaps": sum(row["missing_favorite_count"] for row in filtered_source_rows),
        "unmanaged_seen": sum(row["unmanaged_peer_count"] for row in filtered_source_rows),
    }
    return templates.TemplateResponse(
        request,
        "visibility.html",
        {
            "user": user,
            "provider": provider,
            "query": q,
            "source_filter": source_filter,
            "relation_filter": relation_filter,
            "source_rows": filtered_source_rows,
            "mutual_pairs": filtered_mutual_pairs,
            "one_way_pairs": filtered_one_way_pairs,
            "metrics": filtered_metrics,
            "fleet_metrics": snapshot["metrics"],
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


@app.get("/nodes", response_class=HTMLResponse)
def nodes_page(
    request: Request,
    provider: str | None = None,
    q: str = "",
    status: str = "all",
    favorites: str = "all",
    location: str = "all",
    role: str = "",
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
    ) -> HTMLResponse:
    svc = InventoryService(db)
    all_nodes = svc.list_nodes(provider=provider)
    query = q.strip().lower()
    normalized_role = role.strip().upper()
    nodes: list[ManagedNodeModel] = []
    for node in all_nodes:
        endpoint_hosts = [endpoint.host.lower() for endpoint in node.endpoints if endpoint.host]
        haystack = [
            str(node.provider or "").lower(),
            str(node.provider_node_id or "").lower(),
            str(node.short_name or "").lower(),
            str(node.long_name or "").lower(),
            str(node.hardware_model or "").lower(),
        ]
        if query and not any(query in value for value in haystack + endpoint_hosts):
            continue
        if status == "online" and not node.reachable:
            continue
        if status == "offline" and node.reachable:
            continue
        if favorites == "only" and not node.favorite:
            continue
        if favorites == "exclude" and node.favorite:
            continue
        has_location = node.latitude is not None and node.longitude is not None
        if location == "mapped" and not has_location:
            continue
        if location == "missing" and has_location:
            continue
        if normalized_role and str(node.role or "").upper() != normalized_role:
            continue
        nodes.append(node)
    selected_node_ids = [value for value in request.query_params.getlist("selected_node_ids") if value]
    nodes_by_id = {str(node.id): node for node in all_nodes}
    selected_nodes = [nodes_by_id[node_id] for node_id in selected_node_ids if node_id in nodes_by_id]
    return templates.TemplateResponse(
        request,
        "nodes.html",
        {
            "user": user,
            "nodes": nodes,
            "all_nodes": all_nodes,
            "selected_nodes": selected_nodes,
            "selected_node_ids": selected_node_ids,
            "provider": provider,
            "providers": sorted({node.provider for node in svc.list_nodes()}),
            "roles": sorted({str(node.role).upper() for node in all_nodes if node.role}),
            "filters": {
                "q": q,
                "status": status,
                "favorites": favorites,
                "location": location,
                "role": role,
            },
            "counts": {
                "online": len([node for node in nodes if node.reachable]),
                "offline": len([node for node in nodes if not node.reachable]),
                "mapped": len([node for node in nodes if node.latitude is not None and node.longitude is not None]),
                "selected": len(selected_nodes),
            },
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


@app.get("/nodes/batch-configure", response_class=HTMLResponse)
def nodes_batch_configure_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    svc = InventoryService(db)
    all_nodes = svc.list_nodes()
    nodes_by_id = {str(node.id): node for node in all_nodes}
    selected_node_ids = [value for value in request.query_params.getlist("selected_node_ids") if value]
    selected_nodes = [nodes_by_id[node_id] for node_id in selected_node_ids if node_id in nodes_by_id]
    if not selected_nodes:
        error = quote_plus("Select at least one node before opening batch configure")
        return RedirectResponse(url=f"/nodes?error={error}", status_code=303)
    return templates.TemplateResponse(
        request,
        "nodes_batch_configure.html",
        {
            "user": user,
            "all_nodes": all_nodes,
            "selected_nodes": selected_nodes,
            "selected_node_ids": selected_node_ids,
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


@app.post("/ui/nodes/compare", response_class=HTMLResponse)
def ui_nodes_compare(
    request: Request,
    source_node_id: UUID = Form(...),
    target_node_ids: list[str] = Form(default=[]),
    ignore_location: bool = Form(False),
    use_alignment_profile: bool = Form(False),
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
                "compare_use_alignment_profile": use_alignment_profile,
            },
            status_code=400,
        )
    results = _compare_node_configs(source_node, targets, ignore_location=ignore_location)
    for row in results:
        diffs = row.get("diffs", [])
        if not isinstance(diffs, list):
            continue
        filtered: list[dict[str, Any]] = []
        for item in diffs:
            path = item.get("path", "")
            if not isinstance(path, str):
                continue
            if _is_ignored_compare_path(path):
                continue
            if use_alignment_profile and not _is_alignment_compare_path(path):
                continue
            if use_alignment_profile and _is_presence_only_alignment_diff(item):
                continue
            filtered.append(item)
        row["diffs"] = filtered
        row["diff_count"] = len(filtered)
        row["sections"] = sorted({_section_for_path(d["path"]) for d in filtered})
        if row["status"] != "skipped":
            row["status"] = "different" if filtered else "same"

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
            "compare_use_alignment_profile": use_alignment_profile,
            "compare_results": results,
        },
    )


@app.post("/ui/nodes/batch-configure")
def ui_nodes_batch_configure(
    selected_node_ids: list[str] = Form(default=[]),
    confirm_batch_changes: bool = Form(False),
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
    lora_hop_limit: str = Form(""),
    lora_tx_power: str = Form(""),
    lora_modem_preset: str = Form(""),
    lora_region: str = Form(""),
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
    nodedb_action: str = Form(""),
    nodedb_allowed_roles_text: str = Form("ROUTER\nROUTER_LATE\nCLIENT_BASE"),
    nodedb_source_max_hops: int = Form(0),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        node_ids = _parse_uuid_list(selected_node_ids)
        if not node_ids:
            return RedirectResponse(url="/nodes?error=Select+at+least+one+node", status_code=303)
        all_nodes = InventoryService(db).list_nodes()
        nodes_by_id = {str(node.id): node for node in all_nodes if node.id is not None}
        selected_nodes = [nodes_by_id[str(node_id)] for node_id in node_ids if str(node_id) in nodes_by_id]
        if not confirm_batch_changes:
            return RedirectResponse(
                url="/nodes?error=Review+the+preflight+summary+and+confirm+the+batch+change+before+queueing",
                status_code=303,
            )

        base_patch: dict[str, Any] = {
            "local_config_patch": _parse_json_dict(local_config_patch_json, "Local config patch"),
            "module_config_patch": _parse_json_dict(module_config_patch_json, "Module config patch"),
            "channels_patch": _parse_json_list(channels_patch_json, "Channels patch"),
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
            lora_hop_limit=lora_hop_limit,
            lora_tx_power=lora_tx_power,
            lora_modem_preset=lora_modem_preset,
            lora_region=lora_region,
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

        has_patch_changes = any(
            [
                base_patch.get("local_config_patch"),
                base_patch.get("module_config_patch"),
                base_patch.get("channels_patch"),
                "role" in base_patch,
                "favorite" in base_patch,
            ]
        )
        has_clone_changes = bool(clone_payload)
        has_nodedb_changes = bool(nodedb_action.strip())
        if has_nodedb_changes and (has_patch_changes or has_clone_changes or location_enabled):
            return RedirectResponse(
                url="/nodes?error=NodeDB+mutation+must+be+queued+on+its+own+review+path",
                status_code=303,
            )
        if not any([has_patch_changes, has_clone_changes, location_enabled, has_nodedb_changes]):
            return RedirectResponse(url="/nodes?error=Enable+at+least+one+change+section+before+queueing", status_code=303)

        if has_nodedb_changes:
            if nodedb_action not in {"set_favorite", "remove_favorite", "remove_node"}:
                return RedirectResponse(url="/nodes?error=Unsupported+NodeDB+action", status_code=303)
            allowed_roles = set(_normalize_roles(nodedb_allowed_roles_text))
            source_nodes = [
                node for node in selected_nodes
                if str(node.role or "").upper().strip() in allowed_roles
            ]
            target_node_ids = _collect_routing_favorite_targets(
                db=db,
                source_nodes=source_nodes,
                allowed_roles=allowed_roles,
                max_hops=nodedb_source_max_hops,
            )
            if not target_node_ids:
                return RedirectResponse(url="/nodes?error=No+NodeDB+targets+matched+the+reviewed+selection", status_code=303)
            job = JobsService(db).create(
                job_type="bulk_nodedb_mutation",
                requested_by=user.username,
                source="ui",
                payload={
                    "destination_node_ids": [str(node.id) for node in selected_nodes if node.id is not None],
                    "target_node_ids": target_node_ids,
                    "action": nodedb_action,
                    "exclude_self": True,
                    "dry_run": dry_run,
                },
            )
            enqueue_bulk_nodedb_job(job.id, registry)
            message = quote_plus(f"Batch NodeDB change queued. Job ID: {job.id}")
            return RedirectResponse(url=f"/nodes?message={message}", status_code=303)

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


@app.post("/ui/nodes/nodedb/routing-favorites")
def ui_nodes_nodedb_routing_favorites(
    action: str = Form("set_favorite"),
    allowed_roles_text: str = Form("ROUTER\nROUTER_LATE\nCLIENT_BASE"),
    source_max_hops: int = Form(0),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        if action not in {"set_favorite", "remove_favorite", "remove_node"}:
            return RedirectResponse(url="/nodes?error=Unsupported+NodeDB+action", status_code=303)
        all_nodes = InventoryService(db).list_nodes()
        if not all_nodes:
            return RedirectResponse(url="/nodes?error=No+managed+nodes+found", status_code=303)
        allowed_roles = set(_normalize_roles(allowed_roles_text))
        source_nodes = [
            n for n in all_nodes
            if str(n.role or "").upper().strip() in allowed_roles
        ]
        target_node_ids = _collect_routing_favorite_targets(
            db=db,
            source_nodes=source_nodes,
            allowed_roles=allowed_roles,
            max_hops=source_max_hops,
        )
        if not target_node_ids:
            return RedirectResponse(url="/nodes?error=No+routing+targets+matched+criteria", status_code=303)
        job = _queue_bulk_nodedb_mutation_job(
            db=db,
            requested_by=user.username,
            destination_node_ids=[str(n.id) for n in all_nodes if n.id is not None],
            target_node_ids=target_node_ids,
            action=action,
            dry_run=dry_run,
        )
        message = quote_plus(
            f"Queued bulk NodeDB {action} for {len(all_nodes)} nodes (targets: {len(target_node_ids)}). Job ID: {job.id}"
        )
        return RedirectResponse(url=f"/nodes?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Bulk NodeDB routing favorites failed: {exc}")
        return RedirectResponse(url=f"/nodes?error={error}", status_code=303)


@app.post("/ui/nodes/nodedb/routing-favorites/refresh")
def ui_nodes_nodedb_routing_favorites_refresh(
    allowed_roles_text: str = Form("ROUTER\nROUTER_LATE\nCLIENT_BASE"),
    source_max_hops: int = Form(0),
    remove_unseen: bool = Form(False),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        all_nodes = InventoryService(db).list_nodes()
        if not all_nodes:
            return RedirectResponse(url="/nodes?error=No+managed+nodes+found", status_code=303)

        allowed_roles = set(_normalize_roles(allowed_roles_text))
        destination_node_ids = [str(node.id) for node in all_nodes if node.id is not None]
        seen_target_ids = _collect_routing_favorite_targets(
            db=db,
            source_nodes=all_nodes,
            allowed_roles=allowed_roles,
            max_hops=source_max_hops,
        )
        current_favorite_ids = _collect_current_routing_favorite_targets(
            source_nodes=all_nodes,
            allowed_roles=allowed_roles,
        )
        stale_target_ids = sorted(set(current_favorite_ids) - set(seen_target_ids)) if remove_unseen else []

        created_jobs: list[JobModel] = []
        if seen_target_ids:
            created_jobs.append(
                _queue_bulk_nodedb_mutation_job(
                    db=db,
                    requested_by=user.username,
                    destination_node_ids=destination_node_ids,
                    target_node_ids=seen_target_ids,
                    action="set_favorite",
                    dry_run=dry_run,
                    extra_payload={"refresh_mode": True, "source_max_hops": source_max_hops},
                )
            )
        if stale_target_ids:
            created_jobs.append(
                _queue_bulk_nodedb_mutation_job(
                    db=db,
                    requested_by=user.username,
                    destination_node_ids=destination_node_ids,
                    target_node_ids=stale_target_ids,
                    action="remove_favorite",
                    dry_run=dry_run,
                    extra_payload={"refresh_mode": True, "source_max_hops": source_max_hops},
                )
            )

        if not created_jobs:
            return RedirectResponse(
                url="/nodes?error=No+0-hop+favorite+refresh+changes+detected",
                status_code=303,
            )

        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="node.nodedb.favorite_refresh.queue",
            metadata={
                "source_max_hops": source_max_hops,
                "allowed_roles": sorted(allowed_roles),
                "remove_unseen": remove_unseen,
                "seen_target_count": len(seen_target_ids),
                "stale_target_count": len(stale_target_ids),
                "dry_run": dry_run,
                "job_ids": [str(job.id) for job in created_jobs],
            },
        )

        message = f"Queued favorite refresh for {len(seen_target_ids)} current 0-hop targets"
        if stale_target_ids:
            message += f" and remove_favorite for {len(stale_target_ids)} stale targets"
        message += ". Job IDs: " + ", ".join(str(job.id) for job in created_jobs)
        return RedirectResponse(url=f"/nodes?message={quote_plus(message)}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Favorite refresh failed: {exc}")
        return RedirectResponse(url=f"/nodes?error={error}", status_code=303)


@app.post("/ui/nodes/compare-sync")
def ui_nodes_compare_sync(
    source_node_id: UUID = Form(...),
    target_node_ids: list[str] = Form(default=[]),
    sync_local_config: bool = Form(True),
    sync_module_config: bool = Form(True),
    sync_channels: bool = Form(True),
    sync_location: bool = Form(False),
    use_alignment_profile: bool = Form(False),
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
        source_raw = source_node.raw_metadata if isinstance(source_node.raw_metadata, dict) else {}
        if use_alignment_profile:
            if sync_local_config:
                base_patch["local_config_patch"] = _build_alignment_local_patch(source_raw)
            if sync_module_config:
                base_patch["module_config_patch"] = _build_alignment_module_patch(source_raw)
        if sync_location:
            if source_node.latitude is None or source_node.longitude is None:
                return RedirectResponse(url="/nodes?error=Source+node+has+no+known+location", status_code=303)
            base_patch["latitude"] = source_node.latitude
            base_patch["longitude"] = source_node.longitude
            if source_node.altitude is not None:
                base_patch["altitude"] = source_node.altitude

        clone_payload = {
            "source_node_id": str(source_node_id),
            "include_local_config": sync_local_config and not use_alignment_profile,
            "include_module_config": sync_module_config and not use_alignment_profile,
            "include_channels": sync_channels,
            "include_role": False,
            "include_favorite": False,
        }
        job = JobsService(db).create(
            job_type="multi_node_config_patch",
            requested_by=user.username,
            source="ui",
            payload={
                "node_ids": [str(node_id) for node_id in node_ids],
                "dry_run": dry_run,
                "base_patch": base_patch,
                "clone": clone_payload,
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
    redacted_raw_metadata = _redact_operator_metadata(node.raw_metadata or {})
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
            "redacted_raw_metadata": redacted_raw_metadata,
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
        local_patch = _parse_json_dict(local_config_patch_json, "Local config patch")
        module_patch = _parse_json_dict(module_config_patch_json, "Module config patch")
        channels_patch = _channels_to_patch(_parse_json_list(channels_patch_json, "Channels patch"))

        if full_local_config_json.strip():
            local_patch = _merge_patch(_parse_json_dict(full_local_config_json, "Full localConfig"), local_patch)
        if full_module_config_json.strip():
            module_patch = _merge_patch(_parse_json_dict(full_module_config_json, "Full moduleConfig"), module_patch)
        if full_channels_json.strip():
            channels_patch = _merge_list_patch(
                _channels_to_patch(_parse_json_list(full_channels_json, "Full channels")),
                channels_patch,
            )

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


@app.post("/ui/nodes/{node_id}/zero-hop/nodedb")
def ui_node_zero_hop_nodedb(
    node_id: UUID,
    action: str = Form("set_favorite"),
    allowed_roles_text: str = Form("ROUTER\nROUTER_LATE\nCLIENT_BASE"),
    max_hops: int = Form(0),
    selected_candidate_ids_text: str = Form(""),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        node = db.get(ManagedNodeModel, node_id)
        if node is None:
            return RedirectResponse(url=f"/nodes/{node_id}?error=Node+not+found", status_code=303)
        if action not in {"set_favorite", "remove_favorite", "remove_node"}:
            return RedirectResponse(url=f"/nodes/{node_id}?error=Unsupported+NodeDB+action", status_code=303)

        allowed_roles = set(_normalize_roles(allowed_roles_text))
        candidates = _filter_zero_hop_candidates(
            node.raw_metadata or {},
            max_hops=max_hops,
            allowed_roles=allowed_roles,
        )
        candidate_ids = [item["id"] for item in candidates if isinstance(item.get("id"), str)]
        if not candidate_ids:
            return RedirectResponse(url=f"/nodes/{node_id}?error=No+matching+0-hop+nodes", status_code=303)

        selected = _parse_multi_value(selected_candidate_ids_text)
        selected_set = set(selected)
        targets = [item for item in candidate_ids if (not selected_set or item in selected_set)]
        if not targets:
            return RedirectResponse(url=f"/nodes/{node_id}?error=No+valid+target+nodes+selected", status_code=303)

        job = JobsService(db).create(
            job_type="node_nodedb_mutation",
            requested_by=user.username,
            source="ui",
            payload={
                "destination_node_id": str(node_id),
                "action": action,
                "target_node_ids": targets,
                "dry_run": dry_run,
            },
        )
        enqueue_node_nodedb_job(job.id, registry)

        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="node.zero_hop.nodedb.queue",
            provider=node.provider,
            node_id=node.id,
            metadata={
                "action": action,
                "max_hops": max_hops,
                "allowed_roles": sorted(allowed_roles),
                "candidate_count": len(candidate_ids),
                "selected_count": len(targets),
                "dry_run": dry_run,
                "job_id": str(job.id),
            },
        )
        message = quote_plus(f"Queued NodeDB action {action} for {len(targets)} nodes. Job ID: {job.id}")
        return RedirectResponse(url=f"/nodes/{node_id}?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"0-hop NodeDB action failed: {exc}")
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
        resolved_ids = [str(node.id) for node in resolved if node.id is not None]
        selected_query = urlencode([("selected_node_ids", node_id) for node_id in resolved_ids])
        groups_view.append(
            {
                "group": group,
                "assigned": assigned,
                "resolved": resolved,
                "resolved_ids": resolved_ids,
                "open_members_href": f"/nodes?{selected_query}" if selected_query else "/nodes",
                "configure_members_href": f"/nodes/batch-configure?{selected_query}" if selected_query else "/nodes",
            }
        )
    nodes = InventoryService(db).list_nodes()
    nodes_by_id = {str(node.id): node for node in nodes}
    selected_node_ids = [value for value in request.query_params.getlist("selected_node_ids") if value]
    selected_nodes = [nodes_by_id[node_id] for node_id in selected_node_ids if node_id in nodes_by_id]
    return templates.TemplateResponse(
        request,
        "groups.html",
        {
            "user": user,
            "groups_view": groups_view,
            "nodes": nodes,
            "selected_nodes": selected_nodes,
            "selected_node_ids": selected_node_ids,
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


def _groups_redirect_url(
    *,
    message: str | None = None,
    error: str | None = None,
    selected_node_ids: list[str] | None = None,
) -> str:
    params: list[tuple[str, str]] = []
    if message:
        params.append(("message", message))
    if error:
        params.append(("error", error))
    for node_id in selected_node_ids or []:
        value = str(node_id).strip()
        if value:
            params.append(("selected_node_ids", value))
    if not params:
        return "/groups"
    return f"/groups?{urlencode(params)}"


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
            dynamic_filter=_parse_json_dict(dynamic_filter_json, "Dynamic filter"),
            desired_config_template=_parse_json_dict(desired_template_json, "Desired template"),
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
    selected_node_ids: list[str] = Form(default=[]),
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
        return RedirectResponse(
            url=_groups_redirect_url(message="Node assigned", selected_node_ids=selected_node_ids),
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            url=_groups_redirect_url(error=f"Assign failed: {exc}", selected_node_ids=selected_node_ids),
            status_code=303,
        )


@app.post("/ui/groups/{group_id}/assign-selected")
def ui_group_assign_selected(
    group_id: UUID,
    selected_node_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        node_ids = _parse_uuid_list(selected_node_ids)
        if not node_ids:
            return RedirectResponse(
                url=_groups_redirect_url(error="No selected nodes to assign", selected_node_ids=selected_node_ids),
                status_code=303,
            )
        assigned_count = GroupsService(db).assign_nodes(group_id=group_id, node_ids=node_ids)
        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="group.assign_selected_nodes",
            group_id=group_id,
            metadata={
                "assigned_count": assigned_count,
                "selected_node_ids": [str(node_id) for node_id in node_ids],
            },
        )
        return RedirectResponse(
            url=_groups_redirect_url(
                message=f"Assigned {assigned_count} selected nodes to group",
                selected_node_ids=selected_node_ids,
            ),
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            url=_groups_redirect_url(error=f"Bulk assign failed: {exc}", selected_node_ids=selected_node_ids),
            status_code=303,
        )


@app.post("/ui/groups/{group_id}/remove-node")
def ui_group_remove_node(
    group_id: UUID,
    node_id: UUID = Form(...),
    selected_node_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        GroupsService(db).remove_node(group_id=group_id, node_id=node_id)
        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="group.remove_node",
            group_id=group_id,
            node_id=node_id,
        )
        return RedirectResponse(
            url=_groups_redirect_url(message="Node removed", selected_node_ids=selected_node_ids),
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            url=_groups_redirect_url(error=f"Remove failed: {exc}", selected_node_ids=selected_node_ids),
            status_code=303,
        )


@app.post("/ui/groups/{group_id}/remove-selected")
def ui_group_remove_selected(
    group_id: UUID,
    selected_node_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        node_ids = _parse_uuid_list(selected_node_ids)
        if not node_ids:
            return RedirectResponse(
                url=_groups_redirect_url(error="No selected nodes to remove", selected_node_ids=selected_node_ids),
                status_code=303,
            )
        removed_count = GroupsService(db).remove_nodes(group_id=group_id, node_ids=node_ids)
        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="group.remove_selected_nodes",
            group_id=group_id,
            metadata={
                "removed_count": removed_count,
                "selected_node_ids": [str(node_id) for node_id in node_ids],
            },
        )
        return RedirectResponse(
            url=_groups_redirect_url(
                message=f"Removed {removed_count} selected nodes from group",
                selected_node_ids=selected_node_ids,
            ),
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            url=_groups_redirect_url(error=f"Bulk remove failed: {exc}", selected_node_ids=selected_node_ids),
            status_code=303,
        )


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


@app.post("/ui/groups/{group_id}/nodedb/routing-favorites")
def ui_group_nodedb_routing_favorites(
    group_id: UUID,
    action: str = Form("set_favorite"),
    allowed_roles_text: str = Form("ROUTER\nROUTER_LATE\nCLIENT_BASE"),
    source_max_hops: int = Form(0),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        if action not in {"set_favorite", "remove_favorite", "remove_node"}:
            return RedirectResponse(url="/groups?error=Unsupported+NodeDB+action", status_code=303)
        groups = GroupsService(db)
        group = groups.get_group(group_id)
        if group is None:
            return RedirectResponse(url="/groups?error=Group+not+found", status_code=303)
        resolved = groups.resolve_all_members(group)
        if not resolved:
            return RedirectResponse(url="/groups?error=Group+has+no+resolved+members", status_code=303)
        allowed_roles = set(_normalize_roles(allowed_roles_text))
        source_nodes = [
            n for n in resolved
            if str(n.role or "").upper().strip() in allowed_roles
        ]
        target_node_ids = _collect_routing_favorite_targets(
            db=db,
            source_nodes=source_nodes,
            allowed_roles=allowed_roles,
            max_hops=source_max_hops,
        )
        if not target_node_ids:
            return RedirectResponse(url="/groups?error=No+routing+targets+matched+criteria", status_code=303)
        job = JobsService(db).create(
            job_type="bulk_nodedb_mutation",
            requested_by=user.username,
            source="ui",
            payload={
                "destination_node_ids": [str(n.id) for n in resolved if n.id is not None],
                "target_node_ids": target_node_ids,
                "action": action,
                "exclude_self": True,
                "dry_run": dry_run,
                "group_id": str(group_id),
            },
        )
        enqueue_bulk_nodedb_job(job.id, registry)
        message = quote_plus(
            f"Queued group NodeDB {action} for {len(resolved)} nodes (targets: {len(target_node_ids)}). Job ID: {job.id}"
        )
        return RedirectResponse(url=f"/groups?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Group NodeDB routing favorites failed: {exc}")
        return RedirectResponse(url=f"/groups?error={error}", status_code=303)


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
        desired_template = _parse_json_dict(desired_template_json, "Desired template")
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
            dynamic_filter=_parse_json_dict(dynamic_filter_json, "Dynamic filter"),
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
    status: str = "all",
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    jobs_service = JobsService(db)
    jobs = jobs_service.list_jobs()
    query = request.query_params.get("q", "").strip().lower()
    if query:
        jobs = [
            job for job in jobs
            if query in str(job.id).lower()
            or query in str(job.job_type).lower()
            or query in str(job.status).lower()
            or query in str(job.requested_by).lower()
            or query in str(job.source).lower()
        ]
    if status == "running":
        jobs = [job for job in jobs if job.status in {"pending", "running"}]
    elif status == "failed":
        jobs = [job for job in jobs if job.status == "failed"]
    elif status == "success":
        jobs = [job for job in jobs if job.status == "success"]
    results = jobs_service.list_results(limit=600)
    results_by_job: dict[str, list] = {}
    for row in results:
        key = str(row.job_id)
        bucket = results_by_job.setdefault(key, [])
        if len(bucket) < 30:
            bucket.append(row)
    jobs = sorted(
        jobs,
        key=lambda job: (
            0 if job.status == "failed" else 1 if job.status in {"pending", "running"} else 2,
            _normalize_timestamp(job.created_at) or datetime.fromtimestamp(0, tz=timezone.utc),
        ),
        reverse=False,
    )
    nodes_by_id = {str(node.id): node for node in InventoryService(db).list_nodes() if node.id is not None}
    jobs_view: list[dict[str, Any]] = []
    for job in jobs:
        result_rows = results_by_job.get(str(job.id), [])
        payload = job.payload if isinstance(job.payload, dict) else {}
        target_ids = _extract_job_target_ids(payload)
        preview_labels = [
            _node_operator_label(nodes_by_id[node_id])
            for node_id in target_ids[:3]
            if node_id in nodes_by_id
        ]
        jobs_view.append(
            {
                "job": job,
                "result_rows": result_rows,
                "target_count": _extract_job_target_count(payload, result_rows),
                "result_counts": _summarize_job_results(result_rows),
                "result_summary": _build_job_result_summary(job, result_rows),
                "evidence_summary": _build_job_evidence_summary(result_rows),
                "target_preview": preview_labels,
                "duration": _format_duration(job.started_at, job.finished_at),
            }
        )
    auto_refresh = request.query_params.get("refresh", "on").lower() != "off"
    refresh_toggle_url = "/jobs?refresh=off" if auto_refresh else "/jobs?refresh=on"
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "user": user,
            "jobs": jobs,
            "jobs_view": jobs_view,
            "results_by_job": results_by_job,
            "auto_refresh": auto_refresh,
            "refresh_toggle_url": refresh_toggle_url,
            "filters": {"status": status, "q": query},
            "job_counts": {
                "running": len([job for job in jobs if job.status in {"pending", "running"}]),
                "failed": len([job for job in jobs if job.status == "failed"]),
                "success": len([job for job in jobs if job.status == "success"]),
            },
        },
    )


@app.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    entries = AuditService(db).list_recent(limit=300)
    query = request.query_params.get("q", "").strip().lower()
    if query:
        entries = [
            entry for entry in entries
            if query in str(entry.actor or "").lower()
            or query in str(entry.source or "").lower()
            or query in str(entry.action or "").lower()
            or query in str(entry.provider or "").lower()
            or query in str(entry.node_id or "").lower()
        ]
    nodes_by_id = {str(node.id): node for node in InventoryService(db).list_nodes() if node.id is not None}
    entries_view = []
    for entry in entries:
        node = nodes_by_id.get(str(entry.node_id)) if entry.node_id is not None else None
        target_label = _node_operator_label(node) if node is not None else (entry.provider or "-")
        meta_json = entry.meta_json if isinstance(entry.meta_json, dict) else {}
        result_label = (
            meta_json.get("status")
            or ("Assigned" if meta_json.get("assigned_count") else None)
            or ("Recorded" if entry.after_state or entry.before_state else None)
            or "Recorded"
        )
        entries_view.append(
            {
                "entry": entry,
                "relative_time": _format_relative_timestamp(entry.created_at),
                "exact_time": _format_exact_timestamp(entry.created_at),
                "target_label": target_label,
                "result_label": str(result_label),
            }
        )
    return templates.TemplateResponse(request, "audit.html", {"user": user, "entries": entries, "entries_view": entries_view})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
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
    provider_row = db.scalar(select(ProviderModel).where(ProviderModel.name == "meshtastic"))
    default_settings = {}
    if provider_row and isinstance(provider_row.config, dict):
        payload = provider_row.config.get("default_settings")
        if isinstance(payload, dict):
            default_settings = payload
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "providers": providers,
            "default_settings": default_settings,
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


@app.get("/settings/defaults/review", response_class=HTMLResponse)
def settings_defaults_review_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    provider_row = db.scalar(select(ProviderModel).where(ProviderModel.name == "meshtastic"))
    default_settings = {}
    if provider_row and isinstance(provider_row.config, dict):
        payload = provider_row.config.get("default_settings")
        if isinstance(payload, dict):
            default_settings = payload
    nodes = InventoryService(db).list_nodes(provider="meshtastic")
    field_labels = _summarize_patch_fields(default_settings) if default_settings else []
    return templates.TemplateResponse(
        request,
        "settings_defaults_review.html",
        {
            "user": user,
            "default_settings": default_settings,
            "field_labels": field_labels,
            "nodes": nodes,
            "ui_message": request.query_params.get("message"),
            "ui_error": request.query_params.get("error"),
        },
    )


@app.post("/ui/settings/defaults/save")
def ui_settings_defaults_save(
    mqtt_address: str = Form(""),
    mqtt_username: str = Form(""),
    mqtt_password: str = Form(""),
    mqtt_root: str = Form(""),
    mqtt_enabled: str = Form("keep"),
    mqtt_encryption_enabled: str = Form("keep"),
    mqtt_json_enabled: str = Form("keep"),
    mqtt_map_reporting_enabled: str = Form("keep"),
    primary_uplink_enabled: str = Form("keep"),
    primary_downlink_enabled: str = Form("keep"),
    lora_hop_limit: str = Form(""),
    lora_tx_power: str = Form(""),
    lora_modem_preset: str = Form(""),
    lora_region: str = Form(""),
    lora_tx_enabled: str = Form("keep"),
    lora_use_preset: str = Form("keep"),
    lora_config_ok_to_mqtt: str = Form("keep"),
    network_rsyslog_server: str = Form(""),
    network_ntp_server: str = Form(""),
    telemetry_device_update_interval: str = Form(""),
    position_broadcast_smart_minimum_distance: str = Form(""),
    position_broadcast_smart_minimum_interval_secs: str = Form(""),
    position_fixed_position: str = Form("keep"),
    position_gps_mode: str = Form(""),
    position_gps_update_interval: str = Form(""),
    position_broadcast_secs: str = Form(""),
    position_broadcast_smart_enabled: str = Form("keep"),
    position_flags: str = Form(""),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        default_settings = _build_meshtastic_default_settings_patch(
            mqtt_address=mqtt_address,
            mqtt_username=mqtt_username,
            mqtt_password=mqtt_password,
            mqtt_root=mqtt_root,
            mqtt_enabled=mqtt_enabled,
            mqtt_encryption_enabled=mqtt_encryption_enabled,
            mqtt_json_enabled=mqtt_json_enabled,
            mqtt_map_reporting_enabled=mqtt_map_reporting_enabled,
            primary_uplink_enabled=primary_uplink_enabled,
            primary_downlink_enabled=primary_downlink_enabled,
            lora_hop_limit=lora_hop_limit,
            lora_tx_power=lora_tx_power,
            lora_modem_preset=lora_modem_preset,
            lora_region=lora_region,
            lora_tx_enabled=lora_tx_enabled,
            lora_use_preset=lora_use_preset,
            lora_config_ok_to_mqtt=lora_config_ok_to_mqtt,
            network_rsyslog_server=network_rsyslog_server,
            network_ntp_server=network_ntp_server,
            telemetry_device_update_interval=telemetry_device_update_interval,
            position_broadcast_smart_minimum_distance=position_broadcast_smart_minimum_distance,
            position_broadcast_smart_minimum_interval_secs=position_broadcast_smart_minimum_interval_secs,
            position_fixed_position=position_fixed_position,
            position_gps_mode=position_gps_mode,
            position_gps_update_interval=position_gps_update_interval,
            position_broadcast_secs=position_broadcast_secs,
            position_broadcast_smart_enabled=position_broadcast_smart_enabled,
            position_flags=position_flags,
        )

        provider_row = db.scalar(select(ProviderModel).where(ProviderModel.name == "meshtastic"))
        if provider_row is None:
            provider_row = ProviderModel(name="meshtastic", enabled=True, config={})
            db.add(provider_row)
            db.flush()
        config = dict(provider_row.config) if isinstance(provider_row.config, dict) else {}
        config["default_settings"] = default_settings
        provider_row.config = config
        db.commit()

        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="settings.defaults.save",
            provider="meshtastic",
            metadata={"default_settings": default_settings},
        )
        message = quote_plus("Default settings saved")
        return RedirectResponse(url=f"/settings?message={message}", status_code=303)
    except Exception as exc:
        db.rollback()
        error = quote_plus(f"Failed to save default settings: {exc}")
        return RedirectResponse(url=f"/settings?error={error}", status_code=303)


@app.post("/ui/settings/defaults/apply")
def ui_settings_defaults_apply(
    confirm_apply_defaults: str = Form(""),
    dry_run: str = Form(""),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    try:
        provider_row = db.scalar(select(ProviderModel).where(ProviderModel.name == "meshtastic"))
        if provider_row is None or not isinstance(provider_row.config, dict):
            return RedirectResponse(url="/settings?error=No+saved+default+settings", status_code=303)
        default_settings = provider_row.config.get("default_settings")
        if not isinstance(default_settings, dict):
            return RedirectResponse(url="/settings?error=No+saved+default+settings", status_code=303)

        nodes = InventoryService(db).list_nodes(provider="meshtastic")
        node_ids = [str(node.id) for node in nodes if node.id is not None]
        if not node_ids:
            return RedirectResponse(url="/settings?error=No+managed+Meshtastic+nodes+found", status_code=303)
        confirmed = _parse_optional_bool(confirm_apply_defaults)
        if confirmed is not True:
            error = quote_plus("Review the affected nodes and confirm the defaults rollout before queueing")
            return RedirectResponse(url=f"/settings/defaults/review?error={error}", status_code=303)
        dry_run_value = _parse_optional_bool(dry_run) is True

        job = JobsService(db).create(
            job_type="multi_node_config_patch",
            requested_by=user.username,
            source="ui",
            payload={
                "node_ids": node_ids,
                "dry_run": dry_run_value,
                "base_patch": default_settings,
                "clone": {},
                "location_spread": {"enabled": False},
            },
        )
        enqueue_multi_node_patch_job(job.id, registry)
        message = quote_plus(f"Default settings queued for {len(node_ids)} nodes. Job ID: {job.id}")
        return RedirectResponse(url=f"/settings?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Failed to queue default settings: {exc}")
        return RedirectResponse(url=f"/settings?error={error}", status_code=303)


@app.post("/api/discovery/scan")
def api_discovery_scan(
    payload: DiscoveryRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    if not _has_discovery_targets(payload.hosts, payload.cidrs, payload.manual_endpoints):
        raise HTTPException(status_code=422, detail="Provide at least one host, CIDR range, or manual endpoint")
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


@app.post("/api/nodes/{node_id}/nodedb")
def api_node_nodedb_mutation(
    node_id: UUID,
    payload: NodeDbMutationRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    action = payload.action.strip().lower()
    if action not in {"set_favorite", "remove_favorite", "remove_node"}:
        raise HTTPException(status_code=400, detail="Unsupported NodeDB action")
    targets = [item.strip() for item in payload.target_node_ids if item.strip()]
    if not targets:
        raise HTTPException(status_code=400, detail="No target_node_ids provided")
    node = db.get(ManagedNodeModel, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    job = JobsService(db).create(
        job_type="node_nodedb_mutation",
        requested_by=user.username,
        source="api",
        payload={
            "destination_node_id": str(node_id),
            "action": action,
            "target_node_ids": targets,
            "dry_run": payload.dry_run,
        },
    )
    enqueue_node_nodedb_job(job.id, registry)
    return {"job_id": str(job.id)}


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
