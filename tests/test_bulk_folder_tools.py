"""
Integration-style tests for the 3 bulk folder tools:
bulk_list_folder_contents, bulk_create_folders, bulk_delete_folders.

All APS API calls are intercepted by respx; the auth token is patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_bulk_folder_tools.py -v
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

# Fake folder URNs
PF_ID = "urn:adsk.wipemea:fs.folder:co.projectfiles"
B704_ID = "urn:adsk.wipemea:fs.folder:co.b704"
B117_ID = "urn:adsk.wipemea:fs.folder:co.b117mock"
WIP704_ID = "urn:adsk.wipemea:fs.folder:co.wip704"
STD704_ID = "urn:adsk.wipemea:fs.folder:co.std704"
SENS704_ID = "urn:adsk.wipemea:fs.folder:co.sens704"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _folder(fid, name):
    return {"type": "folders", "id": fid, "attributes": {"name": name, "displayName": name}}


def _item(fid, name):
    return {"type": "items", "id": fid, "attributes": {
        "displayName": name, "lastModifiedTime": "2026-01-01T00:00:00Z", "createUserName": "Alice",
    }}


def _contents(items):
    return {"jsonapi": {"version": "1.0"}, "data": items, "links": {}}


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_base(router):
    """hubs, projects, topFolders('Project Files')."""
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [_folder(PF_ID, "Project Files")]}))


def _contents_route(router, fid, items):
    router.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{fid}/contents").mock(
        return_value=httpx.Response(200, json=_contents(items)))


# ===========================================================================
# bulk_list_folder_contents
# ===========================================================================

@respx.mock
async def test_bulk_list_children_of_filters_and_returns_subfolders_and_files():
    _mock_base(respx)
    # Project Files has two buildings; one is excluded by name.
    _contents_route(respx, PF_ID, [
        _folder(B704_ID, "B-B-704"),
        _folder(B117_ID, "B-B-117 (MOCK-UP)"),
    ])
    _contents_route(respx, B704_ID, [
        _folder(WIP704_ID, "0. WIP"),
        _folder(STD704_ID, "BAC CAD Standard Plans"),
        _item("urn:adsk.wipemea:fs.file:co.f1", "house_BIM-model.nwc"),
    ])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_list_folder_contents", {
            "project_name": PROJECT_NAME,
            "children_of": "Project Files",
            "include_regex": "^B-B-",
            "exclude": ["B-B-117 (MOCK-UP)"],
        })

    data = _parse(result)
    assert data["summary"]["folders_listed"] == 1
    assert data["summary"]["errors"] == 0
    row = data["results"][0]
    assert row["folder"] == "B-B-704"
    assert row["file_count"] == 1
    names = {s["name"] for s in row["subfolders"]}
    assert names == {"0. WIP", "BAC CAD Standard Plans"}
    assert row["files"][0]["name"] == "house_BIM-model.nwc"
    assert row["files"][0]["created_by"] == "Alice"


@respx.mock
async def test_bulk_list_include_files_false_omits_files():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(respx, B704_ID, [
        _folder(WIP704_ID, "0. WIP"),
        _item("urn:adsk.wipemea:fs.file:co.f1", "model.nwc"),
    ])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_list_folder_contents", {
            "project_name": PROJECT_NAME,
            "children_of": "Project Files",
            "include_files": False,
        })

    row = _parse(result)["results"][0]
    assert row["file_count"] == 1
    assert "files" not in row


# ===========================================================================
# bulk_create_folders
# ===========================================================================

@respx.mock
async def test_bulk_create_dry_run_skips_existing():
    _mock_base(respx)
    # parent path 'Project Files/B-B-704' → PF contents (find B704), then B704 children.
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(respx, B704_ID, [_folder(STD704_ID, "BAC CAD Standard Plans")])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_create_folders", {
            "project_name": PROJECT_NAME,
            "items": [
                {"parent": "Project Files/B-B-704", "name": "BAC CAD Standard Plans"},
                {"parent": "Project Files/B-B-704", "name": "Sensitive"},
            ],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["summary"] == {"created": 1, "exists": 1, "errors": 0}
    actions = {r["name"]: r["action"] for r in data["results"]}
    assert actions["BAC CAD Standard Plans"] == "would_exist"
    assert actions["Sensitive"] == "would_create"


@respx.mock
async def test_bulk_create_live_creates_missing():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(respx, B704_ID, [_folder(STD704_ID, "BAC CAD Standard Plans")])
    respx.post(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders").mock(
        return_value=httpx.Response(201, json={"data": {"id": SENS704_ID}}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_create_folders", {
            "project_name": PROJECT_NAME,
            "items": [
                {"parent": "Project Files/B-B-704", "name": "BAC CAD Standard Plans"},
                {"parent": "Project Files/B-B-704", "name": "Sensitive"},
            ],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"] == {"created": 1, "exists": 1, "errors": 0}
    created = next(r for r in data["results"] if r["name"] == "Sensitive")
    assert created["action"] == "created"
    assert created["folder_id"] == SENS704_ID


# ===========================================================================
# bulk_delete_folders
# ===========================================================================

@respx.mock
async def test_bulk_delete_skips_folder_with_files():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(respx, B704_ID, [_folder(WIP704_ID, "0. WIP")])
    # subtree check of 0. WIP finds a stuck file
    _contents_route(respx, WIP704_ID, [_item("urn:adsk.wipemea:fs.file:co.c4r", "tower_C4RModel.rvt")])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_delete_folders", {
            "project_name": PROJECT_NAME,
            "folders": ["Project Files/B-B-704/0. WIP"],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["skipped_has_files"] == 1
    assert data["summary"]["deleted"] == 0
    row = data["results"][0]
    assert row["action"] == "skipped_has_files"
    assert row["file_count"] == 1
    assert row["sample_files"] == ["tower_C4RModel.rvt"]


@respx.mock
async def test_bulk_delete_dry_run_would_delete_empty():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(respx, B704_ID, [_folder(SENS704_ID, "Sensitive")])
    _contents_route(respx, SENS704_ID, [])  # empty subtree

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_delete_folders", {
            "project_name": PROJECT_NAME,
            "folders": ["Project Files/B-B-704/Sensitive"],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["deleted"] == 1  # 'would_delete' buckets into deleted
    assert data["results"][0]["action"] == "would_delete"


@respx.mock
async def test_bulk_delete_live_soft_deletes_empty():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(respx, B704_ID, [_folder(SENS704_ID, "Sensitive")])
    _contents_route(respx, SENS704_ID, [])
    respx.patch(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{SENS704_ID}").mock(
        return_value=httpx.Response(200, json={"data": {"id": SENS704_ID}}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_delete_folders", {
            "project_name": PROJECT_NAME,
            "folders": ["Project Files/B-B-704/Sensitive"],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["deleted"] == 1
    assert data["results"][0]["action"] == "deleted"


@respx.mock
async def test_bulk_delete_missing_folder_is_not_found():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(respx, B704_ID, [_folder(SENS704_ID, "Sensitive")])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_delete_folders", {
            "project_name": PROJECT_NAME,
            "folders": ["Project Files/B-B-704/Does Not Exist"],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["not_found"] == 1
    assert data["results"][0]["action"] == "not_found"
