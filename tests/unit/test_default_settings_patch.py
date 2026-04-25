from __future__ import annotations

from meshonator.api.app import _build_meshtastic_default_settings_patch


def test_build_meshtastic_default_settings_patch_maps_requested_fields() -> None:
    patch = _build_meshtastic_default_settings_patch(
        mqtt_address="172.30.250.10",
        mqtt_username="mt",
        mqtt_password="pw",
        mqtt_root="msh/EU_868",
        mqtt_enabled="true",
        mqtt_encryption_enabled="true",
        mqtt_json_enabled="true",
        mqtt_map_reporting_enabled="true",
        primary_uplink_enabled="true",
        primary_downlink_enabled="false",
        lora_hop_limit="7",
        lora_tx_power="27",
        lora_modem_preset="MEDIUM_FAST",
        lora_region="EU_868",
        lora_tx_enabled="true",
        lora_use_preset="true",
        lora_config_ok_to_mqtt="true",
        network_rsyslog_server="172.30.103.2",
        network_ntp_server="meshtastic.pool.ntp.org",
        telemetry_device_update_interval="2147483647",
        position_broadcast_smart_minimum_distance="100",
        position_broadcast_smart_minimum_interval_secs="30",
        position_fixed_position="true",
        position_gps_mode="ENABLED",
        position_gps_update_interval="86400",
        position_broadcast_secs="43200",
        position_broadcast_smart_enabled="false",
        position_flags="3",
    )
    assert patch["module_config_patch"]["mqtt"]["address"] == "172.30.250.10"
    assert patch["module_config_patch"]["mqtt"]["enabled"] is True
    assert patch["channels_patch"][0]["settings"]["uplinkEnabled"] is True
    assert patch["channels_patch"][0]["settings"]["downlinkEnabled"] is False
    assert patch["local_config_patch"]["lora"]["configOkToMqtt"] is True
    assert patch["local_config_patch"]["network"]["rsyslogServer"] == "172.30.103.2"
    assert patch["local_config_patch"]["network"]["ntpServer"] == "meshtastic.pool.ntp.org"
    assert patch["module_config_patch"]["telemetry"]["deviceUpdateInterval"] == 2147483647
    assert patch["local_config_patch"]["position"]["positionFlags"] == 3
