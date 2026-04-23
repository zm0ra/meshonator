from meshonator.providers.utils.tcp_scan import expand_targets


def test_expand_targets_hosts_and_cidr():
    result = expand_targets(["10.0.0.2"], ["10.0.0.0/30"])
    assert "10.0.0.1" in result
    assert "10.0.0.2" in result
