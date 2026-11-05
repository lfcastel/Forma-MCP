"""
Regression tests for the Aikido AI-code-analysis findings (Nov 2026):
company scoping, offboarding scope, clone name collisions, UUID fast-path disclosure,
symlink-safe token writes, review size limit, bounded gather, list-size caps,
audit-file collisions and the OAuth callback port.
"""
import asyncio
import copy
import json
import os
from unittest.mock import patch

import httpx
import pytest
import respx

import aps_mcp
from conftest import (
    FAKE_TOKEN, FAKE_APP_TOKEN, HUB_ID, ACCOUNT_ID, PROJECT_ID, BARE_PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE, PROJECT_MEMBERS_RESPONSE, PROJECT_ROLES_RESPONSE,
    ACCOUNT_USERS_RESPONSE, USER_A_EMAIL, USER_B_EMAIL, COMPANY_NAME, ROLE_NAME_VIEWER,
    ROLE_NAME_EDITOR,
)
from test_bulk_tools import _mock_common

BASE = aps_mcp.APS_BASE


def _parse(result):
    return json.loads(result[0].text)


async def _call(name, args):
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN), \
         patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        return await aps_mcp.call_tool(name, args)


# --- Empty / ambiguous company selector ----------------------------------------------

@pytest.mark.parametrize("value", ["", "   "])
async def test_empty_company_name_is_rejected(value):
    with pytest.raises(ValueError, match="must not be empty"):
        await _call("bulk_assign_company_users", {
            "company_name": value, "project_names": [PROJECT_NAME], "default_role": "Viewer"})


async def test_ambiguous_company_name_is_refused():
    roster = {"a@x.com": {"company_name": "Acme Steel"}, "b@x.com": {"company_name": "Acme Glass"}}
    with patch("aps_mcp._get_all_accounts_users_map", return_value=roster):
        data = _parse(await _call("bulk_assign_company_users", {
            "company_name": "acme", "project_names": [PROJECT_NAME], "default_role": "Viewer"}))
    assert data["error"] == "ambiguous_company"
    assert data["matches"] == ["Acme Glass", "Acme Steel"]


@respx.mock
async def test_exact_company_wins_over_partial_matches():
    _mock_common(respx)
    roster = {u["email"]: u for u in ACCOUNT_USERS_RESPONSE}
    roster["intruder@x.com"] = {"email": "intruder@x.com", "company_name": COMPANY_NAME + " Glass"}
    with patch("aps_mcp._get_all_accounts_users_map", return_value=roster):
        data = _parse(await _call("bulk_assign_company_users", {
            "company_name": COMPANY_NAME, "project_names": [PROJECT_NAME],
            "default_role": ROLE_NAME_VIEWER, "dry_run": True}))
    assert data["users_found"] == 2
    assert all(r["user"] != "intruder@x.com" for r in data["results"])


# --- Offboarding scope ----------------------------------------------------------------

@respx.mock
async def test_offboarding_reports_single_hub_scope():
    _mock_common(respx)
    data = _parse(await _call("remove_users_from_projects", {
        "user_emails": [USER_A_EMAIL], "project_names": [], "dry_run": True}))
    assert data["scope"]["hubs_scanned"] == [HUB_ID]
    assert "all_hubs=true" in data["scope"]["note"]


@respx.mock
async def test_offboarding_all_hubs_scans_every_hub():
    hub2 = "b.second-hub"
    hubs = copy.deepcopy(HUB_RESPONSE)
    hubs["data"].append({"id": hub2, "type": "hubs", "attributes": {"name": "Second Hub", "region": "EMEA"}})
    _mock_common(respx)
    respx.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=hubs))
    p2 = respx.get(f"{BASE}/project/v1/hubs/{hub2}/projects").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {"pagination": {"totalResults": 0}}}))
    data = _parse(await _call("remove_users_from_projects", {
        "user_emails": [USER_A_EMAIL], "project_names": [], "all_hubs": True, "dry_run": True}))
    assert p2.called
    assert set(data["scope"]["hubs_scanned"]) == {"Test Hub", "Second Hub"}
    assert data["scope"]["note"] is None


# --- clone_user_access: same-named projects keep their own roles ------------------------

@respx.mock
async def test_clone_same_named_projects_keep_their_own_roles():
    pid2, bare2 = "b.other-proj-id", "other-proj-id"
    projects = copy.deepcopy(PROJECTS_RESPONSE)
    projects["data"].append({"id": pid2, "attributes": {"name": PROJECT_NAME, "status": "active"}})
    projects["meta"]["pagination"]["totalResults"] = 2
    members2 = copy.deepcopy(PROJECT_MEMBERS_RESPONSE)
    members2["results"][0]["roles"] = [{"id": "role-editor-only", "name": ROLE_NAME_EDITOR}]
    _mock_common(respx)
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(return_value=httpx.Response(200, json=projects))
    respx.get(f"{BASE}/projects/{pid2}/users").mock(return_value=httpx.Response(200, json=PROJECT_ROLES_RESPONSE))
    respx.get(f"{BASE}/construction/admin/v1/projects/{bare2}/users").mock(
        return_value=httpx.Response(200, json=members2))
    data = _parse(await _call("clone_user_access", {
        "reference_user_email": USER_A_EMAIL, "target_user_emails": [USER_B_EMAIL], "dry_run": True}))
    roles_by_project = {r["project_id"] if "project_id" in r else i: {x.strip() for x in r["role"].split(",")}
                        for i, r in enumerate(data["results"])}
    assert sorted(map(sorted, roles_by_project.values())) == [
        [ROLE_NAME_EDITOR], sorted([ROLE_NAME_VIEWER, ROLE_NAME_EDITOR])]


# --- UUID fast path doesn't disclose a project in an invisible hub -----------------------

@respx.mock
async def test_uuid_fast_path_hides_project_in_unlisted_hub():
    uuid = "3f2c1a9e-1111-2222-3333-444455556666"
    respx.get(f"{BASE}/construction/admin/v1/projects/{uuid}").mock(return_value=httpx.Response(
        200, json={"accountId": "some-other-account", "name": "Secret Project", "platform": "acc"}))
    respx.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    with patch("aps_mcp.get_app_token", return_value=FAKE_APP_TOKEN):
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError) as exc:
                await aps_mcp._resolve_by_id(client, FAKE_TOKEN, uuid, is_url=False)
    assert "not found" in str(exc.value) and "Secret Project" not in str(exc.value)


# --- Symlink-safe token file ----------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_token_write_replaces_planted_symlink(tmp_path):
    victim = tmp_path / "attacker_owned.txt"
    victim.write_text("untouched")
    os.makedirs(aps_mcp._data_dir(), exist_ok=True)
    os.symlink(victim, aps_mcp._token_file())
    aps_mcp._save_refresh_token("rt-secret")
    assert victim.read_text() == "untouched"                # never followed
    assert not os.path.islink(aps_mcp._token_file())       # replaced by a real file
    assert oct(os.stat(aps_mcp._token_file()).st_mode & 0o777) == "0o600"
    assert oct(os.stat(aps_mcp._data_dir()).st_mode & 0o777) == "0o700"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_token_read_ignores_symlink(tmp_path):
    target = tmp_path / "t.json"
    target.write_text(json.dumps({"refresh_token": "planted"}))
    os.makedirs(aps_mcp._data_dir(), exist_ok=True)
    os.symlink(target, aps_mcp._token_file())
    assert aps_mcp._read_plaintext_refresh_token() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_data_dir_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    monkeypatch.setenv("FORMA_MCP_HOME", str(link))
    with pytest.raises(RuntimeError, match="symbolic link"):
        aps_mcp._save_refresh_token("rt")


# --- Resource limits --------------------------------------------------------------------

async def test_create_review_size_checked_before_any_request():
    args = {"file_versions": ["urn:x"] * 600, "files": [{"folder_path": "A", "file_name": "b"}] * 401}
    with patch("aps_mcp._resolve_folder") as rf:
        with pytest.raises(ValueError, match="at most 1000"):
            await aps_mcp._resolve_review_file_versions(None, FAKE_TOKEN, HUB_ID, PROJECT_ID, {}, args)
    rf.assert_not_called()


async def test_gather_bounded_keeps_order_and_concurrency():
    running = 0
    peak = 0

    def make(i):
        async def f():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            running -= 1
            return i
        return f

    out = await aps_mcp._gather_bounded(3, [make(i) for i in range(50)])
    assert out == list(range(50)) and peak <= 3
    assert await aps_mcp._gather_bounded(4, []) == []


async def test_oversized_list_argument_rejected():
    result = await aps_mcp.call_tool("deactivate_hub_companies",
                                     {"company_names": ["Acme"] * (aps_mcp._MAX_LIST_ITEMS + 1)})
    assert result.isError is True
    assert "at most" in json.loads(result.content[0].text)["message"]


# --- Audit files never collide -----------------------------------------------------------

def test_audit_files_unique_within_same_second():
    paths = {aps_mcp._write_audit_csv([{"a": i}], "op") for i in range(20)}
    assert len(paths) == 20


# --- OAuth callback port ------------------------------------------------------------------

def test_callback_port_in_use_gives_clear_error(monkeypatch):
    def boom(*a, **k):
        raise OSError(48, "Address already in use")
    monkeypatch.setattr(aps_mcp, "HTTPServer", boom)
    with pytest.raises(RuntimeError, match="APS_CALLBACK_PORT"):
        aps_mcp._bind_callback_server()
