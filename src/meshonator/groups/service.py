from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from meshonator.db.models import ManagedNodeModel, NodeGroupMemberModel, NodeGroupModel


class GroupsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_groups(self) -> list[NodeGroupModel]:
        return list(self.db.scalars(select(NodeGroupModel).order_by(NodeGroupModel.name)).all())

    def create_group(self, name: str, description: str | None, dynamic_filter: dict, desired_config_template: dict) -> NodeGroupModel:
        group = NodeGroupModel(
            name=name,
            description=description,
            dynamic_filter=dynamic_filter,
            desired_config_template=desired_config_template,
        )
        self.db.add(group)
        self.db.commit()
        return group

    def assign_node(self, group_id: UUID, node_id: UUID) -> None:
        existing = self.db.scalar(
            select(NodeGroupMemberModel).where(
                NodeGroupMemberModel.group_id == group_id,
                NodeGroupMemberModel.node_id == node_id,
            )
        )
        if existing is None:
            self.db.add(NodeGroupMemberModel(group_id=group_id, node_id=node_id))
            self.db.commit()

    def remove_node(self, group_id: UUID, node_id: UUID) -> None:
        self.db.execute(
            delete(NodeGroupMemberModel).where(
                NodeGroupMemberModel.group_id == group_id,
                NodeGroupMemberModel.node_id == node_id,
            )
        )
        self.db.commit()

    def resolve_dynamic_members(self, group: NodeGroupModel) -> list[ManagedNodeModel]:
        f = group.dynamic_filter or {}
        stmt = select(ManagedNodeModel)
        if provider := f.get("provider"):
            stmt = stmt.where(ManagedNodeModel.provider == provider)
        if firmware := f.get("firmware"):
            stmt = stmt.where(ManagedNodeModel.firmware_version == firmware)
        if favorite := f.get("favorite"):
            stmt = stmt.where(ManagedNodeModel.favorite.is_(bool(favorite)))
        if reachable := f.get("reachable"):
            stmt = stmt.where(ManagedNodeModel.reachable.is_(bool(reachable)))
        if role := f.get("role"):
            stmt = stmt.where(ManagedNodeModel.role == role)
        if bbox := f.get("bbox"):
            stmt = stmt.where(
                and_(
                    ManagedNodeModel.latitude >= bbox["min_lat"],
                    ManagedNodeModel.latitude <= bbox["max_lat"],
                    ManagedNodeModel.longitude >= bbox["min_lon"],
                    ManagedNodeModel.longitude <= bbox["max_lon"],
                )
            )
        return list(self.db.scalars(stmt).all())
