from __future__ import annotations

from meshonator.domain.models import ConfigPatch
from meshonator.providers.meshtastic.provider import MeshtasticProvider


class _RoleEnum:
    @staticmethod
    def Value(name: str) -> int:
        mapping = {"PRIMARY": 1, "SECONDARY": 2, "DISABLED": 0}
        return mapping[name]


class _Channel:
    Role = _RoleEnum

    def __init__(self, index: int) -> None:
        self.index = index
        self.role = 1
        self.settings = type("Settings", (), {"name": "", "psk": b""})()
        self.module_settings = type(
            "ModuleSettings",
            (),
            {"position_precision": 0, "positionPrecision": 0, "is_muted": False, "isMuted": False},
        )()


class _Message:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeLocalNode:
    def __init__(self) -> None:
        self.localConfig = _Message(
            device=_Message(role="CLIENT"),
            position=_Message(
                positionBroadcastSecs=900,
                gpsUpdateInterval=120,
                gpsEnabled=False,
                fixedPosition=False,
            ),
            lora=_Message(hopLimit=7, txPower=27),
        )
        self.moduleConfig = _Message(
            telemetry=_Message(deviceUpdateInterval=0, deviceTelemetryEnabled=False),
        )
        self.channels = [_Channel(0)]
        self.written_sections: list[str] = []
        self.written_channels: list[int] = []
        self.owner_calls: list[dict] = []
        self.fixed_position_calls: list[dict] = []

    def writeConfig(self, section: str) -> None:
        self.written_sections.append(section)

    def writeChannel(self, index: int) -> None:
        self.written_channels.append(index)

    def setOwner(self, long_name=None, short_name=None):
        self.owner_calls.append({"long_name": long_name, "short_name": short_name})

    def setFixedPosition(self, lat: float, lon: float, alt=None):
        self.fixed_position_calls.append({"lat": lat, "lon": lon, "alt": alt})


class _FakeConn:
    def __init__(self) -> None:
        self.localNode = _FakeLocalNode()

    def waitForAckNak(self) -> None:
        return None


class _StrictAltitudeLocalNode(_FakeLocalNode):
    def setFixedPosition(self, lat: float, lon: float, alt=None):
        if alt is None:
            raise TypeError("'NoneType' object cannot be interpreted as an integer")
        super().setFixedPosition(lat=lat, lon=lon, alt=alt)


class _StrictAltitudeConn:
    def __init__(self) -> None:
        self.localNode = _StrictAltitudeLocalNode()

    def waitForAckNak(self) -> None:
        return None


class _FavoriteLocalNode:
    def __init__(self) -> None:
        self.favorite_calls: list[tuple[str, str]] = []

    def setFavorite(self, node_id: str) -> None:
        self.favorite_calls.append(("set", node_id))

    def removeFavorite(self, node_id: str) -> None:
        self.favorite_calls.append(("remove", node_id))

    def removeNode(self, node_id: str) -> None:
        self.favorite_calls.append(("delete", node_id))


class _FavoriteConn:
    def __init__(self) -> None:
        self.localNode = _FavoriteLocalNode()
        self.wait_for_ack_calls = 0

    def waitForAckNak(self) -> None:
        self.wait_for_ack_calls += 1


class _AckTimeoutConn(_FavoriteConn):
    def waitForAckNak(self) -> None:
        self.wait_for_ack_calls += 1
        raise RuntimeError("Timed out waiting for an acknowledgment")


def test_apply_patch_writes_local_module_and_channel_sections() -> None:
    provider = MeshtasticProvider()
    conn = _FakeConn()
    patch = ConfigPatch(
        short_name="ABCD",
        long_name="Alpha",
        latitude=53.1,
        longitude=14.5,
        altitude=10,
        local_config_patch={
            "position": {"positionBroadcastSecs": 300, "gpsEnabled": True},
            "lora": {"hopLimit": 5, "txPower": 20},
            "device": {"role": "ROUTER"},
        },
        module_config_patch={"telemetry": {"deviceUpdateInterval": 180, "deviceTelemetryEnabled": True}},
        channels_patch=[{"index": 0, "settings": {"name": "mesh-main"}, "moduleSettings": {"positionPrecision": 13}}],
    )

    result = provider.apply_config_patch(conn, provider_node_id="!a1", patch=patch, dry_run=False)

    assert result["mode"] == "apply"
    assert conn.localNode.owner_calls
    assert conn.localNode.fixed_position_calls
    assert set(conn.localNode.written_sections) == {"position", "lora", "device", "telemetry"}
    assert conn.localNode.written_channels == [0]
    assert conn.localNode.localConfig.position.positionBroadcastSecs == 300
    assert conn.localNode.localConfig.position.gpsEnabled is True
    assert conn.localNode.localConfig.lora.hopLimit == 5
    assert conn.localNode.moduleConfig.telemetry.deviceTelemetryEnabled is True
    assert conn.localNode.channels[0].settings.name == "mesh-main"
    assert conn.localNode.channels[0].module_settings.positionPrecision == 13


def test_apply_patch_location_without_altitude_does_not_fail() -> None:
    provider = MeshtasticProvider()
    conn = _StrictAltitudeConn()
    patch = ConfigPatch(
        latitude=53.12,
        longitude=14.56,
        altitude=None,
    )

    result = provider.apply_config_patch(conn, provider_node_id="!a1", patch=patch, dry_run=False)

    assert result["mode"] == "apply"
    assert result["applied"]["position"]["altitude"] is None
    assert conn.localNode.fixed_position_calls[-1]["alt"] == 0


def test_mutate_node_db_waits_for_ack() -> None:
    provider = MeshtasticProvider()
    conn = _FavoriteConn()

    result = provider.mutate_node_db(
        conn=conn,
        destination_node_id="!6911afb4",
        action="set_favorite",
        target_node_id="!d4a71330",
        dry_run=False,
    )

    assert result["mode"] == "apply"
    assert conn.localNode.favorite_calls == [("set", "!d4a71330")]
    assert conn.wait_for_ack_calls == 1


def test_mutate_node_db_wraps_ack_errors() -> None:
    provider = MeshtasticProvider()
    conn = _AckTimeoutConn()

    try:
        provider.mutate_node_db(
            conn=conn,
            destination_node_id="!6911afb4",
            action="set_favorite",
            target_node_id="!d4a71330",
            dry_run=False,
        )
    except Exception as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected ProviderError")

    assert "Meshtastic NodeDB action set_favorite" in message
    assert "!6911afb4 -> !d4a71330" in message
    assert "Timed out waiting for an acknowledgment" in message
    assert conn.localNode.favorite_calls == [("set", "!d4a71330")]
    assert conn.wait_for_ack_calls == 1
