from __future__ import annotations

from meshonator.config.settings import get_settings
from meshonator.providers.base import Provider
from meshonator.providers.meshcore.provider import MeshCoreProvider
from meshonator.providers.meshtastic.provider import MeshtasticProvider


class ProviderRegistry:
    def __init__(self) -> None:
        settings = get_settings()
        self._providers: dict[str, Provider] = {
            "meshtastic": MeshtasticProvider(),
        }
        if settings.meshcore_enabled:
            self._providers["meshcore"] = MeshCoreProvider()

    def get(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ValueError(f"Provider {name} is not configured") from exc

    def all(self) -> list[Provider]:
        return list(self._providers.values())
