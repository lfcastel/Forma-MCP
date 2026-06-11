"""
Integration-style tests for the move_file and move_folder tools.
All APS API calls are intercepted by respx; auth helpers are patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_move_tools.py -v
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

# Folder / file fixtures local to the move tests
SRC_FOLDER_ID = "urn:adsk.wipprod:fs.folder:co.source"
DEST_FOLDER_ID = "urn:adsk.wipprod:fs.folder:co.dest"
ITEM_ID = "urn:adsk.wipprod:dm.lineage:item-abc"
FILE_NAME = "drawing.pdf"

TOP_FOLDERS_RESPONSE = {
    "data": [
        {"id": SRC_FOLDER_ID, "type": "folders",
         "attributes": {"name": "Source", "displayName": "Source"}},
        {"id": DEST_FOLDER_ID, "type": "folders",
         "attributes": {"name": "Dest", "displayName": "Dest"}},
    ]
}

SRC_CONTENTS_RESPONSE = {
    "data": [
        {"id": ITEM_ID, "type": "items",
         "attributes": {"displayName": FILE_NAME},
         "relationships": {"tip": {"data": {"id": "urn:adsk.wipprod:fs.file:vf.abc?version=1"}}}},
    ]
}


def _parse(result):
    """Unwrap TextContent list → dict."""
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_resolve(router: respx.MockRouter):
    """Routes needed to resolve project + top-level folders."""
    router.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )
    router.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=TOP_FOLDERS_RESPONSE))


pytestmark = [pytest.mark.asyncio]


# ===========================================================================
# move_file
# ===========================================================================

@respx.mock
async def test_move_file_dry_run_returns_would_move():
    _mock_resolve(respx)
    respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{SRC_FOLDER_ID}/contents"
    ).mock(return_value=httpx.Response(200, json=SRC_CONTENTS_RESPONSE))
    patch_route = respx.patch(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}"
    ).mock(return_value=httpx.Response(200, json={}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("move_file", {
            "project_name": PROJECT_NAME,
            "source_folder_path": "Source",
            "file_name": FILE_NAME,
            "destination_folder_path": "Dest",
            "dry_run": True,
        })

    data = _parse(result)
    assert data["status"] == "would_move"
    assert data["dry_run"] is True
    assert data["item_id"] == ITEM_ID
    assert data["destination_folder_id"] == DEST_FOLDER_ID
    assert not patch_route.called  # no mutation on dry run


@respx.mock
async def test_move_file_execute_patches_item_parent():
    _mock_resolve(respx)
    respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{SRC_FOLDER_ID}/contents"
    ).mock(return_value=httpx.Response(200, json=SRC_CONTENTS_RESPONSE))
    patch_route = respx.patch(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}"
    ).mock(return_value=httpx.Response(200, json={"data": {"id": ITEM_ID}}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("move_file", {
            "project_name": PROJECT_NAME,
            "source_folder_path": "Source",
            "file_name": FILE_NAME,
            "destination_folder_path": "Dest",
            "dry_run": False,
        })

    data = _parse(result)
    assert data["status"] == "moved"
    assert patch_route.called
    body = json.loads(patch_route.calls[0].request.content)
    assert body["data"]["type"] == "items"
    assert body["data"]["id"] == ITEM_ID
    assert body["data"]["relationships"]["parent"]["data"]["id"] == DEST_FOLDER_ID


@respx.mock
async def test_move_file_not_found_raises():
    _mock_resolve(respx)
    respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{SRC_FOLDER_ID}/contents"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        with pytest.raises(ValueError, match="not found"):
            await aps_mcp.call_tool("move_file", {
                "project_name": PROJECT_NAME,
                "source_folder_path": "Source",
                "file_name": "missing.pdf",
                "destination_folder_path": "Dest",
                "dry_run": False,
            })


# ===========================================================================
# move_folder
# ===========================================================================

@respx.mock
async def test_move_folder_dry_run_returns_would_move():
    _mock_resolve(respx)
    patch_route = respx.patch(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{SRC_FOLDER_ID}"
    ).mock(return_value=httpx.Response(200, json={}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("move_folder", {
            "project_name": PROJECT_NAME,
            "folder_path": "Source",
            "destination_parent_path": "Dest",
            "dry_run": True,
        })

    data = _parse(result)
    assert data["status"] == "would_move"
    assert data["dry_run"] is True
    assert data["folder_id"] == SRC_FOLDER_ID
    assert data["destination_parent_id"] == DEST_FOLDER_ID
    assert not patch_route.called


@respx.mock
async def test_move_folder_execute_patches_folder_parent():
    _mock_resolve(respx)
    patch_route = respx.patch(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{SRC_FOLDER_ID}"
    ).mock(return_value=httpx.Response(200, json={"data": {"id": SRC_FOLDER_ID}}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("move_folder", {
            "project_name": PROJECT_NAME,
            "folder_path": "Source",
            "destination_parent_path": "Dest",
            "dry_run": False,
        })

    data = _parse(result)
    assert data["status"] == "moved"
    assert patch_route.called
    body = json.loads(patch_route.calls[0].request.content)
    assert body["data"]["type"] == "folders"
    assert body["data"]["id"] == SRC_FOLDER_ID
    assert body["data"]["relationships"]["parent"]["data"]["id"] == DEST_FOLDER_ID
