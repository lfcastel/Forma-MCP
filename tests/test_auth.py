"""
Credential storage (OS keychain + plaintext fallback), the client-secret lookup,
the PKCE/state login flow, the in-memory access token, and `forma-mcp auth ...`.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import httpx
import pytest
import respx

import aps_mcp

TOKEN_URL = f"{aps_mcp.APS_BASE}/authentication/v2/token"


class FakeKeyring:
    """Stands in for the `keyring` module (get/set/delete_password + get_keyring)."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        if (service, user) not in self.store:
            raise KeyError(user)
        del self.store[(service, user)]

    def get_keyring(self):
        return self


@pytest.fixture
def fake_keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(aps_mcp, "_keyring", lambda: kr)
    return kr


def _rt_key():
    return (aps_mcp.KEYRING_SERVICE, f"refresh_token:{aps_mcp.APS_CLIENT_ID}")


def _write_plain_tokens(data: dict) -> str:
    os.makedirs(aps_mcp._data_dir(), exist_ok=True)
    path = aps_mcp._token_file()
    with open(path, "w") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# Keychain detection
# ---------------------------------------------------------------------------

def test_keyring_disabled_by_env(monkeypatch):
    monkeypatch.setenv("FORMA_MCP_KEYRING", "0")
    assert aps_mcp._keyring() is None


def test_keyring_rejects_fail_backend(monkeypatch):
    import keyring
    from keyring.backends import fail
    monkeypatch.setenv("FORMA_MCP_KEYRING", "1")
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    assert aps_mcp._keyring() is None


# ---------------------------------------------------------------------------
# Refresh-token storage
# ---------------------------------------------------------------------------

def test_refresh_token_saved_to_keychain_not_disk(fake_keyring):
    aps_mcp._save_refresh_token("rt1")
    assert fake_keyring.store[_rt_key()] == "rt1"
    assert not os.path.exists(aps_mcp._token_file())
    assert aps_mcp._load_refresh_token() == "rt1"


def test_plaintext_tokens_migrate_into_keychain_and_file_is_deleted(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr(aps_mcp, "LEGACY_TOKEN_FILE", str(tmp_path / "legacy.json"))
    path = _write_plain_tokens({"access_token": "a", "refresh_token": "old_rt", "expires_at": 1})
    assert aps_mcp._load_refresh_token() == "old_rt"
    assert fake_keyring.store[_rt_key()] == "old_rt"
    assert not os.path.exists(path)


def test_legacy_file_next_to_script_is_migrated_too(fake_keyring, tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"refresh_token": "legacy_rt"}))
    monkeypatch.setattr(aps_mcp, "LEGACY_TOKEN_FILE", str(legacy))
    assert aps_mcp._load_refresh_token() == "legacy_rt"
    assert not legacy.exists()


def test_file_fallback_stores_only_the_refresh_token(monkeypatch, tmp_path):
    monkeypatch.setattr(aps_mcp, "LEGACY_TOKEN_FILE", str(tmp_path / "legacy.json"))
    aps_mcp._remember_tokens({"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    with open(aps_mcp._token_file()) as f:
        assert json.load(f) == {"refresh_token": "rt"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_file_fallback_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(aps_mcp, "LEGACY_TOKEN_FILE", str(tmp_path / "legacy.json"))
    aps_mcp._save_refresh_token("rt")
    assert os.stat(aps_mcp._token_file()).st_mode & 0o777 == 0o600


def test_clear_tokens_removes_keychain_entry_file_and_memory(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr(aps_mcp, "LEGACY_TOKEN_FILE", str(tmp_path / "legacy.json"))
    fake_keyring.store[_rt_key()] = "rt"
    path = _write_plain_tokens({"refresh_token": "x"})
    aps_mcp._user_token_cache.update(token="at", expires_at=time.time() + 3600)
    aps_mcp._clear_tokens()
    assert _rt_key() not in fake_keyring.store
    assert not os.path.exists(path)
    assert aps_mcp._user_token_cache["token"] is None


# ---------------------------------------------------------------------------
# Client secret
# ---------------------------------------------------------------------------

def test_client_secret_prefers_env(fake_keyring, monkeypatch):
    fake_keyring.store[(aps_mcp.KEYRING_SERVICE, f"client_secret:{aps_mcp.APS_CLIENT_ID}")] = "from_kr"
    monkeypatch.setenv("APS_CLIENT_SECRET", "from_env")
    assert aps_mcp._client_secret() == "from_env"


def test_client_secret_falls_back_to_keychain(fake_keyring, monkeypatch):
    fake_keyring.store[(aps_mcp.KEYRING_SERVICE, f"client_secret:{aps_mcp.APS_CLIENT_ID}")] = "from_kr"
    monkeypatch.delenv("APS_CLIENT_SECRET")
    assert aps_mcp._client_secret() == "from_kr"
    assert "from_kr" in __import__("base64").b64decode(aps_mcp._get_basic_auth_header()).decode()


def test_run_accepts_keychain_secret_without_env(fake_keyring, monkeypatch):
    fake_keyring.store[(aps_mcp.KEYRING_SERVICE, f"client_secret:{aps_mcp.APS_CLIENT_ID}")] = "s"
    monkeypatch.delenv("APS_CLIENT_SECRET")
    started = []
    monkeypatch.setattr(aps_mcp, "main", lambda: started.append(True) or _noop())
    aps_mcp.run([])
    assert started == [True]


async def _noop():
    return None


def test_run_exits_when_secret_missing_everywhere(monkeypatch):
    monkeypatch.delenv("APS_CLIENT_SECRET")
    with pytest.raises(SystemExit) as exc:
        aps_mcp.run([])
    assert "APS_CLIENT_SECRET" in str(exc.value)
    assert "auth set-secret" in str(exc.value)


# ---------------------------------------------------------------------------
# Access token (memory only) + refresh
# ---------------------------------------------------------------------------

@respx.mock
async def test_access_token_is_cached_in_memory(fake_keyring):
    fake_keyring.store[_rt_key()] = "rt0"
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600}))
    assert await aps_mcp.get_access_token() == "at1"
    assert await aps_mcp.get_access_token() == "at1"
    assert route.call_count == 1
    assert fake_keyring.store[_rt_key()] == "rt1"  # rotated token persisted
    assert "at1" not in fake_keyring.store.values()  # access token never persisted


@respx.mock
async def test_refresh_retries_with_token_rotated_by_another_process(fake_keyring, monkeypatch):
    fake_keyring.store[_rt_key()] = "stale"
    seen = []

    def _token(request):
        rt = urllib.parse.parse_qs(request.content.decode())["refresh_token"][0]
        seen.append(rt)
        if rt == "stale":
            # Simulate a parallel forma-mcp process having rotated it meanwhile
            fake_keyring.store[_rt_key()] = "fresh"
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "next", "expires_in": 3600})

    respx.post(TOKEN_URL).mock(side_effect=_token)
    assert await aps_mcp.get_access_token() == "at"
    assert seen == ["stale", "fresh"]
    assert fake_keyring.store[_rt_key()] == "next"


@respx.mock
async def test_force_refresh_ignores_cached_access_token(fake_keyring):
    fake_keyring.store[_rt_key()] = "rt"
    aps_mcp._user_token_cache.update(token="revoked", expires_at=time.time() + 3600)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "new_at", "refresh_token": "rt2", "expires_in": 3600}))
    assert await aps_mcp._force_refresh_access_token() == "new_at"


# ---------------------------------------------------------------------------
# Browser login: state + PKCE
# ---------------------------------------------------------------------------

def test_auth_url_carries_state_and_pkce_challenge():
    verifier, challenge = aps_mcp._pkce_pair()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(aps_mcp._build_auth_url("st", challenge)).query)
    assert qs["state"] == ["st"]
    assert qs["code_challenge"] == [challenge]
    assert qs["code_challenge_method"] == ["S256"]
    assert 43 <= len(verifier) <= 128


def test_pkce_challenge_is_s256_of_verifier():
    import base64
    import hashlib
    verifier, challenge = aps_mcp._pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected


def _hit(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _run_callback_server(state, timeout=10):
    result = {}

    def target():
        try:
            result["code"] = aps_mcp._wait_for_callback(state, timeout)
        except Exception as e:  # noqa: BLE001
            result["error"] = e

    t = threading.Thread(target=target)
    t.start()
    time.sleep(0.3)  # let the server bind
    return t, result


def test_callback_rejects_wrong_state_then_accepts_right_one():
    t, result = _run_callback_server("good-state")
    assert _hit("http://localhost:8080/oauth/callback?code=evil&state=bad") == 400
    assert _hit("http://localhost:8080/oauth/callback?code=evil") == 400
    assert _hit("http://localhost:8080/oauth/callback?code=real&state=good-state") == 200
    t.join(10)
    assert result == {"code": "real"}


def test_callback_reports_denied_consent():
    t, result = _run_callback_server("s")
    _hit("http://localhost:8080/oauth/callback?error=access_denied&state=s")
    t.join(10)
    assert "access_denied" in str(result["error"])


def test_callback_times_out():
    with pytest.raises(RuntimeError, match="Timed out"):
        aps_mcp._wait_for_callback("s", timeout=0.5)


@respx.mock
async def test_browser_login_sends_code_verifier(fake_keyring, monkeypatch):
    captured = {}

    def fake_wait(state, timeout=None):
        captured["state"] = state
        return "the_code"

    monkeypatch.setattr(aps_mcp, "_wait_for_callback", fake_wait)
    monkeypatch.setattr(aps_mcp.webbrowser, "open", lambda url: captured.setdefault("url", url))

    def _token(request):
        captured["form"] = urllib.parse.parse_qs(request.content.decode())
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

    respx.post(TOKEN_URL).mock(side_effect=_token)
    assert await aps_mcp.get_access_token() == "at"
    url_qs = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
    assert url_qs["state"] == [captured["state"]]
    verifier = captured["form"]["code_verifier"][0]
    import base64
    import hashlib
    assert url_qs["code_challenge"][0] == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert fake_keyring.store[_rt_key()] == "rt"


# ---------------------------------------------------------------------------
# `forma-mcp auth ...`
# ---------------------------------------------------------------------------

def test_cli_set_secret_stores_in_keychain(fake_keyring, monkeypatch):
    monkeypatch.setattr(aps_mcp.getpass, "getpass", lambda prompt: "  s3cret ")
    with pytest.raises(SystemExit) as exc:
        aps_mcp.run(["auth", "set-secret", "--client-id", "cid"])
    assert exc.value.code == 0
    assert fake_keyring.store[(aps_mcp.KEYRING_SERVICE, "client_secret:cid")] == "s3cret"


def test_cli_set_secret_refuses_without_keychain(monkeypatch):
    monkeypatch.setattr(aps_mcp.getpass, "getpass", lambda prompt: "s")
    with pytest.raises(SystemExit) as exc:
        aps_mcp.run(["auth", "set-secret"])
    assert exc.value.code == 1


def test_cli_delete_secret(fake_keyring):
    fake_keyring.store[(aps_mcp.KEYRING_SERVICE, "client_secret:cid")] = "s"
    with pytest.raises(SystemExit):
        aps_mcp.run(["auth", "delete-secret", "--client-id", "cid"])
    assert not fake_keyring.store


def test_cli_logout_clears_tokens(fake_keyring):
    fake_keyring.store[_rt_key()] = "rt"
    with pytest.raises(SystemExit) as exc:
        aps_mcp.run(["auth", "logout"])
    assert exc.value.code == 0
    assert _rt_key() not in fake_keyring.store


def test_cli_status_never_prints_secrets(fake_keyring, monkeypatch, capsys):
    monkeypatch.delenv("APS_CLIENT_SECRET")
    fake_keyring.store[(aps_mcp.KEYRING_SERVICE, f"client_secret:{aps_mcp.APS_CLIENT_ID}")] = "TOPSECRET"
    fake_keyring.store[_rt_key()] = "REFRESHTOKEN"
    with pytest.raises(SystemExit):
        aps_mcp.run(["auth", "status"])
    out = capsys.readouterr().out
    assert "OS keychain" in out
    assert "TOPSECRET" not in out and "REFRESHTOKEN" not in out
