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

_VIEWER = {"id": ROLE_ID_VIEWER, "name": ROLE_NAME_VIEWER}
_EDITOR = {"id": ROLE_ID_EDITOR, "name": ROLE_NAME_EDITOR}


def _members(*specs):
    """Build a /construction/admin/v1/projects/{id}/users response from
    (email, user_id, [role dicts]) tuples — used both for role name→id resolution
    (_fetch_project_roles) and the post-import confirmation poll (_get_project_members_map)."""
    return {"results": [
        {"id": uid, "email": email, "roles": roles} for (email, uid, roles) in specs
    ]}


# BOTH test users present holding the roles they're being assigned — as the members
# endpoint reports them once the async import job completes. The single-call assign polls
# this map to confirm each user landed with their roles. USER_A keeps Viewer+Editor so
# clone (which reads the reference user's roles) still resolves.
PROJECT_MEMBERS_BOTH = _members(
    (USER_A_EMAIL, USER_A_ID, [_VIEWER, _EDITOR]),
    (USER_B_EMAIL, USER_B_ID, [_VIEWER, _EDITOR]),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(result):
    """Unwrap TextContent list → dict."""
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_common(router: respx.MockRouter, members_response=PROJECT_MEMBERS_RESPONSE):
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
    ).mock(return_value=httpx.Response(200, json=members_response))


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
async def test_bulk_assign_execute_imports_with_roles_in_one_call():
    """Single-call live assign: one users:import carries the resolved roleIds (membership
    + roles together). The async job is confirmed by polling the members list."""
    # members endpoint (used for role resolution + the post-import confirmation poll)
    _mock_common(respx, members_response=_members(
        (USER_A_EMAIL, USER_A_ID, [_VIEWER]),
        (USER_B_EMAIL, USER_B_ID, [_VIEWER]),
    ))
    import_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(202, json={"jobId": "job-1"}))   # async

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
    users_sent = json.loads(import_route.calls[0].request.content)["users"]
    assert len(users_sent) == 2
    assert {entry["email"] for entry in users_sent} == {USER_A_EMAIL, USER_B_EMAIL}
    # roleIds carried on the import itself — the resolved role ID, no separate PATCH
    assert all(entry.get("roleIds") == [ROLE_ID_VIEWER] for entry in users_sent)
    assert users_sent[0].get("products")
    # Both confirmed as members holding Viewer via the poll
    assert data["summary"]["success"] == 2


@respx.mock
async def test_bulk_assign_multiple_roles_per_user():
    """default_role as a list assigns several roles at once — a resolvable name and a
    raw role ID both land in the import roleIds array (the endpoint supports multiple)."""
    _empty_ext = {"id": "role-empty-ext-id", "name": "Empty Ext"}
    _mock_common(respx, members_response=_members(
        (USER_A_EMAIL, USER_A_ID, [_VIEWER, _empty_ext]),
    ))
    import_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(202, json={"jobId": "job-1"}))

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
    # Both roles land on the single import: the name resolved to its ID, the raw ID passed through
    assert json.loads(import_route.calls[0].request.content)["users"][0]["roleIds"] == [ROLE_ID_VIEWER, "role-empty-ext-id"]
    assert data["summary"]["success"] >= 1


@respx.mock
async def test_bulk_assign_execute_partial_failure():
    """The import is accepted, but only USER_A shows up (with the role) in the confirmation
    poll — USER_B never lands, so USER_A is `success` and USER_B is `submitted` (queued)."""
    # members map after import: only USER_A present (holding Viewer); USER_B absent
    _mock_common(respx, members_response=_members(
        (USER_A_EMAIL, USER_A_ID, [_VIEWER]),
    ))
    respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(202, json={"jobId": "job-1"}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_users", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL, USER_B_EMAIL],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": False,
        })

    data = _parse(result)
    assert data["summary"]["success"] == 1     # USER_A confirmed with the role
    assert data["summary"]["submitted"] == 1   # USER_B queued but not confirmed
    assert data["summary"]["error"] == 0


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
async def test_update_roles_resolves_unused_role_name_via_cache(tmp_path, monkeypatch):
    """A role NAME no member holds (invisible to the member-walk) resolves to its UUID via
    the role_id_cache.json map — so a human can pass the name, not the ID."""
    _mock_common(respx)
    cache = tmp_path / "role_id_cache.json"
    cache.write_text(json.dumps({"roles_name_to_id": {"BAC BIM Coordinator": "cache-uuid-bim"}}))
    monkeypatch.setenv("APS_ROLE_CACHE", str(cache))
    patch_route = respx.patch(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(200, json={}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("update_user_roles", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL],
            "default_role": "BAC BIM Coordinator",   # name only, held by no member
            "dry_run": False,
        })

    data = _parse(result)
    assert patch_route.called
    # Name resolved from the cache to its UUID for the PATCH — no ID handling by the caller
    assert json.loads(patch_route.calls[0].request.content)["roleIds"] == ["cache-uuid-bim"]
    assert data["summary"]["success"] == 1
    assert not any("not found" in w.lower() for w in data.get("warnings", []))


@respx.mock
async def test_update_roles_unknown_role_name_warns(tmp_path, monkeypatch):
    """A role name that resolves via neither the member-walk nor the cache (and isn't a
    UUID) is still sent to ACC, but a clear warning is surfaced instead of only a cryptic
    'Invalid UUID format' 400."""
    _mock_common(respx)
    cache = tmp_path / "role_id_cache.json"
    cache.write_text(json.dumps({"roles_name_to_id": {}}))   # empty cache
    monkeypatch.setenv("APS_ROLE_CACHE", str(cache))
    respx.patch(
        f"{BASE}/construction/admin/v1/projects/{BARE_PROJECT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(400, json={"errors": [{"detail": "Invalid UUID format"}]}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("update_user_roles", {
            "project_names": [PROJECT_NAME],
            "user_emails": [USER_A_EMAIL],
            "default_role": "Totally New Role",   # not a member role, not in cache, not a UUID
            "dry_run": False,
        })

    data = _parse(result)
    assert any("not found" in w.lower() for w in data["warnings"])


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
    # PROJECT_MEMBERS_BOTH: USER_A (reference) holds Viewer+Editor; USER_B lands with the same
    _mock_common(respx, members_response=PROJECT_MEMBERS_BOTH)
    import_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(202, json={"jobId": "job-1"}))

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("clone_user_access", {
            "reference_user_email": USER_A_EMAIL,
            "target_user_emails": [USER_B_EMAIL],
            "dry_run": False,
        })

    data = _parse(result)
    assert import_route.called
    # All of the reference user's roles cloned in one import call
    assert json.loads(import_route.calls[0].request.content)["users"][0]["roleIds"] == [ROLE_ID_VIEWER, ROLE_ID_EDITOR]
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
    _mock_common(respx, members_response=_members(
        (USER_A_EMAIL, USER_A_ID, [_VIEWER]),
        (USER_B_EMAIL, USER_B_ID, [_VIEWER]),
    ))
    import_route = respx.post(
        f"{BASE}/construction/admin/v2/projects/{BARE_PROJECT_ID}/users:import"
    ).mock(return_value=httpx.Response(202, json={"jobId": "job-1"}))

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
    assert json.loads(import_route.calls[0].request.content)["users"][0]["roleIds"] == [ROLE_ID_VIEWER]
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


def _mock_two_hubs_accounts(router: respx.MockRouter):
    """Two EMEA hubs where the company/users live ONLY in the SECOND hub's account,
    and the target project lives ONLY in the second hub. Proves the account roster and
    project scan both fan across hubs (no hub_name needed)."""
    router.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=TWO_HUBS_RESPONSE)
    )
    # Projects: none in hub one, the cross-hub project in hub two.
    router.get(f"{BASE}/project/v1/hubs/{HUB_ONE_ID}/projects").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {"pagination": {"totalResults": 0}}})
    )
    router.get(f"{BASE}/project/v1/hubs/{HUB_TWO_ID}/projects").mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": CROSS_PROJECT_ID, "attributes": {"name": CROSS_PROJECT_NAME}}],
            "meta": {"pagination": {"totalResults": 1}}})
    )
    # Account rosters: hub one empty, hub two holds both company users.
    router.get(f"{BASE}/hq/v1/accounts/hub-one/users").mock(
        return_value=httpx.Response(200, json=[])
    )
    router.get(f"{BASE}/hq/v1/accounts/hub-two/users").mock(
        return_value=httpx.Response(200, json=ACCOUNT_USERS_RESPONSE)
    )
    # Members + roles for the resolved (second-hub) project.
    router.get(
        f"{BASE}/construction/admin/v1/projects/{CROSS_BARE_PROJECT_ID}/users"
    ).mock(return_value=httpx.Response(200, json=PROJECT_MEMBERS_RESPONSE))


@respx.mock
async def test_company_assign_resolves_company_in_second_hub_without_hub_name():
    """bulk_assign_company_users finds a company whose users live in a non-default EMEA
    hub's account without a hub_name — proves the roster fans across hubs."""
    _mock_two_hubs_accounts(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("bulk_assign_company_users", {
            "company_name": COMPANY_NAME,
            "project_names": [CROSS_PROJECT_NAME],
            "default_role": ROLE_NAME_VIEWER,
            "dry_run": True,
        })

    data = _parse(result)
    # Company users found in the second hub's account (NOT a "no users found" error).
    assert "error" not in data
    assert data["users_found"] == 2
    assert data["summary"]["error"] == 0


@respx.mock
async def test_clone_finds_reference_user_in_second_hub_without_hub_name():
    """clone_user_access finds the reference user in a project living in a non-default
    EMEA hub without a hub_name — proves the project scan fans across hubs."""
    _mock_two_hubs_accounts(respx)

    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):

        result = await aps_mcp.call_tool("clone_user_access", {
            "reference_user_email": USER_A_EMAIL,   # a member of the second-hub project
            "target_user_emails": [USER_B_EMAIL],
            "dry_run": True,
        })

    data = _parse(result)
    # Reference user found (NOT "not found in any project"); target would be cloned in.
    assert "error" not in data
    assert data["projects_cloned"] == 1
    assert data["summary"]["would_add"] == 1
