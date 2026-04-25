from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from meshonator.api.app import (
    _compare_node_configs,
    _is_alignment_compare_path,
    _is_ignored_compare_path,
    _is_presence_only_alignment_diff,
)
from meshonator.db.models import ManagedNodeModel


def _make_node(
    *,
    provider_node_id: str,
    lora_hop_limit: int,
    telemetry_interval: int,
    latitude: float | None,
    longitude: float | None,
) -> ManagedNodeModel:
    return ManagedNodeModel(
        id=uuid4(),
        provider="meshtastic",
        provider_node_id=provider_node_id,
        short_name=provider_node_id,
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        reachable=True,
        latitude=latitude,
        longitude=longitude,
        raw_metadata={
            "preferences": {
                "lora": {"hopLimit": lora_hop_limit, "txPower": 27},
                "position": {"gpsUpdateInterval": 120},
            },
            "modulePreferences": {"telemetry": {"deviceUpdateInterval": telemetry_interval}},
            "channels": [
                {
                    "index": 0,
                    "role": "PRIMARY",
                    "settings": {"name": "mesh-main"},
                    "moduleSettings": {"positionPrecision": 13},
                }
            ],
        },
    )


def test_compare_node_configs_reports_section_differences() -> None:
    source = _make_node(
        provider_node_id="SRC",
        lora_hop_limit=5,
        telemetry_interval=300,
        latitude=53.45111,
        longitude=14.54899,
    )
    target = _make_node(
        provider_node_id="TGT",
        lora_hop_limit=7,
        telemetry_interval=120,
        latitude=53.50000,
        longitude=14.60000,
    )
    results = _compare_node_configs(source, [target], ignore_location=True)
    assert len(results) == 1
    assert results[0]["status"] == "different"
    assert "local_config.lora" in results[0]["sections"]
    assert "module_config.telemetry" in results[0]["sections"]


def test_compare_node_configs_can_ignore_location_differences() -> None:
    source = _make_node(
        provider_node_id="SRC",
        lora_hop_limit=5,
        telemetry_interval=300,
        latitude=53.45111,
        longitude=14.54899,
    )
    target = _make_node(
        provider_node_id="TGT",
        lora_hop_limit=5,
        telemetry_interval=300,
        latitude=53.50000,
        longitude=14.60000,
    )
    ignored = _compare_node_configs(source, [target], ignore_location=True)
    strict = _compare_node_configs(source, [target], ignore_location=False)
    assert ignored[0]["status"] == "same"
    assert strict[0]["status"] == "different"
    assert "location" in strict[0]["sections"]


def test_compare_path_helpers_for_alignment_and_ignored_paths() -> None:
    assert _is_alignment_compare_path("local_config.lora.hopLimit") is True
    assert _is_alignment_compare_path("channels.0.settings.downlinkEnabled") is True
    assert _is_alignment_compare_path("local_config.display.screenOnSecs") is False
    assert _is_ignored_compare_path("local_config.security.privateKey") is True
    assert _is_ignored_compare_path("module_config.ambientLighting.green") is True
    assert _is_ignored_compare_path("module_config.telemetry.deviceUpdateInterval") is False


def test_presence_only_alignment_diff_helper() -> None:
    assert _is_presence_only_alignment_diff(
        {"path": "local_config.lora.bandwidth", "source": None, "target": 250}
    ) is True
    assert _is_presence_only_alignment_diff(
        {"path": "local_config.lora.hopLimit", "source": 7, "target": 3}
    ) is False
    assert _is_presence_only_alignment_diff(
        {"path": "local_config.display.screenOnSecs", "source": None, "target": 600}
    ) is False
