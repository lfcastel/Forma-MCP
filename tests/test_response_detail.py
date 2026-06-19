"""
Tests for the `response_detail` output-verbosity control (and the `fields`
projection on bulk_list_folder_contents).

Covers the 4 token-saving tools: bulk_move_files, bulk_move_folders,
bulk_delete_folders, bulk_list_folder_contents.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_response_detail.py -v
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
EMPTY_ID = "urn:adsk.wipemea:fs.folder:co.empty"
SENS_ID = "urn:adsk.wipemea:fs.folder:co.sens"


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
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [_folder(PF_ID, "Project Files")]}))


def _contents_route(router, fid, items):
    router.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{fid}/contents").mock(
        return_value=httpx.Response(200, json=_contents(items)))


# ===========================================================================
# bulk_move_files — 1 locked file among successes
# ===========================================================================

def _mock_move_batch(router):
    """Project Files → B-B-704 → {0. WIP (3 files), User A (empty)}."""
    _mock_base(router)
    _contents_route(router, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(router, B704_ID, [_folder(WIP704_ID, "0. WIP"), _folder(USERA_ID, "User A")])
    _contents_route(router, WIP704_ID, [
        _item("urn:adsk.wipemea:fs.file:co.m1", "model1.nwc"),
        _item("urn:adsk.wipemea:fs.file:co.m2", "model2.nwc"),
        _item("urn:adsk.wipemea:fs.file:co.c4r", "tower_C4RModel.rvt"),
    ])
    _contents_route(router, USERA_ID, [])
    # The two plain files move; the C4R model is rejected with 403.
    for fid, ok in [("urn:adsk.wipemea:fs.file:co.m1", True),
                    ("urn:adsk.wipemea:fs.file:co.m2", True)]:
        router.patch(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{fid}").mock(
            return_value=httpx.Response(200, json={"data": {"id": fid}}))
    router.patch(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/urn:adsk.wipemea:fs.file:co.c4r").mock(
        return_value=httpx.Response(403, json={"errors": [{"detail": "forbidden"}]}))


def _move_items():
    dest = "Project Files/B-B-704/User A"
    src = "Project Files/B-B-704/0. WIP"
    return [
        {"source": src, "name": "model1.nwc", "destination": dest},
        {"source": src, "name": "model2.nwc", "destination": dest},
        {"source": src, "name": "tower_C4RModel.rvt", "destination": dest},
    ]


@respx.mock
async def test_move_files_changes_default_returns_only_the_locked_file():
    _mock_move_batch(respx)
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME, "items": _move_items(), "dry_run": False,
        })  # response_detail omitted → defaults to "changes"

    data = _parse(result)
    # Counts are computed before filtering and stay accurate.
    assert data["summary"]["moved"] == 2
    assert data["summary"]["skipped_unmovable"] == 1
    # changes drops the 2 successful moves, keeps exactly the 1 locked file.
    assert len(data["results"]) == 1
    assert data["results"][0]["action"] == "skipped_unmovable"
    assert data["results"][0]["file"] == "tower_C4RModel.rvt"


@respx.mock
async def test_move_files_full_echoes_every_row():
    _mock_move_batch(respx)
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME, "items": _move_items(), "dry_run": False,
            "response_detail": "full",
        })

    data = _parse(result)
    assert len(data["results"]) == 3
    assert {r["action"] for r in data["results"]} == {"moved", "skipped_unmovable"}


@respx.mock
async def test_move_files_summary_omits_results_but_keeps_failures():
    _mock_move_batch(respx)
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME, "items": _move_items(), "dry_run": False,
            "response_detail": "summary",
        })

    data = _parse(result)
    assert data["summary"]["moved"] == 2
    assert data["summary"]["skipped_unmovable"] == 1
    # No per-item results array under summary...
    assert "results" not in data
    # ...but the locked file is never dropped.
    assert len(data["failures"]) == 1
    assert data["failures"][0]["action"] == "skipped_unmovable"


@respx.mock
async def test_move_files_summary_all_success_has_no_failures_array():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704")])
    _contents_route(respx, B704_ID, [_folder(WIP704_ID, "0. WIP"), _folder(USERA_ID, "User A")])
    _contents_route(respx, WIP704_ID, [_item("urn:adsk.wipemea:fs.file:co.m1", "model1.nwc")])
    _contents_route(respx, USERA_ID, [])
    respx.patch(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/urn:adsk.wipemea:fs.file:co.m1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "urn:adsk.wipemea:fs.file:co.m1"}}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_files", {
            "project_name": PROJECT_NAME,
            "items": [{"source": "Project Files/B-B-704/0. WIP", "name": "model1.nwc",
                       "destination": "Project Files/B-B-704/User A"}],
            "dry_run": False, "response_detail": "summary",
        })

    data = _parse(result)
    assert data["summary"]["moved"] == 1
    assert "results" not in data
    assert "failures" not in data  # clean run → no failures key at all


# ===========================================================================
# bulk_move_folders
# ===========================================================================

@respx.mock
async def test_move_folders_changes_drops_already_there():
    # B-B-704 is already under Archive → already_there (a no-op), dropped by changes.
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(B704_ID, "B-B-704"), _folder(ARCHIVE_ID, "Archive")])
    _contents_route(respx, ARCHIVE_ID, [_folder(B704_ID, "B-B-704")])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_move_folders", {
            "project_name": PROJECT_NAME,
            "items": [{"folder": "Project Files/B-B-704", "destination": "Project Files/Archive"}],
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["already_there"] == 1
    assert data["results"] == []  # no-op dropped under changes


# ===========================================================================
# bulk_delete_folders
# ===========================================================================

@respx.mock
async def test_delete_changes_keeps_only_skipped_has_files():
    # Two folders: one empty (would_delete), one stuck (skipped_has_files).
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(WIP704_ID, "0. WIP"), _folder(EMPTY_ID, "Empty")])
    _contents_route(respx, WIP704_ID, [_item("urn:adsk.wipemea:fs.file:co.c4r", "tower_C4RModel.rvt")])
    _contents_route(respx, EMPTY_ID, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_delete_folders", {
            "project_name": PROJECT_NAME,
            "folders": ["Project Files/0. WIP", "Project Files/Empty"],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["deleted"] == 1          # the empty one
    assert data["summary"]["skipped_has_files"] == 1
    assert len(data["results"]) == 1
    assert data["results"][0]["action"] == "skipped_has_files"


@respx.mock
async def test_delete_summary_surfaces_stuck_files_as_failures():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(WIP704_ID, "0. WIP")])
    _contents_route(respx, WIP704_ID, [_item("urn:adsk.wipemea:fs.file:co.c4r", "tower_C4RModel.rvt")])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_delete_folders", {
            "project_name": PROJECT_NAME,
            "folders": ["Project Files/0. WIP"],
            "dry_run": True, "response_detail": "summary",
        })

    data = _parse(result)
    assert "results" not in data
    assert len(data["failures"]) == 1
    assert data["failures"][0]["action"] == "skipped_has_files"
    assert data["failures"][0]["sample_files"] == ["tower_C4RModel.rvt"]


# ===========================================================================
# bulk_list_folder_contents — changes drops empties; fields = lean rows
# ===========================================================================

@respx.mock
async def test_list_changes_drops_empty_folders():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(WIP704_ID, "B-B-704"), _folder(EMPTY_ID, "B-B-999")])
    # B-B-704 has a file; B-B-999 is empty.
    _contents_route(respx, WIP704_ID, [_item("urn:adsk.wipemea:fs.file:co.f1", "model.nwc")])
    _contents_route(respx, EMPTY_ID, [])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_list_folder_contents", {
            "project_name": PROJECT_NAME, "children_of": "Project Files",
        })  # default changes

    data = _parse(result)
    assert data["summary"]["folders_listed"] == 2  # count is pre-filter
    assert len(data["results"]) == 1               # only the non-empty one
    assert data["results"][0]["folder"] == "B-B-704"


@respx.mock
async def test_list_summary_omits_results_keeps_errors_channel():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(WIP704_ID, "B-B-704")])
    _contents_route(respx, WIP704_ID, [_item("urn:adsk.wipemea:fs.file:co.f1", "model.nwc")])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_list_folder_contents", {
            "project_name": PROJECT_NAME, "children_of": "Project Files",
            "response_detail": "summary",
        })

    data = _parse(result)
    assert data["summary"]["folders_listed"] == 1
    assert "results" not in data
    assert data["errors"] == []  # errors channel always present


@respx.mock
async def test_list_fields_files_returns_lean_rows():
    _mock_base(respx)
    _contents_route(respx, PF_ID, [_folder(WIP704_ID, "B-B-704")])
    _contents_route(respx, WIP704_ID, [
        _folder(SENS_ID, "0. WIP"),
        _item("urn:adsk.wipemea:fs.file:co.f1", "model.nwc"),
    ])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("bulk_list_folder_contents", {
            "project_name": PROJECT_NAME, "children_of": "Project Files",
            "fields": ["files"], "response_detail": "full",
        })

    row = _parse(result)["results"][0]
    assert "subfolders" not in row          # not requested
    assert row["file_count"] == 1           # always present
    f = row["files"][0]
    assert set(f.keys()) == {"name", "id"}  # lean: no last_modified / created_by
