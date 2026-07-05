"""
Tests for set_projects_status tool.

Covers: not_found, already_archived, would_set_archived (dry_run),
success (execute), invalid status, unarchive round-trip.
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch, AsyncMock

import aps_mcp
from conftest import (
    FAKE_TOKEN, FAKE_APP_TOKEN,
    HUB_ID, ACCOUNT_ID, BARE_PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE,
)

BASE = aps_mcp.APS_BASE


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _admin_projects(projects: list[dict]) -> dict:
    """Envelope for _list_all_projects_admin response (bare IDs, no 'b.' prefix)."""
    return {"results": projects}


def _mock_hub_and_admin_list(projects: list[dict]) -> None:
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    respx.get(
        f"{BASE}/construction/admin/v1/accounts/{ACCOUNT_ID}/projects"
    ).mock(return_value=httpx.Response(200, json=_admin_projects(projects)))


@respx.mock
async def test_set_projects_status_not_found():
    _mock_hub_and_admin_list([])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("set_projects_status", {
            "project_names": ["Nonexistent Project"],
        })

    data = _parse(result)
    assert data["summary"]["not_found"] == 1
    assert data["results"][0]["status"] == "not_found"
    assert data["audit_file"] is None


@respx.mock
async def test_set_projects_status_already_archived_skips_patch():
    _mock_hub_and_admin_list([
        {"id": BARE_PROJECT_ID, "name": PROJECT_NAME, "status": "archived"}
    ])
    patch_route = respx.patch(
        f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/projects/{BARE_PROJECT_ID}"
    ).mock(return_value=httpx.Response(200))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("set_projects_status", {
            "project_names": [PROJECT_NAME],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["already_archived"] == 1
    assert data["results"][0]["status"] == "already_archived"
    assert not patch_route.called, "PATCH should be skipped when already archived"


@respx.mock
async def test_set_projects_status_dry_run_would_archive():
    _mock_hub_and_admin_list([
        {"id": BARE_PROJECT_ID, "name": PROJECT_NAME, "status": "active"}
    ])
    patch_route = respx.patch(
        f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/projects/{BARE_PROJECT_ID}"
    ).mock(return_value=httpx.Response(200))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("set_projects_status", {
            "project_names": [PROJECT_NAME],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["summary"]["would_set_archived"] == 1
    assert data["audit_file"] is None
    assert not patch_route.called, "dry_run must not call PATCH"


@respx.mock
async def test_set_projects_status_execute_archives_and_writes_audit(monkeypatch):
    _mock_hub_and_admin_list([
        {"id": BARE_PROJECT_ID, "name": PROJECT_NAME, "status": "active"}
    ])
    patch_route = respx.patch(
        f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/projects/{BARE_PROJECT_ID}"
    ).mock(return_value=httpx.Response(200))

    audit_calls: list[tuple] = []

    def _fake_audit(rows, operation):
        audit_calls.append((operation, list(rows)))
        return f"audit_{operation}_fake.csv"

    monkeypatch.setattr(aps_mcp, "_write_audit_csv", _fake_audit)
    monkeypatch.setattr(aps_mcp.asyncio, "sleep", AsyncMock())

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("set_projects_status", {
            "project_names": [PROJECT_NAME],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["success"] == 1
    assert data["audit_file"] == "audit_set_projects_status_fake.csv"
    assert patch_route.called
    body = json.loads(patch_route.calls[0].request.content)
    assert body == {"status": "archived"}
    assert len(audit_calls) == 1 and audit_calls[0][0] == "set_projects_status"


@respx.mock
async def test_set_projects_status_unarchive_dry_run_uses_active_status():
    _mock_hub_and_admin_list([
        {"id": BARE_PROJECT_ID, "name": PROJECT_NAME, "status": "archived"}
    ])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("set_projects_status", {
            "project_names": [PROJECT_NAME],
            "status": "active",
            "dry_run": True,
        })

    data = _parse(result)
    assert data["target_status"] == "active"
    assert data["summary"]["would_set_active"] == 1


async def test_set_projects_status_rejects_invalid_status():
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("set_projects_status", {
            "project_names": ["anything"],
            "status": "bogus",
        })

    data = _parse(result)
    assert "error" in data
    assert "bogus" in data["error"]
