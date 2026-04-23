from meshonator.providers.meshcore.provider import MeshCoreProvider
from meshonator.providers.meshtastic.provider import MeshtasticProvider


def test_meshtastic_capabilities_tcp_only():
    caps = MeshtasticProvider().capabilities().model_dump()
    assert caps["can_discover_over_tcp"] is True
    assert "can_remote_write_config" in caps


def test_meshcore_marked_experimental():
    provider = MeshCoreProvider()
    assert provider.experimental is True
    assert provider.operation_matrix().write_config.value == "unsupported_or_restricted"
