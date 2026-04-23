from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from meshonator.domain.models import ConfigPatch, ManagedNode, NodeCapability, OperationMatrix, ProviderHealth


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderConnection:
    endpoint: str
    host: str
    port: int


class Provider(ABC):
    name: str
    experimental: bool = False

    @abstractmethod
    def capabilities(self) -> NodeCapability:
        raise NotImplementedError

    @abstractmethod
    def operation_matrix(self) -> OperationMatrix:
        raise NotImplementedError

    @abstractmethod
    def discover_endpoints(
        self,
        hosts: list[str],
        port: int | None = None,
        *,
        progress_cb: Callable[[int, int, str, bool], None] | None = None,
    ) -> list[ProviderConnection]:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> ProviderHealth:
        raise NotImplementedError

    @abstractmethod
    def connect(self, endpoint: ProviderConnection) -> Any:
        raise NotImplementedError

    @abstractmethod
    def fetch_nodes(self, conn: Any) -> list[ManagedNode]:
        raise NotImplementedError

    @abstractmethod
    def fetch_config(self, conn: Any, provider_node_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def apply_config_patch(
        self,
        conn: Any,
        provider_node_id: str,
        patch: ConfigPatch,
        dry_run: bool,
    ) -> dict[str, Any]:
        raise NotImplementedError
