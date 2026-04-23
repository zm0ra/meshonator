from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ProviderType(StrEnum):
    MESHTASTIC = "meshtastic"
    MESHCORE = "meshcore"


class NodeDriftState(StrEnum):
    COMPLIANT = "compliant"
    DRIFTED = "drifted"
    UNKNOWN = "unknown"


class NodeCapability(BaseModel):
    can_discover_over_tcp: bool = False
    can_remote_read_config: bool = False
    can_remote_write_config: bool = False
    can_batch_write: bool = False
    has_location: bool = False
    has_neighbors: bool = False
    has_firmware_info: bool = False
    has_favorites: bool = False
    has_channels: bool = False


class NodeEndpoint(BaseModel):
    endpoint: str
    host: str
    port: int
    is_primary: bool = True
    source: str = "manual"


class Location(BaseModel):
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    source: str | None = None
    updated_at: datetime | None = None


class FirmwareInfo(BaseModel):
    version: str | None = None
    build: str | None = None


class HardwareInfo(BaseModel):
    model: str | None = None


class ManagedNode(BaseModel):
    id: UUID | None = None
    provider: ProviderType
    provider_node_id: str
    node_num: int | None = None
    short_name: str | None = None
    long_name: str | None = None
    role: str | None = None
    favorite: bool = False
    managed: bool = True
    reachable: bool = False
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    last_successful_sync: datetime | None = None
    location: Location = Field(default_factory=Location)
    firmware: FirmwareInfo = Field(default_factory=FirmwareInfo)
    hardware: HardwareInfo = Field(default_factory=HardwareInfo)
    capabilities: NodeCapability = Field(default_factory=NodeCapability)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class NodeSnapshot(BaseModel):
    node_id: UUID
    snapshot_type: str
    payload: dict[str, Any]
    created_at: datetime


class ConfigPatch(BaseModel):
    short_name: str | None = None
    long_name: str | None = None
    role: str | None = None
    favorite: bool | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    local_config_patch: dict[str, Any] = Field(default_factory=dict)
    module_config_patch: dict[str, Any] = Field(default_factory=dict)
    channels_patch: list[dict[str, Any]] = Field(default_factory=list)


class ProviderOperationSupport(StrEnum):
    NATIVE = "supported_natively"
    CLI_FALLBACK = "supported_via_cli_fallback"
    UNSUPPORTED = "unsupported_or_restricted"


class OperationMatrix(BaseModel):
    rename_node: ProviderOperationSupport
    update_location: ProviderOperationSupport
    update_role: ProviderOperationSupport
    favorite_toggle: ProviderOperationSupport
    read_config: ProviderOperationSupport
    write_config: ProviderOperationSupport
    batch_write: ProviderOperationSupport


class SyncJobRequest(BaseModel):
    node_ids: list[UUID] = Field(default_factory=list)
    group_id: UUID | None = None
    quick: bool = False


class ProviderHealth(BaseModel):
    provider: ProviderType
    status: str
    details: dict[str, Any] = Field(default_factory=dict)
