"""
Read-only mode (`FORMA_MCP_READ_ONLY=1` / `forma-mcp --read-only`): tokens are requested
with read scopes only, and the read-only login is stored apart from the full-scope one.
"""
import json
import os
import urllib.parse

import httpx
import pytest
import respx

import aps_mcp
from tests.test_auth import FakeKeyring

TOKEN_URL = f"{aps_mcp.APS_BASE}/authentication/v2/token"


@pytest.fixture
def fake_keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(aps_mcp, "_keyring", lambda: kr)
    return kr


@pytest.fixture
def read_only(monkeypatch):
    monkeypatch.setenv("FORMA_MCP_READ_ONLY", "1")


def _key(name):
    return (aps_mcp.KEYRING_SERVICE, f"{name}:{aps_mcp.APS_CLIENT_ID}")


def _form(request):
    return {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}


def _scope_in_auth_url():
    url = aps_mcp._build_auth_url("state", "challenge")
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0]


def test_default_mode_requests_full_scopes():
    assert not aps_mcp._read_only()
    assert _scope_in_auth_url() == aps_mcp.SCOPES


def test_read_only_login_requests_only_read_scopes(read_only):
    scope = _scope_in_auth_url()
    assert scope == "data:read account:read"
    assert "write" not in scope and "create" not in scope


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_read_only_env_values(monkeypatch, value):
    monkeypatch.setenv("FORMA_MCP_READ_ONLY", value)
    assert aps_mcp._read_only()


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_read_only_env_off_values(monkeypatch, value):
    monkeypatch.setenv("FORMA_MCP_READ_ONLY", value)
    assert not aps_mcp._read_only()


@respx.mock
async def test_read_only_app_token_requests_account_read_only(read_only):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "app", "expires_in": 3600}))
    assert await aps_mcp.get_app_token() == "app"
    assert _form(route.calls[0].request)["scope"] == "account:read"


@respx.mock
async def test_default_app_token_keeps_write_scope():
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "app", "expires_in": 3600}))
    await aps_mcp.get_app_token()
    assert _form(route.calls[0].request)["scope"] == "account:read account:write"


@respx.mock
async def test_read_only_never_reuses_full_scope_refresh_token(fake_keyring, read_only, monkeypatch):
    """A full-scope login already in the keychain must not be picked up: read-only mode
    looks only at its own entry, so with none stored it falls through to a browser login."""
    fake_keyring.store[_key("refresh_token")] = "full_scope_rt"
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))
    assert await aps_mcp._refresh_from_store() is None
    assert not route.called


@respx.mock
async def test_read_only_refresh_uses_own_token_and_narrow_scope(fake_keyring, read_only):
    fake_keyring.store[_key("refresh_token")] = "full_scope_rt"
    fake_keyring.store[_key("refresh_token_readonly")] = "ro_rt"
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "at", "refresh_token": "ro_rt2", "expires_in": 3600}))
    assert await aps_mcp.get_access_token() == "at"
    form = _form(route.calls[0].request)
    assert form["refresh_token"] == "ro_rt"
    assert form["scope"] == "data:read account:read"
    assert fake_keyring.store[_key("refresh_token_readonly")] == "ro_rt2"
    assert fake_keyring.store[_key("refresh_token")] == "full_scope_rt"  # untouched


def test_read_only_plaintext_fallback_uses_separate_file(read_only, tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"refresh_token": "legacy_full_scope"}))
    monkeypatch.setattr(aps_mcp, "LEGACY_TOKEN_FILE", str(legacy))
    assert aps_mcp._token_file().endswith("tokens.readonly.json")
    assert aps_mcp._load_refresh_token() is None  # legacy (full-scope) file ignored
    aps_mcp._save_refresh_token("ro_rt")
    with open(aps_mcp._token_file()) as f:
        assert json.load(f) == {"refresh_token": "ro_rt"}
    assert legacy.exists()  # the full-scope file is neither read nor removed


def test_read_only_logout_leaves_full_scope_login(fake_keyring, read_only):
    fake_keyring.store[_key("refresh_token")] = "full"
    fake_keyring.store[_key("refresh_token_readonly")] = "ro"
    aps_mcp._clear_tokens()
    assert _key("refresh_token_readonly") not in fake_keyring.store
    assert fake_keyring.store[_key("refresh_token")] == "full"


def test_cli_flag_enables_read_only(monkeypatch):
    called = {}
    monkeypatch.setenv("APS_CLIENT_ID", "cid")
    monkeypatch.setenv("APS_CLIENT_SECRET", "sec")
    monkeypatch.setattr(aps_mcp, "main", lambda: None)
    monkeypatch.setattr(aps_mcp.asyncio, "run", lambda coro: called.setdefault("ro", aps_mcp._read_only()))
    aps_mcp.run(["--read-only"])
    assert called["ro"] is True


def test_auth_status_reports_mode(monkeypatch, capsys):
    monkeypatch.setenv("APS_CLIENT_ID", "cid")
    with pytest.raises(SystemExit):
        aps_mcp.run(["auth", "status", "--read-only"])
    assert "READ-ONLY" in capsys.readouterr().out
