from __future__ import annotations

from meshonator.providers.meshtastic.provider import MeshtasticProvider


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
