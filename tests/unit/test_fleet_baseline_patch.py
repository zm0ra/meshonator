from __future__ import annotations

from meshonator.api.app import _build_fleet_baseline_patch


def test_build_fleet_baseline_patch_maps_mqtt_telemetry_channel_and_rsyslog() -> None:
    local_patch, module_patch, channels_patch = _build_fleet_baseline_patch(
        primary_position_precision="13",
        primary_uplink_enabled="true",
        primary_downlink_enabled="false",
        telemetry_device_update_interval="300",
        telemetry_environment_update_interval="600",
        telemetry_air_quality_interval="900",
        telemetry_power_update_interval="1200",
        telemetry_health_update_interval="1800",
        telemetry_device_enabled="true",
        telemetry_environment_enabled="true",
        telemetry_air_quality_enabled="false",
        telemetry_power_enabled="true",
        telemetry_health_enabled="false",
        mqtt_address="172.30.250.10",
        mqtt_username="mt",
        mqtt_password="secret",
        mqtt_root="msh/EU_868",
        mqtt_enabled="true",
        mqtt_encryption_enabled="true",
        mqtt_json_enabled="true",
        mqtt_map_reporting_enabled="true",
        mqtt_proxy_to_client_enabled="false",
        mqtt_tls_enabled="false",
        network_rsyslog_server="172.30.103.2",
        network_enabled_protocols="1",
    )

    assert local_patch == {"network": {"rsyslogServer": "172.30.103.2", "enabledProtocols": 1}}
    assert module_patch["mqtt"] == {
        "address": "172.30.250.10",
        "username": "mt",
        "password": "secret",
        "root": "msh/EU_868",
        "enabled": True,
        "encryptionEnabled": True,
        "jsonEnabled": True,
        "mapReportingEnabled": True,
        "proxyToClientEnabled": False,
        "tlsEnabled": False,
    }
    assert module_patch["telemetry"] == {
        "deviceUpdateInterval": 300,
        "environmentUpdateInterval": 600,
        "airQualityInterval": 900,
        "powerUpdateInterval": 1200,
        "healthUpdateInterval": 1800,
        "deviceTelemetryEnabled": True,
        "environmentMeasurementEnabled": True,
        "airQualityEnabled": False,
        "powerMeasurementEnabled": True,
        "healthMeasurementEnabled": False,
    }
    assert channels_patch == [
        {
            "index": 0,
            "settings": {"uplinkEnabled": True, "downlinkEnabled": False},
            "moduleSettings": {"positionPrecision": 13},
        }
    ]


def test_build_fleet_baseline_patch_keep_values_produces_empty_patches() -> None:
    local_patch, module_patch, channels_patch = _build_fleet_baseline_patch(
        primary_position_precision="",
        primary_uplink_enabled="keep",
        primary_downlink_enabled="keep",
        telemetry_device_update_interval="",
        telemetry_environment_update_interval="",
        telemetry_air_quality_interval="",
        telemetry_power_update_interval="",
        telemetry_health_update_interval="",
        telemetry_device_enabled="keep",
        telemetry_environment_enabled="keep",
        telemetry_air_quality_enabled="keep",
        telemetry_power_enabled="keep",
        telemetry_health_enabled="keep",
        mqtt_address="",
        mqtt_username="",
        mqtt_password="",
        mqtt_root="",
        mqtt_enabled="keep",
        mqtt_encryption_enabled="keep",
        mqtt_json_enabled="keep",
        mqtt_map_reporting_enabled="keep",
        mqtt_proxy_to_client_enabled="keep",
        mqtt_tls_enabled="keep",
        network_rsyslog_server="",
        network_enabled_protocols="",
    )

    assert local_patch == {}
    assert module_patch == {}
    assert channels_patch == []
