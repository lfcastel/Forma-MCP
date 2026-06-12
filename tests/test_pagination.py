"""
Tests for folder-contents pagination (get_all_folder_contents).
The Data Management contents endpoint caps each page at 200 items and exposes
further pages via JSON:API `links.next`; the helper must follow it and aggregate.
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
FOLDER_ID = "urn:adsk.wipprod:fs.folder:co.big"

pytestmark = [pytest.mark.asyncio]


def _file(i):
    return {"id": f"urn:item:{i}", "type": "items", "attributes": {"displayName": f"file{i}.txt"}}


def _folder(i):
    return {"id": f"urn:folder:{i}", "type": "folders",
            "attributes": {"name": f"Sub {i}", "displayName": f"Sub {i}"}}


@respx.mock
async def test_get_all_folder_contents_follows_next_link():
    """Two pages (200 + 50) should aggregate to 250 items."""
    page1 = [_file(i) for i in range(200)]
    page2 = [_file(i) for i in range(200, 250)]
    next_href = f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents?page%5Bnumber%5D=1"

    route = respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents")
    route.side_effect = [
        httpx.Response(200, json={"data": page1, "links": {"next": {"href": next_href}}}),
        httpx.Response(200, json={"data": page2, "links": {}}),
    ]

    async with httpx.AsyncClient() as client:
        items = await aps_mcp.get_all_folder_contents(
            client, PROJECT_ID, FOLDER_ID, {"Authorization": "Bearer x"}
        )

    assert len(items) == 250
    assert route.call_count == 2


@respx.mock
async def test_get_all_folder_contents_single_page():
    """A single page with no `links.next` returns just that page."""
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents").mock(
        return_value=httpx.Response(200, json={"data": [_file(0), _folder(1)], "links": {}})
    )
    async with httpx.AsyncClient() as client:
        items = await aps_mcp.get_all_folder_contents(
            client, PROJECT_ID, FOLDER_ID, {"Authorization": "Bearer x"}
        )
    assert len(items) == 2


@respx.mock
async def test_get_all_folder_contents_raise_on_error_false_returns_partial():
    """With raise_on_error=False, a 403 mid-walk stops without raising."""
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents").mock(
        return_value=httpx.Response(403, json={"errors": [{"detail": "forbidden"}]})
    )
    async with httpx.AsyncClient() as client:
        items = await aps_mcp.get_all_folder_contents(
            client, PROJECT_ID, FOLDER_ID, {"Authorization": "Bearer x"}, raise_on_error=False
        )
    assert items == []


@respx.mock
async def test_list_folder_contents_aggregates_all_pages():
    """The list_folder_contents tool returns items from every page, not just the first 200."""
    respx.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": FOLDER_ID, "type": "folders", "attributes": {"name": "Big", "displayName": "Big"}},
        ]})
    )

    page1 = [_file(i) for i in range(200)]
    page2 = [_folder(900), _file(250)]  # 3 items on the second page
    next_href = f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents?page%5Bnumber%5D=1"
    route = respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents")
    route.side_effect = [
        httpx.Response(200, json={"data": page1, "links": {"next": {"href": next_href}}}),
        httpx.Response(200, json={"data": page2, "links": {}}),
    ]

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_folder_contents", {
            "project_name": PROJECT_NAME,
            "folder_path": "Big",
        })

    data = json.loads(result[0].text)
    # 201 files (200 on page 1 + 1 on page 2) and 1 folder (page 2)
    assert len(data["files"]) == 201
    assert len(data["folders"]) == 1
