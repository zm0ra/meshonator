from meshonator.discovery.service import DiscoveryService
from meshonator.db.models import ProviderEndpointModel
from meshonator.inventory.service import InventoryService
from meshonator.providers.base import ProviderConnection
from meshonator.providers.registry import ProviderRegistry


class FakeProvider:
    name = "meshtastic"
    experimental = False

    def capabilities(self):
        return ProviderRegistry().get("meshtastic").capabilities()

    def operation_matrix(self):
        return ProviderRegistry().get("meshtastic").operation_matrix()

    def discover_endpoints(self, hosts, port=None):
        return [ProviderConnection(endpoint=f"tcp://{h}:4403", host=h, port=4403) for h in hosts]

    def health(self):
        return ProviderRegistry().get("meshtastic").health()

    def connect(self, endpoint):
        return object()

    def fetch_nodes(self, conn):
        return []

    def fetch_config(self, conn, provider_node_id):
        return {}

    def apply_config_patch(self, conn, provider_node_id, patch, dry_run):
        return {}


def test_discovery_stores_provider_endpoints(db):
    registry = ProviderRegistry()
    registry._providers["meshtastic"] = FakeProvider()
    nodes_before = len(InventoryService(db).list_nodes())
    out = DiscoveryService(db, registry).scan(provider_name="meshtastic", hosts=["10.1.1.1"], source="test")
    assert len(out) == 1
    assert out[0].endpoint.startswith("tcp://")
    endpoint = db.query(ProviderEndpointModel).filter(ProviderEndpointModel.endpoint == out[0].endpoint).one()
    assert endpoint.host == "10.1.1.1"
    assert len(InventoryService(db).list_nodes()) == nodes_before
