from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from meshonator.db.models import ManagedNodeModel


class MapService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def markers(self, provider: str | None = None, favorite: bool | None = None) -> list[dict]:
        stmt = select(ManagedNodeModel).where(
            ManagedNodeModel.latitude.is_not(None),
            ManagedNodeModel.longitude.is_not(None),
        )
        if provider:
            stmt = stmt.where(ManagedNodeModel.provider == provider)
        if favorite is not None:
            stmt = stmt.where(ManagedNodeModel.favorite.is_(favorite))

        rows = self.db.scalars(stmt).all()
        return [
            {
                "id": str(row.id),
                "provider": row.provider,
                "provider_node_id": row.provider_node_id,
                "name": row.long_name or row.short_name or row.provider_node_id,
                "lat": row.latitude,
                "lon": row.longitude,
                "alt": row.altitude,
                "reachable": row.reachable,
                "favorite": row.favorite,
                "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            }
            for row in rows
        ]
