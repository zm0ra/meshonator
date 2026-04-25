from __future__ import annotations

from typing import Any

from meshonator.domain.models import (
    ConfigPatch,
    ManagedNode,
    NodeCapability,
    OperationMatrix,
    ProviderHealth,
    ProviderOperationSupport,
    ProviderType,
)
from meshonator.providers.base import Provider, ProviderConnection, ProviderError
from meshonator.providers.utils.tcp_scan import tcp_is_open

try:
    import meshcore_py  # type: ignore
except Exception:  # pragma: no cover
    meshcore_py = None


class MeshCoreProvider(Provider):
    name = "meshcore"
    experimental = True

    def capabilities(self) -> NodeCapability:
        return NodeCapability(
            can_discover_over_tcp=True,
            can_remote_read_config=False,
            can_remote_write_config=False,
            can_batch_write=False,
            has_location=True,
            has_neighbors=False,
            has_firmware_info=True,
            has_favorites=False,
            has_channels=False,
        )

    def operation_matrix(self) -> OperationMatrix:
        return OperationMatrix(
            rename_node=ProviderOperationSupport.UNSUPPORTED,
            update_location=ProviderOperationSupport.UNSUPPORTED,
            update_role=ProviderOperationSupport.UNSUPPORTED,
            favorite_toggle=ProviderOperationSupport.UNSUPPORTED,
            read_config=ProviderOperationSupport.UNSUPPORTED,
            write_config=ProviderOperationSupport.UNSUPPORTED,
            batch_write=ProviderOperationSupport.UNSUPPORTED,
        )

    def discover_endpoints(
        self,
        hosts: list[str],
        port: int | None = None,
        *,
        progress_cb=None,
    ) -> list[ProviderConnection]:
        tcp_port = port or 19090
        discovered: list[ProviderConnection] = []
        total = len(hosts)
        for index, host in enumerate(hosts, start=1):
            is_open = tcp_is_open(host, tcp_port)
            if progress_cb is not None:
                progress_cb(index, total, host, is_open)
            if not is_open:
                continue
            discovered.append(ProviderConnection(endpoint=f"tcp://{host}:{tcp_port}", host=host, port=tcp_port))
        return discovered

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=ProviderType.MESHCORE,
            status="experimental",
            details={"python_library": "installed" if meshcore_py else "missing"},
        )

    def connect(self, endpoint: ProviderConnection) -> Any:
        if meshcore_py is None:
            raise ProviderError("meshcore_py not available")
        client = getattr(meshcore_py, "Client", None)
        if client is None:
            raise ProviderError("meshcore_py.Client is unavailable")
        return client(endpoint.host, endpoint.port)

    def disconnect(self, conn: Any) -> None:
        if conn is None:
            return
        for method_name in ("close", "disconnect", "stop"):
            fn = getattr(conn, method_name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break

    def fetch_nodes(self, conn: Any) -> list[ManagedNode]:
        contacts = []
        if hasattr(conn, "contacts"):
            contacts = conn.contacts()
        return [
            ManagedNode(
                provider=ProviderType.MESHCORE,
                provider_node_id=str(c.get("id", "unknown")),
                short_name=c.get("name"),
                reachable=True,
                capabilities=self.capabilities(),
                raw_metadata=c,
            )
            for c in contacts
            if isinstance(c, dict)
        ]

    def fetch_config(self, conn: Any, provider_node_id: str) -> dict[str, Any]:
        raise ProviderError("MeshCore config read is currently unsupported in experimental provider")

    def apply_config_patch(
        self,
        conn: Any,
        provider_node_id: str,
        patch: ConfigPatch,
        dry_run: bool,
    ) -> dict[str, Any]:
        raise ProviderError("MeshCore config write is currently unsupported in experimental provider")
