"""
Tests for audit_folder_naming_standards (read-only naming-convention audit) and
the _naming_standard_ids helper.

All APS API calls are intercepted by respx; the auth token is patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_naming_standards.py -v
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, HUB_ID, PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE,
)

BASE = aps_mcp.APS_BASE

pytestmark = [pytest.mark.asyncio]

PF_ID = "urn:adsk.wipemea:fs.folder:co.projectfiles"
STD_A = "3245d045-aaaa-bbbb-cccc-000000000001"
STD_B = "3245d045-aaaa-bbbb-cccc-000000000002"

SUB1_ID = "urn:adsk.wipemea:fs.folder:co.sub1"
SUB2_ID = "urn:adsk.wipemea:fs.folder:co.sub2"
SUB3_ID = "urn:adsk.wipemea:fs.folder:co.sub3"


def _folder(fid, name, standard_ids=None):
    """Folder content entry carrying namingStandardIds (omit for a folder with none)."""
    data = {}
    if standard_ids is not None:
        data["namingStandardIds"] = standard_ids
    return {
        "type": "folders",
        "id": fid,
        "attributes": {
            "name": name,
            "displayName": name,
            "extension": {"type": "folders:autodesk.bim360:Folder", "data": data},
        },
    }


def _item(fid, name):
    return {"type": "items", "id": fid, "attributes": {"displayName": name}}


def _contents(items):
    return {"jsonapi": {"version": "1.0"}, "data": items, "links": {}}


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_base(router, top_folder):
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [top_folder]}))


def _contents_route(router, fid, items):
    router.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{fid}/contents").mock(
        return_value=httpx.Response(200, json=_contents(items)))


# ---------------------------------------------------------------------------
# audit_folder_naming_standards — whole project (no `folder`)
# ---------------------------------------------------------------------------

@respx.mock
async def test_audit_whole_project_groups_and_flags_gaps():
    # Top folder enforces STD_A; one child shares it, one has none, one has STD_B.
    _mock_base(respx, _folder(PF_ID, "Project Files", [STD_A]))
    _contents_route(respx, PF_ID, [
        _folder(SUB1_ID, "With A", [STD_A]),
        _folder(SUB2_ID, "No standard"),
        _folder(SUB3_ID, "With B", [STD_B]),
    ])
    for fid in (SUB1_ID, SUB2_ID, SUB3_ID):
        _contents_route(respx, fid, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("audit_folder_naming_standards", {
            "project_name": PROJECT_NAME,
            "response_detail": "full",
        })

    data = _parse(result)
    s = data["summary"]
    assert s["total_folders"] == 4          # Project Files + 3 children
    assert s["with_standard"] == 3
    assert s["without_standard"] == 1
    assert s["by_standard"] == {STD_A: 2, STD_B: 1}

    rows = {r["path"]: r for r in data["results"]}
    assert rows["Project Files"]["naming_standard_ids"] == [STD_A]
    assert rows["Project Files/No standard"]["has_standard"] is False
    assert rows["Project Files/With B"]["naming_standard_ids"] == [STD_B]


@respx.mock
async def test_audit_changes_returns_only_gaps():
    _mock_base(respx, _folder(PF_ID, "Project Files", [STD_A]))
    _contents_route(respx, PF_ID, [
        _folder(SUB1_ID, "With A", [STD_A]),
        _folder(SUB2_ID, "No standard"),
    ])
    for fid in (SUB1_ID, SUB2_ID):
        _contents_route(respx, fid, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("audit_folder_naming_standards", {
            "project_name": PROJECT_NAME,  # default response_detail == "changes"
        })

    data = _parse(result)
    assert data["summary"]["without_standard"] == 1
    # changes drops the folders that DO have a standard.
    paths = [r["path"] for r in data["results"]]
    assert paths == ["Project Files/No standard"]


@respx.mock
async def test_audit_summary_surfaces_gaps_as_failures():
    _mock_base(respx, _folder(PF_ID, "Project Files", [STD_A]))
    _contents_route(respx, PF_ID, [_folder(SUB2_ID, "No standard")])
    _contents_route(respx, SUB2_ID, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("audit_folder_naming_standards", {
            "project_name": PROJECT_NAME,
            "response_detail": "summary",
        })

    data = _parse(result)
    assert "results" not in data
    assert [f["path"] for f in data["failures"]] == ["Project Files/No standard"]


# ---------------------------------------------------------------------------
# audit_folder_naming_standards — explicit `folder` start + max_depth
# ---------------------------------------------------------------------------

@respx.mock
async def test_audit_explicit_folder_urn_and_max_depth_zero():
    _mock_base(respx, _folder(PF_ID, "Project Files", [STD_A]))
    # Explicit-folder start GETs the folder itself for its own attributes.
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{SUB1_ID}").mock(
        return_value=httpx.Response(200, json={"data": _folder(SUB1_ID, "With A", [STD_A])}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("audit_folder_naming_standards", {
            "project_name": PROJECT_NAME,
            "folder": SUB1_ID,
            "max_depth": 0,          # audit only the start folder, no listing call
            "response_detail": "full",
        })

    data = _parse(result)
    assert data["summary"]["total_folders"] == 1
    assert data["summary"]["by_standard"] == {STD_A: 1}
    assert data["results"][0]["path"] == "With A"
