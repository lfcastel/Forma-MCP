"""
Tests for list_deleted_items (audit of soft-deleted / hidden files and folders)
and the include_hidden option of get_all_folder_contents.

All APS API calls are intercepted by respx; the 3-legged auth helper is patched.
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
F_LIVE = "urn:adsk.wipprod:fs.folder:co.live"
F_DEL = "urn:adsk.wipprod:fs.folder:co.deleted"
LIVE_FILE = "urn:adsk.wipprod:dm.lineage:live"
DEL_FILE = "urn:adsk.wipprod:dm.lineage:del"
DEL_FILE2 = "urn:adsk.wipprod:dm.lineage:del2"
CHILD_FILE = "urn:adsk.wipprod:dm.lineage:child"

TOP_FOLDERS = {
    "data": [{"id": F_TOP, "type": "folders",
              "attributes": {"name": "Project Files", "displayName": "Project Files"}}]
}


def _file(fid, name, modified, hidden, user="Alice"):
    return {"id": fid, "type": "items",
            "attributes": {"displayName": name, "lastModifiedTime": modified,
                           "lastModifiedUserName": user, "hidden": hidden}}


def _folder(fid, name, hidden):
    return {"id": fid, "type": "folders",
            "attributes": {"name": name, "displayName": name, "hidden": hidden,
                           "lastModifiedTime": "2026-07-01"}}


TOP_CONTENTS = {"data": [
    _file(LIVE_FILE, "live.pdf", "2026-05-01", False),
    _file(DEL_FILE, "gone.rvt", "2026-06-01", True, user="Bob"),
    _folder(F_LIVE, "Live", False),
    _folder(F_DEL, "Old", True),
]}
LIVE_CONTENTS = {"data": [_file(DEL_FILE2, "gone2.dwg", "2026-08-01", True)]}
# A file inside a deleted folder that is not itself flagged hidden.
DEL_CONTENTS = {"data": [_file(CHILD_FILE, "inside.ifc", "2026-04-01", False)]}


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
    router.get(
        f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders"
    ).mock(return_value=httpx.Response(200, json=TOP_FOLDERS))


def _contents(router, folder_id, body):
    return router.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{folder_id}/contents"
    ).mock(return_value=httpx.Response(200, json=body))


@respx.mock
async def test_whole_project_lists_hidden_items_and_deleted_folder_contents():
    _mock_project(respx)
    top = _contents(respx, F_TOP, TOP_CONTENTS)
    _contents(respx, F_LIVE, LIVE_CONTENTS)
    _contents(respx, F_DEL, DEL_CONTENTS)

    data = _parse(await _call("list_deleted_items", {"project_name": PROJECT_NAME}))

    # Every listing asks for hidden items.
    assert top.calls[0].request.url.params["includeHidden"] == "true"
    assert data["scope"] == "(entire project)"
    by_name = {f["name"]: f for f in data["files"]}
    assert set(by_name) == {"gone.rvt", "gone2.dwg", "inside.ifc"}  # live.pdf excluded
    assert by_name["gone.rvt"]["deleted_via"] == "self"
    assert by_name["gone.rvt"]["modified_by"] == "Bob"
    assert by_name["gone2.dwg"]["path"] == "Project Files/Live/gone2.dwg"
    assert by_name["inside.ifc"]["deleted_via"] == "parent_folder"
    assert by_name["inside.ifc"]["deleted_folder"] == "Project Files/Old"
    # Newest first.
    assert [f["name"] for f in data["files"]] == ["gone2.dwg", "gone.rvt", "inside.ifc"]
    assert [f["name"] for f in data["folders"]] == ["Old"]
    assert data["summary"] == {
        "deleted_files": 3, "deleted_files_self": 2,
        "deleted_files_in_deleted_folder": 1, "deleted_folders": 1,
        "folders_unreadable": 0,
    }


@respx.mock
async def test_non_recursive_lists_only_direct_children():
    _mock_project(respx)
    _contents(respx, F_TOP, TOP_CONTENTS)
    live = _contents(respx, F_LIVE, LIVE_CONTENTS)

    data = _parse(await _call("list_deleted_items", {
        "project_name": PROJECT_NAME, "folder_path": "Project Files", "recursive": False,
    }))

    assert [f["name"] for f in data["files"]] == ["gone.rvt"]
    assert [f["name"] for f in data["folders"]] == ["Old"]
    assert not live.called


@respx.mock
async def test_include_folders_false_omits_folder_rows():
    _mock_project(respx)
    _contents(respx, F_TOP, TOP_CONTENTS)
    _contents(respx, F_LIVE, LIVE_CONTENTS)
    _contents(respx, F_DEL, DEL_CONTENTS)

    data = _parse(await _call("list_deleted_items", {
        "project_name": PROJECT_NAME, "include_folders": False,
    }))

    assert "folders" not in data
    assert data["summary"]["deleted_folders"] == 0
    assert data["summary"]["deleted_files"] == 3


@respx.mock
async def test_urn_of_deleted_folder_marks_whole_subtree():
    _mock_project(respx)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{F_DEL}").mock(
        return_value=httpx.Response(200, json={"data": _folder(F_DEL, "Old", True)}))
    _contents(respx, F_DEL, DEL_CONTENTS)

    data = _parse(await _call("list_deleted_items", {
        "project_name": PROJECT_NAME, "folder_path": F_DEL,
    }))

    assert data["files"][0]["name"] == "inside.ifc"
    assert data["files"][0]["deleted_via"] == "parent_folder"
    assert data["files"][0]["deleted_folder"] == "Old"


@respx.mock
async def test_unreadable_subfolder_is_reported_not_raised():
    _mock_project(respx)
    _contents(respx, F_TOP, TOP_CONTENTS)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{F_LIVE}/contents").mock(
        return_value=httpx.Response(403, json={}))
    _contents(respx, F_DEL, DEL_CONTENTS)

    data = _parse(await _call("list_deleted_items", {"project_name": PROJECT_NAME}))

    assert data["summary"]["folders_unreadable"] == 1
    assert data["errors"][0]["folder"] == "Project Files/Live"
    assert data["errors"][0]["status"] == 403
    assert {f["name"] for f in data["files"]} == {"gone.rvt", "inside.ifc"}


@respx.mock
async def test_include_hidden_is_kept_on_next_pages():
    page2 = f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{F_TOP}/contents?page%5Bnumber%5D=1"
    route = respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{F_TOP}/contents")
    route.side_effect = [
        httpx.Response(200, json={"data": [_file(DEL_FILE, "a", "x", True)],
                                  "links": {"next": {"href": page2}}}),
        httpx.Response(200, json={"data": [_file(DEL_FILE2, "b", "y", True)]}),
    ]
    async with httpx.AsyncClient() as client:
        items = await aps_mcp.get_all_folder_contents(
            client, PROJECT_ID, F_TOP, {}, include_hidden=True)

    assert len(items) == 2
    assert all(c.request.url.params.get("includeHidden") == "true" for c in route.calls)


@respx.mock
async def test_default_listing_does_not_request_hidden():
    route = _contents(respx, F_TOP, {"data": []})
    async with httpx.AsyncClient() as client:
        await aps_mcp.get_all_folder_contents(client, PROJECT_ID, F_TOP, {})
    assert "includeHidden" not in route.calls[0].request.url.params
