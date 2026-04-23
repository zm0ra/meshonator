from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from meshonator.db.models import ProviderEndpointModel
from meshonator.providers.registry import ProviderRegistry
from meshonator.providers.utils.tcp_scan import expand_targets


class DiscoveryService:
    def __init__(self, db: Session, registry: ProviderRegistry) -> None:
        self.db = db
        self.registry = registry

    def scan(
        self,
        provider_name: str,
        hosts: Iterable[str] = (),
        cidrs: Iterable[str] = (),
        manual_endpoints: Iterable[str] = (),
        port: int | None = None,
        source: str = "manual",
        progress_cb=None,
    ) -> list[ProviderEndpointModel]:
        provider = self.registry.get(provider_name)
        targets = expand_targets(hosts, cidrs)

        for endpoint in manual_endpoints:
            if endpoint.startswith("tcp://"):
                without_prefix = endpoint.removeprefix("tcp://")
                host = without_prefix.split(":")[0]
                targets.append(host)

        unique_targets = sorted(set(targets))
        try:
            discovered = provider.discover_endpoints(unique_targets, port=port, progress_cb=progress_cb)
        except TypeError:
            # Backward compatibility for fake providers in tests that still expose legacy signature.
            discovered = provider.discover_endpoints(unique_targets, port=port)

        saved: list[ProviderEndpointModel] = []
        for found in discovered:
            row = self.db.scalar(
                select(ProviderEndpointModel).where(ProviderEndpointModel.endpoint == found.endpoint)
            )
            if row is None:
                row = ProviderEndpointModel(
                    provider_name=provider_name,
                    endpoint=found.endpoint,
                    host=found.host,
                    port=found.port,
                    source=source,
                    reachable=True,
                    last_seen=datetime.now(timezone.utc),
                    meta_json={"transport": "tcp"},
                )
                self.db.add(row)
            else:
                row.reachable = True
                row.last_seen = datetime.now(timezone.utc)
                row.source = source
            saved.append(row)

        self.db.commit()
        return saved
