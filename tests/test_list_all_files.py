"""
Integration-style tests for list_all_files (recursive, unfiltered file listing)
and a regression test for the shared walker via find_files.

All APS API calls are intercepted by respx; the 3-legged auth helper is patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_list_all_files.py -v
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

pytestmark = [pytest.mark.asyncio]

F_TOP = "urn:adsk.wipprod:fs.folder:co.top"
SUB_ID = "urn:adsk.wipprod:fs.folder:co.sub"
FILE1 = "urn:adsk.wipprod:dm.lineage:file1"
FILE2 = "urn:adsk.wipprod:dm.lineage:file2"

TOP_FOLDERS = {
    "data": [{"id": F_TOP, "type": "folders",
              "attributes": {"name": "Project Files", "displayName": "Project Files"}}]
}
TOP_CONTENTS = {
    "data": [
        {"id": FILE1, "type": "items",
         "attributes": {"displayName": "a.rvt", "lastModifiedTime": "2026-05-01",
                        "lastModifiedUserName": "Alice"}},
        {"id": SUB_ID, "type": "folders",
         "attributes": {"name": "Sub", "displayName": "Sub"}},
    ]
}
SUB_CONTENTS = {
    "data": [
        {"id": FILE2, "type": "items",
         "attributes": {"displayName": "b.dwg", "lastModifiedTime": "2026-06-01",
                        "lastModifiedUserName": "Bob"}},
    ]
}


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


async def _call(name, args):
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        return await aps_mcp.call_tool(name, args)


def _mock_project(router):
    router.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))


def _contents(router, folder_id, body):
    return router.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{folder_id}/contents"
    ).mock(return_value=httpx.Response(200, json=body))


# ===========================================================================
# list_all_files — whole project
# ===========================================================================

@respx.mock
async def test_list_all_files_whole_project():
    _mock_project(respx)
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=TOP_FOLDERS))
    _contents(respx, F_TOP, TOP_CONTENTS)
    _contents(respx, SUB_ID, SUB_CONTENTS)

    data = _parse(await _call("list_all_files", {"project_name": PROJECT_NAME}))

    assert data["scope"] == "(entire project)"
    assert data["result_count"] == 2
    paths = {f["path"] for f in data["files"]}
    assert paths == {"Project Files/a.rvt", "Project Files/Sub/b.dwg"}
    # Newest first: b.dwg (2026-06-01) before a.rvt (2026-05-01)
    assert data["files"][0]["name"] == "b.dwg"


# ===========================================================================
# list_all_files — scoped to a folder subtree via a display-name path
# ===========================================================================

@respx.mock
async def test_list_all_files_folder_subtree():
    _mock_project(respx)
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=TOP_FOLDERS))
    _contents(respx, F_TOP, TOP_CONTENTS)   # path traversal finds "Sub" here
    _contents(respx, SUB_ID, SUB_CONTENTS)  # the subtree walk starts here

    data = _parse(await _call(
        "list_all_files",
        {"project_name": PROJECT_NAME, "folder_path": "Project Files/Sub"}))

    assert data["scope"] == "Project Files/Sub"
    assert data["result_count"] == 1
    assert data["files"][0]["name"] == "b.dwg"
    assert data["files"][0]["path"] == "Project Files/Sub/b.dwg"


# ===========================================================================
# list_all_files also accepts an ID/URL via the `project` param
# ===========================================================================

@respx.mock
async def test_list_all_files_by_name_partial():
    _mock_project(respx)
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=TOP_FOLDERS))
    _contents(respx, F_TOP, TOP_CONTENTS)
    _contents(respx, SUB_ID, SUB_CONTENTS)

    data = _parse(await _call("list_all_files", {"project": "Test Pro"}))

    assert data["project"] == PROJECT_NAME
    assert data["result_count"] == 2


# ===========================================================================
# find_files still filters after the walker refactor (regression)
# ===========================================================================

@respx.mock
async def test_find_files_filters_after_refactor():
    _mock_project(respx)
    respx.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=TOP_FOLDERS))
    _contents(respx, F_TOP, TOP_CONTENTS)
    _contents(respx, SUB_ID, SUB_CONTENTS)

    data = _parse(await _call("find_files", {"project_name": PROJECT_NAME, "query": "dwg"}))

    assert data["result_count"] == 1
    assert data["files"][0]["name"] == "b.dwg"
