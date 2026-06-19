"""
Integration-style tests for the 2 bulk move tools:
bulk_move_files and bulk_move_folders.

All APS API calls are intercepted by respx; the auth token is patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_bulk_move_tools.py -v
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
B704_ID = "urn:adsk.wipemea:fs.folder:co.b704"
WIP704_ID = "urn:adsk.wipemea:fs.folder:co.wip704"
USERA_ID = "urn:adsk.wipemea:fs.folder:co.usera"
ARCHIVE_ID = "urn:adsk.wipemea:fs.folder:co.archive"
FILE_ID = "urn:adsk.wipemea:fs.file:co.model1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _folder(fid, name):
    return {"type": "folders", "id": fid, "attributes": {"name": name, "displayName": name}}


def _item(fid, name):
    return {"type": "items", "id": fid, "attributes": {"displayName": name}}


def _contents(items):
    return {"jsonapi": {"version": "1.0"}, "data": items, "links": {}}


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_base(router):
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [_folder(PF_ID, "Project Files")]}))


def _contents_route(router, fid, items):
    router.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{fid}/contents").mock(
        return_value=httpx.Response(200, json=_contents(items)))


# ===========================================================================
# bulk_move_files
# ===========================================================================

def _mock_buildings(router):
    """Project Files → B-B-704 → {0. WIP, User A}."""
    _mock_base(router)
    _contents_route(router, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(router, B704_ID, [_folder(WIP704_ID, "0. WIP"), _folder(USERA_ID, "User A")])


@respx.mock
async def test_bulk_move_files_dry_run_would_move():
    _mock_buildings(respx)
    _contents_route(respx, WIP704_ID, [_item(FILE_ID, "model.nwc")])
    _contents_route(respx, USERA_ID, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME,
            "items": [{
                "source": "Project Files/B-B-704/0. WIP",
                "name": "model.nwc",
                "destination": "Project Files/B-B-704/User A",
            }],
            "dry_run": True,
            "response_detail": "full",
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["summary"]["moved"] == 1
    row = data["results"][0]
    assert row["action"] == "would_move"
    assert row["item_id"] == FILE_ID


@respx.mock
async def test_bulk_move_files_live_moves():
    _mock_buildings(respx)
    _contents_route(respx, WIP704_ID, [_item(FILE_ID, "model.nwc")])
    _contents_route(respx, USERA_ID, [])
    respx.patch(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{FILE_ID}").mock(
        return_value=httpx.Response(200, json={"data": {"id": FILE_ID}}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME,
            "items": [{"source": "Project Files/B-B-704/0. WIP", "name": "model.nwc",
                       "destination": "Project Files/B-B-704/User A"}],
            "dry_run": False,
            "response_detail": "full",
        })

    data = _parse(result)
    assert data["summary"]["moved"] == 1
    assert data["results"][0]["action"] == "moved"


@respx.mock
async def test_bulk_move_files_c4r_403_is_skipped_unmovable():
    _mock_buildings(respx)
    _contents_route(respx, WIP704_ID, [_item(FILE_ID, "tower_C4RModel.rvt")])
    _contents_route(respx, USERA_ID, [])
    respx.patch(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{FILE_ID}").mock(
        return_value=httpx.Response(403, json={"errors": [{"detail": "forbidden"}]}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME,
            "items": [{"source": "Project Files/B-B-704/0. WIP", "name": "tower_C4RModel.rvt",
                       "destination": "Project Files/B-B-704/User A"}],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["skipped_unmovable"] == 1
    assert data["summary"]["moved"] == 0
    assert data["results"][0]["action"] == "skipped_unmovable"


@respx.mock
async def test_bulk_move_files_already_there_is_idempotent():
    _mock_buildings(respx)
    # File is already in the destination (a re-run after a prior move).
    _contents_route(respx, WIP704_ID, [])
    _contents_route(respx, USERA_ID, [_item(FILE_ID, "model.nwc")])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME,
            "items": [{"source": "Project Files/B-B-704/0. WIP", "name": "model.nwc",
                       "destination": "Project Files/B-B-704/User A"}],
            "dry_run": False,
            "response_detail": "full",
        })

    data = _parse(result)
    assert data["summary"]["already_there"] == 1
    assert data["results"][0]["action"] == "already_there"
    assert data["results"][0]["item_id"] == FILE_ID


@respx.mock
async def test_bulk_move_files_missing_file_is_not_found():
    _mock_buildings(respx)
    _contents_route(respx, WIP704_ID, [])
    _contents_route(respx, USERA_ID, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME,
            "items": [{"source": "Project Files/B-B-704/0. WIP", "name": "ghost.nwc",
                       "destination": "Project Files/B-B-704/User A"}],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["not_found"] == 1
    assert data["results"][0]["action"] == "not_found"


# ===========================================================================
# bulk_move_folders
# ===========================================================================

def _mock_with_archive(router, archive_children):
    """Project Files → {B-B-704, Archive}; Archive holds archive_children."""
    _mock_base(router)
    _contents_route(router, PF_ID, [_folder(B704_ID, "B-B-704"), _folder(ARCHIVE_ID, "Archive")])
    _contents_route(router, ARCHIVE_ID, archive_children)


@respx.mock
async def test_bulk_move_folders_dry_run_would_move():
    _mock_with_archive(respx, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_folders", {
            "project_name": PROJECT_NAME,
            "items": [{"folder": "Project Files/B-B-704", "destination": "Project Files/Archive"}],
            "dry_run": True,
            "response_detail": "full",
        })

    data = _parse(result)
    assert data["summary"]["moved"] == 1
    assert data["results"][0]["action"] == "would_move"
    assert data["results"][0]["folder_id"] == B704_ID


@respx.mock
async def test_bulk_move_folders_live_moves():
    _mock_with_archive(respx, [])
    respx.patch(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{B704_ID}").mock(
        return_value=httpx.Response(200, json={"data": {"id": B704_ID}}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_folders", {
            "project_name": PROJECT_NAME,
            "items": [{"folder": "Project Files/B-B-704", "destination": "Project Files/Archive"}],
            "dry_run": False,
            "response_detail": "full",
        })

    data = _parse(result)
    assert data["summary"]["moved"] == 1
    assert data["results"][0]["action"] == "moved"


@respx.mock
async def test_bulk_move_folders_already_there_is_idempotent():
    # B-B-704 is already a child of Archive.
    _mock_with_archive(respx, [_folder(B704_ID, "B-B-704")])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_folders", {
            "project_name": PROJECT_NAME,
            "items": [{"folder": "Project Files/B-B-704", "destination": "Project Files/Archive"}],
            "dry_run": False,
            "response_detail": "full",
        })

    data = _parse(result)
    assert data["summary"]["already_there"] == 1
    assert data["results"][0]["action"] == "already_there"


@respx.mock
async def test_bulk_move_folders_missing_folder_is_not_found():
    _mock_with_archive(respx, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_folders", {
            "project_name": PROJECT_NAME,
            "items": [{"folder": "Project Files/Does Not Exist", "destination": "Project Files/Archive"}],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["not_found"] == 1
    assert data["results"][0]["action"] == "not_found"
