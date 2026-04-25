from __future__ import annotations

from meshonator.jobs.queue import _sanitize_clone_local_config, _spread_location


def test_sanitize_clone_local_config_removes_security_and_role_when_disabled() -> None:
    source = {
        "device": {"role": "ROUTER", "nodeInfoBroadcastSecs": 10800},
        "security": {"privateKey": "secret", "publicKey": "pub"},
        "position": {"gpsUpdateInterval": 120},
    }
    out = _sanitize_clone_local_config(source, include_role=False)
    assert "security" not in out
    assert out["device"].get("role") is None
    assert out["position"]["gpsUpdateInterval"] == 120


def test_sanitize_clone_local_config_keeps_role_when_enabled() -> None:
    source = {"device": {"role": "CLIENT_BASE", "nodeInfoBroadcastSecs": 10800}}
    out = _sanitize_clone_local_config(source, include_role=True)
    assert out["device"]["role"] == "CLIENT_BASE"


def test_spread_location_creates_distinct_points() -> None:
    center_lat = 53.4511616
    center_lon = 14.548992
    points = [
        _spread_location(index=i, total=4, center_lat=center_lat, center_lon=center_lon, step_m=50)
        for i in range(4)
    ]
    unique_points = {(round(lat, 7), round(lon, 7)) for lat, lon in points}
    assert len(unique_points) == 4
