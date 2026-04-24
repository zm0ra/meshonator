from meshonator.providers.utils.tcp_scan import expand_targets, tcp_probe


def test_expand_targets_hosts_and_cidr():
    result = expand_targets(["10.0.0.2"], ["10.0.0.0/30"])
    assert "10.0.0.1" in result
    assert "10.0.0.2" in result


def test_tcp_probe_returns_structured_payload():
    payload = tcp_probe("127.0.0.1", 9, timeout=0.05)
    assert "is_open" in payload
    assert "reason" in payload
    assert "latency_ms" in payload
