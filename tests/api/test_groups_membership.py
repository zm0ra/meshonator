from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from meshonator.auth.security import bootstrap_admin
from meshonator.groups.service import GroupsService
from meshonator.db.models import ManagedNodeModel


def _login(client):
    r = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    return r.cookies


def test_group_assign_selected_preserves_selection_context(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    n1 = ManagedNodeModel(provider="meshtastic", provider_node_id="!ga1", short_name="GA1", reachable=True)
    n2 = ManagedNodeModel(provider="meshtastic", provider_node_id="!ga2", short_name="GA2", reachable=True)
    db.add(n1)
    db.add(n2)
    db.commit()
    group = GroupsService(db).create_group("assign-selected-group", None, {}, {})

    cookies = _login(client)
    response = client.post(
        f"/ui/groups/{group.id}/assign-selected",
        data={"selected_node_ids": [str(n1.id), str(n2.id)]},
        cookies=cookies,
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    location = response.headers["location"]
    assert location.startswith("/groups?")
    params = parse_qs(urlparse(location).query)
    assert params["message"] == ["Assigned 2 selected nodes to group"]
    assert params["selected_node_ids"] == [str(n1.id), str(n2.id)]

    assigned_ids = {str(node.id) for node in GroupsService(db).list_assigned_members(group.id)}
    assert assigned_ids == {str(n1.id), str(n2.id)}


def test_group_remove_selected_removes_manual_members(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    n1 = ManagedNodeModel(provider="meshtastic", provider_node_id="!gr1", short_name="GR1", reachable=True)
    n2 = ManagedNodeModel(provider="meshtastic", provider_node_id="!gr2", short_name="GR2", reachable=True)
    db.add(n1)
    db.add(n2)
    db.commit()

    groups = GroupsService(db)
    group = groups.create_group("remove-selected-group", None, {}, {})
    groups.assign_nodes(group.id, [n1.id, n2.id])

    cookies = _login(client)
    response = client.post(
        f"/ui/groups/{group.id}/remove-selected",
        data={"selected_node_ids": [str(n1.id), str(n2.id)]},
        cookies=cookies,
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    location = response.headers["location"]
    assert location.startswith("/groups?")
    params = parse_qs(urlparse(location).query)
    assert params["message"] == ["Removed 2 selected nodes from group"]
    assert params["selected_node_ids"] == [str(n1.id), str(n2.id)]

    assert GroupsService(db).list_assigned_members(group.id) == []


def test_manual_only_group_does_not_resolve_all_nodes(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    manual_member = ManagedNodeModel(provider="meshtastic", provider_node_id="!gm1", short_name="GM1", reachable=True)
    other_node = ManagedNodeModel(provider="meshtastic", provider_node_id="!gm2", short_name="GM2", reachable=True)
    db.add(manual_member)
    db.add(other_node)
    db.commit()

    groups = GroupsService(db)
    group = groups.create_group("manual-only-group", None, {}, {})
    groups.assign_node(group.id, manual_member.id)

    resolved_ids = {str(node.id) for node in groups.resolve_all_members(group)}
    assert resolved_ids == {str(manual_member.id)}