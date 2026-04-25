from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.protobuf.json_format import MessageToDict

from meshonator.config.settings import get_settings
from meshonator.domain.models import (
    ConfigPatch,
    FirmwareInfo,
    HardwareInfo,
    Location,
    ManagedNode,
    NodeCapability,
    OperationMatrix,
    ProviderHealth,
    ProviderOperationSupport,
    ProviderType,
)
from meshonator.providers.base import Provider, ProviderConnection, ProviderError
from meshonator.providers.meshtastic.cli_fallback import MeshtasticCliFallback
from meshonator.providers.utils.json_safe import to_json_safe
from meshonator.providers.utils.tcp_scan import tcp_probe

try:
    from meshtastic.tcp_interface import TCPInterface
except Exception:  # pragma: no cover
    TCPInterface = None  # type: ignore[assignment]


class MeshtasticProvider(Provider):
    name = "meshtastic"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.cli = MeshtasticCliFallback(timeout_s=self.settings.provider_timeout_s)

    def capabilities(self) -> NodeCapability:
        return NodeCapability(
            can_discover_over_tcp=True,
            can_remote_read_config=True,
            can_remote_write_config=True,
            can_batch_write=True,
            has_location=True,
            has_neighbors=True,
            has_firmware_info=True,
            has_favorites=True,
            has_channels=True,
        )

    def operation_matrix(self) -> OperationMatrix:
        return OperationMatrix(
            rename_node=ProviderOperationSupport.NATIVE,
            update_location=ProviderOperationSupport.NATIVE,
            update_role=ProviderOperationSupport.CLI_FALLBACK,
            favorite_toggle=ProviderOperationSupport.NATIVE,
            read_config=ProviderOperationSupport.NATIVE,
            write_config=ProviderOperationSupport.NATIVE,
            batch_write=ProviderOperationSupport.NATIVE,
        )

    def discover_endpoints(
        self,
        hosts: list[str],
        port: int | None = None,
        *,
        progress_cb=None,
    ) -> list[ProviderConnection]:
        tcp_port = port or self.settings.meshtastic_default_tcp_port
        discovered: list[ProviderConnection] = []
        total = len(hosts)
        for index, host in enumerate(hosts, start=1):
            probe = tcp_probe(host, tcp_port, timeout=self.settings.discovery_connect_timeout_s)
            is_open = probe["is_open"]
            if progress_cb is not None:
                try:
                    progress_cb(index, total, host, is_open, probe)
                except TypeError:
                    progress_cb(index, total, host, is_open)
            if not is_open:
                continue
            discovered.append(ProviderConnection(endpoint=f"tcp://{host}:{tcp_port}", host=host, port=tcp_port))
        return discovered

    def health(self) -> ProviderHealth:
        ready = TCPInterface is not None
        return ProviderHealth(
            provider=ProviderType.MESHTASTIC,
            status="ok" if ready else "degraded",
            details={"python_library": "installed" if ready else "missing"},
        )

    def connect(self, endpoint: ProviderConnection) -> Any:
        if TCPInterface is None:
            raise ProviderError("meshtastic python library unavailable")
        try:
            conn = TCPInterface(hostname=endpoint.host, portNumber=endpoint.port)
            try:
                conn.waitForConfig()
            except Exception:
                # Continue even if full config fetch is partial; caller can still use available data.
                pass
            return conn
        except Exception as exc:  # pragma: no cover
            raise ProviderError(f"Failed to connect to {endpoint.endpoint}: {exc}") from exc

    def fetch_nodes(self, conn: Any) -> list[ManagedNode]:
        now = datetime.now(timezone.utc)
        local_node = getattr(conn, "localNode", None)
        info_payload = _build_meshtastic_info_payload(conn)
        my_info = info_payload.get("myInfo", {})
        metadata = info_payload.get("metadata", {})
        owner = info_payload.get("owner", {})
        local_record = info_payload.get("localNodeRecord", {})
        preferences = info_payload.get("preferences", {})

        out: list[ManagedNode] = []
        if local_node is not None:
            short_name = owner.get("shortName")
            long_name = owner.get("longName")
            node_num = my_info.get("myNodeNum") if isinstance(my_info, dict) else None
            position = local_record.get("position", {}) if isinstance(local_record, dict) else {}
            last_heard = local_record.get("lastHeard") if isinstance(local_record, dict) else None
            role = _resolve_role(owner, metadata, preferences, local_record)
            out.append(
                ManagedNode(
                    provider=ProviderType.MESHTASTIC,
                    provider_node_id=_resolve_provider_node_id(owner, node_num, short_name),
                    node_num=node_num,
                    short_name=short_name,
                    long_name=long_name,
                    role=role,
                    favorite=bool(local_record.get("isFavorite")) if isinstance(local_record, dict) else False,
                    first_seen=now,
                    last_seen=_ts_to_dt(last_heard) or now,
                    reachable=True,
                    location=Location(
                        latitude=position.get("latitude"),
                        longitude=position.get("longitude"),
                        altitude=position.get("altitude"),
                        source=position.get("locationSource"),
                    ),
                    firmware=FirmwareInfo(version=(metadata.get("firmwareVersion") if isinstance(metadata, dict) else None)),
                    hardware=HardwareInfo(model=(metadata.get("hwModel") if isinstance(metadata, dict) else None)),
                    capabilities=self.capabilities(),
                    raw_metadata=to_json_safe(info_payload),
                )
            )

        return out

    def fetch_config(self, conn: Any, provider_node_id: str) -> dict[str, Any]:
        info_payload = _build_meshtastic_info_payload(conn)
        return {
            "provider_node_id": provider_node_id,
            "owner": info_payload.get("owner", {}),
            "myInfo": info_payload.get("myInfo", {}),
            "metadata": info_payload.get("metadata", {}),
            "nodesInMesh": info_payload.get("nodesInMesh", {}),
            "preferences": info_payload.get("preferences", {}),
            "modulePreferences": info_payload.get("modulePreferences", {}),
            "channels": info_payload.get("channels", []),
            "primaryChannelUrl": info_payload.get("primaryChannelUrl"),
        }

    def apply_config_patch(
        self,
        conn: Any,
        provider_node_id: str,
        patch: ConfigPatch,
        dry_run: bool,
    ) -> dict[str, Any]:
        if dry_run:
            return {
                "provider_node_id": provider_node_id,
                "mode": "dry_run",
                "patch": patch.model_dump(),
                "supported": True,
            }

        local_node = getattr(conn, "localNode", None)
        if local_node is None:
            raise ProviderError("No local node available")

        applied: dict[str, Any] = {}
        if patch.short_name is not None or patch.long_name is not None:
            name_payload = {
                "short_name": patch.short_name,
                "long_name": patch.long_name,
            }
            if hasattr(local_node, "setOwner"):
                local_node.setOwner(
                    long_name=patch.long_name,
                    short_name=patch.short_name,
                )
                applied["name"] = name_payload

        if patch.latitude is not None and patch.longitude is not None and hasattr(local_node, "setFixedPosition"):
            local_node.setFixedPosition(lat=patch.latitude, lon=patch.longitude, alt=patch.altitude)
            applied["position"] = {
                "latitude": patch.latitude,
                "longitude": patch.longitude,
                "altitude": patch.altitude,
            }

        if patch.local_config_patch and hasattr(local_node, "writeConfig"):
            local_node.writeConfig("local", patch.local_config_patch)
            applied["local_config"] = patch.local_config_patch

        if patch.module_config_patch and hasattr(local_node, "writeConfig"):
            local_node.writeConfig("module", patch.module_config_patch)
            applied["module_config"] = patch.module_config_patch

        return {"provider_node_id": provider_node_id, "mode": "apply", "applied": applied, "supported": True}


def _safe_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return {"value": str(value)}


def _safe_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_safe_dict(v) for v in value]
    return [_safe_dict(value)]


def _msg_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        return MessageToDict(value, preserving_proto_field_name=False)
    except Exception:
        return _safe_dict(value)


def _build_meshtastic_info_payload(conn: Any) -> dict[str, Any]:
    local_node = getattr(conn, "localNode", None)
    nodes_in_mesh = getattr(conn, "nodes", {}) or {}
    my_info = _msg_to_dict(getattr(conn, "myInfo", None))
    metadata = _msg_to_dict(getattr(conn, "metadata", None))

    owner: dict[str, Any] = {}
    local_node_record: dict[str, Any] = {}
    my_node_num = my_info.get("myNodeNum")
    if isinstance(nodes_in_mesh, dict):
        for value in nodes_in_mesh.values():
            if not isinstance(value, dict):
                continue
            if my_node_num is not None and value.get("num") == my_node_num:
                local_node_record = value
                user = value.get("user")
                if isinstance(user, dict):
                    owner = user
                break

    preferences = {}
    module_preferences = {}
    channels: list[dict[str, Any]] = []
    primary_url = None
    if local_node is not None:
        preferences = _msg_to_dict(getattr(local_node, "localConfig", None))
        module_preferences = _msg_to_dict(getattr(local_node, "moduleConfig", None))
        for channel in getattr(local_node, "channels", []) or []:
            channels.append(_msg_to_dict(channel))
        if hasattr(local_node, "getURL"):
            try:
                primary_url = local_node.getURL()
            except Exception:
                primary_url = None

    return {
        "owner": owner,
        "myInfo": my_info,
        "metadata": metadata,
        "localNodeRecord": to_json_safe(local_node_record),
        "nodesInMesh": to_json_safe(nodes_in_mesh),
        "preferences": preferences,
        "modulePreferences": module_preferences,
        "channels": channels,
        "primaryChannelUrl": primary_url,
    }


def _resolve_provider_node_id(owner: dict[str, Any], node_num: Any, short_name: str | None) -> str:
    owner_id = owner.get("id") if isinstance(owner, dict) else None
    if isinstance(owner_id, str) and owner_id.strip():
        return owner_id.strip()
    if isinstance(node_num, int):
        return f"node-{node_num}"
    if short_name and short_name.strip():
        return short_name.strip()
    return "local-unknown"


def _resolve_role(
    owner: dict[str, Any],
    metadata: dict[str, Any],
    preferences: dict[str, Any],
    local_record: dict[str, Any],
) -> str | None:
    candidates = [
        owner.get("role") if isinstance(owner, dict) else None,
        metadata.get("role") if isinstance(metadata, dict) else None,
        local_record.get("user", {}).get("role") if isinstance(local_record, dict) and isinstance(local_record.get("user"), dict) else None,
        preferences.get("device", {}).get("role") if isinstance(preferences, dict) and isinstance(preferences.get("device"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _ts_to_dt(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except Exception:
        return None
