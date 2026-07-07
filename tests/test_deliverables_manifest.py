"""
Tests for export_deliverables_manifest — the compact, filename-only recursive
file listing used to cross-check an ACC project/folder against an external
deliverable list (e.g. an Excel checklist).

All APS API calls are intercepted by respx; the auth token is patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_deliverables_manifest.py -v
"""
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
SUB1_ID = "urn:adsk.wipemea:fs.folder:co.sub1"


def _folder(fid, name):
    return {"type": "folders", "id": fid,
            "attributes": {"name": name, "displayName": name}}


def _item(fid, name):
    return {"type": "items", "id": fid, "attributes": {"displayName": name}}


def _contents(items):
    return {"jsonapi": {"version": "1.0"}, "data": items, "links": {}}


def _mock_base(router, top_folder):
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [top_folder]}))


def _contents_route(router, fid, items):
    router.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{fid}/contents").mock(
        return_value=httpx.Response(200, json=_contents(items)))


def _text(result):
    assert result and result[0].type == "text"
    return result[0].text


def _mock_tree():
    """Top folder with 2 files + one subfolder; subfolder repeats a filename."""
    _mock_base(respx, _folder(PF_ID, "Project Files"))
    _contents_route(respx, PF_ID, [
        _item("urn:a", "ARC-Model-01.rvt"),
        _item("urn:b", "notes.txt"),
        _folder(SUB1_ID, "Architecture"),
    ])
    _contents_route(respx, SUB1_ID, [
        _item("urn:c", "ARC-Model-01.rvt"),   # duplicate name across folders
        _item("urn:d", "Site-Survey.pdf"),
    ])


@respx.mock
async def test_manifest_dedups_sorts_and_counts():
    _mock_tree()
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("export_deliverables_manifest", {
            "project_name": PROJECT_NAME,
        })

    text = _text(result)
    header, _, body = text.partition("\n\n")
    # 4 files on disk, 3 unique names (ARC-Model-01.rvt appears twice).
    assert "4 files (3 unique names)" in header
    assert PROJECT_NAME in header
    assert "(entire project)" in header
    # Filenames only, deduped, sorted case-insensitively — no ids/paths/dates.
    assert body.splitlines() == ["ARC-Model-01.rvt", "notes.txt", "Site-Survey.pdf"]
    assert "urn:" not in body
    assert "Architecture" not in body   # folder names never appear


@respx.mock
async def test_manifest_extension_filter():
    _mock_tree()
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("export_deliverables_manifest", {
            "project_name": PROJECT_NAME,
            "extensions": ["rvt", ".PDF"],   # leading dots and case ignored
        })

    text = _text(result)
    header, _, body = text.partition("\n\n")
    assert "3 files (2 unique names)" in header   # notes.txt excluded
    assert "extensions: .pdf, .rvt" in header
    assert body.splitlines() == ["ARC-Model-01.rvt", "Site-Survey.pdf"]


@respx.mock
async def test_manifest_empty_tree():
    _mock_base(respx, _folder(PF_ID, "Project Files"))
    _contents_route(respx, PF_ID, [])
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("export_deliverables_manifest", {
            "project_name": PROJECT_NAME,
        })

    text = _text(result)
    assert "0 files (0 unique names)" in text
    assert "(no files found)" in text
