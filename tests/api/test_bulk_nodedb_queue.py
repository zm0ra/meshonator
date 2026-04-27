from __future__ import annotations

from meshonator.auth.security import bootstrap_admin
from meshonator.db.models import JobModel, ManagedNodeModel
from meshonator.groups.service import GroupsService


def _login(client):
    r = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    return r.cookies


def test_queue_fleet_nodedb_routing_favorites_job(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    source = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!src",
        short_name="SRC",
        role="ROUTER",
        reachable=True,
        raw_metadata={
            "nodesInMesh": {
                "!a1": {"hopsAway": 0, "user": {"id": "!a1", "role": "ROUTER"}},
                "!a2": {"hopsAway": 0, "user": {"id": "!a2", "role": "CLIENT_BASE"}},
                "!a3": {"hopsAway": 1, "user": {"id": "!a3", "role": "ROUTER"}},
            }
        },
    )
    target = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!dst",
        short_name="DST",
        role="CLIENT",
        reachable=True,
        raw_metadata={"nodesInMesh": {}},
    )
    db.add(source)
    db.add(target)
    db.commit()

    cookies = _login(client)
    response = client.post(
        "/ui/nodes/nodedb/routing-favorites",
        data={
            "action": "set_favorite",
            "allowed_roles_text": "ROUTER\nROUTER_LATE\nCLIENT_BASE",
            "source_max_hops": "0",
        },
        cookies=cookies,
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"].startswith("/nodes?message=")

    job = db.query(JobModel).order_by(JobModel.created_at.desc()).first()
    assert job is not None
    assert job.job_type == "bulk_nodedb_mutation"
    assert set(job.payload["target_node_ids"]) == {"!a1", "!a2"}
    queued_destinations = set(job.payload["destination_node_ids"])
    assert {str(source.id), str(target.id)}.issubset(queued_destinations)


def test_queue_group_nodedb_routing_favorites_job(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    n1 = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!g1",
        short_name="G1",
        role="ROUTER",
        reachable=True,
        raw_metadata={"nodesInMesh": {"!r1": {"hopsAway": 0, "user": {"id": "!r1", "role": "ROUTER"}}}},
    )
    n2 = ManagedNodeModel(
        provider="meshtastic",
        provider_node_id="!g2",
        short_name="G2",
        role="ROUTER_LATE",
        reachable=True,
        raw_metadata={"nodesInMesh": {"!r2": {"hopsAway": 0, "user": {"id": "!r2", "role": "CLIENT_BASE"}}}},
    )
    db.add(n1)
    db.add(n2)
    db.commit()

    groups = GroupsService(db)
    group = groups.create_group(
        name="grp-nodedb",
        description=None,
        dynamic_filter={"provider": "__none__"},
        desired_config_template={},
    )
    groups.assign_node(group.id, n1.id)
    groups.assign_node(group.id, n2.id)

    cookies = _login(client)
    response = client.post(
        f"/ui/groups/{group.id}/nodedb/routing-favorites",
        data={
            "action": "set_favorite",
            "allowed_roles_text": "ROUTER\nROUTER_LATE\nCLIENT_BASE",
            "source_max_hops": "0",
        },
        cookies=cookies,
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"].startswith("/groups?message=")

    job = db.query(JobModel).order_by(JobModel.created_at.desc()).first()
    assert job is not None
    assert job.job_type == "bulk_nodedb_mutation"
    assert set(job.payload["destination_node_ids"]) == {str(n1.id), str(n2.id)}
    assert set(job.payload["target_node_ids"]) == {"!r1", "!r2"}
