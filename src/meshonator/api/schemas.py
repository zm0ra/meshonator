from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class DiscoveryRequest(BaseModel):
    provider: str = "meshtastic"
    hosts: list[str] = Field(default_factory=list)
    cidrs: list[str] = Field(default_factory=list)
    manual_endpoints: list[str] = Field(default_factory=list)
    port: int | None = None


class ConfigPatchRequest(BaseModel):
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


class BatchPatchRequest(BaseModel):
    node_ids: list[UUID]
    patch: ConfigPatchRequest
    dry_run: bool = True


class GroupCreateRequest(BaseModel):
    name: str
    description: str | None = None
    dynamic_filter: dict[str, Any] = Field(default_factory=dict)
    desired_config_template: dict[str, Any] = Field(default_factory=dict)


class NodeDbMutationRequest(BaseModel):
    action: str
    target_node_ids: list[str] = Field(default_factory=list)
    dry_run: bool = True
