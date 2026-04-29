from __future__ import annotations

import socket

from meshonator.providers.meshtastic.provider import MeshtasticProvider
from meshonator.providers.base import ProviderConnection, ProviderError


class _FakeLocalNode:
    localConfig = {"device": {"role": "client"}}
    moduleConfig = {}
    channels = []

    def getURL(self) -> str:
        return "https://example.invalid/channel"


class _FakeConn:
    def __init__(self, my_info: dict, metadata: dict, nodes: dict) -> None:
        self.localNode = _FakeLocalNode()
        self.myInfo = my_info
        self.metadata = metadata
        self.nodes = nodes


def test_fetch_nodes_uses_owner_id_and_owner_role() -> None:
    provider = MeshtasticProvider()
    conn = _FakeConn(
        my_info={"myNodeNum": 2717510324},
        metadata={"firmwareVersion": "2.5.2", "hwModel": "SEEED_XIAO_S3", "role": "CLIENT"},
        nodes={
            "!a1f9eab4": {
                "num": 2717510324,
                "user": {
                    "id": "!a1f9eab4",
                    "shortName": "BKO7",
                    "longName": "Szczecin Bukowo",
                    "role": "ROUTER",
                },
                "isFavorite": True,
                "lastHeard": 1776946486,
                "position": {"latitude": 53.0, "longitude": 14.0, "altitude": 10},
            }
        },
    )

    nodes = provider.fetch_nodes(conn)

    assert len(nodes) == 1
    node = nodes[0]
    assert node.provider_node_id == "!a1f9eab4"
    assert node.short_name == "BKO7"
    assert node.long_name == "Szczecin Bukowo"
    assert node.role == "ROUTER"
    assert node.favorite is False
    assert node.location.latitude == 53.0
    assert node.location.longitude == 14.0


def test_fetch_nodes_role_falls_back_to_metadata_then_preferences() -> None:
    provider = MeshtasticProvider()

    conn_metadata_role = _FakeConn(
        my_info={"myNodeNum": 10},
        metadata={"role": "client_mute"},
        nodes={"!x": {"num": 10, "user": {"id": "!x", "shortName": "x"}}},
    )
    node_metadata_role = provider.fetch_nodes(conn_metadata_role)[0]
    assert node_metadata_role.role == "CLIENT_MUTE"

    class _PrefLocalNode(_FakeLocalNode):
        localConfig = {"device": {"role": "tracker"}}

    conn_pref_role = _FakeConn(
        my_info={"myNodeNum": 11},
        metadata={},
        nodes={"!y": {"num": 11, "user": {"id": "!y", "shortName": "y"}}},
    )
    conn_pref_role.localNode = _PrefLocalNode()
    node_pref_role = provider.fetch_nodes(conn_pref_role)[0]
    assert node_pref_role.role == "TRACKER"


def test_connect_applies_provider_timeout_and_restores_socket_default(monkeypatch) -> None:
    observed: dict[str, float | None] = {}

    class _FakeTcpInterface:
        def __init__(self, hostname: str, portNumber: int, timeout: int) -> None:
            observed["during_init"] = socket.getdefaulttimeout()
            observed["timeout_arg"] = timeout

        def waitForConfig(self) -> None:
            observed["during_wait"] = socket.getdefaulttimeout()

    monkeypatch.setattr("meshonator.providers.meshtastic.provider.TCPInterface", _FakeTcpInterface)
    provider = MeshtasticProvider()
    provider.settings.provider_timeout_s = 0.25

    previous_timeout = socket.getdefaulttimeout()
    connection = provider.connect(ProviderConnection(endpoint="tcp://172.30.251.2:4403", host="172.30.251.2", port=4403))

    assert isinstance(connection, _FakeTcpInterface)
    assert observed["during_init"] == 0.25
    assert observed["during_wait"] == 0.25
    assert observed["timeout_arg"] == 1
    assert socket.getdefaulttimeout() == previous_timeout


def test_connect_wraps_timeout_errors(monkeypatch) -> None:
    class _FakeTcpInterface:
        def __init__(self, hostname: str, portNumber: int, timeout: int) -> None:
            raise TimeoutError("timed out")

    monkeypatch.setattr("meshonator.providers.meshtastic.provider.TCPInterface", _FakeTcpInterface)
    provider = MeshtasticProvider()

    try:
        provider.connect(ProviderConnection(endpoint="tcp://172.30.251.3:4403", host="172.30.251.3", port=4403))
    except ProviderError as exc:
        assert "172.30.251.3:4403" in str(exc)
        assert "timed out" in str(exc)
    else:
        raise AssertionError("Expected ProviderError")


def test_connect_retries_after_initial_timeout(monkeypatch) -> None:
    attempts = {"count": 0}

    class _FakeTcpInterface:
        def __init__(self, hostname: str, portNumber: int, timeout: int) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TimeoutError("timed out")

        def waitForConfig(self) -> None:
            return None

    monkeypatch.setattr("meshonator.providers.meshtastic.provider.TCPInterface", _FakeTcpInterface)
    provider = MeshtasticProvider()
    provider.settings.provider_connect_retries = 1

    connection = provider.connect(ProviderConnection(endpoint="tcp://172.30.105.37:4403", host="172.30.105.37", port=4403))

    assert isinstance(connection, _FakeTcpInterface)
    assert attempts["count"] == 2


def test_connect_passes_rounded_provider_timeout_to_meshtastic_interface(monkeypatch) -> None:
    observed: dict[str, int] = {}

    class _FakeTcpInterface:
        def __init__(self, hostname: str, portNumber: int, timeout: int) -> None:
            observed["timeout"] = timeout

        def waitForConfig(self) -> None:
            return None

    monkeypatch.setattr("meshonator.providers.meshtastic.provider.TCPInterface", _FakeTcpInterface)
    provider = MeshtasticProvider()
    provider.settings.provider_timeout_s = 30.0

    provider.connect(ProviderConnection(endpoint="tcp://172.30.105.37:4403", host="172.30.105.37", port=4403))

    assert observed["timeout"] == 30
