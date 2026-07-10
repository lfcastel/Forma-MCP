"""
Integration-style tests for the 5 new bulk user management tools.
All APS API calls are intercepted by respx; auth helpers are patched.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/ -v
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, FAKE_APP_TOKEN,
    HUB_ID, ACCOUNT_ID, PROJECT_ID, BARE_PROJECT_ID, PROJECT_NAME,
    ROLE_ID_VIEWER, ROLE_ID_EDITOR, ROLE_NAME_VIEWER, ROLE_NAME_EDITOR,
    USER_A_EMAIL, USER_A_ID, USER_B_EMAIL, USER_B_ID,
    COMPANY_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE, ACCOUNT_USERS_RESPONSE,
    PROJECT_ROLES_RESPONSE, PROJECT_MEMBERS_RESPONSE, IMPORT_SUCCESS_RESPONSE,
)

BASE = aps_mcp.APS_BASE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(result):
    """Unwrap TextContent list → dict."""
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_common(router: respx.MockRouter):
    """Register the routes every tool needs: hubs, projects, roles, members."""
    router.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )
    router.get(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/users").mock(
        return_value=httpx.Response(200, json=ACCOUNT_USERS_RESPONSE)
    )
    # _fetch_project_roles uses /projects/{id}/users (Forma endpoint)
    router.get(f"{BASE}/projects/{PROJECT_ID}/users").mock(
        return_value=httpx.Response(200, json=PROJECT_ROLES_RESPONSE)
    )
    # _get_project_members_map uses /construction/admin/v1/projects/{id}/users
    router.get(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users"
    ).mock(return_value=httpx.Response(200, json=PROJECT_MEMBERS_RESPONSE))


# Patch both auth helpers for all tests in this module
pytestmark = [
    pytest.mark.asyncio,
]


# ===========================================================================
# bulk_assign_users
# ===========================================================================

@respx.mock
async def test_bulk_assign_dry_run_returns_would_add():
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_users", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL, USER_B_EMAIL],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": True,
        })

    data = _parse(result)
    assert data["dry_run"] is True
    # USER_A is already in PROJECT_MEMBERS_RESPONSE → already_member
    # USER_B is not in the project mock → would_add
    assert data["summary"]["already_member"] == 1
    assert data["summary"]["would_add"] == 1
    assert data["summary"]["error"] == 0
    assert data["audit_file"] is None  # no file written on dry run


@respx.mock
async def test_bulk_assign_dry_run_unresolved_role_passes_through():
    """A role the member-walk can't see (e.g. an empty/newly-created role given by
    its ID) is no longer pre-rejected — dry-run reports would_add and the value is
    left for the ACC API to validate on import."""
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_users", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_B_EMAIL],   # not a member yet → would_add
            "default_role": "role-empty-ext-id",   # held by no member
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["error"] == 0
    assert data["summary"]["would_add"] == 1
    assert data["results"][0]["role"] == "role-empty-ext-id"


@respx.mock
async def test_bulk_assign_execute_calls_import_endpoint():
    _mock_common(respx)
    import_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(200, json=IMPORT_SUCCESS_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_users", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL, USER_B_EMAIL],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": False,
        })

    data = _parse(result)
    assert import_route.called
    body = json.loads(import_route.calls[0].request.content)
    assert "users" in body
    users_sent = body["users"]
    assert len(users_sent) == 2
    emails_sent = {entry["email"] for entry in users_sent}
    assert USER_A_EMAIL in emails_sent
    assert USER_B_EMAIL in emails_sent
    # Role ID must be resolved and injected as roleIds
    assert users_sent[0].get("roleIds") == [ROLE_ID_VIEWER]
    # products must be present
    assert users_sent[0].get("products")
    assert data["summary"]["success"] == 2


@respx.mock
async def test_bulk_assign_multiple_roles_per_user():
    """default_role as a list assigns several roles at once — a resolvable name and a
    raw role ID both land in the roleIds array (the endpoint supports multiple)."""
    _mock_common(respx)
    import_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(200, json=IMPORT_SUCCESS_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_users", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL],
            "default_role": [ROLE_NAME_VIEWER, "role-empty-ext-id"],  # name + raw id
            "dry_run": False,
        })

    data = _parse(result)
    assert import_route.called
    users_sent = json.loads(import_route.calls[0].request.content)["users"]
    # Both roles present: the name resolved to its ID, the raw ID passed through
    assert users_sent[0]["roleIds"] == [ROLE_ID_VIEWER, "role-empty-ext-id"]
    assert data["summary"]["success"] >= 1


@respx.mock
async def test_bulk_assign_execute_partial_failure():
    """If the API returns per-user failure for one user, summary reflects it."""
    _mock_common(respx)
    respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(200, json=[
        {"email": USER_A_EMAIL, "success": True, "message": ""},
        {"email": USER_B_EMAIL, "success": False, "message": "User already exists"},
    ]))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_users", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL, USER_B_EMAIL],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["success"] == 1
    assert data["summary"]["error"] == 1


@respx.mock
async def test_bulk_assign_unresolvable_project_returns_error():
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {"pagination": {"totalResults": 0}}})
    )

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_users", {
            "project_names": ["DoesNotExist"],
            "user_emails": [USER_A_EMAIL],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["error"] >= 1
    assert any("not found" in r["message"].lower() for r in data["results"])


@respx.mock
async def test_bulk_assign_role_override_per_project():
    """role_overrides should pick a different role for the target project."""
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_users", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL],
            "default_role": ROLE_NAME_VIEWER,
            "role_overrides": {PROJECT_NAME: ROLE_NAME_EDITOR},
            "dry_run": True,
        })

    data = _parse(result)
    assert data["results"][0]["role"] == ROLE_NAME_EDITOR


# ===========================================================================
# update_user_roles
# ===========================================================================

@respx.mock
async def test_update_roles_dry_run_shows_would_update():
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("update_user_roles", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL],
            "default_role": ROLE_NAME_EDITOR,
            "dry_run": True,
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["summary"]["would_update"] == 1
    assert ROLE_NAME_EDITOR in data["results"][0]["message"]


@respx.mock
async def test_update_roles_skips_non_member():
    """A user in the account but not in the project should appear as 'skipped'."""
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        # USER_B is in ACCOUNT_USERS_RESPONSE but not in PROJECT_MEMBERS_RESPONSE
        result = await aps_mcp.call_tool("update_user_roles", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_B_EMAIL],
            "default_role": ROLE_NAME_EDITOR,
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["skipped"] == 1
    assert data["summary"]["error"] == 0


@respx.mock
async def test_update_roles_execute_calls_patch():
    _mock_common(respx)
    patch_route = respx.patch(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(200, json={}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("update_user_roles", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL],
            "default_role": ROLE_NAME_EDITOR,
            "dry_run": False,
        })

    data = _parse(result)
    assert patch_route.called
    body = json.loads(patch_route.calls[0].request.content)
    assert body.get("roleIds") == [ROLE_ID_EDITOR]
    assert body.get("products")
    assert data["summary"]["success"] == 1


@respx.mock
async def test_update_roles_unresolved_role_passes_through_to_api():
    """An unresolved role value is sent to ACC as a raw role ID (no client-side
    pre-check) — the API is the authority on whether the role exists."""
    _mock_common(respx)
    patch_route = respx.patch(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(200, json={"id": USER_A_ID}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("update_user_roles", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL],
            "default_role": "role-empty-ext-id",   # held by no member
            "dry_run": False,
        })

    data = _parse(result)
    assert patch_route.called
    body = json.loads(patch_route.calls.last.request.content)
    assert body["roleIds"] == ["role-empty-ext-id"]
    assert data["summary"]["success"] == 1


# ===========================================================================
# remove_users_from_projects
# ===========================================================================

@respx.mock
async def test_remove_dry_run_shows_would_remove():
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("remove_users_from_projects", {
            "user_emails": [USER_A_EMAIL],
            "project_names": [PROJECT_NAME],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["summary"]["would_remove"] == 1


@respx.mock
async def test_remove_skips_non_member_when_project_specified():
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("remove_users_from_projects", {
            "user_emails": ["nobody@external.com"],
            "project_names": [PROJECT_NAME],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["summary"]["skipped"] == 1
    assert data["summary"]["would_remove"] == 0


@respx.mock
async def test_remove_execute_calls_delete():
    _mock_common(respx)
    delete_route = respx.delete(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(204))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("remove_users_from_projects", {
            "user_emails": [USER_A_EMAIL],
            "project_names": [PROJECT_NAME],
            "dry_run": False,
        })

    data = _parse(result)
    assert delete_route.called
    assert data["summary"]["success"] == 1


@respx.mock
async def test_remove_all_projects_scans_hub():
    """Passing empty project_names should iterate ALL hub projects."""
    _mock_common(respx)
    delete_route = respx.delete(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(204))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("remove_users_from_projects", {
            "user_emails": [USER_A_EMAIL],
            "project_names": [],   # empty = all projects
            "dry_run": False,
        })

    data = _parse(result)
    assert delete_route.called
    assert data["summary"]["success"] == 1


# ===========================================================================
# clone_user_access
# ===========================================================================

@respx.mock
async def test_clone_reference_not_found_returns_error():
    """If the reference user is in no projects, return an error dict."""
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE)
    )
    # Members list has no one matching the reference email
    respx.get(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users"
    ).mock(return_value=httpx.Response(200, json={"results": []}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("clone_user_access", {
            "reference_user_email": "nobody@example.com",
            "target_user_emails": [USER_B_EMAIL],
            "dry_run": True,
        })

    data = _parse(result)
    assert "error" in data
    assert "nobody@example.com" in data["error"]


@respx.mock
async def test_clone_dry_run_applies_reference_role():
    """Reference user found in one project → target user gets would_add with same role."""
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("clone_user_access", {
            "reference_user_email": USER_A_EMAIL,
            "target_user_emails": [USER_B_EMAIL],
            "dry_run": True,
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["projects_cloned"] == 1
    assert data["summary"]["would_add"] == 1
    # ALL of the reference user's roles are cloned (roleIds is an array), not just the first
    cloned_roles = {r.strip() for r in data["results"][0]["role"].split(",")}
    assert cloned_roles == {ROLE_NAME_VIEWER, ROLE_NAME_EDITOR}


@respx.mock
async def test_clone_execute_calls_import():
    _mock_common(respx)
    import_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(200, json=[
        {"email": USER_B_EMAIL, "success": True, "message": ""},
    ]))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("clone_user_access", {
            "reference_user_email": USER_A_EMAIL,
            "target_user_emails": [USER_B_EMAIL],
            "dry_run": False,
        })

    data = _parse(result)
    assert import_route.called
    assert data["summary"]["success"] == 1


# ===========================================================================
# bulk_assign_company_users
# ===========================================================================

@respx.mock
async def test_company_assign_unknown_company_returns_error():
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_company_users", {
            "company_name": "NonExistentCo",
            "project_names": [PROJECT_NAME],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": True,
        })

    data = _parse(result)
    assert "error" in data


@respx.mock
async def test_company_assign_dry_run_resolves_company_members():
    """Both test users belong to COMPANY_NAME — both should appear as would_add."""
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_company_users", {
            "company_name": COMPANY_NAME,
            "project_names": [PROJECT_NAME],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": True,
        })

    data = _parse(result)
    assert data["dry_run"] is True
    assert data["users_found"] == 2
    # USER_A is already in PROJECT_MEMBERS_RESPONSE → already_member
    # USER_B is not in the project mock → would_add
    assert data["summary"]["already_member"] == 1
    assert data["summary"]["would_add"] == 1


@respx.mock
async def test_company_assign_user_filter_restricts_to_subset():
    _mock_common(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_company_users", {
            "company_name": COMPANY_NAME,
            "project_names": [PROJECT_NAME],
            "default_role": ROLE_NAME_VIEWER,
            "user_filter": [USER_A_EMAIL],  # only Alice
            "dry_run": True,
        })

    data = _parse(result)
    assert data["users_found"] == 1
    assert data["results"][0]["user"] == USER_A_EMAIL


@respx.mock
async def test_company_assign_execute_calls_import():
    _mock_common(respx)
    import_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(200, json=IMPORT_SUCCESS_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_company_users", {
            "company_name": COMPANY_NAME,
            "project_names": [PROJECT_NAME],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": False,
        })

    data = _parse(result)
    assert import_route.called
    assert data["summary"]["success"] == 2


# ===========================================================================
# Cross-hub project resolution (the two-EMEA-hubs fix)
# ===========================================================================

# Two EMEA hubs. resolve_hub() picks the FIRST one; the target project lives in
# the SECOND. The old per-hub resolver searched only the first hub and failed
# with "Project not found"; the modern _resolve_project_ref fans out across all
# hubs, so the bulk-user tools now resolve the project without a hub_name hint.
HUB_ONE_ID = "b.hub-one"
HUB_TWO_ID = "b.hub-two"
CROSS_PROJECT_ID = "b.cross-proj"
CROSS_BARE_PROJECT_ID = "cross-proj"
CROSS_PROJECT_NAME = "Cross Hub Project"

TWO_HUBS_RESPONSE = {
    "data": [
        {"id": HUB_ONE_ID, "attributes": {
            "name": "BAC - Default Hub", "region": "EMEA",
            "hubType": "autodesk.bim360:Account"}},
        {"id": HUB_TWO_ID, "attributes": {
            "name": "BAC - EU Hub", "region": "EMEA",
            "hubType": "autodesk.bim360:Account"}},
    ]
}


@respx.mock
async def test_remove_resolves_project_in_second_hub_without_hub_name():
    """remove_users_from_projects finds a project living in a non-default EMEA
    hub without a hub_name — proves the cross-hub resolver fix."""
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=TWO_HUBS_RESPONSE)
    )
    # First hub has no matching project; the project lives in the second hub.
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ONE_ID}/projects").mock(
        return_value=httpx.Response(200, json={
            "data": [], "meta": {"pagination": {"totalResults": 0}}})
    )
    respx.get(f"{BASE}/project/v1/hubs/{HUB_TWO_ID}/projects").mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": CROSS_PROJECT_ID,
                      "attributes": {"name": CROSS_PROJECT_NAME}}],
            "meta": {"pagination": {"totalResults": 1}}})
    )
    # Membership lookup for the resolved (second-hub) project.
    respx.get(
        f"{BASE}/construction/admin/v1/projects/{CROSS_BARE_PROJECT_ID}/users"
    ).mock(return_value=httpx.Response(200, json=PROJECT_MEMBERS_RESPONSE))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("remove_users_from_projects", {
            "project_names": [CROSS_PROJECT_NAME],
            "user_emails": [USER_A_EMAIL],
            "dry_run": True,
        })

    data = _parse(result)
    # Project resolved across hubs → USER_A (a member) is flagged would_remove,
    # NOT reported as a "Project not found" error.
    assert data["summary"]["would_remove"] == 1
    assert data["summary"]["error"] == 0
    assert data["results"][0]["project"] == CROSS_PROJECT_NAME
    assert data["results"][0]["status"] == "would_remove"
