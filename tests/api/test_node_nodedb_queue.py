from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import JobModel, ManagedNodeModel


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert response.status_code in (302, 303)
    return response.cookies


def test_queue_zero_hop_nodedb_mutation_job(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    node = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!source",
        short_name="SRC",
        reachable=True,
        raw_metadata={
            "nodesInMesh": {
                "!a1f9eab4": {"hopsAway": 0, "user": {"id": "!a1f9eab4", "role": "ROUTER"}},
                "!53047302": {"hopsAway": 0, "user": {"id": "!53047302", "role": "CLIENT_BASE"}},
            }
        },
    )
    db.add(node)
    db.commit()

    cookies = _login(client)
    response = client.post(
        f"/ui/nodes/{node.id}/zero-hop/nodedb",
        data={
            "action": "set_favorite",
            "max_hops": "0",
            "allowed_roles_text": "ROUTER\nCLIENT_BASE",
            "selected_candidate_ids_text": "",
        },
        cookies=cookies,
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"].startswith(f"/nodes/{node.id}?message=")

    job = db.query(JobModel).order_by(JobModel.created_at.desc()).first()
    assert job is not None
    assert job.job_type == "node_nodedb_mutation"
    assert job.payload["action"] == "set_favorite"
    assert set(job.payload["target_node_ids"]) == {"!a1f9eab4", "!53047302"}
