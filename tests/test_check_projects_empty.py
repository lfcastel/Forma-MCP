"""
Tests for check_projects_empty tool.

Covers: empty project, has_files, sample_limit, max_files_per_project early-stop,
not_found, topFolders error.
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN,
    HUB_ID, PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE,
)

BASE = aps_mcp.APS_BASE


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_hub_and_projects() -> None:
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )


def _top_folders(folders: list[tuple[str, str]]) -> dict:
    """Shape a topFolders response. `folders` = list of (id, display_name)."""
    return {"data": [
        {"id": fid, "attributes": {"displayName": fname}}
        for fid, fname in folders
    ]}


def _folder_contents(items: list[dict]) -> dict:
    """Shape a folder contents response. Items must have 'type' + 'id' + 'attributes.displayName'."""
    return {"data": items}


def _item(iid: str, name: str) -> dict:
    return {"type": "items", "id": iid, "attributes": {"displayName": name}}


def _subfolder(fid: str, name: str) -> dict:
    return {"type": "folders", "id": fid, "attributes": {"displayName": name}}


@respx.mock
async def test_check_projects_empty_project_with_no_top_folders_is_empty():
    _mock_hub_and_projects()
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=_top_folders([])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("check_projects_empty", {
            "project_names": [PROJECT_NAME],
        })

    data = _parse(result)
    assert data["summary"]["empty"] == 1
    assert data["summary"]["has_files"] == 0
    assert data["results"][0]["file_count"] == 0


@respx.mock
async def test_check_projects_empty_project_with_files_is_has_files():
    _mock_hub_and_projects()
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=_top_folders([("folder-1", "Project Files")])))
    respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/folder-1/contents"
    ).mock(return_value=httpx.Response(200, json=_folder_contents([
        _item("item-1", "drawing.dwg"),
        _item("item-2", "spec.pdf"),
    ])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("check_projects_empty", {
            "project_names": [PROJECT_NAME],
        })

    data = _parse(result)
    assert data["summary"]["has_files"] == 1
    assert data["results"][0]["file_count"] == 2
    assert len(data["results"][0]["sample_files"]) == 2


@respx.mock
async def test_check_projects_empty_recurses_into_subfolders():
    _mock_hub_and_projects()
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=_top_folders([("top-1", "Project Files")])))
    respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/top-1/contents"
    ).mock(return_value=httpx.Response(200, json=_folder_contents([
        _subfolder("sub-1", "Drawings"),
        _item("item-top", "readme.txt"),
    ])))
    respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/sub-1/contents"
    ).mock(return_value=httpx.Response(200, json=_folder_contents([
        _item("item-sub", "plan.dwg"),
    ])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("check_projects_empty", {
            "project_names": [PROJECT_NAME],
        })

    data = _parse(result)
    assert data["results"][0]["file_count"] == 2
    assert data["results"][0]["folder_count"] == 2


@respx.mock
async def test_check_projects_empty_sample_limit_caps_returned_paths():
    _mock_hub_and_projects()
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=_top_folders([("f", "Project Files")])))
    respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/f/contents"
    ).mock(return_value=httpx.Response(200, json=_folder_contents([
        _item(f"i{n}", f"file{n}.txt") for n in range(10)
    ])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("check_projects_empty", {
            "project_names": [PROJECT_NAME],
            "sample_limit": 3,
        })

    data = _parse(result)
    assert data["results"][0]["file_count"] == 10
    assert len(data["results"][0]["sample_files"]) == 3


@respx.mock
async def test_check_projects_empty_max_files_stops_early():
    _mock_hub_and_projects()
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=_top_folders([("f", "Project Files")])))
    respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/f/contents"
    ).mock(return_value=httpx.Response(200, json=_folder_contents([
        _item(f"i{n}", f"file{n}.txt") for n in range(50)
    ])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("check_projects_empty", {
            "project_names": [PROJECT_NAME],
            "max_files_per_project": 5,
        })

    data = _parse(result)
    assert data["results"][0]["file_count"] == 5
    assert data["results"][0]["status"] == "has_files"


@respx.mock
async def test_check_projects_empty_top_folders_error_is_surfaced():
    _mock_hub_and_projects()
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(500, json={"error": "boom"}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("check_projects_empty", {
            "project_names": [PROJECT_NAME],
        })

    data = _parse(result)
    assert data["summary"]["error"] == 1
    assert data["results"][0]["status"] == "error"


@respx.mock
async def test_check_projects_empty_unknown_project_is_not_found():
    _mock_hub_and_projects()

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("check_projects_empty", {
            "project_names": ["Definitely Not A Project"],
        })

    data = _parse(result)
    assert data["summary"]["not_found"] == 1
    assert data["results"][0]["status"] == "not_found"
