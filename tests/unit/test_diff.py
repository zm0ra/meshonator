from uuid import uuid4

from meshonator.operations.service import OperationsService
from meshonator.providers.registry import ProviderRegistry


def test_diff_unknown_when_not_enough_snapshots(db):
    svc = OperationsService(db, ProviderRegistry())
    out = svc.diff_latest_configs(node_id=uuid4(), config_type="provider_config")
    assert out["status"] == "unknown"
