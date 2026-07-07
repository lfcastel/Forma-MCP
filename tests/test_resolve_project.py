"""
Integration-style tests for the resolve_project tool and the project param
retrofit on the single-project tools.

All APS API calls are intercepted by respx; both auth helpers are patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_resolve_project.py -v
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, FAKE_APP_TOKEN,
    HUB_ID, PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE,
)

BASE = aps_mcp.APS_BASE

pytestmark = [pytest.mark.asyncio]

# ---------------------------------------------------------------------------
# Local, clearly-fake UUID-shaped fixtures. conftest's IDs ("b.test-proj-id")
# are NOT valid UUIDs, so _extract_uuid won't fire on them — the ID/URL path
# needs real UUID shapes here. These are placeholders, never real ACC IDs.
# ---------------------------------------------------------------------------

PROJ_UUID = "00000000-0000-0000-0000-000000000001"
HUB_UUID = "00000000-0000-0000-0000-0000000000aa"
UNKNOWN_UUID = "00000000-0000-0000-0000-0000000000ff"

ADMIN_HUB_ID = f"b.{HUB_UUID}"
ADMIN_HUB_NAME = "BAC EU Hub (test)"
ADMIN_PROJECT_NAME = "Test (BAC)"

ADMIN_PROJECT_RESPONSE = {
    "id": PROJ_UUID,
    "accountId": HUB_UUID,
    "name": ADMIN_PROJECT_NAME,
    "platform": "acc",
}

ADMIN_HUBS_RESPONSE = {
    "data": [{
        "id": ADMIN_HUB_ID,
        "attributes": {"name": ADMIN_HUB_NAME, "region": "EMEA",
                       "hubType": "autodesk.bim360:Account"},
    }]
}

ACC_URL = f"https://acc.autodesk.eu/docs/files/projects/{PROJ_UUID}"


def _parse(result):
    """Unwrap TextContent list → dict."""
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _auth():
    return (
        patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN),
        patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN),
    )


async def _call(name, args):
    p1, p2 = _auth()
    with p1, p2:
        return await aps_mcp.call_tool(name, args)


def _mock_admin_id(router: respx.MockRouter, uuid=PROJ_UUID, status=200,
                   body=ADMIN_PROJECT_RESPONSE):
    """Admin-API by-id route + the hub lookup that maps accountId → hub name."""
    admin = router.get(
        f"{BASE}/construction/admin/v1/projects/{uuid}"
    ).mock(return_value=httpx.Response(status, json=body))
    router.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=ADMIN_HUBS_RESPONSE)
    )
    return admin


# ===========================================================================
# 1. URL → Admin fast path
# ===========================================================================

@respx.mock
async def test_resolve_by_url():
    admin = _mock_admin_id(respx)

    data = _parse(await _call("resolve_project", {"query": ACC_URL}))

    assert data["project_id"] == f"b.{PROJ_UUID}"
    assert data["project_name"] == ADMIN_PROJECT_NAME
    assert data["hub_id"] == ADMIN_HUB_ID
    assert data["hub_name"] == ADMIN_HUB_NAME
    assert data["region"] == "EMEA"
    assert data["platform"] == "acc"
    assert data["matched_by"] == "url"

    # One Admin call, auto-routed: no Region header, trimmed fields param.
    req = admin.calls.last.request
    assert "region" not in {k.lower() for k in req.headers.keys()}
    assert req.url.params.get("fields") == "accountId,name,platform"


# ===========================================================================
# 2. Bare b.<uuid> → Admin fast path, matched_by="id"
# ===========================================================================

@respx.mock
async def test_resolve_by_bare_id():
    _mock_admin_id(respx)

    data = _parse(await _call("resolve_project", {"query": f"b.{PROJ_UUID}"}))

    assert data["project_id"] == f"b.{PROJ_UUID}"
    assert data["hub_id"] == ADMIN_HUB_ID
    assert data["matched_by"] == "id"


# ===========================================================================
# 3. Name partial → fan-out-by-name
# ===========================================================================

@respx.mock
async def test_resolve_by_name():
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )

    data = _parse(await _call("resolve_project", {"query": "Test Pro"}))

    assert data["project_id"] == PROJECT_ID
    assert data["project_name"] == PROJECT_NAME
    assert data["hub_id"] == HUB_ID
    assert data["matched_by"] == "name"


# ===========================================================================
# 4. Ambiguous name → candidates, no silent guess
# ===========================================================================

AMB_HUBS = {"data": [{"id": "b.hub-amb",
                      "attributes": {"name": "Amb Hub", "region": "EMEA"}}]}
AMB_PROJECTS = {
    "data": [
        {"id": "b.p1", "attributes": {"name": "Test Alpha"}},
        {"id": "b.p2", "attributes": {"name": "Test Beta"}},
    ],
    "meta": {"pagination": {"totalResults": 2}},
}


@respx.mock
async def test_resolve_ambiguous_name():
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=AMB_HUBS)
    )
    respx.get(f"{BASE}/project/v1/hubs/b.hub-amb/projects").mock(
        return_value=httpx.Response(200, json=AMB_PROJECTS)
    )

    data = _parse(await _call("resolve_project", {"query": "Test"}))

    assert data["ambiguous"] is True
    assert len(data["candidates"]) == 2
    assert {c["project_id"] for c in data["candidates"]} == {"b.p1", "b.p2"}


# ===========================================================================
# 5. Unknown ID → clean not_found (single 404, no region retry)
# ===========================================================================

@respx.mock
async def test_resolve_unknown_id():
    _mock_admin_id(respx, uuid=UNKNOWN_UUID, status=404, body={})

    data = _parse(await _call("resolve_project", {"query": f"b.{UNKNOWN_UUID}"}))

    assert data["error"] == "not_found"
    assert UNKNOWN_UUID in data["message"]


# ===========================================================================
# 6. Retrofit smoke — a single-project tool accepts a URL via `project`
# ===========================================================================

TOP_FOLDERS_RESPONSE = {
    "data": [{"id": "urn:adsk.wipprod:fs.folder:co.pf", "type": "folders",
              "attributes": {"name": "Project Files", "displayName": "Project Files"}}]
}


@respx.mock
async def test_project_param_url_on_list_top_folders():
    _mock_admin_id(respx)
    top = respx.get(
        f"{BASE}/project/v1/hubs/{ADMIN_HUB_ID}/projects/b.{PROJ_UUID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=TOP_FOLDERS_RESPONSE))

    data = _parse(await _call("list_top_folders", {"project": ACC_URL}))

    assert top.called
    assert data["project"] == ADMIN_PROJECT_NAME
    assert any(f["name"] == "Project Files" for f in data["top_folders"])


# ===========================================================================
# 7. Backward compat — project_name alias still resolves by name
# ===========================================================================

@respx.mock
async def test_project_name_alias_still_works():
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=TOP_FOLDERS_RESPONSE))

    data = _parse(await _call("list_top_folders", {"project_name": PROJECT_NAME}))

    assert data["project"] == PROJECT_NAME
    assert data["top_folders"][0]["name"] == "Project Files"
