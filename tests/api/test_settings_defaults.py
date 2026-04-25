from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import JobModel, ManagedNodeModel, ProviderModel


def _login(client):
    r = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    return r.cookies


def test_settings_save_and_apply_defaults(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    cookies = _login(client)

    save = client.post(
        "/ui/settings/defaults/save",
        data={
            "mqtt_address": "172.30.250.10",
            "mqtt_username": "mt",
            "mqtt_password": "mt",
            "mqtt_root": "msh/EU_868",
            "mqtt_enabled": "true",
            "primary_uplink_enabled": "true",
            "primary_downlink_enabled": "false",
            "lora_config_ok_to_mqtt": "true",
            "network_rsyslog_server": "172.30.103.2",
            "network_ntp_server": "meshtastic.pool.ntp.org",
            "telemetry_device_update_interval": "2147483647",
            "position_broadcast_smart_minimum_distance": "100",
            "position_broadcast_smart_minimum_interval_secs": "30",
            "position_fixed_position": "true",
            "position_gps_mode": "ENABLED",
            "position_gps_update_interval": "86400",
            "position_broadcast_secs": "43200",
            "position_broadcast_smart_enabled": "false",
            "position_flags": "3",
        },
        cookies=cookies,
        follow_redirects=False,
    )
    assert save.status_code in (302, 303)
    provider = db.query(ProviderModel).filter(ProviderModel.name == "meshtastic").first()
    assert provider is not None
    defaults = provider.config.get("default_settings")
    assert defaults["module_config_patch"]["mqtt"]["address"] == "172.30.250.10"

    db.add(ManagedNodeModel(provider="meshtastic", provider_node_id="!settings", short_name="SET", favorite=True, reachable=True))
    db.commit()

    apply = client.post("/ui/settings/defaults/apply", data={}, cookies=cookies, follow_redirects=False)
    assert apply.status_code in (302, 303)
    job = db.query(JobModel).order_by(JobModel.created_at.desc()).first()
    assert job is not None
    assert job.job_type == "multi_node_config_patch"
    assert job.payload["base_patch"]["module_config_patch"]["mqtt"]["address"] == "172.30.250.10"
