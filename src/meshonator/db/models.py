from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from meshonator.db.base import Base


UUID_COL = Uuid(as_uuid=True)


class ProviderModel(Base):
    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderEndpointModel(Base):
    __tablename__ = "provider_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    provider_name: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(String(255), unique=True)
    host: Mapped[str] = mapped_column(String(255), index=True)
    port: Mapped[int] = mapped_column(Integer, default=4403)
    source: Mapped[str] = mapped_column(String(64), default="manual")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reachable: Mapped[bool] = mapped_column(Boolean, default=False)
    meta_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class ManagedNodeModel(Base):
    __tablename__ = "managed_nodes"
    __table_args__ = (UniqueConstraint("provider", "provider_node_id", name="uq_provider_node"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    provider_node_id: Mapped[str] = mapped_column(String(128), index=True)
    node_num: Mapped[int | None] = mapped_column(Integer, index=True)
    short_name: Mapped[str | None] = mapped_column(String(64), index=True)
    long_name: Mapped[str | None] = mapped_column(String(128))
    firmware_version: Mapped[str | None] = mapped_column(String(128), index=True)
    hardware_model: Mapped[str | None] = mapped_column(String(128), index=True)
    role: Mapped[str | None] = mapped_column(String(64), index=True)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=True)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    altitude: Mapped[float | None] = mapped_column(Float)
    location_source: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reachable: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    raw_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    capability_matrix: Mapped[dict] = mapped_column(JSON, default=dict)

    endpoints: Mapped[list["NodeEndpointModel"]] = relationship(
        back_populates="node", cascade="all,delete", lazy="selectin"
    )


class NodeEndpointModel(Base):
    __tablename__ = "node_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID_COL, ForeignKey("managed_nodes.id", ondelete="CASCADE"))
    endpoint: Mapped[str] = mapped_column(String(255), index=True)
    host: Mapped[str] = mapped_column(String(255), index=True)
    port: Mapped[int] = mapped_column(Integer, default=4403)
    source: Mapped[str] = mapped_column(String(64), default="discovery")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    node: Mapped[ManagedNodeModel] = relationship(back_populates="endpoints")


class NodeGroupModel(Base):
    __tablename__ = "node_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    dynamic_filter: Mapped[dict] = mapped_column(JSON, default=dict)
    desired_config_template: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NodeGroupMemberModel(Base):
    __tablename__ = "node_group_members"
    __table_args__ = (UniqueConstraint("group_id", "node_id", name="uq_group_node"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID_COL, ForeignKey("node_groups.id", ondelete="CASCADE"))
    node_id: Mapped[uuid.UUID] = mapped_column(UUID_COL, ForeignKey("managed_nodes.id", ondelete="CASCADE"))


class NodeTagModel(Base):
    __tablename__ = "node_tags"
    __table_args__ = (UniqueConstraint("node_id", "tag", name="uq_node_tag"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID_COL, ForeignKey("managed_nodes.id", ondelete="CASCADE"))
    tag: Mapped[str] = mapped_column(String(64), index=True)


class NodeSnapshotModel(Base):
    __tablename__ = "node_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID_COL, ForeignKey("managed_nodes.id", ondelete="CASCADE"))
    snapshot_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class NodeConfigModel(Base):
    __tablename__ = "node_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID_COL, ForeignKey("managed_nodes.id", ondelete="CASCADE"))
    config_type: Mapped[str] = mapped_column(String(64), index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    version_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class NodeDesiredStateModel(Base):
    __tablename__ = "node_desired_state"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID_COL, ForeignKey("managed_nodes.id", ondelete="CASCADE"), unique=True
    )
    desired_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    drift_state: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class JobModel(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="system", index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class JobResultModel(Base):
    __tablename__ = "job_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID_COL, ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID_COL, ForeignKey("managed_nodes.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str | None] = mapped_column(String(64), index=True)
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID_COL, ForeignKey("managed_nodes.id", ondelete="SET NULL"))
    group_id: Mapped[uuid.UUID | None] = mapped_column(UUID_COL, ForeignKey("node_groups.id", ondelete="SET NULL"))
    before_state: Mapped[dict] = mapped_column(JSON, default=dict)
    after_state: Mapped[dict] = mapped_column(JSON, default=dict)
    meta_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID_COL, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
