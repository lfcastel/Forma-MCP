"""
Integration-style tests for the hub-level directory tools:
    list_account_companies, bulk_add_hub_users, bulk_add_hub_companies,
    deactivate_hub_users, deactivate_hub_companies.

All APS API calls are intercepted by respx; auth helpers are patched.

Run with:
    python -m pytest tests/test_hub_admin_tools.py -v
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, FAKE_APP_TOKEN,
    HUB_ID, ACCOUNT_ID,
    USER_A_EMAIL, USER_A_ID, USER_B_EMAIL,
    COMPANY_NAME, COMPANY_ID, NEW_USER_EMAIL, NEW_COMPANY_NAME,
    HUB_RESPONSE, ACCOUNT_USERS_RESPONSE, ACCOUNT_COMPANIES_RESPONSE,
    USER_IMPORT_SUCCESS_RESPONSE, COMPANY_IMPORT_SUCCESS_RESPONSE,
)

BASE = aps_mcp.APS_BASE

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _mock_hub(router: respx.MockRouter, *, users=None, companies=None):
    """Register the read routes the hub tools need: hubs, account users,
    account companies. Pass `users`/`companies` to override the directory."""
    router.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    router.get(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/users").mock(
        return_value=httpx.Response(200, json=ACCOUNT_USERS_RESPONSE if users is None else users)
    )
    router.get(f"{BASE}/construction/admin/v1/accounts/{ACCOUNT_ID}/companies").mock(
        return_value=httpx.Response(
            200, json=ACCOUNT_COMPANIES_RESPONSE if companies is None else companies
        )
    )


# The two auth helpers are async; patch() auto-uses AsyncMock for async targets.
def _auth_patches():
    return (
        patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN),
        patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN),
    )


async def _call(name, args):
    p1, p2 = _auth_patches()
    with p1, p2:
        return await aps_mcp.call_tool(name, args)


# ---------------------------------------------------------------------------
# list_account_companies
# ---------------------------------------------------------------------------

@respx.mock
async def test_list_account_companies_returns_directory():
    _mock_hub(respx)
    data = _parse(await _call("list_account_companies", {}))
    assert data["count"] == 1
    assert data["companies"][0]["name"] == COMPANY_NAME
    assert data["companies"][0]["company_id"] == COMPANY_ID


# ---------------------------------------------------------------------------
# bulk_add_hub_users
# ---------------------------------------------------------------------------

@respx.mock
async def test_add_hub_users_dry_run_splits_existing_and_new():
    _mock_hub(respx)
    import_route = respx.post(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/users/import")
    data = _parse(await _call("bulk_add_hub_users", {
        "user_emails": [USER_A_EMAIL, NEW_USER_EMAIL],
        "company_name": COMPANY_NAME,
        "default_role": "Project Member",
        "dry_run": True,
    }))
    assert data["dry_run"] is True
    assert not import_route.called                     # no writes on dry run
    assert data["audit_file"] is None
    assert data["summary"]["already_exists"] == 1
    assert data["summary"]["would_add"] == 1
    # default response_detail="changes" drops the already_exists no-op row
    statuses = {r["status"] for r in data["results"]}
    assert statuses == {"would_add"}


@respx.mock
async def test_add_hub_users_company_not_found_errors():
    _mock_hub(respx)
    data = _parse(await _call("bulk_add_hub_users", {
        "user_emails": [NEW_USER_EMAIL],
        "company_name": "Nonexistent Co",
        "dry_run": True,
    }))
    assert "error" in data
    assert "Nonexistent Co" in data["error"]


@respx.mock
async def test_add_hub_users_execute_calls_import():
    _mock_hub(respx)
    import_route = respx.post(
        f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/users/import"
    ).mock(return_value=httpx.Response(200, json=USER_IMPORT_SUCCESS_RESPONSE))
    data = _parse(await _call("bulk_add_hub_users", {
        "user_emails": [NEW_USER_EMAIL],
        "company_name": COMPANY_NAME,
        "default_role": "Project Member",
        "dry_run": False,
    }))
    assert import_route.called
    body = json.loads(import_route.calls[0].request.content)
    assert isinstance(body, list)
    assert body[0]["email"] == NEW_USER_EMAIL
    assert body[0]["company_id"] == COMPANY_ID
    assert body[0]["default_role"] == "Project Member"
    assert data["summary"].get("added") == 1
    assert data["audit_file"] is not None


@respx.mock
async def test_add_hub_users_execute_partial_failure():
    _mock_hub(respx)
    # bob@ is new here (override the directory to contain only alice)
    only_alice = [u for u in ACCOUNT_USERS_RESPONSE if u["email"] == USER_A_EMAIL]
    _mock_hub(respx, users=only_alice)
    envelope = {
        "success": 1, "failure": 1,
        "success_items": [{"email": NEW_USER_EMAIL}],
        "failure_items": [{"email": USER_B_EMAIL, "errors": ["invalid email"]}],
    }
    respx.post(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/users/import").mock(
        return_value=httpx.Response(200, json=envelope)
    )
    data = _parse(await _call("bulk_add_hub_users", {
        "user_emails": [NEW_USER_EMAIL, USER_B_EMAIL],
        "company_name": COMPANY_NAME,
        "dry_run": False,
        "response_detail": "full",
    }))
    by_email = {r["email"]: r["status"] for r in data["results"]}
    assert by_email[NEW_USER_EMAIL] == "added"
    assert by_email[USER_B_EMAIL] == "error"


@respx.mock
async def test_add_hub_users_batches_of_50():
    _mock_hub(respx, users=[])  # empty directory → all emails are new
    import_route = respx.post(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/users/import").mock(
        return_value=httpx.Response(200, json={"success": 30, "failure": 0, "failure_items": []})
    )
    emails = [f"user{i}@example.com" for i in range(60)]
    data = _parse(await _call("bulk_add_hub_users", {
        "user_emails": emails,
        "company_name": COMPANY_NAME,
        "dry_run": False,
    }))
    assert import_route.call_count == 2                # 60 → 50 + 10
    assert data["summary"].get("added") == 60


# ---------------------------------------------------------------------------
# bulk_add_hub_companies
# ---------------------------------------------------------------------------

@respx.mock
async def test_add_hub_companies_dry_run_splits_existing_and_new():
    _mock_hub(respx)
    import_route = respx.post(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/companies/import")
    data = _parse(await _call("bulk_add_hub_companies", {
        "companies": [
            {"name": COMPANY_NAME, "trade": "General Contractor"},
            {"name": NEW_COMPANY_NAME, "trade": "Electrical"},
        ],
        "dry_run": True,
    }))
    assert not import_route.called
    assert data["summary"]["already_exists"] == 1
    assert data["summary"]["would_add"] == 1


@respx.mock
async def test_add_hub_companies_missing_trade_errors():
    _mock_hub(respx)
    data = _parse(await _call("bulk_add_hub_companies", {
        "companies": [{"name": NEW_COMPANY_NAME}],   # no trade
        "dry_run": True,
        "response_detail": "full",
    }))
    assert data["results"][0]["status"] == "error"


@respx.mock
async def test_add_hub_companies_execute_calls_import():
    _mock_hub(respx)
    import_route = respx.post(
        f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/companies/import"
    ).mock(return_value=httpx.Response(201, json=COMPANY_IMPORT_SUCCESS_RESPONSE))
    data = _parse(await _call("bulk_add_hub_companies", {
        "companies": [{"name": NEW_COMPANY_NAME, "trade": "Electrical", "city": "Brussels"}],
        "dry_run": False,
    }))
    assert import_route.called
    body = json.loads(import_route.calls[0].request.content)
    assert isinstance(body, list)
    assert body[0]["name"] == NEW_COMPANY_NAME
    assert body[0]["trade"] == "Electrical"
    assert data["summary"].get("added") == 1
    assert data["audit_file"] is not None


@respx.mock
async def test_add_hub_companies_batches_of_50():
    _mock_hub(respx)
    import_route = respx.post(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/companies/import").mock(
        return_value=httpx.Response(201, json={"success": 30, "failure": 0, "failure_items": []})
    )
    companies = [{"name": f"Co {i}", "trade": "Trade"} for i in range(60)]
    data = _parse(await _call("bulk_add_hub_companies", {
        "companies": companies, "dry_run": False,
    }))
    assert import_route.call_count == 2
    assert data["summary"].get("added") == 60


# ---------------------------------------------------------------------------
# deactivate_hub_users
# ---------------------------------------------------------------------------

@respx.mock
async def test_deactivate_hub_users_dry_run():
    _mock_hub(respx)
    patch_route = respx.patch(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/users/{USER_A_ID}")
    data = _parse(await _call("deactivate_hub_users", {
        "user_emails": [USER_A_EMAIL, "ghost@example.com"],
        "dry_run": True,
        "response_detail": "full",
    }))
    assert not patch_route.called
    by_email = {r["email"]: r["status"] for r in data["results"]}
    assert by_email[USER_A_EMAIL] == "would_deactivate"
    assert by_email["ghost@example.com"] == "not_found"


@respx.mock
async def test_deactivate_hub_users_execute():
    _mock_hub(respx)
    patch_route = respx.patch(
        f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/users/{USER_A_ID}"
    ).mock(return_value=httpx.Response(200, json={"id": USER_A_ID, "status": "inactive"}))
    data = _parse(await _call("deactivate_hub_users", {
        "user_emails": [USER_A_EMAIL],
        "dry_run": False,
    }))
    assert patch_route.called
    assert json.loads(patch_route.calls[0].request.content) == {"status": "inactive"}
    assert data["summary"].get("deactivated") == 1
    assert data["audit_file"] is not None


# ---------------------------------------------------------------------------
# deactivate_hub_companies
# ---------------------------------------------------------------------------

@respx.mock
async def test_deactivate_hub_companies_dry_run():
    _mock_hub(respx)
    patch_route = respx.patch(f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/companies/{COMPANY_ID}")
    data = _parse(await _call("deactivate_hub_companies", {
        "company_names": [COMPANY_NAME, "Ghost Co"],
        "dry_run": True,
        "response_detail": "full",
    }))
    assert not patch_route.called
    by_name = {r["name"]: r["status"] for r in data["results"]}
    assert by_name[COMPANY_NAME] == "would_deactivate"
    assert by_name["Ghost Co"] == "not_found"


@respx.mock
async def test_deactivate_hub_companies_execute():
    _mock_hub(respx)
    patch_route = respx.patch(
        f"{BASE}/hq/v1/accounts/{ACCOUNT_ID}/companies/{COMPANY_ID}"
    ).mock(return_value=httpx.Response(200, json={"id": COMPANY_ID, "status": "inactive"}))
    data = _parse(await _call("deactivate_hub_companies", {
        "company_names": [COMPANY_NAME],
        "dry_run": False,
    }))
    assert patch_route.called
    assert json.loads(patch_route.calls[0].request.content) == {"status": "inactive"}
    assert data["summary"].get("deactivated") == 1
