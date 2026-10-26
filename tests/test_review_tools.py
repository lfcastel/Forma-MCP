"""
Integration-style tests for the file-review tools (list_reviews, get_review).
All APS API calls are intercepted by respx; auth helpers are patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_review_tools.py -v
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN,
    HUB_ID, BARE_PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE,
    WORKFLOW_ID, WORKFLOWS_RESPONSE,
)

BASE = aps_mcp.APS_BASE
RV_BASE = f"{BASE}/construction/reviews/v1/projects/{BARE_PROJECT_ID}/reviews"
WF_BASE = f"{BASE}/construction/reviews/v1/projects/{BARE_PROJECT_ID}/workflows"

REVIEW_ID = "11111111-2222-3333-4444-555555555555"
OTHER_REVIEW_ID = "99999999-8888-7777-6666-555555555555"
REVIEW = {"id": REVIEW_ID, "sequenceId": 11, "name": "Test Review", "status": "OPEN",
          "workflowId": WORKFLOW_ID}


def _page(results, total=None):
    return {"pagination": {"limit": 50, "offset": 0,
                           "totalResults": len(results) if total is None else total},
            "results": results}


pytestmark = [pytest.mark.asyncio]


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_resolve(router: respx.MockRouter):
    router.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )


# ===========================================================================
# list_reviews
# ===========================================================================

@respx.mock
async def test_list_reviews_returns_and_sends_region():
    _mock_resolve(respx)
    route = respx.get(RV_BASE).mock(return_value=httpx.Response(200, json=_page([REVIEW])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_reviews", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert data["project"] == PROJECT_NAME
    assert data["result_count"] == 1
    assert data["reviews"][0]["id"] == REVIEW_ID
    assert route.calls[0].request.headers["x-ads-region"] == aps_mcp.REVIEWS_REGION


@respx.mock
async def test_list_reviews_forwards_filters():
    _mock_resolve(respx)
    route = respx.get(RV_BASE).mock(return_value=httpx.Response(200, json=_page([])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        await aps_mcp.call_tool("list_reviews", {
            "project_name": PROJECT_NAME,
            "status": "OPEN",
            "created_at": "2026-06-01..2026-06-30",
            "next_action_by_role": "1572818",
            "approved_by": ["AAAA1111", "BBBB2222"],
            "archived": False,
            "sort": "createdAt desc",
        })

    q = route.calls[0].request.url.params
    assert q["filter[status]"] == "OPEN"
    assert q["filter[createdAt]"] == "2026-06-01..2026-06-30"
    assert q["filter[nextActionByRole]"] == "1572818"
    assert q["filter[approvedBy]"] == "AAAA1111,BBBB2222"
    assert q["filter[archived]"] == "false"
    assert q["sort"] == "createdAt desc"
    assert "filter[workflowId]" not in q


@respx.mock
async def test_list_reviews_resolves_workflow_name():
    _mock_resolve(respx)
    respx.get(WF_BASE).mock(return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE))
    route = respx.get(RV_BASE).mock(return_value=httpx.Response(200, json=_page([REVIEW])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        await aps_mcp.call_tool("list_reviews", {
            "project_name": PROJECT_NAME, "workflow_name": "design review",
        })

    assert route.calls[0].request.url.params["filter[workflowId]"] == WORKFLOW_ID


@respx.mock
async def test_list_reviews_paginates():
    _mock_resolve(respx)
    route = respx.get(RV_BASE).mock(side_effect=[
        httpx.Response(200, json=_page([{"id": "r1"}, {"id": "r2"}], total=3)),
        httpx.Response(200, json=_page([{"id": "r3"}], total=3)),
    ])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_reviews", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert route.call_count == 2
    assert [r["id"] for r in data["reviews"]] == ["r1", "r2", "r3"]


# ===========================================================================
# get_review
# ===========================================================================

@respx.mock
async def test_get_review_by_id_summary_only():
    _mock_resolve(respx)
    route = respx.get(f"{RV_BASE}/{REVIEW_ID}").mock(
        return_value=httpx.Response(200, json=REVIEW)
    )
    progress = respx.get(f"{RV_BASE}/{REVIEW_ID}/progress")

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_review", {
            "project_name": PROJECT_NAME, "review_id": REVIEW_ID,
        })

    data = _parse(result)
    assert data["review"]["id"] == REVIEW_ID
    assert set(data) == {"project", "review"}
    assert route.calls[0].request.headers["x-ads-region"] == aps_mcp.REVIEWS_REGION
    assert not progress.called


@respx.mock
async def test_get_review_by_sequence_id_picks_exact_match():
    """filter[sequenceId] is a partial match — 11 also returns 211."""
    _mock_resolve(respx)
    lookup = respx.get(RV_BASE).mock(return_value=httpx.Response(200, json=_page([
        {"id": OTHER_REVIEW_ID, "sequenceId": 211}, REVIEW,
    ])))
    respx.get(f"{RV_BASE}/{REVIEW_ID}").mock(return_value=httpx.Response(200, json=REVIEW))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_review", {
            "project_name": PROJECT_NAME, "sequence_id": 11,
        })

    assert _parse(result)["review"]["id"] == REVIEW_ID
    assert lookup.calls[0].request.url.params["filter[sequenceId]"] == "11"


@respx.mock
async def test_get_review_by_sequence_id_falls_back_to_archived():
    _mock_resolve(respx)
    lookup = respx.get(RV_BASE).mock(side_effect=[
        httpx.Response(200, json=_page([])),
        httpx.Response(200, json=_page([REVIEW])),
    ])
    respx.get(f"{RV_BASE}/{REVIEW_ID}").mock(return_value=httpx.Response(200, json=REVIEW))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_review", {
            "project_name": PROJECT_NAME, "sequence_id": 11,
        })

    assert _parse(result)["review"]["id"] == REVIEW_ID
    assert [c.request.url.params["filter[archived]"] for c in lookup.calls] == ["false", "true"]


@respx.mock
async def test_get_review_unknown_sequence_id_errors():
    _mock_resolve(respx)
    respx.get(RV_BASE).mock(return_value=httpx.Response(200, json=_page([])))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        with pytest.raises(ValueError, match="No review with sequence ID"):
            await aps_mcp.call_tool("get_review", {
                "project_name": PROJECT_NAME, "sequence_id": 999,
            })


async def test_get_review_requires_a_reference():
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp._resolve_review_project", return_value=(BARE_PROJECT_ID, PROJECT_NAME)):
        with pytest.raises(ValueError, match="review_id"):
            await aps_mcp.call_tool("get_review", {"project_name": PROJECT_NAME})


@respx.mock
async def test_get_review_includes_progress_versions_workflow():
    _mock_resolve(respx)
    respx.get(f"{RV_BASE}/{REVIEW_ID}").mock(return_value=httpx.Response(200, json=REVIEW))
    respx.get(f"{RV_BASE}/{REVIEW_ID}/progress").mock(return_value=httpx.Response(
        200, json=_page([{"stepId": "s1", "status": "SUBMITTED"}])))
    versions = respx.get(f"{RV_BASE}/{REVIEW_ID}/versions").mock(return_value=httpx.Response(
        200, json=_page([{"urn": "urn:fake:v1", "approveStatus": {"label": "Approved"}}])))
    respx.get(f"{RV_BASE}/{REVIEW_ID}/workflow").mock(return_value=httpx.Response(
        200, json={"id": WORKFLOW_ID, "steps": []}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_review", {
            "project_name": PROJECT_NAME, "review_id": REVIEW_ID,
            "include": ["progress", "versions", "workflow"],
            "approve_status": ["Approved", "Rejected"],
        })

    data = _parse(result)
    assert data["progress"][0]["status"] == "SUBMITTED"
    assert data["versions"][0]["urn"] == "urn:fake:v1"
    assert data["workflow"]["id"] == WORKFLOW_ID
    assert versions.calls[0].request.url.params.get_list("filter[approveStatus]") == [
        "Approved", "Rejected",
    ]


@respx.mock
async def test_get_review_surfaces_error_body():
    _mock_resolve(respx)
    respx.get(f"{RV_BASE}/{REVIEW_ID}").mock(
        return_value=httpx.Response(404, json={"detail": "Review not found"})
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_review", {
            "project_name": PROJECT_NAME, "review_id": REVIEW_ID,
        })

    data = _parse(result)
    assert data["error"] == 404


# ===========================================================================
# create_review
# ===========================================================================

from conftest import (  # noqa: E402
    PROJECT_ID, FAKE_APP_TOKEN, PROJECT_MEMBERS_RESPONSE,
    USER_A_EMAIL, USER_A_AUTODESK_ID,
)

FOLDER_ID = "urn:adsk.wipprod:fs.folder:co.design"
TIP_URN = "urn:adsk.wipprod:fs.file:vf.abc?version=3"
ADMIN_USERS = f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users"


def _mock_folder(router: respx.MockRouter):
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": FOLDER_ID, "type": "folders",
             "attributes": {"name": "Design", "displayName": "Design"}},
        ]}))
    router.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": "urn:item-1", "type": "items",
             "attributes": {"displayName": "plan.pdf"},
             "relationships": {"tip": {"data": {"id": TIP_URN}}}},
        ]}))


@respx.mock
async def test_create_review_posts_files_and_workflow_by_name():
    _mock_resolve(respx)
    _mock_folder(respx)
    respx.get(WF_BASE).mock(return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE))
    post = respx.post(RV_BASE).mock(return_value=httpx.Response(201, json=REVIEW))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("create_review", {
            "project_name": PROJECT_NAME,
            "name": "Test Review",
            "workflow_name": "design review",
            "notes": "Please check",
            "file_versions": ["urn:raw:v1"],
            "files": [{"folder_path": "Design", "file_name": "PLAN.pdf"}],
        })

    data = _parse(result)
    assert data["status"] == "created"
    assert data["review"]["id"] == REVIEW_ID
    req = post.calls[0].request
    assert req.headers["x-ads-region"] == aps_mcp.REVIEWS_REGION
    body = json.loads(req.content)
    assert body == {
        "name": "Test Review",
        "workflowId": WORKFLOW_ID,
        "fileVersions": [{"urn": "urn:raw:v1"}, {"urn": TIP_URN}],
        "notes": "Please check",
    }


@respx.mock
async def test_create_review_dry_run_does_not_post():
    _mock_resolve(respx)
    post = respx.post(RV_BASE).mock(return_value=httpx.Response(201, json=REVIEW))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("create_review", {
            "project_name": PROJECT_NAME, "name": "R", "workflow_id": WORKFLOW_ID,
            "file_versions": ["urn:raw:v1"], "dry_run": True,
        })

    data = _parse(result)
    assert data["status"] == "would_create"
    assert data["request_body"]["fileVersions"] == [{"urn": "urn:raw:v1"}]
    assert not post.called


@respx.mock
async def test_create_review_resolves_override_step_reviewers():
    _mock_resolve(respx)
    respx.get(ADMIN_USERS).mock(return_value=httpx.Response(200, json=PROJECT_MEMBERS_RESPONSE))
    respx.get(WF_BASE).mock(return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE))
    post = respx.post(RV_BASE).mock(return_value=httpx.Response(201, json=REVIEW))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        await aps_mcp.call_tool("create_review", {
            "project_name": PROJECT_NAME, "name": "R", "workflow_id": WORKFLOW_ID,
            "file_versions": ["urn:raw:v1"],
            "workflow_options": {"steps": [{"id": "step-1", "reviewer_users": [USER_A_EMAIL]}]},
        })

    step = json.loads(post.calls[0].request.content)["workflowOptions"]["steps"][0]
    assert step == {"id": "step-1", "candidates": {"users": [{"autodeskId": USER_A_AUTODESK_ID}]}}


@respx.mock
async def test_create_review_requires_files():
    _mock_resolve(respx)
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         pytest.raises(ValueError, match="at least one file"):
        await aps_mcp.call_tool("create_review", {
            "project_name": PROJECT_NAME, "name": "R", "workflow_id": WORKFLOW_ID,
        })


@respx.mock
async def test_create_review_surfaces_error_body():
    _mock_resolve(respx)
    respx.post(RV_BASE).mock(return_value=httpx.Response(400, json={"detail": "bad urn"}))
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("create_review", {
            "project_name": PROJECT_NAME, "name": "R", "workflow_id": WORKFLOW_ID,
            "file_versions": ["urn:bad"],
        })
    data = _parse(result)
    assert data["error"] == 400


@respx.mock
async def test_create_review_step_reviewers_by_name_and_copy_folder():
    _mock_resolve(respx)
    _mock_folder(respx)
    respx.get(ADMIN_USERS).mock(return_value=httpx.Response(200, json=PROJECT_MEMBERS_RESPONSE))
    respx.get(WF_BASE).mock(return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE))
    respx.get(f"{WF_BASE}/{WORKFLOW_ID}").mock(return_value=httpx.Response(200, json={
        "id": WORKFLOW_ID,
        "steps": [{"id": "step-0", "name": "Start", "type": "INITIATOR"},
                  {"id": "step-1", "name": "Final Approval", "type": "APPROVER"}],
    }))
    post = respx.post(RV_BASE).mock(return_value=httpx.Response(201, json=REVIEW))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        await aps_mcp.call_tool("create_review", {
            "project_name": PROJECT_NAME, "name": "R", "workflow_id": WORKFLOW_ID,
            "file_versions": ["urn:raw:v1"],
            "step_reviewers": [{"step_name": "final approval", "reviewer_users": [USER_A_EMAIL]}],
            "copy_to_folder": "Design",
        })

    opts = json.loads(post.calls[0].request.content)["workflowOptions"]
    assert opts == {
        "steps": [{"id": "step-1", "candidates": {"users": [{"autodeskId": USER_A_AUTODESK_ID}]}}],
        "copyFilesOptions": {"folderUrn": FOLDER_ID},
    }


async def test_review_payload_rejects_step_without_reviewers():
    with pytest.raises(ValueError, match="at least one reviewer"):
        aps_mcp._review_create_payload(
            {"name": "R", "step_reviewers": [{"step_id": "step-1"}]},
            WORKFLOW_ID, [{"urn": "urn:raw:v1"}],
        )


async def test_review_payload_unknown_step_name():
    with pytest.raises(ValueError, match="No step named"):
        aps_mcp._review_create_payload(
            {"name": "R", "step_reviewers": [{"step_name": "Nope", "candidates": {"users": [{"autodeskId": "X"}]}}]},
            WORKFLOW_ID, [{"urn": "urn:raw:v1"}], step_ids_by_name={"start": "step-0"},
        )
