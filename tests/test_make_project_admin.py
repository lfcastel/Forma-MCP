"""
Tests for make_project_admin tool.

Covers per-project states: already_admin, would_update/updated (existing member),
would_add/added (new member), plus not-found projects and 'all active' scope.
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, FAKE_APP_TOKEN,
    HUB_ID, ACCOUNT_ID, PROJECT_ID, BARE_PROJECT_ID, PROJECT_NAME,
    USER_A_EMAIL, USER_A_ID, USER_B_EMAIL,
    HUB_RESPONSE, PROJECTS_RESPONSE,
)

BASE = aps_mcp.APS_BASE


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _members(members: list[dict]) -> dict:
    return {"results": members}


def _mock_hub_projects_and_members(members: list[dict]) -> None:
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )
    respx.get(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users"
    ).mock(return_value=httpx.Response(200, json=_members(members)))


@respx.mock
async def test_make_project_admin_already_admin_is_skipped():
    _mock_hub_projects_and_members([{
        "id": USER_A_ID, "email": USER_A_EMAIL,
        "accessLevels": ["projectAdmin"],
    }])
    post_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(200, json=[]))
    patch_route = respx.patch(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(200))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("make_project_admin", {
            "user_email": USER_A_EMAIL,
            "project_names": [PROJECT_NAME],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["already_admin"] == 1
    assert not post_route.called
    assert not patch_route.called


@respx.mock
async def test_make_project_admin_existing_member_dry_run_would_update():
    _mock_hub_projects_and_members([{
        "id": USER_A_ID, "email": USER_A_EMAIL,
        "accessLevels": ["executive"],
    }])
    patch_route = respx.patch(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(200))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("make_project_admin", {
            "user_email": USER_A_EMAIL,
            "project_names": [PROJECT_NAME],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["summary"]["would_update"] == 1
    assert data["audit_file"] is None
    assert not patch_route.called


@respx.mock
async def test_make_project_admin_existing_member_execute_patches_access_levels(monkeypatch):
    _mock_hub_projects_and_members([{
        "id": USER_A_ID, "email": USER_A_EMAIL,
        "accessLevels": ["executive"],
    }])
    patch_route = respx.patch(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(200))

    monkeypatch.setattr(
        aps_mcp, "_write_audit_csv",
        lambda rows, op: f"audit_{op}_fake.csv",
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("make_project_admin", {
            "user_email": USER_A_EMAIL,
            "project_names": [PROJECT_NAME],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["updated"] == 1
    assert patch_route.called
    body = json.loads(patch_route.calls[0].request.content)
    assert body == {"accessLevels": ["projectAdmin"]}
    assert data["audit_file"] == "audit_make_project_admin_fake.csv"


@respx.mock
async def test_make_project_admin_not_a_member_dry_run_would_add():
    _mock_hub_projects_and_members([{
        "id": USER_A_ID, "email": USER_A_EMAIL,
        "accessLevels": ["executive"],
    }])
    post_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(200, json=[]))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("make_project_admin", {
            "user_email": USER_B_EMAIL,
            "project_names": [PROJECT_NAME],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["would_add"] == 1
    assert not post_route.called


@respx.mock
async def test_make_project_admin_not_a_member_execute_posts_users_import(monkeypatch):
    _mock_hub_projects_and_members([])
    post_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(200, json=[
        {"email": USER_B_EMAIL, "success": True},
    ]))

    monkeypatch.setattr(
        aps_mcp, "_write_audit_csv",
        lambda rows, op: f"audit_{op}_fake.csv",
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("make_project_admin", {
            "user_email": USER_B_EMAIL,
            "project_names": [PROJECT_NAME],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["added"] == 1
    assert post_route.called
    body = json.loads(post_route.calls[0].request.content)
    assert body["users"][0]["accessLevels"] == ["projectAdmin"]
    assert body["users"][0]["email"] == USER_B_EMAIL


@respx.mock
async def test_make_project_admin_unknown_project_returns_error():
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("make_project_admin", {
            "user_email": USER_A_EMAIL,
            "project_names": ["Definitely Not A Project"],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["error"] == 1
    assert data["results"][0]["status"] == "error"


@respx.mock
async def test_make_project_admin_all_active_scope_uses_hub_project_list():
    """When project_names is omitted, iterate every active project in the hub."""
    _mock_hub_projects_and_members([{
        "id": USER_A_ID, "email": USER_A_EMAIL,
        "accessLevels": ["projectAdmin"],
    }])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("make_project_admin", {
            "user_email": USER_A_EMAIL,
            "dry_run": True,
        })

    data = _parse(result)
    assert data["scope"] == "all_active"
    assert data["summary"]["already_admin"] == 1
