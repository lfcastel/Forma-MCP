"""Tests for get_file_attributes (Docs versions:batch-get + custom-attribute-definitions)."""
import json
import urllib.parse

import httpx
import respx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, HUB_ID, PROJECT_ID, BARE_PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE,
)

BASE = aps_mcp.APS_BASE
FOLDER_ID = "urn:adsk.wipprod:fs.folder:co.docs"
BATCH = f"{BASE}/bim360/docs/v1/projects/{BARE_PROJECT_ID}/versions:batch-get"
DEFS = (f"{BASE}/bim360/docs/v1/projects/{BARE_PROJECT_ID}/folders/"
        f"{urllib.parse.quote(FOLDER_ID, safe='')}/custom-attribute-definitions")


def _item(n):
    return {"id": f"urn:adsk.wipprod:dm.lineage:item-{n}", "type": "items",
            "attributes": {"displayName": f"DRW-{n}.pdf"}}


def _result(n, status="approved", label="Approved"):
    return {
        "urn": f"urn:adsk.wipprod:fs.file:vf.item-{n}?version=3",
        "itemUrn": f"urn:adsk.wipprod:dm.lineage:item-{n}",
        "name": f"DRW-{n}.pdf", "title": "Ground floor", "number": "",
        "revisionNumber": 2, "processState": "PROCESSING_COMPLETE",
        "approvalStatus": {"label": label, "value": status},
        "customAttributes": [{"id": 1, "type": "array", "name": "Status", "value": "A2"}],
    }


def _parse(result):
    return json.loads(result[0].text)


def _mock_folder(router, items):
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": FOLDER_ID, "type": "folders", "attributes": {"name": "Docs", "displayName": "Docs"}}]}))
    router.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents").mock(
        return_value=httpx.Response(200, json={"data": items}))


async def _call(args):
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        return await aps_mcp.call_tool("get_file_attributes", {"project_name": PROJECT_NAME, **args})


@respx.mock
async def test_folder_files_are_batched_in_50s_and_flattened():
    items = [_item(i) for i in range(120)]
    _mock_folder(respx, items)

    def respond(request):
        urns = json.loads(request.content)["urns"]
        assert len(urns) <= 50
        return httpx.Response(200, json={"results": [_result(u.rsplit("-", 1)[1]) for u in urns], "errors": []})

    route = respx.post(BATCH).mock(side_effect=respond)
    data = _parse(await _call({"folder_path": "Docs"}))

    assert route.call_count == 3
    assert data["file_count"] == 120
    assert data["by_approval_status"] == {"Approved": 120}
    row = data["files"][0]
    assert row["revision_number"] == 2
    assert row["version_number"] == 3
    assert row["custom_attributes"] == {"Status": "A2"}
    assert row["path"].startswith("Docs/DRW-")


@respx.mock
async def test_name_filter_errors_and_definitions():
    _mock_folder(respx, [_item(1), _item(2), {"id": "x", "type": "items", "attributes": {"displayName": "notes.txt"}}])
    respx.post(BATCH).mock(return_value=httpx.Response(200, json={
        "results": [_result(1)],
        "errors": [{"urn": "urn:adsk.wipprod:dm.lineage:item-2", "code": "ERR_RESOURCE_NOT_EXIST"}],
    }))
    defs = respx.get(DEFS).mock(return_value=httpx.Response(200, json={
        "results": [{"id": 1, "name": "Status", "type": "array", "arrayValues": ["S0", "A2"]},
                    {"id": 2, "name": "Description", "type": "string"}],
        "pagination": {"totalResults": 2},
    }))
    data = _parse(await _call({"folder_path": "Docs", "name_contains": "drw", "include_definitions": True}))

    assert defs.called
    assert data["file_count"] == 1
    assert data["errors"][0]["code"] == "ERR_RESOURCE_NOT_EXIST"
    assert data["attribute_definitions"][0] == {"id": 1, "name": "Status", "type": "array", "values": ["S0", "A2"]}


@respx.mock
async def test_explicit_urns_and_bad_chunk_reported_per_urn():
    respx.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))
    route = respx.post(BATCH).mock(return_value=httpx.Response(403, json={"detail": "no access"}))
    data = _parse(await _call({
        "item_ids": ["urn:adsk.wipprod:dm.lineage:item-1"],
        "version_urns": ["urn:adsk.wipprod:fs.file:vf.item-2?version=1"],
    }))
    assert json.loads(route.calls.last.request.content)["urns"] == [
        "urn:adsk.wipprod:fs.file:vf.item-2?version=1", "urn:adsk.wipprod:dm.lineage:item-1"]
    assert data["file_count"] == 0
    assert {e["code"] for e in data["errors"]} == {403}


async def test_rejects_non_urn_input():
    import pytest
    with patch("aps_mcp._resolve_project_arg", return_value=(HUB_ID, PROJECT_ID, PROJECT_NAME)):
        with pytest.raises(ValueError, match="ACC URN"):
            await _call({"item_ids": ["https://evil.example/x"]})


@respx.mock
async def test_whole_project_large_run_writes_csv():
    import csv as _csv
    items = [_item(i) for i in range(250)]
    _mock_folder(respx, items)

    def respond(request):
        urns = json.loads(request.content)["urns"]
        res = [_result(u.rsplit("-", 1)[1]) for u in urns]
        res[0]["customAttributes"] = []  # one file without Status
        return httpx.Response(200, json={"results": res, "errors": []})

    respx.post(BATCH).mock(side_effect=respond)
    data = _parse(await _call({}))  # no selectors = entire project

    assert data["scope"] == "(entire project)"
    assert data["file_count"] == 250
    assert "files" not in data
    assert data["files_missing_attribute"] == {"Status": 5}
    with open(data["csv_file"], encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) == 250
    assert rows[1]["attr:Status"] == "A2"
    assert rows[1]["path"].startswith("Docs/DRW-")


@respx.mock
async def test_output_csv_forced_for_small_run():
    _mock_folder(respx, [_item(1)])
    respx.post(BATCH).mock(return_value=httpx.Response(200, json={"results": [_result(1)], "errors": []}))
    data = _parse(await _call({"folder_path": "Docs", "output": "csv"}))
    assert data["csv_file"].endswith(".csv") and "files" not in data
