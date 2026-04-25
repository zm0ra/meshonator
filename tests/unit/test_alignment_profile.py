from __future__ import annotations

from meshonator.api.app import _build_alignment_local_patch, _build_alignment_module_patch


def test_build_alignment_local_patch_selects_only_configured_paths() -> None:
    raw = {
        "preferences": {
            "lora": {"hopLimit": 7, "ignoreMqtt": True, "spreadFactor": 11, "bandwidth": 250, "codingRate": 5, "txPower": 27},
            "device": {"tzdef": "CET-1CEST", "rebroadcastMode": "CORE_PORTNUMS_ONLY", "role": "ROUTER", "nodeInfoBroadcastSecs": 10800},
            "network": {"ntpServer": "meshtastic.pool.ntp.org", "enabledProtocols": 1, "wifiEnabled": True},
            "position": {"broadcastSmartMinimumIntervalSecs": 30, "gpsMode": "ENABLED", "gpsUpdateInterval": 86400, "positionBroadcastSecs": 43200},
            "security": {"privateKey": "secret", "publicKey": "pub"},
        }
    }
    out = _build_alignment_local_patch(raw)
    assert out["lora"]["hopLimit"] == 7
    assert out["device"]["tzdef"] == "CET-1CEST"
    assert out["network"]["ntpServer"] == "meshtastic.pool.ntp.org"
    assert out["position"]["positionBroadcastSecs"] == 43200
    assert "txPower" not in out.get("lora", {})
    assert "security" not in out


def test_build_alignment_module_patch_omits_ambient_blue_and_green() -> None:
    raw = {
        "modulePreferences": {
            "ambientLighting": {"blue": 10, "green": 20, "red": 30},
            "mqtt": {"enabled": True, "address": "172.30.250.10"},
        }
    }
    out = _build_alignment_module_patch(raw)
    assert out["ambientLighting"].get("blue") is None
    assert out["ambientLighting"].get("green") is None
    assert out["ambientLighting"].get("red") == 30
    assert out["mqtt"]["enabled"] is True
