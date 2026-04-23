from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from meshonator.db.models import AuditLogModel


class AuditService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def log(
        self,
        actor: str,
        source: str,
        action: str,
        provider: str | None = None,
        node_id: UUID | None = None,
        group_id: UUID | None = None,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLogModel:
        row = AuditLogModel(
            actor=actor,
            source=source,
            action=action,
            provider=provider,
            node_id=node_id,
            group_id=group_id,
            before_state=before_state or {},
            after_state=after_state or {},
            meta_json=metadata or {},
        )
        self.db.add(row)
        self.db.commit()
        return row

    def list_recent(self, limit: int = 200) -> list[AuditLogModel]:
        stmt = select(AuditLogModel).order_by(AuditLogModel.created_at.desc()).limit(limit)
        return list(self.db.scalars(stmt).all())
