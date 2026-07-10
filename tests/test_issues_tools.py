"""
Integration-style tests for the Issues tools (list_issues, create_issue,
list_issue_types, list_issue_attribute_definitions, list_issue_attribute_mappings).
All APS API calls are intercepted by respx; auth helpers are patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_issues_tools.py -v
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN,
    HUB_ID, PROJECT_ID, BARE_PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE,
    ISSUE_SUBTYPE_ID,
    ISSUES_RESPONSE, CREATED_ISSUE_RESPONSE, ISSUE_TYPES_RESPONSE,
    ISSUE_ATTR_DEFS_RESPONSE, ISSUE_ATTR_MAPPINGS_RESPONSE,
    SINGLE_ISSUE_RESPONSE,
)

BASE = aps_mcp.APS_BASE
ISSUES_BASE = f"{BASE}/construction/issues/v1/projects/{BARE_PROJECT_ID}"

pytestmark = [pytest.mark.asyncio]


def _parse(result):
    """Unwrap TextContent list → dict."""
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_resolve(router: respx.MockRouter):
    """Routes needed to resolve a project by name (hubs + projects fan-out)."""
    router.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )


# ===========================================================================
# list_issues
# ===========================================================================

@respx.mock
async def test_list_issues_returns_issues():
    _mock_resolve(respx)
    respx.get(f"{ISSUES_BASE}/issues").mock(
        return_value=httpx.Response(200, json=ISSUES_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_issues", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert data["project"] == PROJECT_NAME
    assert data["result_count"] == 1
    assert data["issues"][0]["id"] == "issue-id-1"


@respx.mock
async def test_list_issues_sends_region_header():
    _mock_resolve(respx)
    route = respx.get(f"{ISSUES_BASE}/issues").mock(
        return_value=httpx.Response(200, json=ISSUES_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        await aps_mcp.call_tool("list_issues", {"project_name": PROJECT_NAME})

    assert route.called
    assert route.calls[0].request.headers["x-ads-region"] == aps_mcp.ISSUES_REGION


@respx.mock
async def test_list_issues_forwards_filters():
    _mock_resolve(respx)
    route = respx.get(f"{ISSUES_BASE}/issues").mock(
        return_value=httpx.Response(200, json=ISSUES_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        await aps_mcp.call_tool("list_issues", {
            "project_name": PROJECT_NAME,
            "status": "open",
            "search": "slab",
            "deleted": True,
            "sort_by": "-displayId",
        })

    assert route.called
    q = route.calls[0].request.url.params
    assert q["filter[status]"] == "open"
    assert q["filter[search]"] == "slab"
    assert q["filter[deleted]"] == "true"
    assert q["sortBy"] == "-displayId"


@respx.mock
async def test_list_issues_paginates_top_level_pagination():
    """Proves _get_all_issues reads top-level `pagination.totalResults` and keeps
    paging past the first page (get_all_pages would stop after page one)."""
    _mock_resolve(respx)
    page1 = {
        "pagination": {"limit": 100, "offset": 0, "totalResults": 3},
        "results": [{"id": "i1"}, {"id": "i2"}],
    }
    page2 = {
        "pagination": {"limit": 100, "offset": 2, "totalResults": 3},
        "results": [{"id": "i3"}],
    }
    route = respx.get(f"{ISSUES_BASE}/issues").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_issues", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert route.call_count == 2
    assert data["result_count"] == 3
    assert [i["id"] for i in data["issues"]] == ["i1", "i2", "i3"]
    # second request advanced the offset
    assert route.calls[1].request.url.params["offset"] == "2"


# ===========================================================================
# create_issue
# ===========================================================================

@respx.mock
async def test_create_issue_posts_body_and_returns_issue():
    _mock_resolve(respx)
    post_route = respx.post(f"{ISSUES_BASE}/issues").mock(
        return_value=httpx.Response(201, json=CREATED_ISSUE_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("create_issue", {
            "project_name": PROJECT_NAME,
            "title": "New issue",
            "issue_subtype_id": ISSUE_SUBTYPE_ID,
            "status": "open",
            "description": "Something is wrong",
            "published": False,
        })

    data = _parse(result)
    assert data["status"] == "created"
    assert data["issue"]["id"] == "issue-id-new"
    assert post_route.called
    body = json.loads(post_route.calls[0].request.content)
    assert body["title"] == "New issue"
    assert body["issueSubtypeId"] == ISSUE_SUBTYPE_ID
    assert body["status"] == "open"
    assert body["description"] == "Something is wrong"
    assert body["published"] is False
    # snake_case args not supplied must not leak into the payload
    assert "assignedTo" not in body


@respx.mock
async def test_create_issue_surfaces_error_body():
    _mock_resolve(respx)
    respx.post(f"{ISSUES_BASE}/issues").mock(
        return_value=httpx.Response(400, json={"detail": "bad subtype"})
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("create_issue", {
            "project_name": PROJECT_NAME,
            "title": "New issue",
            "issue_subtype_id": "bogus",
            "status": "open",
        })

    data = _parse(result)
    assert data["error"] == 400
    assert data["body"]["detail"] == "bad subtype"


# ===========================================================================
# list_issue_types
# ===========================================================================

@respx.mock
async def test_list_issue_types_includes_subtypes_by_default():
    _mock_resolve(respx)
    route = respx.get(f"{ISSUES_BASE}/issue-types").mock(
        return_value=httpx.Response(200, json=ISSUE_TYPES_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_issue_types", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert data["result_count"] == 1
    assert data["types"][0]["subtypes"][0]["code"] == "DEF"
    assert route.calls[0].request.url.params["include"] == "subtypes"


@respx.mock
async def test_list_issue_types_omits_include_when_disabled():
    _mock_resolve(respx)
    route = respx.get(f"{ISSUES_BASE}/issue-types").mock(
        return_value=httpx.Response(200, json=ISSUE_TYPES_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        await aps_mcp.call_tool("list_issue_types", {
            "project_name": PROJECT_NAME, "include_subtypes": False,
        })

    assert "include" not in route.calls[0].request.url.params


# ===========================================================================
# list_issue_attribute_definitions / mappings
# ===========================================================================

@respx.mock
async def test_list_issue_attribute_definitions():
    _mock_resolve(respx)
    respx.get(f"{ISSUES_BASE}/issue-attribute-definitions").mock(
        return_value=httpx.Response(200, json=ISSUE_ATTR_DEFS_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool(
            "list_issue_attribute_definitions", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert data["result_count"] == 1
    assert data["definitions"][0]["dataType"] == "list"


@respx.mock
async def test_list_issue_attribute_mappings():
    _mock_resolve(respx)
    respx.get(f"{ISSUES_BASE}/issue-attribute-mappings").mock(
        return_value=httpx.Response(200, json=ISSUE_ATTR_MAPPINGS_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool(
            "list_issue_attribute_mappings", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert data["result_count"] == 1
    assert data["mappings"][0]["mappedItemType"] == "issueSubtype"


# ===========================================================================
# get_issue
# ===========================================================================

@respx.mock
async def test_get_issue_by_id():
    _mock_resolve(respx)
    route = respx.get(f"{ISSUES_BASE}/issues/issue-id-1").mock(
        return_value=httpx.Response(200, json=SINGLE_ISSUE_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_issue", {
            "project_name": PROJECT_NAME, "issue_id": "issue-id-1",
        })

    data = _parse(result)
    assert data["issue"]["id"] == "issue-id-1"
    assert route.called
    # region header present; no displayId lookup performed
    assert route.calls[0].request.headers["x-ads-region"] == aps_mcp.ISSUES_REGION


@respx.mock
async def test_get_issue_by_display_id_resolves_uuid():
    _mock_resolve(respx)
    # display_id -> uuid lookup via filter[displayId]
    lookup = respx.get(f"{ISSUES_BASE}/issues").mock(
        return_value=httpx.Response(200, json={
            "pagination": {"limit": 2, "offset": 0, "totalResults": 1},
            "results": [{"id": "issue-id-1", "displayId": 1}],
        })
    )
    get_route = respx.get(f"{ISSUES_BASE}/issues/issue-id-1").mock(
        return_value=httpx.Response(200, json=SINGLE_ISSUE_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_issue", {
            "project_name": PROJECT_NAME, "display_id": 1,
        })

    data = _parse(result)
    assert data["issue"]["id"] == "issue-id-1"
    assert lookup.called
    assert lookup.calls[0].request.url.params["filter[displayId]"] == "1"
    assert get_route.called


@respx.mock
async def test_get_issue_missing_display_id_errors():
    _mock_resolve(respx)
    respx.get(f"{ISSUES_BASE}/issues").mock(
        return_value=httpx.Response(200, json={
            "pagination": {"limit": 2, "offset": 0, "totalResults": 0},
            "results": [],
        })
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        with pytest.raises(ValueError, match="displayId"):
            await aps_mcp.call_tool("get_issue", {
                "project_name": PROJECT_NAME, "display_id": 999,
            })


# ===========================================================================
# update_issue
# ===========================================================================

@respx.mock
async def test_update_issue_patches_supplied_fields():
    _mock_resolve(respx)
    patch_route = respx.patch(f"{ISSUES_BASE}/issues/issue-id-1").mock(
        return_value=httpx.Response(200, json={**SINGLE_ISSUE_RESPONSE, "status": "closed"})
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("update_issue", {
            "project_name": PROJECT_NAME,
            "issue_id": "issue-id-1",
            "status": "closed",
            "description": "Resolved on site",
        })

    data = _parse(result)
    assert data["status"] == "updated"
    assert data["issue"]["status"] == "closed"
    assert patch_route.called
    body = json.loads(patch_route.calls[0].request.content)
    assert body == {"status": "closed", "description": "Resolved on site"}
    # untouched fields must not be sent
    assert "title" not in body


@respx.mock
async def test_update_issue_no_fields_errors():
    _mock_resolve(respx)
    patch_route = respx.patch(f"{ISSUES_BASE}/issues/issue-id-1").mock(
        return_value=httpx.Response(200, json=SINGLE_ISSUE_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        with pytest.raises(ValueError, match="Nothing to update"):
            await aps_mcp.call_tool("update_issue", {
                "project_name": PROJECT_NAME, "issue_id": "issue-id-1",
            })

    assert not patch_route.called


@respx.mock
async def test_update_issue_surfaces_error_body():
    _mock_resolve(respx)
    respx.patch(f"{ISSUES_BASE}/issues/issue-id-1").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("update_issue", {
            "project_name": PROJECT_NAME, "issue_id": "issue-id-1", "status": "closed",
        })

    data = _parse(result)
    assert data["error"] == 404
    assert data["body"]["detail"] == "not found"
