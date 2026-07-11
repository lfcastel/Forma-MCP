"""
Integration-style tests for the approval-workflow tools (list_workflows,
get_workflow, create_workflow, bulk_create_workflows) plus the candidate resolver.
All APS API calls are intercepted by respx; auth helpers are patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/test_workflow_tools.py -v
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, FAKE_APP_TOKEN,
    HUB_ID, BARE_PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE,
    PROJECT_MEMBERS_RESPONSE,
    USER_A_EMAIL, USER_A_AUTODESK_ID, ROLE_NAME_EDITOR, ROLE_EDITOR_AUTODESK_ID,
    COMPANY_NAME, COMPANY_AUTODESK_ID,
    WORKFLOW_ID, WORKFLOWS_RESPONSE, CREATED_WORKFLOW_RESPONSE, SINGLE_WORKFLOW_RESPONSE,
)

BASE = aps_mcp.APS_BASE
WF_BASE = f"{BASE}/construction/reviews/v1/projects/{BARE_PROJECT_ID}/workflows"
ADMIN_USERS = f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users"

pytestmark = [pytest.mark.asyncio]


def _parse(result):
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


def _mock_directory(router: respx.MockRouter):
    """Routes for _build_project_directory: users from project members, and role/
    company IDs harvested from the existing-workflows list."""
    router.get(ADMIN_USERS).mock(
        return_value=httpx.Response(200, json=PROJECT_MEMBERS_RESPONSE)
    )
    router.get(WF_BASE).mock(
        return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE)
    )


# ===========================================================================
# list_workflows
# ===========================================================================

@respx.mock
async def test_list_workflows_returns_and_sends_region():
    _mock_resolve(respx)
    route = respx.get(WF_BASE).mock(
        return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_workflows", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert data["project"] == PROJECT_NAME
    assert data["result_count"] == 1
    assert data["workflows"][0]["id"] == WORKFLOW_ID
    assert route.calls[0].request.headers["x-ads-region"] == aps_mcp.REVIEWS_REGION


@respx.mock
async def test_list_workflows_forwards_filters():
    _mock_resolve(respx)
    route = respx.get(WF_BASE).mock(
        return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        await aps_mcp.call_tool("list_workflows", {
            "project_name": PROJECT_NAME, "status": "INACTIVE", "sort": "name desc",
        })

    q = route.calls[0].request.url.params
    assert q["filter[status]"] == "INACTIVE"
    assert q["sort"] == "name desc"


@respx.mock
async def test_list_workflows_paginates_top_level_pagination():
    _mock_resolve(respx)
    page1 = {"pagination": {"limit": 50, "offset": 0, "totalResults": 3},
             "results": [{"id": "w1"}, {"id": "w2"}]}
    page2 = {"pagination": {"limit": 50, "offset": 2, "totalResults": 3},
             "results": [{"id": "w3"}]}
    route = respx.get(WF_BASE).mock(side_effect=[
        httpx.Response(200, json=page1), httpx.Response(200, json=page2),
    ])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_workflows", {"project_name": PROJECT_NAME})

    data = _parse(result)
    assert route.call_count == 2
    assert data["result_count"] == 3
    assert [w["id"] for w in data["workflows"]] == ["w1", "w2", "w3"]


# ===========================================================================
# get_workflow
# ===========================================================================

@respx.mock
async def test_get_workflow_by_id():
    _mock_resolve(respx)
    route = respx.get(f"{WF_BASE}/{WORKFLOW_ID}").mock(
        return_value=httpx.Response(200, json=SINGLE_WORKFLOW_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_workflow", {
            "project_name": PROJECT_NAME, "workflow_id": WORKFLOW_ID,
        })

    data = _parse(result)
    assert data["workflow"]["id"] == WORKFLOW_ID
    assert route.calls[0].request.headers["x-ads-region"] == aps_mcp.REVIEWS_REGION


@respx.mock
async def test_get_workflow_by_name_resolves_via_list():
    _mock_resolve(respx)
    lookup = respx.get(WF_BASE).mock(
        return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE)
    )
    get_route = respx.get(f"{WF_BASE}/{WORKFLOW_ID}").mock(
        return_value=httpx.Response(200, json=SINGLE_WORKFLOW_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_workflow", {
            "project_name": PROJECT_NAME, "name": "design review",  # case-insensitive
        })

    data = _parse(result)
    assert data["workflow"]["id"] == WORKFLOW_ID
    assert lookup.called and get_route.called


@respx.mock
async def test_get_workflow_unknown_name_errors():
    _mock_resolve(respx)
    respx.get(WF_BASE).mock(return_value=httpx.Response(200, json=WORKFLOWS_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        with pytest.raises(ValueError, match="No workflow named"):
            await aps_mcp.call_tool("get_workflow", {
                "project_name": PROJECT_NAME, "name": "Nonexistent",
            })


# ===========================================================================
# create_workflow (candidate resolution)
# ===========================================================================

@respx.mock
async def test_create_workflow_resolves_reviewers_and_posts_camelcase():
    _mock_resolve(respx)
    _mock_directory(respx)
    post = respx.post(WF_BASE).mock(
        return_value=httpx.Response(201, json=CREATED_WORKFLOW_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("create_workflow", {
            "project_name": PROJECT_NAME,
            "name": "New Workflow",
            "steps": [
                {"name": "Kickoff", "type": "INITIATOR"},
                {"name": "Approve", "type": "APPROVER", "duration": 5,
                 "due_date_type": "WORKDAY",
                 "reviewer_users": [USER_A_EMAIL],
                 "reviewer_roles": [ROLE_NAME_EDITOR],
                 "reviewer_companies": [COMPANY_NAME]},
            ],
        })

    data = _parse(result)
    assert data["status"] == "created"
    assert data["workflow"]["id"] == "workflow-id-new"
    body = json.loads(post.calls[0].request.content)
    assert body["name"] == "New Workflow"
    # copyFilesOptions defaulted
    assert body["copyFilesOptions"] == {"enabled": False}
    approver = body["steps"][1]
    assert approver["dueDateType"] == "WORKDAY"
    assert approver["duration"] == 5
    # friendly references resolved to autodeskId (roles/companies harvested from
    # existing workflows, users from project members)
    assert approver["candidates"]["users"] == [{"autodeskId": USER_A_AUTODESK_ID}]
    assert approver["candidates"]["roles"] == [{"autodeskId": ROLE_EDITOR_AUTODESK_ID}]
    assert approver["candidates"]["companies"] == [{"autodeskId": COMPANY_AUTODESK_ID}]


@respx.mock
async def test_create_workflow_defaults_due_date_type_on_reviewer_steps():
    """The API requires dueDateType on REVIEWER/APPROVER steps; the tool fills the
    documented CALENDAR_DAY default when the caller omits it (but never on INITIATOR)."""
    _mock_resolve(respx)
    _mock_directory(respx)
    post = respx.post(WF_BASE).mock(
        return_value=httpx.Response(201, json=CREATED_WORKFLOW_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        await aps_mcp.call_tool("create_workflow", {
            "project_name": PROJECT_NAME, "name": "Defaults",
            "steps": [
                {"name": "Kickoff", "type": "INITIATOR"},
                {"name": "Approve", "type": "APPROVER", "duration": 5,
                 "reviewer_users": [USER_A_EMAIL]},
            ],
        })

    body = json.loads(post.calls[0].request.content)
    assert "dueDateType" not in body["steps"][0]          # INITIATOR untouched
    assert body["steps"][1]["dueDateType"] == "CALENDAR_DAY"  # APPROVER defaulted


@respx.mock
async def test_create_workflow_unresolved_reviewer_errors():
    _mock_resolve(respx)
    _mock_directory(respx)
    post = respx.post(WF_BASE).mock(return_value=httpx.Response(201, json=CREATED_WORKFLOW_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        with pytest.raises(ValueError, match="Could not resolve"):
            await aps_mcp.call_tool("create_workflow", {
                "project_name": PROJECT_NAME,
                "name": "Bad Reviewers",
                "steps": [{"name": "Approve", "type": "APPROVER",
                           "reviewer_users": ["Ghost Person"]}],
            })

    assert not post.called


@respx.mock
async def test_create_workflow_group_review_min_exceeds_candidates_errors():
    """A MINIMUM group review with min > resolvable reviewers is caught before the
    POST (would otherwise be a raw 400 from the API)."""
    _mock_resolve(respx)
    _mock_directory(respx)
    post = respx.post(WF_BASE).mock(return_value=httpx.Response(201, json=CREATED_WORKFLOW_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        with pytest.raises(ValueError, match="groupReview min"):
            await aps_mcp.call_tool("create_workflow", {
                "project_name": PROJECT_NAME, "name": "Too High Min",
                "steps": [{"name": "Review", "type": "REVIEWER", "duration": 5,
                           "group_review": {"enabled": True, "type": "MINIMUM", "min": 2},
                           "reviewer_users": [USER_A_EMAIL]}],  # only 1 candidate
            })

    assert not post.called


@respx.mock
async def test_create_workflow_surfaces_error_body():
    _mock_resolve(respx)
    _mock_directory(respx)
    respx.post(WF_BASE).mock(return_value=httpx.Response(409, json={"detail": "name exists"}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("create_workflow", {
            "project_name": PROJECT_NAME, "name": "Dup",
            "steps": [{"name": "s", "type": "APPROVER", "reviewer_users": [USER_A_EMAIL]}],
        })

    data = _parse(result)
    assert data["error"] == 409
    assert data["body"]["detail"] == "name exists"


# ===========================================================================
# bulk_create_workflows
# ===========================================================================

@respx.mock
async def test_bulk_create_workflows_dry_run_posts_nothing():
    _mock_resolve(respx)
    _mock_directory(respx)
    post = respx.post(WF_BASE).mock(return_value=httpx.Response(201, json=CREATED_WORKFLOW_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("bulk_create_workflows", {
            "project_name": PROJECT_NAME,
            "workflows": [
                {"name": "WF A", "steps": [{"name": "s", "type": "APPROVER",
                                            "reviewer_users": [USER_A_EMAIL]}]},
                {"name": "WF B", "steps": [{"name": "s", "type": "APPROVER",
                                            "reviewer_roles": [ROLE_NAME_EDITOR]}]},
            ],
            "response_detail": "full",
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["summary"]["would_create"] == 2
    assert not post.called
    assert "audit_file" not in data


@respx.mock
async def test_bulk_create_workflows_live_maps_409_and_writes_audit():
    _mock_resolve(respx)
    _mock_directory(respx)
    respx.post(WF_BASE).mock(side_effect=[
        httpx.Response(201, json=CREATED_WORKFLOW_RESPONSE),
        httpx.Response(409, json={"detail": "exists"}),
    ])

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("bulk_create_workflows", {
            "project_name": PROJECT_NAME,
            "dry_run": False,
            "workflows": [
                {"name": "WF A", "steps": [{"name": "s", "type": "APPROVER",
                                            "reviewer_users": [USER_A_EMAIL]}]},
                {"name": "WF B", "steps": [{"name": "s", "type": "APPROVER",
                                            "reviewer_users": [USER_A_EMAIL]}]},
            ],
        })

    data = _parse(result)
    assert data["summary"].get("created") == 1
    assert data["summary"].get("already_exists") == 1
    assert "audit_file" in data
    # 'changes' default detail keeps only noteworthy rows (already_exists), drops created
    actions = {r["action"] for r in data.get("results", [])}
    assert "already_exists" in actions
    assert "created" not in actions


@respx.mock
async def test_bulk_create_workflows_bad_spec_reported_without_post():
    _mock_resolve(respx)
    _mock_directory(respx)
    post = respx.post(WF_BASE).mock(return_value=httpx.Response(201, json=CREATED_WORKFLOW_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("bulk_create_workflows", {
            "project_name": PROJECT_NAME,
            "dry_run": False,
            "workflows": [
                {"name": "Bad", "steps": [{"name": "s", "type": "APPROVER",
                                           "reviewer_users": ["Ghost"]}]},
            ],
        })

    data = _parse(result)
    assert data["summary"].get("error") == 1
    assert not post.called


# ===========================================================================
# _resolve_candidates (unit)
# ===========================================================================

async def test_resolve_candidates_passthrough_raw_id():
    directory = {"users_by_name": {}, "users_by_email": {},
                 "roles_by_name": {}, "companies_by_name": {}}
    out = aps_mcp._resolve_candidates({"reviewer_users": ["ADSKRAW00042"]}, directory)
    assert out["users"] == [{"autodeskId": "ADSKRAW00042"}]


async def test_resolve_candidates_unresolved_raises():
    directory = {"users_by_name": {}, "users_by_email": {},
                 "roles_by_name": {}, "companies_by_name": {}}
    with pytest.raises(ValueError, match="Alice"):
        aps_mcp._resolve_candidates({"reviewer_users": ["Alice"]}, directory)


async def test_looks_like_autodesk_id():
    assert aps_mcp._looks_like_autodesk_id("ADSKALICE001")
    assert not aps_mcp._looks_like_autodesk_id("Alice Test")  # has space
    assert not aps_mcp._looks_like_autodesk_id("Alice")       # no digit


# ===========================================================================
# _build_project_directory — role/company Reviews-ID resolution (3 tiers)
# ===========================================================================

# A member holding a role exposes that role's Reviews-API numeric autodeskId as
# `roleGroupId`, and the member's company id as `companyGroupId`, in the SAME admin
# users payload used for user ids. (Ints here to prove the str() coercion.)
MEMBERS_WITH_GROUP_IDS = {
    "results": [
        {
            "autodeskId": "USR1", "email": "u@x.com", "name": "U One",
            "companyName": "Contoso", "companyGroupId": 990000020,
            "roles": [{"id": "uuid-admin", "name": "BAC Admin", "roleGroupId": 990000010}],
        }
    ]
}
EMPTY_WORKFLOWS = {"pagination": {"limit": 50, "offset": 0, "totalResults": 0}, "results": []}


@respx.mock
async def test_build_directory_resolves_role_and_company_from_members_payload():
    """Tier 1: a role/company with ≥1 member resolves by name from roleGroupId /
    companyGroupId — no dependency on an existing workflow using it."""
    respx.get(ADMIN_USERS).mock(return_value=httpx.Response(200, json=MEMBERS_WITH_GROUP_IDS))
    respx.get(WF_BASE).mock(return_value=httpx.Response(200, json=EMPTY_WORKFLOWS))

    async with httpx.AsyncClient() as client:
        directory = await aps_mcp._build_project_directory(client, FAKE_TOKEN, BARE_PROJECT_ID)

    assert directory["roles_by_name"]["bac admin"] == "990000010"
    assert directory["companies_by_name"]["contoso"] == "990000020"


@respx.mock
async def test_build_directory_tier3_oxygen_cache_fallback(tmp_path, monkeypatch):
    """Tier 3: a genuinely empty role (no member, no workflow) resolves by name from the
    role_id_cache.json 'roles_name_to_oxygen_id' map — same cache mechanic as role UUIDs."""
    cache = tmp_path / "role_id_cache.json"
    cache.write_text(json.dumps({
        "roles_name_to_oxygen_id": {"Empty Role": "990000030"},
        "companies_name_to_oxygen_id": {"Ghost Co": "990000040"},
    }))
    monkeypatch.setenv("APS_ROLE_CACHE", str(cache))
    # No members, no workflows — only the cache can resolve these names.
    respx.get(ADMIN_USERS).mock(return_value=httpx.Response(200, json={"results": []}))
    respx.get(WF_BASE).mock(return_value=httpx.Response(200, json=EMPTY_WORKFLOWS))

    async with httpx.AsyncClient() as client:
        directory = await aps_mcp._build_project_directory(client, FAKE_TOKEN, BARE_PROJECT_ID)

    assert directory["roles_by_name"]["empty role"] == "990000030"
    assert directory["companies_by_name"]["ghost co"] == "990000040"


@respx.mock
async def test_create_workflow_resolves_role_via_member_role_group_id():
    """End-to-end: a role no workflow uses, but that a member holds, now resolves by name
    (the fix for the 'missing role-id' failure) — its roleGroupId becomes the candidate."""
    _mock_resolve(respx)
    respx.get(ADMIN_USERS).mock(return_value=httpx.Response(200, json=MEMBERS_WITH_GROUP_IDS))
    respx.get(WF_BASE).mock(return_value=httpx.Response(200, json=EMPTY_WORKFLOWS))
    post = respx.post(WF_BASE).mock(
        return_value=httpx.Response(201, json=CREATED_WORKFLOW_RESPONSE)
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        result = await aps_mcp.call_tool("create_workflow", {
            "project_name": PROJECT_NAME, "name": "Role By Name",
            "steps": [{"name": "Approve", "type": "APPROVER", "duration": 5,
                       "reviewer_roles": ["BAC Admin"]}],
        })

    data = _parse(result)
    assert data["status"] == "created"
    body = json.loads(post.calls[0].request.content)
    assert body["steps"][0]["candidates"]["roles"] == [{"autodeskId": "990000010"}]


async def test_load_oxygen_id_maps(tmp_path, monkeypatch):
    """The oxygen-id cache loaders read their own keys, lowercased, and degrade to {}."""
    cache = tmp_path / "role_id_cache.json"
    cache.write_text(json.dumps({
        "roles_name_to_oxygen_id": {"BAC Admin": "990000010"},
        "companies_name_to_oxygen_id": {"Contoso": 990000020},   # int coerced
    }))
    monkeypatch.setenv("APS_ROLE_CACHE", str(cache))
    assert aps_mcp._load_role_oxygen_id() == {"bac admin": "990000010"}
    assert aps_mcp._load_company_oxygen_id() == {"contoso": "990000020"}


# ===========================================================================
# get_data_connector_requests — captures role_oxygen_id for the Reviews tools
# ===========================================================================

@respx.mock
async def test_data_connector_export_captures_oxygen_id():
    """The admin export's admin_project_roles CSV carries role_oxygen_id (the Reviews
    numeric autodeskId); get_data_connector_requests surfaces it as a name→oxygen map to
    merge into the cache, alongside the existing role_id→name map."""
    import io as _io, zipfile as _zip
    account_id = aps_mcp._to_bare_id(HUB_ID)
    req_id, job_id = "req-1", "job-1"
    signed = "https://signed.example/autodesk_data_extract.zip"

    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as zf:
        zf.writestr(
            "admin_project_roles.csv",
            "role_id,name,role_oxygen_id\n"
            "uuid-admin,BAC Admin,990000010\n"
            "uuid-empty,Empty Role,990000030\n",
        )
    zip_bytes = buf.getvalue()

    respx.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    respx.get(
        f"{BASE}/data-connector/v1/accounts/{account_id}/requests/{req_id}/jobs"
    ).mock(return_value=httpx.Response(200, json={
        "results": [{"id": job_id, "status": "complete", "completionStatus": "success",
                     "completedAt": "2026-07-11T00:00:00Z"}]
    }))
    respx.get(
        f"{BASE}/data-connector/v1/accounts/{account_id}/jobs/{job_id}/data/autodesk_data_extract.zip"
    ).mock(return_value=httpx.Response(200, json={"signedUrl": signed}))
    respx.get(signed).mock(return_value=httpx.Response(200, content=zip_bytes))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("get_data_connector_requests", {"request_id": req_id})

    data = _parse(result)
    assert data["roles"]["uuid-admin"] == "BAC Admin"
    assert data["roles_name_to_oxygen_id"] == {
        "BAC Admin": "990000010", "Empty Role": "990000030",
    }
