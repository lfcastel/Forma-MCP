import os
import io
import re
import csv
import zipfile
import base64
import json
import time
import asyncio
import webbrowser
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Any
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, CallToolResult

APS_CLIENT_ID = os.environ["APS_CLIENT_ID"]
APS_CLIENT_SECRET = os.environ["APS_CLIENT_SECRET"]
APS_BASE = "https://developer.api.autodesk.com"
REDIRECT_URI = "http://localhost:8080/oauth/callback"
SCOPES = "data:read data:write data:create account:read account:write"

# The ACC Issues API routes per-region; all our Issues traffic is EMEA. Sent as
# the `x-ads-region` header on every Issues call (avoids the auto-route latency).
# One-line change here if a US hub is ever added.
ISSUES_REGION = "EMEA"

# The ACC Reviews (approval workflows) API is region-routed just like Issues; all
# Brussels Airport traffic is EMEA. Sent as `x-ads-region` on every Reviews call.
REVIEWS_REGION = "EMEA"

# tokens.json sits next to this script
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")

app = Server("aps-mcp")

# Default product access granted to users added/updated via bulk tools.
# docs = document management (core); insight = required by the Admin API.
DEFAULT_PRODUCTS = [
    {"key": "docs", "access": "member"},
    {"key": "insight", "access": "member"},
]

# The project `users:import` call is asynchronous (returns 202 + a jobId), so after a
# live bulk assign we poll the members list to confirm each user landed with their roles.
# Best-effort: a user unconfirmed within the budget is reported as `submitted`, not failed.
# (Tests set the delay to 0 so they never actually sleep.)
_ASSIGN_POLL_ATTEMPTS = 10
_ASSIGN_POLL_DELAY = 3.0

# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _load_tokens() -> dict:
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tokens(data: dict) -> None:
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# 3-legged OAuth flow
# ---------------------------------------------------------------------------

def _build_auth_url() -> str:
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": APS_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    })
    return f"{APS_BASE}/authentication/v2/authorize?{params}"


def _wait_for_callback() -> str:
    """Spin up a one-shot HTTP server on port 8080 and block until the auth code arrives."""
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            if code:
                received["code"] = code
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Authorization successful. You can close this tab.</h2>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>Missing code parameter.</h2>")

        def log_message(self, *args):
            pass  # suppress access log noise

    server = HTTPServer(("localhost", 8080), Handler)
    server.timeout = 120  # 2 min to complete login
    while "code" not in received:
        server.handle_request()
    server.server_close()
    return received["code"]


def _get_basic_auth_header() -> str:
    """Build the Basic auth header value for client_credentials flows."""
    return base64.b64encode(
        f"{APS_CLIENT_ID}:{APS_CLIENT_SECRET}".encode()
    ).decode()


async def _exchange_code(code: str) -> dict:
    creds = _get_basic_auth_header()
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{APS_BASE}/authentication/v2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )
        res.raise_for_status()
        return res.json()


async def _refresh_tokens(refresh_token: str) -> dict:
    creds = _get_basic_auth_header()
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{APS_BASE}/authentication/v2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        res.raise_for_status()
        return res.json()


async def get_access_token() -> str:
    """Return a valid 3-legged access token, refreshing or re-authorizing as needed."""
    now = time.time()
    stored = _load_tokens()

    # Happy path: valid access token still in window
    if stored.get("access_token") and now < stored.get("expires_at", 0) - 60:
        return stored["access_token"]

    # Try refresh if we have a refresh token
    if stored.get("refresh_token"):
        try:
            data = await _refresh_tokens(stored["refresh_token"])
            stored = {
                "access_token": data["access_token"],
                # APS rotates refresh tokens — always save the new one
                "refresh_token": data.get("refresh_token", stored["refresh_token"]),
                "expires_at": now + data.get("expires_in", 3600),
            }
            _save_tokens(stored)
            return stored["access_token"]
        except httpx.HTTPStatusError:
            # Refresh token expired (14-day window passed) — fall through to re-auth
            pass

    # Full re-authorization needed — open browser and wait for callback
    auth_url = _build_auth_url()
    print(f"\n[APS Auth] Opening browser for authorization...\n{auth_url}\n", flush=True)
    webbrowser.open(auth_url)

    # _wait_for_callback blocks the thread; run in executor so asyncio loop stays alive
    loop = asyncio.get_event_loop()
    code = await loop.run_in_executor(None, _wait_for_callback)

    data = await _exchange_code(code)
    stored = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": now + data.get("expires_in", 3600),
    }
    _save_tokens(stored)
    return stored["access_token"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _force_refresh_access_token() -> str:
    """Force a fresh 3-legged access token, ignoring the cached-token window.

    Used when APS returns 401 mid-batch: a long bulk run can outlive a token
    even though our cached `expires_at` hasn't elapsed (e.g. the token was
    revoked). Refreshes via the stored refresh token; falls back to the full
    `get_access_token` flow (browser re-auth) if no refresh token is available.
    """
    stored = _load_tokens()
    if stored.get("refresh_token"):
        data = await _refresh_tokens(stored["refresh_token"])
        now = time.time()
        stored = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", stored["refresh_token"]),
            "expires_at": now + data.get("expires_in", 3600),
        }
        _save_tokens(stored)
        return stored["access_token"]
    return await get_access_token()


def _bearer_refresher(hdrs: dict):
    """Return an async callback that force-refreshes the 3-legged token and
    updates `hdrs['Authorization']` in place.

    Pass the result as `on_unauthorized` to `_request_with_retry` (directly or
    via `get_all_folder_contents`) so a single shared headers dict keeps working
    across a token expiry without re-resolving anything. Guarded by an internal
    lock so concurrent bulk requests trigger at most one refresh."""
    lock = asyncio.Lock()

    async def _refresh() -> None:
        async with lock:
            current = hdrs.get("Authorization")
            new_token = await _force_refresh_access_token()
            new_auth = f"Bearer {new_token}"
            # If another concurrent request already refreshed, don't refresh again.
            if hdrs.get("Authorization") == current:
                hdrs["Authorization"] = new_auth

    return _refresh


def _to_bare_id(aps_id: str) -> str:
    """Strip the 'b.' prefix from APS hub/project IDs."""
    return aps_id.removeprefix("b.")


def _ensure_b_prefix(aps_id: str) -> str:
    """Re-add the 'b.' prefix used by Data-Management hub/project IDs (idempotent)."""
    return aps_id if aps_id.startswith("b.") else f"b.{aps_id}"


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _extract_uuid(query: str) -> str | None:
    """Return the first UUID found in a bare ID, 'b.'-ID, or ACC URL — else None.

    A UUID means the query is an ID/URL (fast Admin-API path); its absence means
    the query is a project name (fan-out-by-name path)."""
    if not query:
        return None
    m = _UUID_RE.search(query)
    return m.group(0).lower() if m else None


def _is_url(query: str) -> bool:
    return "://" in (query or "")


def _extract_response_items(data: "Any") -> list:
    """Extract an item list from APS responses that use 'results', 'data', or bare lists."""
    if isinstance(data, list):
        return data
    return data.get("results", data.get("data", []))


def _norm_emails(emails: list[str]) -> list[str]:
    return [e.lower().strip() for e in emails]


def _norm_region(arguments: dict) -> str:
    return (arguments.get("region") or "EMEA").strip().upper()


def _error_body(r: "httpx.Response") -> "Any":
    """Return parsed JSON from a response, falling back to raw text."""
    try:
        return r.json()
    except Exception:
        return r.text


class APSQuotaError(Exception):
    """Raised when APS responds 429 (rate/quota limit) and the call cannot proceed.

    Carries a user-facing message and the `Retry-After` hint (seconds) if present,
    so the tool can report a clean "can't proceed right now" result instead of
    hanging on long retries or surfacing an opaque exception.
    """

    def __init__(self, message: str, retry_after: "int | None" = None):
        super().__init__(message)
        self.retry_after = retry_after


def _safe_int(value: "Any") -> "int | None":
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_quota_429(r: "httpx.Response") -> bool:
    """Whether a 429 is a hard quota limit (vs a transient rate spike).

    APS quota errors carry wording like `"developerMessage": "Quota limit
    exceeded."` — retrying within seconds won't help, so we fail fast on these.
    """
    try:
        return "quota" in json.dumps(r.json()).lower()
    except Exception:
        return False


def _quota_message(r: "httpx.Response") -> str:
    """Build a clear, user-facing message for a 429 response."""
    detail = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            detail = body.get("developerMessage") or body.get("title") or body.get("detail") or ""
    except Exception:
        pass
    retry_after = r.headers.get("Retry-After")
    parts = ["Autodesk APS rate/quota limit reached (HTTP 429)."]
    if detail:
        parts.append(str(detail))
    if retry_after:
        parts.append(f"Retry-After: {retry_after}s.")
    parts.append("The MCP server stopped instead of hanging — please wait and try again later.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 2-legged OAuth (app-only endpoints like HQ account users)
# ---------------------------------------------------------------------------

_app_token_cache: dict = {"token": None, "expires_at": 0}


async def get_app_token() -> str:
    """Return a valid 2-legged (client_credentials) access token."""
    now = time.time()
    if _app_token_cache["token"] and now < _app_token_cache["expires_at"] - 60:
        return _app_token_cache["token"]

    creds = _get_basic_auth_header()
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{APS_BASE}/authentication/v2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            },
            data={"grant_type": "client_credentials", "scope": "account:read account:write"},
        )
        res.raise_for_status()
        data = res.json()
        _app_token_cache["token"] = data["access_token"]
        _app_token_cache["expires_at"] = now + data.get("expires_in", 3600)
        return _app_token_cache["token"]


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------

async def get_all_pages(
    client: httpx.AsyncClient, url: str, headers: dict, params: dict | None = None
) -> list:
    items = []
    params = dict(params or {})
    params.setdefault("limit", 200)
    offset = 0
    while True:
        params["offset"] = offset
        res = await _request_with_retry(client, "get", url, headers=headers, params=params)
        res.raise_for_status()
        body = res.json()
        data = body.get("data") or body.get("results") or []
        items.extend(data)
        meta = body.get("meta", {})
        pagination = meta.get("pagination", {})
        total = pagination.get("totalResults", len(items))
        if len(items) >= total or not data:
            break
        offset = len(items)
    return items


async def _get_all_issues(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    params: dict | None = None,
    *,
    page_limit: int = 100,
) -> list:
    """Fetch ALL rows from an ACC Issues API list endpoint (issues, issue-types,
    attribute-definitions/mappings).

    Same limit/offset scheme as `get_all_pages`, but the Issues API places the
    `pagination` object at the **top level** of the body (not under `meta`), so
    `get_all_pages` can't read `totalResults` and would stop after the first
    page. This loop reads it from the right place. Issues cap each page at 100;
    the type/attribute endpoints allow up to 200 (pass `page_limit`)."""
    items: list = []
    params = dict(params or {})
    params.setdefault("limit", page_limit)
    offset = 0
    while True:
        params["offset"] = offset
        res = await _request_with_retry(client, "get", url, headers=headers, params=params)
        res.raise_for_status()
        body = res.json()
        page = body.get("results") or []
        items.extend(page)
        total = (body.get("pagination") or {}).get("totalResults", len(items))
        if len(items) >= total or not page:
            break
        offset = len(items)
    return items


async def get_all_folder_contents(
    client: httpx.AsyncClient,
    project_id: str,
    folder_id: str,
    headers: dict,
    *,
    raise_on_error: bool = True,
    on_unauthorized=None,
) -> list:
    """Return ALL items in a folder, following JSON:API `links.next` pagination.

    The Data Management `folders/{id}/contents` endpoint caps each page at 200
    items and exposes further pages via `links.next` — not the limit/offset +
    `meta.pagination` scheme that `get_all_pages` handles. Use this for any
    folder that may hold more than 200 files/subfolders.

    With `raise_on_error=False`, a non-success response (e.g. a 403 on a
    restricted subfolder) stops pagination and returns what was collected so
    far instead of raising — used by best-effort tree walks.
    """
    url = f"{APS_BASE}/data/v1/projects/{project_id}/folders/{folder_id}/contents"
    params: dict | None = {"page[limit]": 200}
    items: list = []
    while url:
        res = await _request_with_retry(
            client, "get", url, headers=headers, params=params,
            on_unauthorized=on_unauthorized,
        )
        if not res.is_success:
            if raise_on_error:
                res.raise_for_status()
            break
        body = res.json()
        items.extend(body.get("data", []))
        nxt = (body.get("links") or {}).get("next")
        if isinstance(nxt, dict):
            url = nxt.get("href")
        elif isinstance(nxt, str):
            url = nxt
        else:
            url = None
        params = None  # the `next` href already carries pagination params
    return items


async def _resolve_start_folders(
    client: httpx.AsyncClient,
    token: str,
    headers: dict,
    hub_id: str,
    project_id: str,
    folder_path: str | None,
) -> list:
    """Return the folder(s) a recursive walk should start from: the resolved
    `folder_path` when given, else the project's top-level folders. Shared by
    `find_files`, `find_folder`, and `list_all_files`."""
    if folder_path:
        folder_id, _ = await _resolve_folder(client, token, hub_id, project_id, folder_path)
        return [{"id": folder_id, "attributes": {"displayName": folder_path}}]
    res = await client.get(
        f"{APS_BASE}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders",
        headers=headers,
    )
    res.raise_for_status()
    return res.json().get("data", [])


async def _walk_project_files(
    client: httpx.AsyncClient,
    project_id: str,
    headers: dict,
    start_folders: list,
    predicate=None,
    max_depth: int = 8,
) -> list[dict]:
    """Recursively collect files under each start folder (depth-bounded), returning
    file dicts with full display-name paths, newest first. `predicate(name_lower)`
    filters files; None keeps every file. Sub-folder listing errors are skipped
    (best-effort walk). Shared by `find_files` (substring predicate) and
    `list_all_files` (no predicate)."""
    results: list[dict] = []

    async def walk(folder_id: str, path: str, depth: int):
        if depth > max_depth:
            return
        contents = await get_all_folder_contents(
            client, project_id, folder_id, headers, raise_on_error=False
        )
        tasks = []
        for item in contents:
            a = item.get("attributes", {})
            if item["type"] == "folders":
                tasks.append(walk(item["id"], f"{path}/{_folder_name(a)}", depth + 1))
                continue
            display_name = a.get("displayName", "")
            if predicate is None or predicate(display_name.lower()):
                results.append({
                    "name": display_name,
                    "path": f"{path}/{display_name}",
                    "id": item["id"],
                    "last_modified": a.get("lastModifiedTime"),
                    "modified_by": a.get("lastModifiedUserName"),
                })
        await asyncio.gather(*tasks)

    await asyncio.gather(*[
        walk(f["id"], _folder_name(f["attributes"]), 0) for f in start_folders
    ])
    results.sort(key=lambda x: x.get("last_modified") or "", reverse=True)
    return results


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    on_unauthorized=None,
    **kwargs,
) -> httpx.Response:
    """Issue an HTTP request, retrying transient 429s/503s with a short, capped backoff.

    Fails fast with a clear `APSQuotaError` when APS signals a hard quota limit
    (retrying within seconds can't clear it) or when the retries are exhausted —
    so the caller surfaces a "can't proceed" message instead of hanging.

    When `on_unauthorized` is given (an async callback), a single 401 triggers it
    once — typically to refresh an expired token by mutating the shared headers
    dict in place — then the request is retried with the updated credentials. This
    lets a long bulk batch outlive a token. Without the callback, a 401 is returned
    unchanged (existing behaviour).
    """
    refreshed = False
    for attempt in range(max_retries + 1):
        r = await getattr(client, method)(url, **kwargs)
        # One-time token refresh on 401 (long batches can outlive a token).
        if r.status_code == 401 and on_unauthorized is not None and not refreshed:
            await on_unauthorized()
            refreshed = True
            continue
        # Transient 503 → brief, capped back-off, then retry.
        if r.status_code == 503 and attempt < max_retries:
            await asyncio.sleep(min(2 ** attempt, 10))
            continue
        if r.status_code != 429:
            return r
        # Hard quota, or out of retries → stop now with a clear message.
        if _is_quota_429(r) or attempt == max_retries:
            raise APSQuotaError(_quota_message(r), _safe_int(r.headers.get("Retry-After")))
        # Transient rate spike → brief, capped back-off, then retry.
        wait = _safe_int(r.headers.get("Retry-After"))
        if wait is None:
            wait = min(2 ** attempt, 10)
        await asyncio.sleep(min(wait, 10))
    return r  # unreachable; keeps type-checkers happy


# ---------------------------------------------------------------------------
# Name → ID resolver layer
# ---------------------------------------------------------------------------

async def resolve_hub(
    client: httpx.AsyncClient,
    token: str,
    region: str = "EMEA",
    hub_name: str | None = None,
) -> tuple[str, str]:
    res = await client.get(f"{APS_BASE}/project/v1/hubs", headers=auth_headers(token))
    res.raise_for_status()
    hubs = res.json().get("data", [])
    if not hubs:
        raise ValueError("No hubs found for this account.")
    region_upper = region.upper()
    matched = [h for h in hubs if (h["attributes"].get("region") or "").upper() == region_upper]
    if not matched:
        available = [(h["attributes"]["name"], h["attributes"].get("region")) for h in hubs]
        raise ValueError(f"No {region_upper} hub found. Available hubs: {available}")
    if hub_name:
        hub_name_lower = hub_name.lower()
        exact = [h for h in matched if h["attributes"]["name"].lower() == hub_name_lower]
        partial = [h for h in matched if hub_name_lower in h["attributes"]["name"].lower()]
        name_matched = exact or partial
        if not name_matched:
            available_names = [h["attributes"]["name"] for h in matched]
            raise ValueError(f"Hub '{hub_name}' not found in {region_upper}. Available: {available_names}")
        hub = name_matched[0]
    else:
        hub = matched[0]
    return hub["id"], hub["attributes"]["name"]


async def resolve_project(
    client: httpx.AsyncClient,
    token: str,
    project_name: str,
    hub_id: str | None = None,
    region: str = "EMEA",
    hub_name: str | None = None,
) -> tuple[str, str, str]:
    if hub_id is None:
        hub_id, _ = await resolve_hub(client, token, region, hub_name=hub_name)

    projects = await get_all_pages(
        client, f"{APS_BASE}/project/v1/hubs/{hub_id}/projects", auth_headers(token)
    )

    name_lower = project_name.lower()
    exact = [p for p in projects if p["attributes"]["name"].lower() == name_lower]
    partial = [p for p in projects if name_lower in p["attributes"]["name"].lower()]

    candidates = exact or partial
    if not candidates:
        names = [p["attributes"]["name"] for p in projects[:20]]
        raise ValueError(
            f"Project '{project_name}' not found. Available projects (first 20): {names}"
        )
    if len(candidates) > 1 and not exact:
        names = [p["attributes"]["name"] for p in candidates]
        raise ValueError(
            f"Ambiguous project name '{project_name}'. Matches: {names}. Be more specific."
        )

    project = candidates[0]
    return hub_id, project["id"], project["attributes"]["name"]


async def _get_all_hubs(client: httpx.AsyncClient, token: str) -> list:
    """Return the raw hub list (data array) the account can see."""
    res = await client.get(f"{APS_BASE}/project/v1/hubs", headers=auth_headers(token))
    res.raise_for_status()
    return res.json().get("data", [])


def _project_ref(project_id: str, project_name: str, hub: dict | None,
                 platform: str | None, matched_by: str) -> dict:
    """Assemble a ProjectRef, always emitting 'b.'-prefixed IDs (see spec)."""
    hub = hub or {}
    hub_id = hub.get("id")
    attrs = hub.get("attributes", {})
    return {
        "project_id": _ensure_b_prefix(project_id),
        "project_name": project_name,
        "hub_id": _ensure_b_prefix(hub_id) if hub_id else None,
        "hub_name": attrs.get("name"),
        "region": attrs.get("region"),
        "platform": platform,
        "matched_by": matched_by,
    }


async def _resolve_project_ref(
    client: httpx.AsyncClient,
    token: str,
    query: str,
    region: str = "EMEA",
    hub_name: str | None = None,
    allow_multiple: bool = False,
) -> dict:
    """Resolve a project from a name, bare/'b.'-prefixed ID, or ACC URL, together
    with its owning hub. Returns a ProjectRef dict, or — for a name matching more
    than one project when ``allow_multiple`` — ``{"ambiguous": True, "candidates": [...]}``.

    ID/URL queries take the ACC Admin fast path (one call, auto-routed, no hub
    iteration). Name queries fan out across hubs. Raises ValueError on not-found
    and on ambiguity when ``allow_multiple`` is False."""
    if not query or not query.strip():
        raise ValueError("Empty project query — provide a project name, ID, or ACC URL.")
    query = query.strip()

    uuid = _extract_uuid(query)
    if uuid:
        return await _resolve_by_id(client, token, uuid, is_url=_is_url(query))
    return await _resolve_by_name(
        client, token, query, region=region, hub_name=hub_name, allow_multiple=allow_multiple
    )


async def _resolve_by_id(
    client: httpx.AsyncClient, token: str, bare_uuid: str, is_url: bool
) -> dict:
    """ID/URL fast path: ask the ACC Admin API for the project's owning account
    (hub) directly. 2-legged token, NO Region header (auto-routes across regions)."""
    app_token = await get_app_token()
    r = await client.get(
        f"{APS_BASE}/construction/admin/v1/projects/{bare_uuid}",
        params={"fields": "accountId,name,platform"},
        headers=auth_headers(app_token),
    )
    if r.status_code == 404:
        raise ValueError(
            f"Project {bare_uuid} not found — check the ID or your token's account access."
        )
    if r.status_code in (401, 403):
        # Token lacks account:read for the Admin GET — fall back to matching the id
        # across the hubs the 3-legged user token can see.
        return await _resolve_id_by_enumeration(client, token, bare_uuid, is_url=is_url)
    r.raise_for_status()
    data = r.json()

    account_id = data.get("accountId")
    hub = None
    if account_id:
        hubs = await _get_all_hubs(client, token)
        hub = next((h for h in hubs if _to_bare_id(h["id"]) == account_id), None)
        if hub is None:
            # Admin can see the project but the user token can't list the hub — still
            # emit the hub id we know, without a display name/region.
            hub = {"id": _ensure_b_prefix(account_id), "attributes": {}}
    return _project_ref(
        _ensure_b_prefix(bare_uuid),
        data.get("name"),
        hub,
        data.get("platform"),
        "url" if is_url else "id",
    )


async def _resolve_id_by_enumeration(
    client: httpx.AsyncClient, token: str, bare_uuid: str, is_url: bool
) -> dict:
    """Fallback ID resolver: scan every hub's project list for a matching project id."""
    hubs = await _get_all_hubs(client, token)
    results = await _gather_bounded(
        8,
        [
            (lambda h=h: get_all_pages(
                client, f"{APS_BASE}/project/v1/hubs/{h['id']}/projects", auth_headers(token)
            ))
            for h in hubs
        ],
    )
    for hub, projects in zip(hubs, results):
        for p in projects:
            if _to_bare_id(p["id"]) == bare_uuid:
                return _project_ref(
                    p["id"], p["attributes"]["name"], hub, None, "url" if is_url else "id"
                )
    raise ValueError(
        f"Project {bare_uuid} not found — check the ID or your token's account access."
    )


async def _resolve_by_name(
    client: httpx.AsyncClient,
    token: str,
    query: str,
    region: str = "EMEA",
    hub_name: str | None = None,
    allow_multiple: bool = False,
) -> dict:
    """Name path: fan out across hubs and partial-match the project name. Searching
    every hub (not just the region's first) is the wrong-hub fix."""
    hubs = await _get_all_hubs(client, token)
    if not hubs:
        raise ValueError("No hubs found for this account.")

    search_hubs = hubs
    if hub_name:
        hn = hub_name.lower()
        exact = [h for h in hubs if h["attributes"]["name"].lower() == hn]
        partial = [h for h in hubs if hn in h["attributes"]["name"].lower()]
        search_hubs = exact or partial
        if not search_hubs:
            available = [h["attributes"]["name"] for h in hubs]
            raise ValueError(f"Hub '{hub_name}' not found. Available hubs: {available}")

    project_lists = await _gather_bounded(
        8,
        [
            (lambda h=h: get_all_pages(
                client, f"{APS_BASE}/project/v1/hubs/{h['id']}/projects", auth_headers(token)
            ))
            for h in search_hubs
        ],
    )

    name_lower = query.lower()
    exact_matches, partial_matches = [], []
    for hub, projects in zip(search_hubs, project_lists):
        for p in projects:
            pname = p["attributes"]["name"]
            if pname.lower() == name_lower:
                exact_matches.append((p, hub))
            elif name_lower in pname.lower():
                partial_matches.append((p, hub))

    matches = exact_matches or partial_matches
    if not matches:
        searched = [h["attributes"]["name"] for h in search_hubs]
        raise ValueError(f"Project '{query}' not found — searched hubs: {searched}")

    if len(matches) > 1 and not exact_matches:
        candidates = [
            _project_ref(p["id"], p["attributes"]["name"], hub, None, "name")
            for p, hub in matches
        ]
        if allow_multiple:
            return {"ambiguous": True, "candidates": candidates}
        names = [f"{c['project_name']} ({c['hub_name']})" for c in candidates]
        raise ValueError(
            f"Ambiguous project name '{query}'. Matches: {names}. Be more specific "
            f"(pass a project ID/URL or hub_name)."
        )

    project, hub = matches[0]
    return _project_ref(project["id"], project["attributes"]["name"], hub, None, "name")


async def _resolve_project_arg(
    client: httpx.AsyncClient, token: str, arguments: dict
) -> tuple[str, str, str]:
    """Entry-point resolver for project-scoped tools: accepts either the new
    ``project`` param (name / ID / ACC URL) or the legacy ``project_name`` alias,
    and returns the ``(hub_id, project_id, project_name)`` tuple every call site
    already expects. Raises on not-found/ambiguous (single-project semantics)."""
    query = arguments.get("project") or arguments.get("project_name")
    if not query:
        raise ValueError(
            "Provide 'project' (project name, ID, or ACC URL) or 'project_name'."
        )
    ref = await _resolve_project_ref(
        client,
        token,
        query,
        region=_norm_region(arguments),
        hub_name=arguments.get("hub_name"),
        allow_multiple=False,
    )
    return ref["hub_id"], ref["project_id"], ref["project_name"]


async def _resolve_issue_project(
    client: httpx.AsyncClient, token: str, arguments: dict
) -> tuple[str, str]:
    """Resolve a project for the Issues API: same resolution as every other tool,
    but returns the **bare** project id (no 'b.' prefix) that the Issues endpoints
    require, plus the resolved display name."""
    _, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
    return _to_bare_id(project_id), resolved_name


async def _resolve_issue_ref(
    client: httpx.AsyncClient, issue_hdrs: dict, project_id: str, arguments: dict
) -> str:
    """Return an issue's UUID from either ``issue_id`` (a UUID, preferred) or
    ``display_id`` (the friendly issue number, e.g. 191). A display id is resolved
    with one cheap list call filtered on ``filter[displayId]``. Raises ValueError
    if neither is given or the display id matches nothing."""
    issue_id = arguments.get("issue_id")
    if issue_id:
        return issue_id
    display_id = arguments.get("display_id")
    if display_id is None:
        raise ValueError("Provide 'issue_id' (UUID) or 'display_id' (the issue number).")
    url = f"{APS_BASE}/construction/issues/v1/projects/{project_id}/issues"
    rows = await _get_all_issues(
        client, url, issue_hdrs,
        {"filter[displayId]": display_id, "fields": "id,displayId"}, page_limit=2,
    )
    if not rows:
        raise ValueError(f"No issue with displayId {display_id} in this project.")
    return rows[0]["id"]


# ---------------------------------------------------------------------------
# Approval-workflow (ACC Reviews API) helpers
# ---------------------------------------------------------------------------

WORKFLOWS_URL = APS_BASE + "/construction/reviews/v1/projects/{pid}/workflows"


async def _resolve_review_project(
    client: httpx.AsyncClient, token: str, arguments: dict
) -> tuple[str, str]:
    """Resolve a project for the Reviews API → (bare project UUID, display name).
    Same resolution as every other tool; the Reviews endpoints want the bare id."""
    _, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
    return _to_bare_id(project_id), resolved_name


async def _resolve_workflow_ref(
    client: httpx.AsyncClient, wf_hdrs: dict, bare_project_id: str, arguments: dict
) -> str:
    """Return an approval workflow's UUID from ``workflow_id`` (a UUID, preferred)
    or ``name`` (one list call, exact case-insensitive match). Raises ValueError if
    neither is given, the name matches nothing, or it is ambiguous."""
    workflow_id = arguments.get("workflow_id")
    if workflow_id:
        return workflow_id
    name = arguments.get("name")
    if not name:
        raise ValueError("Provide 'workflow_id' (UUID) or 'name'.")
    rows = await _get_all_issues(
        client, WORKFLOWS_URL.format(pid=bare_project_id), wf_hdrs, page_limit=50
    )
    matches = [w for w in rows if (w.get("name") or "").lower() == name.lower()]
    if not matches:
        raise ValueError(f"No workflow named '{name}' in this project.")
    if len(matches) > 1:
        raise ValueError(f"Multiple workflows named '{name}' — pass 'workflow_id' instead.")
    return matches[0]["id"]


def _looks_like_autodesk_id(value: str) -> bool:
    """Heuristic: an opaque Autodesk ID (alphanumeric, ≥8 chars, contains a digit) —
    distinguishes a pasted raw id from a person/role/company name (which rarely
    contains a digit and often has spaces)."""
    return bool(re.fullmatch(r"[A-Za-z0-9]{8,}", value)) and any(c.isdigit() for c in value)


async def _build_project_directory(
    client: httpx.AsyncClient, token: str, bare_project_id: str
) -> dict:
    """Build case-insensitive lookup maps that resolve friendly reviewer references
    (user names/emails, role names, company names) → the ``autodeskId`` values the
    Reviews Workflows API requires for step candidates.

    Two distinct ID spaces, so two distinct sources:

    * **Users** — from the ACC Admin ``projects/{id}/users`` endpoint (3-legged, same
      source as list_project_members). A user's opaque ``autodeskId`` there is exactly
      what the Reviews API wants.
    * **Roles & companies** — the Reviews API keys these by a *numeric* ``autodeskId``
      that is **not** the project-role UUID from the Admin API, and Autodesk exposes no
      endpoint to discover it. The only reliable source is the candidates already
      embedded in the project's existing workflows, so we harvest role/company
      name→autodeskId from them. Names unused by any existing workflow can't be
      resolved (pass a raw autodeskId instead)."""
    hdrs = auth_headers(token)
    users_by_name: dict[str, str] = {}
    users_by_email: dict[str, str] = {}
    roles_by_name: dict[str, str] = {}
    companies_by_name: dict[str, str] = {}
    params: dict = {"limit": 200, "offset": 0}
    while True:
        r = await client.get(
            f"{APS_BASE}/construction/admin/v1/projects/{bare_project_id}/users",
            headers=hdrs, params=params,
        )
        if not r.is_success:
            break
        users = _extract_response_items(r.json())
        for u in users:
            aid = u.get("autodeskId")
            if aid:
                name = u.get("name") or f"{u.get('firstName','')} {u.get('lastName','')}".strip()
                if name:
                    users_by_name[name.lower()] = aid
                email = u.get("email")
                if email:
                    users_by_email[email.lower()] = aid
            # Roles & companies (Tier 1): the Reviews API's numeric candidate autodeskId
            # for a role is that role's `roleGroupId`, and for a company the user's
            # `companyGroupId` — both already present in this same admin users payload
            # (verified live: identical to the value embedded in existing workflow
            # candidates). Free, and covers every role/company that has ≥1 member.
            for role in (u.get("roles") or []):
                rname, rgid = role.get("name"), role.get("roleGroupId")
                if rname and rgid:
                    roles_by_name.setdefault(rname.lower(), str(rgid))
            cname, cgid = u.get("companyName"), u.get("companyGroupId")
            if cname and cgid:
                companies_by_name.setdefault(cname.lower(), str(cgid))
        if len(users) < params["limit"]:
            break
        params["offset"] += params["limit"]

    # Tier 2: harvest role/company autodeskIds from existing workflows' candidates. This
    # adds a role/company that is *used by a workflow but currently has no member* (so
    # Tier 1 missed it). Best-effort — a project with no workflows yet simply yields
    # nothing extra here.
    try:
        wf_hdrs = {**hdrs, "x-ads-region": REVIEWS_REGION}
        workflows = await _get_all_issues(
            client, WORKFLOWS_URL.format(pid=bare_project_id), wf_hdrs, page_limit=50
        )
        for wf in workflows:
            for step in (wf.get("steps") or []):
                cands = step.get("candidates") or {}
                for role in cands.get("roles", []):
                    if role.get("name") and role.get("autodeskId"):
                        roles_by_name.setdefault(role["name"].lower(), role["autodeskId"])
                for comp in cands.get("companies", []):
                    if comp.get("name") and comp.get("autodeskId"):
                        companies_by_name.setdefault(comp["name"].lower(), comp["autodeskId"])
    except Exception:
        pass  # harvesting is best-effort; unresolved names fall back to the cache / raw id

    # Tier 3: fill any remaining gaps (a role with zero members that no workflow uses
    # either — a genuinely empty role) from the oxygen-id cache. That value is the Data
    # Connector 'admin' export's `role_oxygen_id`, the only source that lists empty roles'
    # Reviews IDs. Same cache mechanic as the role-UUID cache; refreshed on a miss.
    for nm, oid in _load_role_oxygen_id().items():
        roles_by_name.setdefault(nm, oid)
    for nm, oid in _load_company_oxygen_id().items():
        companies_by_name.setdefault(nm, oid)

    return {
        "users_by_name": users_by_name,
        "users_by_email": users_by_email,
        "roles_by_name": roles_by_name,
        "companies_by_name": companies_by_name,
    }


def _resolve_candidates(step: dict, directory: dict) -> dict:
    """Translate a step's friendly reviewer references into the Reviews API
    ``candidates`` shape: ``{users:[{autodeskId}], roles:[...], companies:[...]}``.

    Accepts per step: ``reviewer_users`` (names or emails), ``reviewer_roles`` (role
    names), ``reviewer_companies`` (company names). A value that is already an
    autodeskId (matches a directory value or looks like one) is passed through. Any
    unmatched reference raises ValueError naming every unresolved entry, so a bad
    Excel row fails loudly instead of silently dropping a reviewer."""
    unresolved: list[str] = []

    def _lookup(value, by_name: dict, by_email: "dict | None" = None):
        key = str(value).strip()
        low = key.lower()
        if by_email and low in by_email:
            return by_email[low]
        if low in by_name:
            return by_name[low]
        known_ids = set(by_name.values())
        if by_email:
            known_ids |= set(by_email.values())
        if key in known_ids or _looks_like_autodesk_id(key):
            return key
        unresolved.append(key)
        return None

    candidates: dict = {}
    users = [
        {"autodeskId": aid}
        for v in (step.get("reviewer_users") or [])
        if (aid := _lookup(v, directory["users_by_name"], directory["users_by_email"]))
    ]
    roles = [
        {"autodeskId": aid}
        for v in (step.get("reviewer_roles") or [])
        if (aid := _lookup(v, directory["roles_by_name"]))
    ]
    companies = [
        {"autodeskId": aid}
        for v in (step.get("reviewer_companies") or [])
        if (aid := _lookup(v, directory["companies_by_name"]))
    ]
    if unresolved:
        raise ValueError(
            "Could not resolve these reviewer references to Autodesk IDs: "
            + ", ".join(unresolved)
            + ". Users come from list_project_members (the 'autodesk_id' field). Role and "
            "company IDs use a separate numeric space, resolved from (1) any member holding "
            "the role / in the company, (2) a workflow already using it, or (3) the "
            "role_id_cache.json 'roles_name_to_oxygen_id' map. An unresolved name is a "
            "genuinely empty role (no member, no workflow) missing from the cache — refresh "
            "it via a Data Connector export (its role_oxygen_id column) and merge, or pass "
            "the raw numeric autodeskId from get_workflow."
        )
    if users:
        candidates["users"] = users
    if roles:
        candidates["roles"] = roles
    if companies:
        candidates["companies"] = companies
    return candidates


def _workflow_create_payload(spec: dict, directory: dict) -> dict:
    """Build the POST body for an approval workflow from a friendly ``spec``
    (snake_case → API camelCase), resolving each step's reviewer references via
    ``directory``. Only supplied keys are included. Shared by create_workflow and
    bulk_create_workflows. Raises ValueError on a missing name or unresolved
    reviewer."""
    if not spec.get("name"):
        raise ValueError("Workflow 'name' is required.")
    body: dict = {"name": spec["name"]}
    for src in ("description", "notes"):
        if spec.get(src) is not None:
            body[src] = spec[src]
    if spec.get("initiator_edit_permissions") is not None:
        body["additionalOptions"] = {
            "initiatorEditPermissions": spec["initiator_edit_permissions"]
        }
    if spec.get("additional_approval_status_options") is not None:
        body["additionalApprovalStatusOptions"] = spec["additional_approval_status_options"]

    steps_out = []
    for step in (spec.get("steps") or []):
        s: dict = {"name": step.get("name"), "type": step.get("type")}
        if step.get("duration") is not None:
            s["duration"] = step["duration"]
        if step.get("due_date_type") is not None:
            s["dueDateType"] = step["due_date_type"]
        elif step.get("type") in ("REVIEWER", "APPROVER"):
            # The API rejects a REVIEWER/APPROVER step with no dueDateType (400),
            # even though CALENDAR_DAY is its documented default — supply it.
            s["dueDateType"] = "CALENDAR_DAY"
        if step.get("group_review") is not None:
            s["groupReview"] = step["group_review"]
        candidates = _resolve_candidates(step, directory)
        if not candidates and step.get("candidates"):
            candidates = step["candidates"]  # allow a pre-built passthrough candidates dict
        if candidates:
            s["candidates"] = candidates
        # A MINIMUM group review whose `min` exceeds the step's candidate count is a
        # guaranteed 400 ("groupReview.min is greater than the total number of
        # candidates") — catch it here with a clear message instead of a raw API error.
        gr = s.get("groupReview") or {}
        if gr.get("enabled") and gr.get("type") == "MINIMUM" and gr.get("min"):
            cand_count = sum(len(v) for v in (candidates or {}).values() if isinstance(v, list))
            if gr["min"] > cand_count:
                raise ValueError(
                    f"Step '{s.get('name')}': groupReview min ({gr['min']}) exceeds the "
                    f"number of reviewers on the step ({cand_count}). Add more reviewers "
                    f"or lower min."
                )
        steps_out.append(s)
    body["steps"] = steps_out

    # copyFilesOptions is required by the API; default to "don't copy" when omitted.
    cfo = spec.get("copy_files_options")
    body["copyFilesOptions"] = cfo if cfo is not None else {"enabled": False}
    if spec.get("attached_attributes") is not None:
        body["attachedAttributes"] = spec["attached_attributes"]
    if spec.get("update_attributes_options") is not None:
        body["updateAttributesOptions"] = spec["update_attributes_options"]
    return body


async def _resolve_folder_with_hub(
    client: httpx.AsyncClient,
    token: str,
    hub_id: str,
    project_id: str,
    folder_path: str,
) -> tuple[str, str]:
    hdrs = auth_headers(token)
    res = await client.get(
        f"{APS_BASE}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders",
        headers=hdrs,
    )
    res.raise_for_status()
    current_items = res.json().get("data", [])

    parts = [p.strip() for p in folder_path.strip("/").split("/") if p.strip()]
    if not parts:
        raise ValueError("folder_path cannot be empty.")

    current_id = current_name = None
    for i, part in enumerate(parts):
        part_lower = part.lower()
        match = next(
            (f for f in current_items if _folder_name_matches(f["attributes"], part_lower)),
            None,
        )
        if match is None:
            available = [_folder_name(f["attributes"]) for f in current_items]
            raise ValueError(f"Folder '{part}' not found. Available: {available}")
        current_id = match["id"]
        current_name = _folder_name(match["attributes"])
        if i < len(parts) - 1:
            contents = await get_all_folder_contents(client, project_id, current_id, hdrs)
            current_items = [x for x in contents if x["type"] == "folders"]

    return current_id, current_name


async def _resolve_folder(
    client: "httpx.AsyncClient",
    token: str,
    hub_id: str,
    project_id: str,
    folder_path_or_urn: str,
) -> tuple[str, str]:
    """Resolve a display-name path or a raw folder URN to (folder_id, folder_name).
    URNs (starting with 'urn:') skip path traversal entirely with a single GET.
    """
    if folder_path_or_urn.startswith("urn:"):
        r = await client.get(
            f"{APS_BASE}/data/v1/projects/{project_id}/folders/{folder_path_or_urn}",
            headers=auth_headers(token),
        )
        r.raise_for_status()
        attrs = r.json().get("data", {}).get("attributes", {})
        name = _folder_name(attrs) or folder_path_or_urn
        return folder_path_or_urn, name
    return await _resolve_folder_with_hub(client, token, hub_id, project_id, folder_path_or_urn)


async def _subtree_has_files(
    client: httpx.AsyncClient,
    project_id: str,
    folder_id: str,
    hdrs: dict,
) -> bool:
    """Return True if any files (items) exist anywhere in the folder's subtree."""
    contents = await get_all_folder_contents(client, project_id, folder_id, hdrs)
    for item in contents:
        if item["type"] == "items":
            return True
    for item in contents:
        if item["type"] == "folders":
            if await _subtree_has_files(client, project_id, item["id"], hdrs):
                return True
    return False


async def _subtree_file_info(
    client: httpx.AsyncClient,
    project_id: str,
    folder_id: str,
    hdrs: dict,
    *,
    sample_limit: int = 5,
    on_unauthorized=None,
) -> tuple[int, list[str]]:
    """Count files anywhere in a folder's subtree and collect up to `sample_limit`
    file names. Returns (file_count, sample_names).

    Used by `bulk_delete_folders` to populate the `skipped_has_files` rows the
    orchestrator surfaces to the user. The common case (an empty legacy folder)
    costs a single listing that returns no files and recurses into empty
    subfolders only; folders that DO hold files are the minority and are skipped,
    so the full count walk there is acceptable.
    """
    count = 0
    sample: list[str] = []
    contents = await get_all_folder_contents(
        client, project_id, folder_id, hdrs, on_unauthorized=on_unauthorized
    )
    for item in contents:
        if item["type"] == "items":
            count += 1
            if len(sample) < sample_limit:
                sample.append(item.get("attributes", {}).get("displayName") or item["id"])
    for item in contents:
        if item["type"] == "folders":
            sub_count, sub_sample = await _subtree_file_info(
                client, project_id, item["id"], hdrs,
                sample_limit=sample_limit, on_unauthorized=on_unauthorized,
            )
            count += sub_count
            for name in sub_sample:
                if len(sample) < sample_limit:
                    sample.append(name)
    return count, sample


async def _gather_bounded(max_concurrency: int, factories: list) -> list:
    """Run a list of zero-arg async factories concurrently, capped at
    `max_concurrency` in flight at once, preserving input order in the results."""
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _run(factory):
        async with sem:
            return await factory()

    return await asyncio.gather(*[_run(f) for f in factories])


def _folder_create_payload(folder_name: str, parent_id: str) -> dict:
    """JSON:API body to create a folder under `parent_id` (shared by the single
    `create_folder` tool and `bulk_create_folders`)."""
    return {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": "folders",
            "attributes": {
                "name": folder_name,
                "displayName": folder_name,
                "extension": {
                    "type": "folders:autodesk.bim360:Folder",
                    "version": "1.0",
                },
            },
            "relationships": {
                "parent": {"data": {"type": "folders", "id": parent_id}},
            },
        },
    }


def _folder_hide_payload(folder_id: str) -> dict:
    """JSON:API body to soft-delete (hide) a folder (shared by the single
    `delete_folder` tool and `bulk_delete_folders`)."""
    return {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": "folders",
            "id": folder_id,
            "attributes": {"hidden": True},
        },
    }


def _reparent_payload(entity_type: str, entity_id: str, dest_parent_id: str) -> dict:
    """JSON:API body to move an item or folder by changing its parent (shared by
    the single `move_file`/`move_folder` tools and their bulk counterparts).
    `entity_type` is "items" (file) or "folders"."""
    return {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": entity_type,
            "id": entity_id,
            "relationships": {
                "parent": {"data": {"type": "folders", "id": dest_parent_id}},
            },
        },
    }


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _fmt_project(p: dict) -> dict:
    a = p.get("attributes", {})
    return {"id": p["id"], "name": a.get("name"), "status": a.get("status"), "type": a.get("projectType")}


def _folder_name(attrs: dict) -> str:
    """Human-facing folder name. Forma/ACC folders carry both `name` and
    `displayName`; the ACC/Forma UI shows `name`, so prefer it and fall back
    to `displayName` (and then "") when absent."""
    return attrs.get("name") or attrs.get("displayName") or ""


def _folder_name_matches(attrs: dict, target_lower: str) -> bool:
    """Whether a folder path segment matches this folder, comparing against
    BOTH `name` and `displayName` (case-insensitive) so paths typed from either
    label resolve."""
    for key in ("name", "displayName"):
        val = attrs.get(key)
        if val and val.lower() == target_lower:
            return True
    return False


def _naming_standard_ids(attrs: dict) -> list:
    """Return the naming-standard IDs assigned to a folder (empty list if none).

    ACC/Forma folders carry these under `extension.data.namingStandardIds`. A
    folder with a configured naming convention has a non-empty list; a folder
    without any has it absent or empty. Every subfolder's value is already
    present in its parent's `contents` listing, so a tree audit reads it for
    free — no per-folder GET needed."""
    data = (attrs.get("extension") or {}).get("data") or {}
    ids = data.get("namingStandardIds")
    return [i for i in ids if i] if isinstance(ids, list) else []


def _fmt_folder(f: dict) -> dict:
    return {"id": f["id"], "name": _folder_name(f["attributes"]), "type": "folder"}


def _fmt_item(i: dict) -> dict:
    a = i.get("attributes", {})
    return {
        "id": i["id"],
        "name": a.get("displayName"),
        "type": i.get("type"),
        "last_modified": a.get("lastModifiedTime"),
        "created_by": a.get("createUserName"),
    }


# ---------------------------------------------------------------------------
# Permission matrix helpers
# ---------------------------------------------------------------------------

async def _get_folder_perms(
    client: httpx.AsyncClient, project_id: str, folder_id: str, hdrs: dict
) -> list[dict]:
    bare_project_id = _to_bare_id(project_id)
    r = await client.get(
        f"{APS_BASE}/bim360/docs/v1/projects/{bare_project_id}/folders/{folder_id}/permissions",
        headers=hdrs,
    )
    if not r.is_success:
        return []
    data = r.json()
    return data if isinstance(data, list) else data.get("data", [])


async def _walk_folder_tree(
    client: httpx.AsyncClient,
    project_id: str,
    bare_id: str,
    folder_id: str,
    folder_path: str,
    hdrs: dict,
    max_depth: int,
    depth: int = 0,
) -> list[dict]:
    perms = await _get_folder_perms(client, project_id, folder_id, hdrs)
    results = [{"folder_id": folder_id, "folder_path": folder_path, "permissions": perms}]

    if depth >= max_depth:
        return results

    contents = await get_all_folder_contents(
        client, project_id, folder_id, hdrs, raise_on_error=False
    )

    tasks = [
        _walk_folder_tree(
            client, project_id, bare_id, item["id"],
            f"{folder_path}/{_folder_name(item['attributes'])}",
            hdrs, max_depth, depth + 1,
        )
        for item in contents
        if item["type"] == "folders"
    ]
    for sub in await asyncio.gather(*tasks):
        results.extend(sub)
    return results


async def _walk_naming_standards(
    client: httpx.AsyncClient,
    project_id: str,
    folder_id: str,
    folder_path: str,
    folder_attrs: dict,
    hdrs: dict,
    max_depth: int,
    sem: asyncio.Semaphore,
    depth: int = 0,
    *,
    on_unauthorized=None,
) -> list[dict]:
    """Flatten a folder subtree into one row per folder describing its assigned
    naming standard(s). The current folder's `folder_attrs` (carrying the
    `namingStandardIds`) come from the parent's `contents` listing, so each
    subfolder is read for free; only the listing call to enumerate children
    costs a request, bounded by `sem`. A restricted subfolder (403) stops that
    branch without aborting the audit (`raise_on_error=False`)."""
    ids = _naming_standard_ids(folder_attrs)
    rows = [{
        "path": folder_path,
        "folder_id": folder_id,
        "naming_standard_ids": ids,
        "has_standard": bool(ids),
    }]
    if depth >= max_depth:
        return rows

    async with sem:
        contents = await get_all_folder_contents(
            client, project_id, folder_id, hdrs,
            raise_on_error=False, on_unauthorized=on_unauthorized,
        )

    tasks = [
        _walk_naming_standards(
            client, project_id, sf["id"],
            f"{folder_path}/{_folder_name(sf['attributes'])}",
            sf["attributes"], hdrs, max_depth, sem, depth + 1,
            on_unauthorized=on_unauthorized,
        )
        for sf in contents
        if sf["type"] == "folders"
    ]
    for sub in await asyncio.gather(*tasks):
        rows.extend(sub)
    return rows


async def _fetch_project_roles(
    client: httpx.AsyncClient, project_id: str, hdrs: dict
) -> dict[str, str]:
    """Return a role_id → role_name map by paging GET construction/admin/v1/projects/{projectId}/users."""
    bare_id = _to_bare_id(project_id)
    role_map: dict[str, str] = {}
    params: dict = {"limit": 200, "offset": 0}
    while True:
        r = await client.get(
            f"{APS_BASE}/construction/admin/v1/projects/{bare_id}/users",
            headers=hdrs,
            params=params,
        )
        if not r.is_success:
            break
        data = r.json()
        users = _extract_response_items(data)
        for user in users:
            for role in user.get("roles", []):
                rid = role.get("id", "")
                rname = role.get("name", "")
                if rid and rname:
                    role_map[rid] = rname
        if len(users) < params["limit"]:
            break
        params["offset"] += params["limit"]
    return role_map


# ---------------------------------------------------------------------------
# Bulk user management helpers
# ---------------------------------------------------------------------------

async def _get_project_members_map(
    client: httpx.AsyncClient, project_id: str, hdrs: dict
) -> dict[str, dict]:
    """Return email.lower() → user dict for all members (Admin API)."""
    bare_id = _to_bare_id(project_id)
    members: dict[str, dict] = {}
    params: dict = {"limit": 200, "offset": 0}
    while True:
        r = await client.get(
            f"{APS_BASE}/construction/admin/v1/projects/{bare_id}/users",
            headers=hdrs,
            params=params,
        )
        if not r.is_success:
            break
        data = r.json()
        users = _extract_response_items(data)
        for u in users:
            email = (u.get("email") or "").lower()
            if email:
                members[email] = u
        if len(users) < params["limit"]:
            break
        params["offset"] += params["limit"]
    return members


async def _get_account_users_map(
    client: httpx.AsyncClient, account_id: str, app_token: str
) -> dict[str, dict]:
    """Return email.lower() → user dict for all account users (HQ API, 2-legged)."""
    users: dict[str, dict] = {}
    offset = 0
    while True:
        r = await client.get(
            f"{APS_BASE}/hq/v1/accounts/{account_id}/users",
            headers=auth_headers(app_token),
            params={"limit": 100, "offset": offset},
        )
        if not r.is_success:
            break
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        for u in page:
            email = (u.get("email") or "").lower()
            if email:
                users[email] = u
        if len(page) < 100:
            break
        offset += 100
    return users


async def _get_all_hubs_filtered(
    client: httpx.AsyncClient, token: str, hub_name: str | None = None
) -> list[dict]:
    """Return every hub (raw data entries), or the ``hub_name``-filtered subset (exact
    then partial match). Fanning across all hubs — not just the region's first — is the
    wrong-hub fix that lets the bulk-user tools reach a project or account roster living
    in a non-default hub (the two-EMEA-hubs trap)."""
    hubs = await _get_all_hubs(client, token)
    if not hubs:
        raise ValueError("No hubs found for this account.")
    if hub_name:
        hn = hub_name.lower()
        exact = [h for h in hubs if h["attributes"]["name"].lower() == hn]
        partial = [h for h in hubs if hn in h["attributes"]["name"].lower()]
        selected = exact or partial
        if not selected:
            available = [h["attributes"]["name"] for h in hubs]
            raise ValueError(f"Hub '{hub_name}' not found. Available hubs: {available}")
        return selected
    return hubs


async def _get_all_accounts_users_map(
    client: httpx.AsyncClient, token: str, app_token: str, hub_name: str | None = None
) -> dict[str, dict]:
    """Merge the per-hub account user rosters across every hub (or the ``hub_name``-
    filtered subset) into one email→user map. The HQ roster is per-hub-account, so a user
    who lives only in a non-default hub's account would otherwise be wrongly flagged
    'not in roster' — fanning across hubs is the wrong-hub fix. A 2-legged app token can
    read every account the app is authorised on, so one token serves all hubs."""
    hubs = await _get_all_hubs_filtered(client, token, hub_name)
    maps = await _gather_bounded(
        8,
        [(lambda h=h: _get_account_users_map(client, _to_bare_id(h["id"]), app_token)) for h in hubs],
    )
    merged: dict[str, dict] = {}
    for m in maps:
        merged.update(m)
    return merged


async def _get_account_companies(
    client: httpx.AsyncClient, account_id: str, app_token: str
) -> list[dict]:
    """Return all companies in the account (ACC Admin GET companies, 2-legged).

    Pages `construction/admin/v1/accounts/{id}/companies`, which returns a
    `{pagination, results}` envelope (limit max 200). Used both to expose the
    account company directory (`list_account_companies`) and to resolve a
    company name → id for `bulk_add_hub_users`."""
    companies: list[dict] = []
    offset = 0
    limit = 200
    while True:
        r = await client.get(
            f"{APS_BASE}/construction/admin/v1/accounts/{account_id}/companies",
            headers=auth_headers(app_token),
            params={"limit": limit, "offset": offset},
        )
        if not r.is_success:
            break
        page = _extract_response_items(r.json())
        if not page:
            break
        companies.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return companies


def _resolve_company_id(companies: list[dict], name: str) -> "str | None":
    """Case-insensitive company name → id lookup (exact match on `name`)."""
    name_lower = name.lower().strip()
    for c in companies:
        if (c.get("name") or "").lower().strip() == name_lower:
            return c.get("id")
    return None


def _import_item_key(item: "Any", key_field: str) -> str:
    """Best-effort extract the identifying key (e.g. 'email' or 'name') from a
    success/failure entry in an HQ bulk-import envelope, tolerating both flat
    ({email: ...}) and nested ({item: {email: ...}}) shapes. Lower-cased."""
    if not isinstance(item, dict):
        return ""
    if item.get(key_field):
        return str(item[key_field]).lower().strip()
    inner = item.get("item") or item.get("data") or {}
    if isinstance(inner, dict) and inner.get(key_field):
        return str(inner[key_field]).lower().strip()
    return ""


def _import_item_error(item: "Any") -> str:
    """Best-effort human-readable error from a failure entry in an import envelope."""
    if not isinstance(item, dict):
        return "import failed"
    errs = item.get("errors") or item.get("error")
    if errs:
        return errs if isinstance(errs, str) else json.dumps(errs)
    return json.dumps(item)


_BARE_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_uuid(value: str) -> bool:
    """True if the value is a bare role UUID — i.e. already a raw ID, not a name."""
    return bool(_BARE_UUID_RE.match(str(value).strip()))


def _role_cache_path() -> "str | None":
    """Locate the role-name → UUID cache JSON. Precedence: the `APS_ROLE_CACHE` env var,
    then a repo-local `role_id_cache.json`, then the conventional aps-skill copy under
    `~/.claude/skills/aps/`. Returns None if none exist (resolution then degrades to the
    member-walk only)."""
    env = os.environ.get("APS_ROLE_CACHE")
    if env:
        return env if os.path.exists(env) else None
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "role_id_cache.json")
    if os.path.exists(here):
        return here
    skill = os.path.expanduser(os.path.join("~", ".claude", "skills", "aps", "role_id_cache.json"))
    return skill if os.path.exists(skill) else None


def _load_role_name_to_id() -> dict[str, str]:
    """Load a lowercase role-name → UUID map from the role cache (see `_role_cache_path`).
    This is the ONE source that lists empty/newly-created roles the member-walk can't see
    (it's built from a Data Connector 'admin' export). Read fresh each call — the file is
    tiny — and returns {} on a missing/unreadable/malformed file so resolution degrades
    gracefully to the member-walk. Lets a human pass a role *name* even for a role no
    member holds yet, instead of having to look up its UUID."""
    path = _role_cache_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for name, rid in (data.get("roles_name_to_id") or {}).items():
        if name and rid:
            out[str(name).strip().lower()] = str(rid)
    return out


def _load_oxygen_map(key: str) -> dict[str, str]:
    """Load a lowercase name → oxygen-id map from the role cache under ``key``.

    The 'oxygen id' is the short *numeric* Autodesk ID the **Reviews Workflows** API
    wants for a role/company step candidate (a different ID space than the role UUID in
    ``roles_name_to_id``). It comes from the Data Connector 'admin' export's
    ``role_oxygen_id`` column — the one source that also lists **empty** roles' Reviews
    IDs. Read fresh each call; returns {} on a missing/unreadable/malformed file so
    resolution degrades gracefully to the members-payload / workflow-harvest sources."""
    path = _role_cache_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for name, oid in (data.get(key) or {}).items():
        if name and oid:
            out[str(name).strip().lower()] = str(oid)
    return out


def _load_role_oxygen_id() -> dict[str, str]:
    """Role name → Reviews-API numeric autodeskId, from the cache's
    ``roles_name_to_oxygen_id`` map (see `_load_oxygen_map`)."""
    return _load_oxygen_map("roles_name_to_oxygen_id")


def _load_company_oxygen_id() -> dict[str, str]:
    """Company name → Reviews-API numeric autodeskId, from the cache's
    ``companies_name_to_oxygen_id`` map (see `_load_oxygen_map`)."""
    return _load_oxygen_map("companies_name_to_oxygen_id")


def _resolve_role_id(role_name: str, role_map: dict[str, str]) -> str | None:
    """Case-insensitive role name → role ID lookup."""
    name_lower = role_name.lower()
    for rid, rname in role_map.items():
        if rname.lower() == name_lower:
            return rid
    return None


def _as_role_list(role_spec: "Any") -> list[str]:
    """Normalize a role spec — a single role name/ID string, or a list of them — into a
    de-duplicated, blank-stripped list. The project `users:import` `roleIds` field is an
    array, so a user can hold several roles at once (not just one)."""
    if role_spec is None:
        return []
    items = [role_spec] if isinstance(role_spec, str) else list(role_spec)
    out: list[str] = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s)
    return out


def _resolve_role_ids(
    role_spec: "Any", role_map: dict[str, str], name_cache: "dict[str, str] | None" = None
) -> list[str]:
    """Resolve every role in the spec to its ID. Order per entry: the project member-walk
    map, then the hub-wide role-name cache (the only source for an empty/unused role's ID —
    see `_load_role_name_to_id`), then pass the value through unchanged as a raw ID so the
    ACC API stays the authority. This lets a human pass a role *name* even for a role no
    member holds yet, without ever handling a UUID."""
    if name_cache is None:
        name_cache = _load_role_name_to_id()
    out: list[str] = []
    for name in _as_role_list(role_spec):
        rid = _resolve_role_id(name, role_map) or name_cache.get(str(name).strip().lower()) or name
        out.append(rid)
    return out


def _unresolved_role_names(
    role_spec: "Any", role_map: dict[str, str], name_cache: "dict[str, str] | None" = None
) -> list[str]:
    """Role tokens that resolve to neither a member-held role nor a cached role and don't
    look like a raw UUID — i.e. a likely typo or a role missing from the cache. Surfaced as
    a warning so a bare 'Invalid UUID format' 400 from ACC isn't the only feedback."""
    if name_cache is None:
        name_cache = _load_role_name_to_id()
    out: list[str] = []
    for name in _as_role_list(role_spec):
        if _resolve_role_id(name, role_map) or name_cache.get(str(name).strip().lower()):
            continue
        if _looks_like_uuid(name):
            continue
        out.append(name)
    return out


def _role_display(role_spec: "Any") -> str:
    """Human-readable rendering of a role spec (one or many) for result rows / audit CSV."""
    return ", ".join(_as_role_list(role_spec))


def _write_audit_csv(rows: list[dict], operation: str) -> str:
    """Write audit rows to a timestamped CSV in the audit_logs/ folder. Returns file path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_logs")
    os.makedirs(audit_dir, exist_ok=True)
    filepath = os.path.join(audit_dir, f"audit_{operation}_{timestamp}.csv")
    if rows:
        # Union of keys across all rows (first-seen order) so heterogeneous rows
        # — e.g. only error rows carry a "message" — don't trip DictWriter.
        fieldnames = list(dict.fromkeys(k for row in rows for k in row.keys()))
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
            writer.writeheader()
            writer.writerows(rows)
    return filepath


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_hubs",
            description="List all ACC/BIM360 hubs the account has access to. Entry point for hub IDs.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_projects",
            description=(
                "List all projects in the account's primary hub. "
                "Returns project names and IDs. Use project names with other tools."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "hub_id": {"type": "string", "description": "Hub ID (b.xxxx). Omit to use hub_name or first hub."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
            },
        ),
        Tool(
            name="resolve_project",
            description=(
                "Resolve an ACC project from a name, a project ID (b.xxxx or bare UUID), "
                "or a full ACC URL, and return it together with its owning hub in one call. "
                "For an ID/URL this is a single Admin-API call (auto-routed across regions, "
                "no hub guessing) — the reliable way to pin a project when multiple hubs "
                "share a region. A name matching more than one project returns candidates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Project name (partial ok), project ID (b.xxxx or bare UUID), or full ACC URL."},
                    "region": {"type": "string", "description": "Hub region hint for name searches (e.g. EMEA, US). Defaults to EMEA; ignored for ID/URL queries."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_top_folders",
            description=(
                "List top-level folders of a project (e.g. 'Project Files', 'Plans'). "
                "Accepts project name — no need to know the project ID."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_folder_contents",
            description=(
                "List files and sub-folders inside a folder path within a project. "
                "folder_path is slash-separated display names, e.g. 'Project Files/Drawings/Structural'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder_path": {"type": "string", "description": "Slash-separated path e.g. 'Project Files/Drawings', or a raw folder URN for faster resolution."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": ["folder_path"],
            },
        ),
        Tool(
            name="rename_folder",
            description=(
                "Rename a single folder in an ACC project. "
                "folder_path is the slash-separated display-name path to the folder to rename, "
                "e.g. 'Project Files/Drawings/Old Name'. "
                "Returns folder_id, old_name, and new_name on success."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder_path": {
                        "type": "string",
                        "description": "Slash-separated path e.g. 'Project Files/Drawings/My Folder', or a raw folder URN for faster resolution.",
                    },
                    "new_name": {"type": "string", "description": "New display name for the folder."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["folder_path", "new_name"],
            },
        ),
        Tool(
            name="rename_file",
            description=(
                "Rename a file in an ACC project by creating a new version with the new name "
                "(no re-upload needed). "
                "folder_path is the slash-separated path to the folder containing the file, "
                "e.g. 'Project Files/Drawings'. "
                "file_name is the current display name of the file (exact match, case-insensitive). "
                "Returns item_id, old_name, new_name, and the new version ID on success."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder_path": {
                        "type": "string",
                        "description": "Slash-separated path to the folder containing the file, or a raw folder URN for faster resolution.",
                    },
                    "file_name": {"type": "string", "description": "Current display name of the file."},
                    "new_name": {"type": "string", "description": "New display name for the file."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["folder_path", "file_name", "new_name"],
            },
        ),
        Tool(
            name="find_folder",
            description=(
                "Search for folders by name (substring, case-insensitive) across a project. "
                "Optionally limit the search to a starting folder_path. "
                "Returns matching folder names, full paths, and IDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "query": {"type": "string", "description": "Folder name substring to search for."},
                    "folder_path": {"type": "string", "description": "Limit search to this folder and its descendants (optional). Accepts a slash-separated path or a raw folder URN."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="create_folder",
            description=(
                "Create a new folder inside an existing parent folder in an ACC project. "
                "parent_folder_path is the slash-separated path to the folder that will contain "
                "the new folder, e.g. 'Project Files/Drawings'. "
                "Returns the new folder_id and name on success."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "parent_folder_path": {
                        "type": "string",
                        "description": "Slash-separated path to the parent folder, e.g. 'Project Files/Drawings', or a raw folder URN for faster resolution.",
                    },
                    "folder_name": {"type": "string", "description": "Name for the new folder."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["parent_folder_path", "folder_name"],
            },
        ),
        Tool(
            name="delete_folder",
            description=(
                "Soft-delete (hide) a folder in an ACC project by setting hidden=true. "
                "ACC does not permanently delete folders — this is reversible by an admin. "
                "Refuses to delete if any files exist anywhere in the folder's subtree. "
                "Set dry_run=true (default) to preview without making changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder_path": {
                        "type": "string",
                        "description": "Slash-separated path e.g. 'Project Files/Drawings/Old Folder', or a raw folder URN.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), preview without making changes.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["folder_path"],
            },
        ),
        Tool(
            name="move_file",
            description=(
                "Move a single file from one folder to another within an ACC project "
                "by changing its parent folder (no re-upload — the file keeps its version history). "
                "source_folder_path is the slash-separated path to the folder currently containing the file; "
                "file_name is its current display name (exact match, case-insensitive); "
                "destination_folder_path is the slash-separated path to the target folder. "
                "Note: cloud-workshared Revit models (C4RModel) cannot be moved via the API and will return a 403. "
                "Set dry_run=true (default) to preview without making changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "source_folder_path": {
                        "type": "string",
                        "description": "Slash-separated path to the folder currently containing the file, or a raw folder URN.",
                    },
                    "file_name": {"type": "string", "description": "Current display name of the file to move."},
                    "destination_folder_path": {
                        "type": "string",
                        "description": "Slash-separated path to the destination folder, or a raw folder URN.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), preview without making changes.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["source_folder_path", "file_name", "destination_folder_path"],
            },
        ),
        Tool(
            name="move_folder",
            description=(
                "Move a folder into a different parent folder within an ACC project "
                "by changing its parent relationship. The folder and all of its contents move together. "
                "folder_path is the slash-separated path to the folder to move; "
                "destination_parent_path is the slash-separated path to the folder that will become its new parent. "
                "Set dry_run=true (default) to preview without making changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder_path": {
                        "type": "string",
                        "description": "Slash-separated path to the folder to move, e.g. 'Project Files/Drawings/My Folder', or a raw folder URN.",
                    },
                    "destination_parent_path": {
                        "type": "string",
                        "description": "Slash-separated path to the new parent folder, or a raw folder URN.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), preview without making changes.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["folder_path", "destination_parent_path"],
            },
        ),
        Tool(
            name="list_project_members",
            description=(
                "List all members of an ACC project with their roles and companies. "
                "Requires Account Admin access on the service account."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_account_users",
            description=(
                "List all users in the ACC account (hub level) with name, email, company, role, "
                "status, and last sign-in date. Use this to audit inactive users, find who hasn't "
                "logged in recently, or get a full account-wide user overview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
            },
        ),
        Tool(
            name="find_recent_activity",
            description=(
                "Show recently modified files in a project since a given date. "
                "since_date format: YYYY-MM-DD. Defaults to last 7 days."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "since_date": {"type": "string", "description": "YYYY-MM-DD, defaults to 7 days ago"},
                    "limit": {"type": "integer", "description": "Max items (default 50)"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="find_files",
            description=(
                "Search for files by name (substring, case-insensitive) across a project. "
                "Optionally limit to a folder_path. Useful when you don't know the folder."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "query": {"type": "string", "description": "Filename substring"},
                    "folder_path": {"type": "string", "description": "Limit search to this folder (optional). Accepts a slash-separated path or a raw folder URN."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_all_files",
            description=(
                "List every file in a project (or in one folder and all its subfolders), "
                "recursively. Omit folder_path for the whole project; pass it to scope to a "
                "subtree. Returns each file's full path, id, and last-modified info, newest "
                "first. This is the unfiltered companion to find_files — use it to inventory "
                "or export all file URNs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder_path": {"type": "string", "description": "Limit to this folder and its subfolders (optional). Slash-separated path or a raw folder URN. Omit for the entire project."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="export_deliverables_manifest",
            description=(
                "Return a compact, plain-text list of every file NAME under a project (or one "
                "folder and all its subfolders), deduplicated and sorted A→Z, one per line. "
                "Purpose-built for cross-checking what's actually present against an external "
                "deliverable list (e.g. an Excel checklist): it strips all metadata (no ids, "
                "paths, dates, or owners) so the output stays token-lean even for large trees. "
                "Optionally filter to specific file extensions. Omit folder_path for the whole "
                "project; pass it to scope to a subtree. For full per-file detail (paths/URNs/"
                "dates) use list_all_files instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder_path": {"type": "string", "description": "Limit to this folder and its subfolders (optional). Slash-separated path or a raw folder URN. Omit for the entire project."},
                    "extensions": {"type": "array", "items": {"type": "string"}, "description": "Optional list of file extensions to include (e.g. ['rvt','ifc','pdf']). Leading dots and case are ignored. Omit to include every file."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_project_roles",
            description=(
                "List all roles defined in an ACC project and show which members hold each role. "
                "Returns a deduplicated role list (role_id + name) and a members list with each "
                "person's email, display name, company, and assigned role names. "
                "Use the role list to resolve role IDs; use the members list to audit who has what access."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_project_companies",
            description=(
                "List all companies linked to an ACC project. "
                "Returns company_id and name. Use to resolve company IDs when reading the permission matrix."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="export_permission_matrix",
            description=(
                "Walk a folder tree and export all permissions as a matrix: "
                "rows = folders, columns = roles only, "
                "cells = effective permission level name (e.g. 'View/Download'), "
                "tagged '(inherited)' when the permission comes only from a parent folder. "
                "Empty cell means the role has no access to that folder. "
                "Returns a 'matrix' pivot table for visual review and a 'flat_rows' list "
                "(one row per folder×role, with 'actions', 'inherit_actions', and "
                "'permission_level') for use with apply_permission_changes. "
                "Subject names and inherited permissions are resolved directly from the "
                "permissions endpoint — no prerequisite export steps required."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder_path": {
                        "type": "string",
                        "description": "Root folder to scan, e.g. 'Project Files/20_SHARED_Extern', or a raw folder URN for faster resolution.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Folder levels to scan below the root (default 2).",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["folder_path"],
            },
        ),
        Tool(
            name="create_role_data_export",
            description=(
                "STEP 1 of 2 to resolve the FULL project-roles catalog (id <-> name), "
                "INCLUDING empty roles that are assigned to no members yet. Forma exposes no "
                "project-roles endpoint, so this one-time Data Connector export (admin service "
                "group) is the only supported way to see roles the members API can't. "
                "Use the returned role IDs for EITHER use case: "
                "(1) ASSIGN users to a role via bulk_assign_users / update_user_roles by passing "
                "the role_id as the role — the only way to place the first person into an "
                "empty/newly-created role; or "
                "(2) LABEL role IDs with names in export_permission_matrix. "
                "Rate limited to 24 jobs per hub per day. "
                "After calling this, immediately call get_data_connector_requests (STEP 2) "
                "with the returned request_id to poll until complete and download the role map."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "send_email": {
                        "type": "boolean",
                        "description": "Send completion email. Default: true.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_data_connector_requests",
            description=(
                "STEP 2 of 2 to resolve the full project-roles catalog (see create_role_data_export). "
                "Lists Data Connector requests and, when a completed job is found, immediately "
                "downloads and parses the role CSV from the signed ZIP URL (valid only 60 s). "
                "Pass request_id (returned by create_role_data_export) to wait for that specific "
                "job — the tool polls internally every 15 s (up to 10 min) so you never need to "
                "call it repeatedly. Returns a role_id→name map (EMPTY roles included) ready to "
                "feed into bulk_assign_users / update_user_roles (assign to a role by its id) or "
                "export_permission_matrix (label role ids with names). Also returns "
                "'roles_name_to_oxygen_id' (name→Reviews numeric autodeskId, from the export's "
                "role_oxygen_id column) — merge it into role_id_cache.json to let the approval-"
                "workflow tools resolve reviewer roles by name, including empty ones."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                    "request_id": {
                        "type": "string",
                        "description": "Poll for this specific request until complete. Returned by create_role_data_export.",
                    },
                },
            },
        ),
        Tool(
            name="apply_permission_changes",
            description=(
                "Apply permission changes to one or more folders in an ACC project. "
                "Each change specifies a folder_id, a subject (role or company by ID), "
                "and the desired full action list. Pass an empty actions list to remove "
                "all permissions for that subject on that folder. "
                "Use the folder_id and subject_id values from export_permission_matrix output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "changes": {
                        "type": "array",
                        "description": "Permission changes to apply.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "folder_id": {"type": "string"},
                                "subject_id": {"type": "string"},
                                "subject_type": {
                                    "type": "string",
                                    "description": "ROLE or COMPANY",
                                },
                                "actions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Full desired action list, e.g. ['VIEW','DOWNLOAD','COLLABORATE']. "
                                        "Empty list = remove this subject's permissions entirely."
                                    ),
                                },
                            },
                            "required": ["folder_id", "subject_id", "subject_type", "actions"],
                        },
                    },
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["changes"],
            },
        ),
        Tool(
            name="bulk_assign_users",
            description=(
                "Add one or more users (by email) to one or more ACC projects with specified roles. "
                "Supports a default role for all projects plus per-project role overrides. "
                "Set dry_run=true (default) to preview what would happen before executing. "
                "Covers: internal/external onboarding, project-specific roles, no-licence onboarding."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of project names (partial match ok).",
                    },
                    "user_emails": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of user email addresses to add.",
                    },
                    "default_role": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": "Role applied to all projects unless overridden by role_overrides. A role name OR role ID; pass a list to assign multiple roles per user, e.g. [\"Editor\", \"BIM Coordinator\"].",
                    },
                    "role_overrides": {
                        "type": "object",
                        "description": "Per-project role map, e.g. {\"Project A\": \"Editor\", \"Project B\": [\"Viewer\", \"EXT Architect\"]}. Each value is a role name/ID or a list of them (multiple roles per user).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), return a preview without making any changes.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_names", "user_emails"],
            },
        ),
        Tool(
            name="update_user_roles",
            description=(
                "Change the role of existing project members across one or more ACC projects. "
                "Only users already in the project are updated — others are skipped without error. "
                "Set dry_run=true (default) to preview changes before executing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_names": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "user_emails": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "default_role": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": "New role(s) to apply across all projects unless overridden. A role name OR role ID; pass a list to set multiple roles per member. This REPLACES the member's existing roles.",
                    },
                    "role_overrides": {
                        "type": "object",
                        "description": "Per-project role map, e.g. {\"Project A\": \"Admin\", \"Project B\": [\"Editor\", \"Viewer\"]}. Each value is a role name/ID or a list of them.",
                    },
                    "dry_run": {"type": "boolean"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_names", "user_emails", "default_role"],
            },
        ),
        Tool(
            name="remove_users_from_projects",
            description=(
                "Remove one or more users from one or more ACC projects (Leaver / offboarding). "
                "Pass an empty project_names list to remove users from ALL projects they are members of. "
                "Set dry_run=true (default) to preview before executing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user_emails": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "project_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Projects to remove users from. Empty list = all projects they are members of.",
                    },
                    "dry_run": {"type": "boolean"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["user_emails"],
            },
        ),
        Tool(
            name="clone_user_access",
            description=(
                "Copy all project memberships and roles from a reference user to one or more target users. "
                "Scans all hub projects, finds the reference user's role in each, then applies "
                "the same roles to the target users. "
                "Set dry_run=true (default) to preview. Note: slow for accounts with many projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "reference_user_email": {
                        "type": "string",
                        "description": "Email of the user whose access profile to copy.",
                    },
                    "target_user_emails": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails of users who should receive the same access.",
                    },
                    "dry_run": {"type": "boolean"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["reference_user_email", "target_user_emails"],
            },
        ),
        Tool(
            name="bulk_assign_company_users",
            description=(
                "Add all (or a filtered subset of) users from a named company to one or more ACC projects. "
                "Looks up company members from the account, then calls the same bulk-assign logic. "
                "Use user_filter to restrict to specific email addresses within the company. "
                "Set dry_run=true (default) to preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "company_name": {
                        "type": "string",
                        "description": "Company name (partial match ok).",
                    },
                    "project_names": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "default_role": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": "Role(s) applied to every company member unless overridden. A role name OR role ID; pass a list for multiple roles per user.",
                    },
                    "role_overrides": {
                        "type": "object",
                        "description": "Per-project role map; each value is a role name/ID or a list of them.",
                    },
                    "user_filter": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: restrict to these emails within the company.",
                    },
                    "dry_run": {"type": "boolean"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["company_name", "project_names", "default_role"],
            },
        ),
        Tool(
            name="list_account_companies",
            description=(
                "List every company in the hub's company directory (account-level), "
                "with id, name, trade and status. Read-only. Use this to find a "
                "company's id, to check whether a company already exists before "
                "importing, or to see the trades in use. Account-wide — not the "
                "project-scoped `list_project_companies`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="bulk_add_hub_users",
            description=(
                "Onboard users to the HUB member directory in bulk (account-level), "
                "each with a company and an optional default role. This is the "
                "account-onboarding step that must happen BEFORE a user can be added "
                "to a project — distinct from `bulk_assign_users`, which assigns "
                "already-existing hub members to projects. Idempotent: emails already "
                "in the hub are reported `already_exists` and skipped. `company_name` "
                "is required and resolved to a company id via the account directory "
                "(run `bulk_add_hub_companies` / `list_account_companies` first if the "
                "company does not exist yet). Batches of 50 per API call. "
                "Set dry_run=true (default) to preview. Note: this cannot elevate a "
                "user to account admin."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user_emails": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails of the users to add to the hub.",
                    },
                    "company_name": {
                        "type": "string",
                        "description": "Company to assign to every new user (required; resolved to a company id via the account directory).",
                    },
                    "default_role": {
                        "type": "string",
                        "description": "Optional default role string applied to every new user.",
                    },
                    "names": {
                        "type": "object",
                        "description": "Optional map of email -> {\"first_name\": ..., \"last_name\": ...} to set display names.",
                    },
                    "dry_run": {"type": "boolean"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only rows that were added or errored (drops already_exists no-ops). 'full' echoes every row. 'summary' omits results but keeps a failures array. Counts are accurate regardless.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max import batches in flight at once (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["user_emails", "company_name"],
            },
        ),
        Tool(
            name="bulk_add_hub_companies",
            description=(
                "Import partner companies into the hub's company directory in bulk "
                "(account-level). Idempotent: a company whose name already exists is "
                "reported `already_exists` and skipped. Each company needs a `name` "
                "and a `trade`; optional address/contact fields are passed through. "
                "Batches of 50 per API call. Set dry_run=true (default) to preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "companies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "trade": {"type": "string"},
                                "address_line_1": {"type": "string"},
                                "address_line_2": {"type": "string"},
                                "city": {"type": "string"},
                                "state_or_province": {"type": "string"},
                                "postal_code": {"type": "string"},
                                "country": {"type": "string"},
                                "phone": {"type": "string"},
                                "website_url": {"type": "string"},
                                "description": {"type": "string"},
                                "erp_id": {"type": "string"},
                                "tax_id": {"type": "string"},
                            },
                            "required": ["name", "trade"],
                        },
                        "description": "Companies to import. Each requires name + trade.",
                    },
                    "dry_run": {"type": "boolean"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only rows that were added or errored (drops already_exists no-ops). 'full' echoes every row. 'summary' omits results but keeps a failures array. Counts are accurate regardless.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max import batches in flight at once (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["companies"],
            },
        ),
        Tool(
            name="deactivate_hub_users",
            description=(
                "Offboard users from the hub by setting their account status to "
                "inactive (account-level). The HQ API has no hard delete, so this is "
                "the soft-offboard path. Idempotent-ish: an email not found in the hub "
                "is reported `not_found`. Set dry_run=true (default) to preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user_emails": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails of the hub users to deactivate.",
                    },
                    "dry_run": {"type": "boolean"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only rows that were deactivated or errored. 'full' echoes every row. 'summary' omits results but keeps a failures array.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max PATCHes in flight at once (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["user_emails"],
            },
        ),
        Tool(
            name="deactivate_hub_companies",
            description=(
                "Deactivate companies in the hub's company directory by setting their "
                "status to inactive (account-level). Soft-offboard path (no hard "
                "delete). A company name not found is reported `not_found`. "
                "Set dry_run=true (default) to preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "company_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of the companies to deactivate.",
                    },
                    "dry_run": {"type": "boolean"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only rows that were deactivated or errored. 'full' echoes every row. 'summary' omits results but keeps a failures array.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max PATCHes in flight at once (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["company_names"],
            },
        ),
        Tool(
            name="bulk_list_folder_contents",
            description=(
                "Audit engine (read-only): list the immediate contents of many folders in one call. "
                "Either pass an explicit `folders` list (slash-separated paths and/or raw folder URNs), "
                "OR pass `children_of` to list the contents of EVERY immediate subfolder of that folder "
                "(e.g. audit all building folders under 'Project Files' in a single call). "
                "Returns each folder's subfolders (name + id) and files, so an orchestrator can diff "
                "against a desired template without extra lookups. No dry_run (read-only)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folders": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit folders to list (slash-separated paths or raw folder URNs). Use this OR children_of.",
                    },
                    "children_of": {
                        "type": "string",
                        "description": "List the contents of every immediate subfolder of this folder (path or URN). Use this OR folders.",
                    },
                    "include_regex": {
                        "type": "string",
                        "description": "With children_of: only audit subfolders whose display name matches this regex (e.g. '^B-B-').",
                    },
                    "exclude": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Subfolder display names to skip (exact, case-insensitive).",
                    },
                    "include_files": {
                        "type": "boolean",
                        "description": "If true (default), return each folder's files; if false, return only subfolders + file_count.",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["files", "subfolders"]},
                        "description": "Restrict each folder row to these sections (default: both). When provided, file entries are returned lean (id + name only, dropping last_modified/created_by) — ideal when you only need URNs to feed into a move.",
                    },
                    "include_naming_standard": {
                        "type": "boolean",
                        "description": "If true, each subfolder row also carries its assigned naming convention as `naming_standard_ids` (empty list = none). Free — the data is already in the listing — so you get folder contents and naming conventions in one pass instead of also calling audit_folder_naming_standards. Default false.",
                    },
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only folders that have files or subfolders (empties dropped). 'full' echoes every folder. 'summary' returns only the counts (the always-present 'errors' array still surfaces any folder that failed to list). Counts are accurate regardless.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max folders listed in parallel (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="audit_folder_naming_standards",
            description=(
                "Read-only audit: walk a folder subtree and report which naming "
                "convention (naming standard) each folder enforces, and which have "
                "none. Reads each folder's assigned standard from its parent's "
                "listing — no per-folder lookup. Pass `folder` (slash-separated path "
                "or raw folder URN) to audit that folder and everything beneath it; "
                "omit it to audit every top-level folder of the project. The summary "
                "groups folders by standard id (`by_standard`) and counts the gaps; "
                "the folders WITHOUT any standard are the noteworthy rows. No dry_run "
                "(read-only). NOTE: a folder's assigned standard governs file naming "
                "in that folder — this does not check whether existing files comply."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folder": {
                        "type": "string",
                        "description": "Folder to audit (slash-separated path or raw folder URN), including its whole subtree. Omit to audit every top-level folder of the project.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "How many levels below the start folder(s) to descend (default 25 — effectively the whole tree). 0 audits only the start folder(s) themselves.",
                    },
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only folders that have NO naming standard (the gaps to fix). 'full' echoes every folder with its standard id(s). 'summary' omits the per-folder rows but still surfaces the no-standard folders via a 'failures' array. The 'summary.by_standard' counts are accurate regardless.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max folder listings in flight at once (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="bulk_create_folders",
            description=(
                "Idempotent batch folder create. Each item is {parent, name} where parent is a "
                "slash-separated path or a raw folder URN. With skip_if_exists=true (default), a child "
                "folder whose name already exists under its parent is reported as 'exists' and never "
                "duplicated — so re-running a partially-completed batch converges cleanly. "
                "Set dry_run=true (default) to preview ('would_create' / 'would_exist')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "parent": {"type": "string", "description": "Parent folder path or URN."},
                                "name": {"type": "string", "description": "Name for the new child folder."},
                            },
                            "required": ["parent", "name"],
                        },
                        "description": "Folders to create.",
                    },
                    "skip_if_exists": {"type": "boolean", "description": "If true (default), never create a duplicate same-named child."},
                    "dry_run": {"type": "boolean", "description": "If true (default), preview without making changes."},
                    "continue_on_error": {"type": "boolean", "description": "If true (default), one failing item never aborts the batch."},
                    "max_concurrency": {"type": "integer", "description": "Max creates in parallel (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["items"],
            },
        ),
        Tool(
            name="bulk_delete_folders",
            description=(
                "File-safe batch soft-delete (hide) of many folders. Each target is a slash-separated "
                "path or a raw folder URN. Identical safety to the single delete_folder: a folder with "
                "any file anywhere in its subtree is NEVER deleted — it is reported as 'skipped_has_files' "
                "with file_count + sample_files (these are the 'stuck files', typically cloud-workshared "
                "Revit models, for a human to move). Empty folders are soft-deleted (admin-reversible). "
                "Set dry_run=true (default) to preview ('would_delete')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "folders": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Folders to soft-delete (slash-separated paths or raw folder URNs).",
                    },
                    "dry_run": {"type": "boolean", "description": "If true (default), preview without making changes."},
                    "continue_on_error": {"type": "boolean", "description": "If true (default), one failing item never aborts the batch."},
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only the rows needing attention (skipped_has_files + errors; the deleted/would_delete/not_found no-ops are dropped). 'full' echoes every row. 'summary' returns only the counts, but failures (skipped_has_files + errors) are still surfaced via a 'failures' array. Counts are accurate regardless.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max folders processed in parallel (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["folders"],
            },
        ),
        Tool(
            name="bulk_move_files",
            description=(
                "Batch-move many files into new folders (changes each file's parent; no re-upload, "
                "version history preserved). Each item gives a destination plus EITHER a raw `item_id` "
                "(e.g. a file id from bulk_list_folder_contents) OR a `source` folder + `name` to look it "
                "up by. Idempotent: a file already in its destination is reported 'already_there'. "
                "Cloud-workshared Revit models (C4RModel) return 403 and are reported 'skipped_unmovable' "
                "(move them in the Revit/ACC UI). Set dry_run=true (default) to preview ('would_move')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "string", "description": "Raw file (item) URN. Use this OR source+name."},
                                "source": {"type": "string", "description": "Folder (path or URN) currently containing the file. Use with name."},
                                "name": {"type": "string", "description": "File display name within source (exact, case-insensitive)."},
                                "destination": {"type": "string", "description": "Target folder (path or URN)."},
                            },
                            "required": ["destination"],
                        },
                        "description": "Files to move.",
                    },
                    "dry_run": {"type": "boolean", "description": "If true (default), preview without making changes."},
                    "continue_on_error": {"type": "boolean", "description": "If true (default), one failing item never aborts the batch."},
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only the rows needing attention (errors + skipped_unmovable + not_found; the moved/would_move/already_there no-ops are dropped). 'full' echoes every row. 'summary' returns only the counts, but failures (errors + skipped_unmovable + not_found) are still surfaced via a 'failures' array. Counts are accurate regardless.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max moves in parallel (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["items"],
            },
        ),
        Tool(
            name="bulk_move_folders",
            description=(
                "Batch-move many folders (with all their contents) under new parent folders by changing "
                "each folder's parent. Each item gives a `folder` to move (path or URN) and a `destination` "
                "parent (path or URN). Idempotent: a folder already directly under its destination is "
                "reported 'already_there'. Set dry_run=true (default) to preview ('would_move')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "folder": {"type": "string", "description": "Folder to move (path or URN)."},
                                "destination": {"type": "string", "description": "Target parent folder (path or URN)."},
                            },
                            "required": ["folder", "destination"],
                        },
                        "description": "Folders to move.",
                    },
                    "dry_run": {"type": "boolean", "description": "If true (default), preview without making changes."},
                    "continue_on_error": {"type": "boolean", "description": "If true (default), one failing item never aborts the batch."},
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns the summary plus only the rows needing attention (errors + not_found; the moved/would_move/already_there no-ops are dropped). 'full' echoes every row. 'summary' returns only the counts, but failures (errors + not_found) are still surfaced via a 'failures' array. Counts are accurate regardless.",
                    },
                    "max_concurrency": {"type": "integer", "description": "Max moves in parallel (default 8)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["items"],
            },
        ),
        Tool(
            name="list_issues",
            description=(
                "List issues in an ACC project (Issues module). Returns file-related "
                "(pushpin) and general issues with their comments/attachments metadata; "
                "sheet-related issues from the Forma Build Sheets tool are not returned. "
                "Auto-paginates all matches. Issue objects are large — narrow with the "
                "filters below and/or pass `fields` to slim each row. Type/subtype come "
                "back as UUIDs; use list_issue_types to decode them. Not compatible with "
                "BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "status": {"type": "string", "description": "Filter by status (comma-separate multiple). Values: draft, open, pending, in_progress, completed, in_review, not_approved, in_dispute, closed."},
                    "search": {"type": "string", "description": "Free-text search over issue title and display id."},
                    "display_id": {"type": "string", "description": "Filter by the user-friendly numeric display id (comma-separate multiple)."},
                    "assigned_to": {"type": "string", "description": "Filter by current assignee Autodesk ID (comma-separate multiple)."},
                    "created_by": {"type": "string", "description": "Filter by the Autodesk ID of the issue creator (comma-separate multiple)."},
                    "due_date": {"type": "string", "description": "Filter by due date. YYYY-MM-DD, a range 'YYYY-MM-DD..YYYY-MM-DD', or an open-ended range."},
                    "issue_type_id": {"type": "string", "description": "Filter by issue type (category) UUID (comma-separate multiple)."},
                    "issue_subtype_id": {"type": "string", "description": "Filter by issue subtype (type) UUID (comma-separate multiple)."},
                    "linked_document_urn": {"type": "string", "description": "Retrieve pushpin issues linked to the given file item IDs (3D model or PDF lineage URNs; comma-separate multiple)."},
                    "deleted": {"type": "boolean", "description": "If true, return only deleted issues; if false (default on the API), only undeleted. Requires elevated permissions to see others' deleted issues."},
                    "sort_by": {"type": "string", "description": "Sort fields, comma-separated; prefix a field with '-' for descending (e.g. 'status,-displayId')."},
                    "fields": {"type": "string", "description": "Comma-separated fields to return per issue (slims the payload). id, title, status, issueTypeId are always included."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="create_issue",
            description=(
                "Create an issue in an ACC project. Posts immediately (no dry_run). "
                "`issue_subtype_id` and `status` are required — get a valid "
                "`issue_subtype_id` from list_issue_types (the API's 'subtype' = the "
                "product's 'Type'). Assignee/watcher/root-cause/location IDs are NOT "
                "discoverable via this API; per Autodesk, extract them with the Data "
                "Connector. Custom fields come from list_issue_attribute_definitions. "
                "Not compatible with BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "title": {"type": "string", "description": "Issue title (max 100 chars)."},
                    "issue_subtype_id": {"type": "string", "description": "UUID of the issue subtype (the product's 'Type') — from list_issue_types."},
                    "status": {"type": "string", "description": "Initial status. Values: draft, open, pending, in_progress, completed, in_review, not_approved, in_dispute, closed."},
                    "description": {"type": "string", "description": "Issue description (max 1000 chars)."},
                    "assigned_to": {"type": "string", "description": "Autodesk ID of the assignee (member/company/role). Requires assigned_to_type."},
                    "assigned_to_type": {"type": "string", "description": "Assignee type: user, company, or role. Requires assigned_to."},
                    "due_date": {"type": "string", "description": "Due date, ISO8601 (YYYY-MM-DD)."},
                    "start_date": {"type": "string", "description": "Start date, ISO8601 (YYYY-MM-DD)."},
                    "location_id": {"type": "string", "description": "LBS location UUID."},
                    "location_details": {"type": "string", "description": "Free-text location (max 250 chars)."},
                    "root_cause_id": {"type": "string", "description": "Root-cause type UUID."},
                    "published": {"type": "boolean", "description": "Publish the issue (default false = unpublished draft)."},
                    "watchers": {"type": "array", "items": {"type": "string"}, "description": "Autodesk IDs of members to add as watchers."},
                    "custom_attributes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "attributeDefinitionId": {"type": "string", "description": "Custom-attribute UUID (from list_issue_attribute_definitions)."},
                                "value": {"description": "Value: string, number, or null."},
                            },
                            "required": ["attributeDefinitionId", "value"],
                        },
                        "description": "Custom field values to set on the issue.",
                    },
                    "gps_coordinates": {
                        "type": "object",
                        "properties": {
                            "latitude": {"type": "number"},
                            "longitude": {"type": "number"},
                        },
                        "description": "Optional GPS location of the issue.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": ["title", "issue_subtype_id", "status"],
            },
        ),
        Tool(
            name="list_issue_types",
            description=(
                "List an ACC project's issue categories (API 'type') and, by default, "
                "their types (API 'subtype') with the 3-char pushpin code. Use this to "
                "find a valid issue_subtype_id for create_issue and to decode the "
                "type/subtype UUIDs returned by list_issues. Does not return deleted "
                "items. Not compatible with BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "include_subtypes": {"type": "boolean", "description": "Include each category's types (subtypes). Default true."},
                    "is_active": {"type": "boolean", "description": "If set, filter to active (true) or inactive (false) categories only. Default: both."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_issue_attribute_definitions",
            description=(
                "List an ACC project's issue custom fields (custom attributes): id, "
                "title, description, dataType (list/text/paragraph/numeric), and — for "
                "list-type fields — the dropdown options with their ids. Use to "
                "interpret and populate the customAttributes on issues. Not compatible "
                "with BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "data_type": {"type": "string", "description": "Filter by data type (comma-separate multiple). Values: list, text, paragraph, numeric."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_issue_attribute_mappings",
            description=(
                "List which issue custom fields are assigned to which issue categories "
                "(mappedItemType 'issueType') and types ('issueSubtype'). By default "
                "returns only directly-assigned mappings, not inherited ones. Pair with "
                "list_issue_attribute_definitions to know each field's shape. Not "
                "compatible with BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "attribute_definition_id": {"type": "string", "description": "Filter by custom-attribute definition UUID (comma-separate multiple)."},
                    "mapped_item_id": {"type": "string", "description": "Filter by mapped item UUID — a project, type, or subtype (comma-separate multiple)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_issue",
            description=(
                "Get a single issue by its UUID (`issue_id`) or its friendly number "
                "(`display_id`, e.g. 191). Returns the full issue object. Not "
                "compatible with BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "issue_id": {"type": "string", "description": "The issue UUID (preferred). Provide this or display_id."},
                    "display_id": {"type": "integer", "description": "The friendly issue number (e.g. 191). Resolved to the UUID via one lookup. Provide this or issue_id."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="update_issue",
            description=(
                "Update an existing issue (edit fields, change status, reassign). "
                "Identify it by `issue_id` (UUID) or `display_id`. Posts immediately. "
                "Only the fields you supply are changed. Check an issue's "
                "permittedStatuses/permittedAttributes (via get_issue) if a change is "
                "rejected. Updating a deleted issue is not allowed. Not compatible with "
                "BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "issue_id": {"type": "string", "description": "The issue UUID (preferred). Provide this or display_id."},
                    "display_id": {"type": "integer", "description": "The friendly issue number (e.g. 191). Provide this or issue_id."},
                    "title": {"type": "string", "description": "New title (max 100 chars)."},
                    "description": {"type": "string", "description": "New description (max 1000 chars)."},
                    "issue_subtype_id": {"type": "string", "description": "New issue subtype (the product's 'Type') UUID — from list_issue_types."},
                    "status": {"type": "string", "description": "New status. Values: draft, open, pending, in_progress, completed, in_review, not_approved, in_dispute, closed."},
                    "assigned_to": {"type": "string", "description": "Autodesk ID of the assignee (member/company/role). Requires assigned_to_type."},
                    "assigned_to_type": {"type": "string", "description": "Assignee type: user, company, or role. Requires assigned_to."},
                    "due_date": {"type": "string", "description": "Due date, ISO8601 (YYYY-MM-DD)."},
                    "start_date": {"type": "string", "description": "Start date, ISO8601 (YYYY-MM-DD)."},
                    "location_id": {"type": "string", "description": "LBS location UUID."},
                    "location_details": {"type": "string", "description": "Free-text location (max 250 chars)."},
                    "root_cause_id": {"type": "string", "description": "Root-cause type UUID."},
                    "published": {"type": "boolean", "description": "Publish (true) or unpublish (false) the issue."},
                    "watchers": {"type": "array", "items": {"type": "string"}, "description": "Autodesk IDs of members to set as watchers."},
                    "custom_attributes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "attributeDefinitionId": {"type": "string", "description": "Custom-attribute UUID (from list_issue_attribute_definitions)."},
                                "value": {"description": "Value: string, number, or null."},
                            },
                            "required": ["attributeDefinitionId", "value"],
                        },
                        "description": "Custom field values to set on the issue.",
                    },
                    "gps_coordinates": {
                        "type": "object",
                        "properties": {
                            "latitude": {"type": "number"},
                            "longitude": {"type": "number"},
                        },
                        "description": "GPS location of the issue.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_workflows",
            description=(
                "List approval workflows (ACC Reviews module) in a project. Each "
                "workflow defines the steps, reviewers/approvers, durations, approval "
                "statuses, and post-review copy/attribute actions used when creating "
                "file reviews. Auto-paginates all matches. By default the API returns "
                "only ACTIVE workflows — pass status=INACTIVE for disabled ones. Not "
                "compatible with BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "status": {"type": "string", "description": "Filter by status: ACTIVE (default) or INACTIVE. Cannot be combined with initiator."},
                    "initiator": {"type": "boolean", "description": "If true, return only workflows initiated by the current user (ignored for project admins). Cannot be combined with status."},
                    "sort": {"type": "string", "description": "Sort field: name, status, or updatedAt; append ' desc' for descending (e.g. 'name desc')."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_workflow",
            description=(
                "Get a single approval workflow by its UUID (`workflow_id`) or by its "
                "`name` (one lookup among the project's ACTIVE workflows). Returns the "
                "full workflow object including every step's resolved candidates "
                "(users/roles/companies with their Autodesk IDs). Not compatible with "
                "BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "workflow_id": {"type": "string", "description": "The workflow UUID (preferred). Provide this or name."},
                    "name": {"type": "string", "description": "The workflow name (exact, case-insensitive). Resolved to the UUID via one list call. Provide this or workflow_id."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": [],
            },
        ),
        Tool(
            name="create_workflow",
            description=(
                "Create an approval workflow in an ACC project. Posts immediately (no "
                "dry_run — use bulk_create_workflows for a previewable batch). Reviewers "
                "are given as friendly names/emails/role names/company names per step "
                "(reviewer_users / reviewer_roles / reviewer_companies) and resolved to "
                "Autodesk IDs automatically; raw autodeskId values also pass through. A "
                "name collision returns a 409. Not compatible with BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "name": {"type": "string", "description": "Workflow name — must be unique within the project (max 255)."},
                    "description": {"type": "string", "description": "Workflow description (max 4096)."},
                    "notes": {"type": "string", "description": "Custom note shown to all reviewers during the review (max 4096)."},
                    "initiator_edit_permissions": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["REVIEWER_ASSIGNMENTS_AND_DURATION", "APPROVERS"]},
                        "description": "Extra edit permissions granted to the review initiator in the UI. Omit/empty = none.",
                    },
                    "additional_approval_status_options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Display name, unique across all statuses (max 255)."},
                                "value": {"type": "string", "enum": ["APPROVED", "REJECTED"], "description": "Underlying outcome the custom status maps to."},
                            },
                            "required": ["label", "value"],
                        },
                        "description": "Custom approval statuses added on top of the built-in APPROVED/REJECTED (up to 50).",
                    },
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Step name (max 255)."},
                                "type": {"type": "string", "enum": ["INITIATOR", "REVIEWER", "APPROVER"], "description": "INITIATOR (first, launches review), REVIEWER (intermediate), or APPROVER (final decision)."},
                                "duration": {"type": "integer", "description": "Days allowed for the step (1–99). REVIEWER/APPROVER only."},
                                "due_date_type": {"type": "string", "enum": ["CALENDAR_DAY", "WORKDAY"], "description": "How the due date is counted (REVIEWER/APPROVER only). Defaults to CALENDAR_DAY — the API requires this field on those steps, so the tool fills it in when omitted."},
                                "group_review": {
                                    "type": "object",
                                    "properties": {
                                        "enabled": {"type": "boolean", "description": "Allow multiple reviewers on this step."},
                                        "type": {"type": "string", "enum": ["ALL", "MINIMUM"], "description": "ALL reviewers must respond, or a MINIMUM number."},
                                        "min": {"type": "integer", "description": "Minimum responders when type=MINIMUM (1–30)."},
                                    },
                                    "description": "Group-review rule. REVIEWER steps only.",
                                },
                                "reviewer_users": {"type": "array", "items": {"type": "string"}, "description": "Reviewers/approvers by user name or email (resolved to Autodesk IDs via project members). Raw autodeskId also accepted."},
                                "reviewer_roles": {"type": "array", "items": {"type": "string"}, "description": "Reviewers/approvers by role name. Resolved to the Reviews numeric autodeskId from: any member holding the role, a workflow already using it, or the role_id_cache.json 'roles_name_to_oxygen_id' map. Only a genuinely empty role (no member, no workflow, not cached) needs the raw numeric autodeskId (see get_workflow)."},
                                "reviewer_companies": {"type": "array", "items": {"type": "string"}, "description": "Reviewers/approvers by company name. Resolved from any member's company, a workflow already using it, or the cache — otherwise pass the raw autodeskId."},
                                "candidates": {"type": "object", "description": "Advanced: a pre-built candidates object ({users/roles/companies:[{autodeskId}]}). Used only if no reviewer_* keys are given."},
                            },
                            "required": ["name", "type"],
                        },
                        "description": "Ordered workflow steps. Steps run in the order given.",
                    },
                    "copy_files_options": {
                        "type": "object",
                        "properties": {
                            "enabled": {"type": "boolean", "description": "Copy approved files to a target folder when the review completes."},
                            "allowOverride": {"type": "boolean", "description": "Let the initiator change the target folder."},
                            "condition": {"type": "string", "enum": ["ANY", "ALL"], "description": "Copy when ANY or ALL files are approved."},
                            "folderUrn": {"type": "string", "description": "Target folder URN for approved copies."},
                            "includeMarkups": {"type": "boolean", "description": "Include published markups on copied files."},
                            "disableOverrideMarkupSetting": {"type": "boolean", "description": "Lock the markup setting during review setup."},
                        },
                        "description": "Post-review copy-approved-files action. Defaults to {enabled:false} when omitted.",
                    },
                    "attached_attributes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer", "description": "Custom attribute id to apply after completion."},
                                "required": {"type": "boolean", "description": "Whether the approver must supply a value."},
                            },
                            "required": ["id"],
                        },
                        "description": "Custom attributes applied to approved files (Update Attributes action).",
                    },
                    "update_attributes_options": {
                        "type": "object",
                        "properties": {
                            "enableAttachedAttributes": {"type": "boolean"},
                            "updateSourceAndCopiedFiles": {"type": "boolean"},
                            "allowApproverToUpdateRejectedFiles": {"type": "boolean"},
                        },
                        "description": "Controls how the attached_attributes are applied. Requires a copy action.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": ["name", "steps"],
            },
        ),
        Tool(
            name="bulk_create_workflows",
            description=(
                "Create many approval workflows in one project from a list of specs — "
                "purpose-built for pushing an Excel template of workflows into ACC. Each "
                "item has the same shape as create_workflow (minus the project). "
                "Reviewer names/emails/roles/companies are resolved to Autodesk IDs once "
                "up front and reused across all rows. Defaults to dry_run=true "
                "('would_create'); a live run writes a timestamped audit CSV. A name "
                "collision is reported per-row as 'already_exists' (409). Not compatible "
                "with BIM 360 projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (partial ok), ID (b.xxx or bare UUID), or full ACC URL — preferred over project_name; pins the exact hub."},
                    "project_name": {"type": "string", "description": "Alias of 'project' (name only). Kept for backward compatibility."},
                    "workflows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Workflow name (unique within the project)."},
                                "description": {"type": "string"},
                                "notes": {"type": "string"},
                                "initiator_edit_permissions": {"type": "array", "items": {"type": "string"}},
                                "additional_approval_status_options": {"type": "array", "items": {"type": "object"}},
                                "steps": {"type": "array", "items": {"type": "object"}, "description": "Same step shape as create_workflow (name, type, duration, due_date_type, group_review, reviewer_users/roles/companies)."},
                                "copy_files_options": {"type": "object"},
                                "attached_attributes": {"type": "array", "items": {"type": "object"}},
                                "update_attributes_options": {"type": "object"},
                            },
                            "required": ["name", "steps"],
                        },
                        "description": "The workflows to create.",
                    },
                    "dry_run": {"type": "boolean", "description": "If true (default), preview ('would_create') without posting."},
                    "continue_on_error": {"type": "boolean", "description": "If true (default), one failing row never aborts the batch."},
                    "max_concurrency": {"type": "integer", "description": "Max workflow POSTs in parallel (default 8)."},
                    "response_detail": {
                        "type": "string",
                        "enum": ["summary", "changes", "full"],
                        "description": "Output verbosity. 'changes' (default) returns summary plus only the rows needing attention (errors + already_exists). 'full' echoes every row. 'summary' omits results but still surfaces a 'failures' array. Counts are accurate regardless.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'My Company - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region for name resolution. Defaults to EMEA."},
                },
                "required": ["workflows"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _quota_error_result(message: str, retry_after: "int | None") -> CallToolResult:
    """Build a tool result for a 429: readable JSON content AND `isError=True`, so
    the calling agent both sees it's a failure and can read why / when to retry."""
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps({
            "error": "quota_exceeded",
            "status": 429,
            "message": message,
            "retry_after_seconds": retry_after,
        }, indent=2))],
    )


def _shape_bulk_response(payload: dict, results: list, detail: "str | None",
                         noteworthy, failure=None) -> dict:
    """Gate a bulk tool's per-item `results` array on `response_detail`, leaving the
    already-computed `summary` counts untouched.

    - "full": echo every row (legacy behaviour).
    - "changes" (default): keep only rows for which `noteworthy(row)` is true — the
      no-op/success-noise (moved/already_there/deleted/empty folders) is dropped.
    - "summary": omit `results` entirely, but NEVER drop failures: any row matching
      `failure(row)` is still surfaced under a `failures` array (so a summary run
      still returns the locked/unmovable list a caller must act on).

    `failure` defaults to `noteworthy` (for the mutating tools every noteworthy row
    is a problem); pass an explicit predicate where the two differ (the audit tool:
    a non-empty folder is noteworthy but not a failure).
    """
    detail = detail or "changes"
    if failure is None:
        failure = noteworthy
    if detail == "full":
        payload["results"] = results
    elif detail == "summary":
        fails = [r for r in results if failure(r)]
        if fails:
            payload["failures"] = fails
    else:  # "changes"
        payload["results"] = [r for r in results if noteworthy(r)]
    return payload


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> "list[TextContent] | CallToolResult":
    """Entry point: dispatch the tool, converting any 429 rate/quota limit into a
    clean error result (`isError=True` with a readable message) so the calling
    agent is told it can't proceed (and why) instead of hanging or seeing an
    opaque error."""
    try:
        return await _dispatch_tool(name, arguments)
    except APSQuotaError as e:
        return _quota_error_result(str(e), e.retry_after)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 429:
            return _quota_error_result(
                _quota_message(e.response),
                _safe_int(e.response.headers.get("Retry-After")),
            )
        raise


async def _dispatch_tool(name: str, arguments: dict) -> list[TextContent]:
    token = await get_access_token()

    async with httpx.AsyncClient(timeout=30) as client:
        hdrs = auth_headers(token)

        if name == "list_hubs":
            res = await client.get(f"{APS_BASE}/project/v1/hubs", headers=hdrs)
            res.raise_for_status()
            hubs = res.json().get("data", [])
            summary = [
                {"id": h["id"], "name": h["attributes"]["name"],
                 "region": h["attributes"].get("region"), "type": h["attributes"].get("hubType")}
                for h in hubs
            ]
            return [TextContent(type="text", text=json.dumps(summary, indent=2))]

        if name == "list_projects":
            hub_id = arguments.get("hub_id")
            if hub_id is None:
                hub_id, _ = await resolve_hub(client, token, _norm_region(arguments), hub_name=arguments.get("hub_name"))
            projects = await get_all_pages(
                client, f"{APS_BASE}/project/v1/hubs/{hub_id}/projects", hdrs
            )
            return [TextContent(type="text", text=json.dumps([_fmt_project(p) for p in projects], indent=2))]

        if name == "resolve_project":
            try:
                ref = await _resolve_project_ref(
                    client, token, arguments["query"],
                    region=_norm_region(arguments), allow_multiple=True,
                )
            except ValueError as e:
                ref = {"error": "not_found", "message": str(e)}
            return [TextContent(type="text", text=json.dumps(ref, indent=2))]

        if name == "list_top_folders":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            res = await client.get(
                f"{APS_BASE}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders",
                headers=hdrs,
            )
            res.raise_for_status()
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "top_folders": [_fmt_folder(f) for f in res.json().get("data", [])],
            }, indent=2))]

        if name == "list_folder_contents":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            folder_path = arguments["folder_path"]
            folder_id, _ = await _resolve_folder(client, token, hub_id, project_id, folder_path)
            items = await get_all_folder_contents(client, project_id, folder_id, hdrs)
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "path": folder_path,
                "folders": [_fmt_folder(i) for i in items if i["type"] == "folders"],
                "files": [_fmt_item(i) for i in items if i["type"] == "items"],
            }, indent=2))]

        if name == "rename_folder":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            folder_path = arguments["folder_path"]
            new_name = arguments["new_name"].strip()
            if not new_name:
                raise ValueError("new_name cannot be empty.")

            folder_id, old_name = await _resolve_folder(
                client, token, hub_id, project_id, folder_path
            )
            payload = {
                "jsonapi": {"version": "1.0"},
                "data": {
                    "type": "folders",
                    "id": folder_id,
                    "attributes": {"name": new_name},
                },
            }
            patch_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            r = await client.patch(
                f"{APS_BASE}/data/v1/projects/{project_id}/folders/{folder_id}",
                headers=patch_hdrs,
                json=payload,
            )
            if not r.is_success:
                body = _error_body(r)
                return [TextContent(type="text", text=json.dumps({
                    "error": r.status_code, "body": body,
                }, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "folder_id": folder_id,
                "old_name": old_name,
                "new_name": new_name,
                "status": "renamed",
            }, indent=2))]

        if name == "rename_file":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            folder_path = arguments["folder_path"]
            file_name = arguments["file_name"].strip()
            new_name = arguments["new_name"].strip()
            if not new_name:
                raise ValueError("new_name cannot be empty.")

            folder_id, _ = await _resolve_folder(client, token, hub_id, project_id, folder_path)

            contents = await get_all_folder_contents(client, project_id, folder_id, hdrs)
            items = [i for i in contents if i["type"] == "items"]
            name_lower = file_name.lower()
            match = next(
                (i for i in items if (i["attributes"].get("displayName") or "").lower() == name_lower),
                None,
            )
            if match is None:
                available = [i["attributes"].get("displayName") for i in items]
                raise ValueError(
                    f"File '{file_name}' not found in '{folder_path}'. Available files: {available}"
                )

            item_id = match["id"]
            tip_version_id = (
                match.get("relationships", {}).get("tip", {}).get("data", {}).get("id")
            )
            if not tip_version_id:
                # Fallback: fetch the item directly
                item_r = await client.get(
                    f"{APS_BASE}/data/v1/projects/{project_id}/items/{item_id}",
                    headers=hdrs,
                )
                item_r.raise_for_status()
                tip_version_id = (
                    item_r.json().get("data", {})
                    .get("relationships", {}).get("tip", {}).get("data", {}).get("id")
                )
            if not tip_version_id:
                raise ValueError(f"Could not determine tip version for '{file_name}'.")

            payload = {
                "jsonapi": {"version": "1.0"},
                "data": {
                    "type": "versions",
                    "attributes": {
                        "name": new_name,
                        "extension": {"version": "1.0"},
                    },
                    "relationships": {
                        "item": {"data": {"type": "items", "id": item_id}},
                    },
                },
            }
            post_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            r = await client.post(
                f"{APS_BASE}/data/v1/projects/{project_id}/versions",
                headers=post_hdrs,
                params={"copyFrom": tip_version_id},
                json=payload,
            )
            if not r.is_success:
                body = _error_body(r)
                return [TextContent(type="text", text=json.dumps({
                    "error": r.status_code, "body": body,
                }, indent=2))]

            resp_data = r.json().get("data", {})
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "item_id": item_id,
                "old_name": file_name,
                "new_name": new_name,
                "new_version_id": resp_data.get("id"),
                "status": "renamed",
            }, indent=2))]

        if name == "move_file":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            source_folder_path = arguments["source_folder_path"]
            file_name = arguments["file_name"].strip()
            destination_folder_path = arguments["destination_folder_path"]
            dry_run = arguments.get("dry_run", True)

            src_folder_id, _ = await _resolve_folder(
                client, token, hub_id, project_id, source_folder_path
            )

            contents = await get_all_folder_contents(client, project_id, src_folder_id, hdrs)
            items = [i for i in contents if i["type"] == "items"]
            name_lower = file_name.lower()
            match = next(
                (i for i in items if (i["attributes"].get("displayName") or "").lower() == name_lower),
                None,
            )
            if match is None:
                available = [i["attributes"].get("displayName") for i in items]
                raise ValueError(
                    f"File '{file_name}' not found in '{source_folder_path}'. Available files: {available}"
                )
            item_id = match["id"]

            dest_folder_id, _ = await _resolve_folder(
                client, token, hub_id, project_id, destination_folder_path
            )

            if dry_run:
                return [TextContent(type="text", text=json.dumps({
                    "project": resolved_name,
                    "item_id": item_id,
                    "file_name": file_name,
                    "from": source_folder_path,
                    "to": destination_folder_path,
                    "destination_folder_id": dest_folder_id,
                    "status": "would_move",
                    "dry_run": True,
                }, indent=2))]

            payload = _reparent_payload("items", item_id, dest_folder_id)
            patch_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            r = await client.patch(
                f"{APS_BASE}/data/v1/projects/{project_id}/items/{item_id}",
                headers=patch_hdrs,
                json=payload,
            )
            if not r.is_success:
                body = _error_body(r)
                return [TextContent(type="text", text=json.dumps({
                    "error": r.status_code, "body": body,
                }, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "item_id": item_id,
                "file_name": file_name,
                "from": source_folder_path,
                "to": destination_folder_path,
                "destination_folder_id": dest_folder_id,
                "status": "moved",
            }, indent=2))]

        if name == "move_folder":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            folder_path = arguments["folder_path"]
            destination_parent_path = arguments["destination_parent_path"]
            dry_run = arguments.get("dry_run", True)

            folder_id, folder_name = await _resolve_folder(
                client, token, hub_id, project_id, folder_path
            )
            dest_parent_id, _ = await _resolve_folder(
                client, token, hub_id, project_id, destination_parent_path
            )

            if dry_run:
                return [TextContent(type="text", text=json.dumps({
                    "project": resolved_name,
                    "folder_id": folder_id,
                    "name": folder_name,
                    "from": folder_path,
                    "to": destination_parent_path,
                    "destination_parent_id": dest_parent_id,
                    "status": "would_move",
                    "dry_run": True,
                }, indent=2))]

            payload = _reparent_payload("folders", folder_id, dest_parent_id)
            patch_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            r = await client.patch(
                f"{APS_BASE}/data/v1/projects/{project_id}/folders/{folder_id}",
                headers=patch_hdrs,
                json=payload,
            )
            if not r.is_success:
                body = _error_body(r)
                return [TextContent(type="text", text=json.dumps({
                    "error": r.status_code, "body": body,
                }, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "folder_id": folder_id,
                "name": folder_name,
                "from": folder_path,
                "to": destination_parent_path,
                "destination_parent_id": dest_parent_id,
                "status": "moved",
            }, indent=2))]

        if name == "create_folder":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            parent_path = arguments["parent_folder_path"]
            folder_name = arguments["folder_name"].strip()
            if not folder_name:
                raise ValueError("folder_name cannot be empty.")

            parent_id, _ = await _resolve_folder(client, token, hub_id, project_id, parent_path)

            payload = _folder_create_payload(folder_name, parent_id)
            post_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            r = await client.post(
                f"{APS_BASE}/data/v1/projects/{project_id}/folders",
                headers=post_hdrs,
                json=payload,
            )
            if not r.is_success:
                body = _error_body(r)
                return [TextContent(type="text", text=json.dumps({
                    "error": r.status_code, "body": body,
                }, indent=2))]

            new_folder = r.json().get("data", {})
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "parent_folder_id": parent_id,
                "folder_id": new_folder.get("id"),
                "folder_name": folder_name,
                "status": "created",
            }, indent=2))]

        if name == "delete_folder":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            folder_path = arguments["folder_path"]
            dry_run = arguments.get("dry_run", True)

            folder_id, folder_name = await _resolve_folder(
                client, token, hub_id, project_id, folder_path
            )

            has_files = await _subtree_has_files(client, project_id, folder_id, hdrs)

            if dry_run:
                return [TextContent(type="text", text=json.dumps({
                    "dry_run": True,
                    "project": resolved_name,
                    "folder_id": folder_id,
                    "folder_name": folder_name,
                    "has_files_in_subtree": has_files,
                    "would_delete": not has_files,
                    "message": (
                        "Folder cannot be deleted: files exist in its subtree."
                        if has_files else
                        "Folder is empty and can be deleted. Set dry_run=false to proceed."
                    ),
                }, indent=2))]

            if has_files:
                raise ValueError(
                    f"Refusing to delete '{folder_name}': files exist in its subtree. "
                    "Remove all files first, then retry."
                )

            payload = _folder_hide_payload(folder_id)
            patch_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            r = await client.patch(
                f"{APS_BASE}/data/v1/projects/{project_id}/folders/{folder_id}",
                headers=patch_hdrs,
                json=payload,
            )
            if not r.is_success:
                body = _error_body(r)
                return [TextContent(type="text", text=json.dumps({
                    "error": r.status_code, "body": body,
                }, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "folder_id": folder_id,
                "folder_name": folder_name,
                "status": "deleted",
                "note": "Folder is hidden (soft-deleted). It can be restored by an ACC admin.",
            }, indent=2))]

        if name == "list_project_members":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            proj_id_clean = _to_bare_id(project_id)
            account_id = _to_bare_id(hub_id)

            # Fetch project members (3-legged) and account users (2-legged) in parallel
            app_token = await get_app_token()
            proj_res, acct_res = await asyncio.gather(
                client.get(
                    f"{APS_BASE}/construction/admin/v1/projects/{proj_id_clean}/users",
                    headers=hdrs,
                    params={"limit": 200},
                ),
                client.get(
                    f"{APS_BASE}/hq/v1/accounts/{account_id}/users",
                    headers=auth_headers(app_token),
                    params={"limit": 100},
                ),
            )

            if proj_res.status_code == 404:
                return [TextContent(type="text", text=f"Member list unavailable for '{resolved_name}'. Service account needs Account Admin access.")]
            proj_res.raise_for_status()

            # Build email → last_sign_in lookup from HQ endpoint
            last_sign_in: dict[str, str] = {}
            if acct_res.status_code == 200:
                for u in acct_res.json():
                    email = u.get("email")
                    if email:
                        last_sign_in[email.lower()] = u.get("last_sign_in")

            users = proj_res.json().get("results", proj_res.json().get("data", []))
            members = [
                {
                    "name": u.get("name") or f"{u.get('firstName','')} {u.get('lastName','')}".strip(),
                    "email": u.get("email"),
                    "autodesk_id": u.get("autodeskId"),
                    "role": u.get("role") or u.get("roleId"),
                    "status": u.get("status"),
                    "company": u.get("companyName"),
                    "last_sign_in": last_sign_in.get((u.get("email") or "").lower()),
                }
                for u in users
            ]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name, "member_count": len(members), "members": members
            }, indent=2))]

        if name == "list_account_users":
            hub_id, _ = await resolve_hub(client, token, _norm_region(arguments), hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)
            app_token = await get_app_token()
            # HQ API returns a plain array (not wrapped in {"data": []}), max limit 100
            users = []
            offset = 0
            while True:
                res = await client.get(
                    f"{APS_BASE}/hq/v1/accounts/{account_id}/users",
                    headers=auth_headers(app_token),
                    params={"limit": 100, "offset": offset},
                )
                res.raise_for_status()
                page = res.json()
                if not isinstance(page, list) or not page:
                    break
                users.extend(page)
                if len(page) < 100:
                    break
                offset += 100
            summary = [
                {
                    "name": f"{u.get('first_name','')} {u.get('last_name','')}".strip() or u.get("name"),
                    "email": u.get("email"),
                    "company": u.get("company_name"),
                    "role": u.get("role"),
                    "status": u.get("status"),
                    "last_sign_in": u.get("last_sign_in"),
                }
                for u in users
            ]
            return [TextContent(type="text", text=json.dumps({
                "account_id": account_id,
                "user_count": len(summary),
                "users": summary,
            }, indent=2))]

        if name == "find_recent_activity":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            limit = int(arguments.get("limit", 50))
            since_raw = arguments.get("since_date")
            since_dt = (
                datetime.fromisoformat(since_raw).replace(tzinfo=timezone.utc)
                if since_raw
                else datetime.now(timezone.utc) - timedelta(days=7)
            )

            async def collect_recent(folder_id: str, collected: list, depth: int = 0):
                if len(collected) >= limit or depth > 6:
                    return
                contents = await get_all_folder_contents(
                    client, project_id, folder_id, hdrs, raise_on_error=False
                )
                tasks = []
                for item in contents:
                    a = item.get("attributes", {})
                    if item["type"] == "folders":
                        tasks.append(collect_recent(item["id"], collected, depth + 1))
                    else:
                        lm = a.get("lastModifiedTime")
                        if lm:
                            item_dt = datetime.fromisoformat(lm.replace("Z", "+00:00"))
                            if item_dt >= since_dt:
                                collected.append({
                                    "name": a.get("displayName"),
                                    "last_modified": lm,
                                    "modified_by": a.get("lastModifiedUserName"),
                                    "created_by": a.get("createUserName"),
                                })
                await asyncio.gather(*tasks)

            res = await client.get(
                f"{APS_BASE}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders",
                headers=hdrs,
            )
            res.raise_for_status()
            collected: list = []
            await asyncio.gather(*[collect_recent(f["id"], collected) for f in res.json().get("data", [])])
            collected.sort(key=lambda x: x["last_modified"], reverse=True)
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "since": since_dt.date().isoformat(),
                "activity_count": len(collected[:limit]),
                "activity": collected[:limit],
            }, indent=2))]

        if name == "find_files":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            query = arguments["query"].lower()
            start_folders = await _resolve_start_folders(
                client, token, hdrs, hub_id, project_id, arguments.get("folder_path")
            )
            results = await _walk_project_files(
                client, project_id, hdrs, start_folders, predicate=lambda n: query in n
            )
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name, "query": query,
                "result_count": len(results), "files": results,
            }, indent=2))]

        if name == "list_all_files":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            folder_path = arguments.get("folder_path")
            start_folders = await _resolve_start_folders(
                client, token, hdrs, hub_id, project_id, folder_path
            )
            results = await _walk_project_files(
                client, project_id, hdrs, start_folders
            )
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "scope": folder_path or "(entire project)",
                "result_count": len(results), "files": results,
            }, indent=2))]

        if name == "export_deliverables_manifest":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            folder_path = arguments.get("folder_path")
            # Normalise the optional extension filter to a lowercase, dot-prefixed set.
            raw_exts = arguments.get("extensions") or []
            exts = {("." + e.lstrip(".")).lower() for e in raw_exts if e and e.strip()}
            predicate = None
            if exts:
                predicate = lambda n: any(n.endswith(e) for e in exts)  # noqa: E731
            start_folders = await _resolve_start_folders(
                client, token, hdrs, hub_id, project_id, folder_path
            )
            results = await _walk_project_files(
                client, project_id, hdrs, start_folders, predicate=predicate
            )
            total = len(results)
            names = sorted({r["name"] for r in results}, key=str.lower)
            scope = folder_path or "(entire project)"
            header = (
                f"project: {resolved_name} | scope: {scope} | "
                f"{total} files ({len(names)} unique names)"
            )
            if exts:
                header += f" | extensions: {', '.join(sorted(exts))}"
            body = "\n".join(names) if names else "(no files found)"
            return [TextContent(type="text", text=f"{header}\n\n{body}")]

        if name == "find_folder":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            query = arguments["query"].lower()
            start_folders = await _resolve_start_folders(
                client, token, hdrs, hub_id, project_id, arguments.get("folder_path")
            )

            results: list[dict] = []

            async def search_folders(folder_id: str, path: str, depth: int = 0):
                if depth > 8:
                    return
                contents = await get_all_folder_contents(
                    client, project_id, folder_id, hdrs, raise_on_error=False
                )
                tasks = []
                for item in contents:
                    if item["type"] != "folders":
                        continue
                    folder_name = _folder_name(item["attributes"])
                    item_path = f"{path}/{folder_name}"
                    if query in folder_name.lower():
                        results.append({
                            "name": folder_name,
                            "path": item_path,
                            "id": item["id"],
                        })
                    tasks.append(search_folders(item["id"], item_path, depth + 1))
                await asyncio.gather(*tasks)

            await asyncio.gather(*[
                search_folders(f["id"], _folder_name(f["attributes"])) for f in start_folders
            ])
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name, "query": query,
                "result_count": len(results), "folders": results,
            }, indent=2))]

        if name == "list_project_roles":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            bare_id = _to_bare_id(project_id)
            role_map: dict[str, str] = {}
            members = []
            params: dict = {"limit": 200, "offset": 0}
            while True:
                r = await client.get(
                    f"{APS_BASE}/construction/admin/v1/projects/{bare_id}/users",
                    headers=hdrs,
                    params=params,
                )
                if not r.is_success:
                    break
                data = r.json()
                users = _extract_response_items(data)
                for u in users:
                    for role in u.get("roles", []):
                        rid, rname = role.get("id", ""), role.get("name", "")
                        if rid and rname:
                            role_map[rid] = rname
                    members.append({
                        "email": u.get("email", ""),
                        "name": u.get("name", ""),
                        "autodesk_id": u.get("autodeskId"),
                        "company": u.get("companyName", u.get("company_name", "")),
                        "roles": [r.get("name", "") for r in u.get("roles", []) if r.get("name")],
                    })
                if len(users) < params["limit"]:
                    break
                params["offset"] += params["limit"]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "roles": [{"role_id": rid, "name": rname} for rid, rname in role_map.items()],
                "members": members,
            }, indent=2))]

        if name == "list_project_companies":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            bare_project_id = _to_bare_id(project_id)
            account_id = _to_bare_id(hub_id)
            app_token = await get_app_token()
            companies = []
            offset = 0
            while True:
                r = await client.get(
                    f"{APS_BASE}/hq/v1/accounts/{account_id}/projects/{bare_project_id}/companies",
                    headers=auth_headers(app_token),
                    params={"limit": 100, "offset": offset},
                )
                r.raise_for_status()
                page = r.json()
                batch = _extract_response_items(page)
                for c in batch:
                    companies.append({"company_id": c["id"], "name": c["name"]})
                if len(batch) < 100:
                    break
                offset += 100
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "company_count": len(companies),
                "companies": companies,
            }, indent=2))]

        if name == "export_permission_matrix":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            folder_path = arguments["folder_path"]
            max_depth = int(arguments.get("max_depth", 2))
            bare_id = _to_bare_id(project_id)

            folder_id, _ = await _resolve_folder(client, token, hub_id, project_id, folder_path)
            folder_data = await _walk_folder_tree(
                client, project_id, bare_id, folder_id, folder_path, hdrs, max_depth
            )

            # Ordered from most to least permissive so the first match wins.
            _PERMISSION_LEVELS: list[tuple[str, frozenset]] = [
                ("Full controller",                          frozenset({"PUBLISH", "VIEW", "DOWNLOAD", "COLLABORATE", "PUBLISH_MARKUP", "EDIT", "CONTROL"})),
                ("View/Download+PublishMarkups+Upload+Edit", frozenset({"PUBLISH", "VIEW", "DOWNLOAD", "COLLABORATE", "PUBLISH_MARKUP", "EDIT"})),
                ("View/Download+PublishMarkups+Upload",      frozenset({"PUBLISH", "VIEW", "DOWNLOAD", "COLLABORATE", "PUBLISH_MARKUP"})),
                ("View/Download+PublishMarkups",             frozenset({"VIEW", "DOWNLOAD", "COLLABORATE", "PUBLISH_MARKUP"})),
                ("View/Download",                            frozenset({"VIEW", "DOWNLOAD", "COLLABORATE"})),
                ("View Only",                                frozenset({"VIEW", "COLLABORATE"})),
            ]

            def _actions_to_level(actions: list) -> str:
                s = frozenset(actions)
                if not s:
                    return ""
                for label, required in _PERMISSION_LEVELS:
                    if s == required:
                        return label
                return ",".join(sorted(s))

            # Collect ROLE subjects only (not users or companies) in encounter order.
            # Names come directly from the permissions response.
            subject_order: list[str] = []
            subject_meta: dict[str, dict] = {}
            for fd in folder_data:
                for p in fd["permissions"]:
                    sid = p.get("subjectId", "")
                    stype = p.get("subjectType", "")
                    if not sid or stype != "ROLE" or sid in subject_meta:
                        continue
                    name_val = p.get("name") or p.get("subjectName") or p.get("displayName") or f"[{stype} {sid[:8]}]"
                    subject_meta[sid] = {"id": sid, "type": stype, "name": name_val}
                    subject_order.append(sid)

            # Pivot matrix: rows = folders, columns = roles/companies.
            # Cell = effective permission level (union of actions + inheritActions).
            # Tag "(inherited)" when the folder has no explicit actions of its own.
            matrix = []
            for fd in folder_data:
                perm_lookup = {p["subjectId"]: p for p in fd["permissions"] if p.get("subjectId")}
                row: dict = {"folder_path": fd["folder_path"], "folder_id": fd["folder_id"]}
                for sid in subject_order:
                    sname = subject_meta[sid]["name"]
                    entry = perm_lookup.get(sid)
                    if entry:
                        explicit = entry.get("actions", [])
                        inherited = entry.get("inheritActions", [])
                        effective = list(set(explicit) | set(inherited))
                        level = _actions_to_level(effective)
                        row[sname] = level + (" (inherited)" if not explicit and inherited else "")
                    else:
                        row[sname] = ""
                matrix.append(row)

            # Flat rows for apply_permission_changes: one row per folder × role.
            flat_rows = []
            for fd in folder_data:
                for p in fd["permissions"]:
                    sid = p.get("subjectId", "")
                    stype = p.get("subjectType", "")
                    if not sid or stype != "ROLE":
                        continue
                    meta = subject_meta.get(sid, {"id": sid, "type": stype, "name": sid})
                    explicit = sorted(p.get("actions", []))
                    inherited = sorted(p.get("inheritActions", []))
                    effective = sorted(set(explicit) | set(inherited))
                    flat_rows.append({
                        "folder_path": fd["folder_path"],
                        "folder_id": fd["folder_id"],
                        "subject_name": meta["name"],
                        "subject_id": sid,
                        "subject_type": stype,
                        "permission_level": _actions_to_level(effective),
                        "actions": explicit,
                        "inherit_actions": inherited,
                    })

            matrix_columns = ["folder_path", "folder_id"] + [subject_meta[s]["name"] for s in subject_order]
            flat_columns = [
                "folder_path", "folder_id", "subject_name", "subject_id",
                "subject_type", "permission_level", "actions", "inherit_actions",
            ]

            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "folder_root": folder_path,
                "subjects": [subject_meta[s] for s in subject_order],
                "matrix_columns": matrix_columns,
                "matrix": matrix,
                "flat_columns": flat_columns,
                "flat_rows": flat_rows,
            }, indent=2))]

        if name == "create_role_data_export":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            account_id = _to_bare_id(hub_id)
            bare_project_id = _to_bare_id(project_id)
            payload = {
                "description": f"Role export for {resolved_name}",
                "scheduleInterval": "ONE_TIME",
                "effectiveFrom": datetime.now(timezone.utc).isoformat(),
                "serviceGroups": ["admin"],
                "projectIdList": [bare_project_id],
                "sendEmail": arguments.get("send_email", True),
            }
            r = await client.post(
                f"{APS_BASE}/data-connector/v1/accounts/{account_id}/requests",
                headers=hdrs,
                json=payload,
            )
            if not r.is_success:
                body = _error_body(r)
                return [TextContent(type="text", text=json.dumps({"error": r.status_code, "body": body}, indent=2))]
            data = r.json()
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "request_id": data.get("id"),
                "status": "created",
                "note": (
                    "Job is running. Call get_data_connector_requests with this request_id — "
                    "it will poll internally until complete and download roles automatically."
                ),
                "response": data,
            }, indent=2))]

        if name == "get_data_connector_requests":
            hub_id, _ = await resolve_hub(client, token, _norm_region(arguments), hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)
            request_id = arguments.get("request_id")

            async def _poll_for_job(req_id: str) -> dict:
                poll_interval = 15
                poll_timeout = 600
                elapsed = 0
                while elapsed < poll_timeout:
                    r = await client.get(
                        f"{APS_BASE}/data-connector/v1/accounts/{account_id}/requests/{req_id}/jobs",
                        headers=hdrs,
                        params={"sort": "desc", "limit": 1},
                    )
                    r.raise_for_status()
                    jobs = r.json().get("results", [])
                    if jobs and (jobs[0].get("status") or "").lower() == "complete":
                        return jobs[0]
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                raise TimeoutError(f"Job for request {req_id} did not complete within {poll_timeout}s.")

            async def _get_signed_url(job_id: str) -> str:
                r = await client.get(
                    f"{APS_BASE}/data-connector/v1/accounts/{account_id}/jobs/{job_id}/data/autodesk_data_extract.zip",
                    headers=hdrs,
                )
                r.raise_for_status()
                return r.json()["signedUrl"]

            async def _download_roles(signed_url: str) -> tuple[dict[str, str], dict[str, str]]:
                """Return ``(roles, roles_oxygen)`` from the admin_project_roles CSV:
                ``roles`` = {role_id(UUID): role_name} (feeds the role-UUID cache and
                export_permission_matrix); ``roles_oxygen`` = {role_name: role_oxygen_id},
                the Reviews-API numeric autodeskId for the reviewer-workflow tools."""
                dl = await client.get(signed_url, timeout=120)
                dl.raise_for_status()
                roles: dict[str, str] = {}
                roles_oxygen: dict[str, str] = {}
                zf = zipfile.ZipFile(io.BytesIO(dl.content))
                for zfname in zf.namelist():
                    if "role" in zfname.lower() and zfname.endswith(".csv"):
                        csv_text = zf.read(zfname).decode("utf-8-sig")
                        for row in csv.DictReader(io.StringIO(csv_text)):
                            rid = row.get("role_id") or row.get("roleId") or row.get("id", "")
                            rname = row.get("name") or row.get("role_name") or row.get("roleName", "")
                            oxy = row.get("role_oxygen_id") or row.get("roleOxygenId") or ""
                            if rid and rname:
                                roles[rid] = rname
                            if rname and oxy:
                                roles_oxygen[rname] = str(oxy)
                return roles, roles_oxygen

            if request_id:
                job = await _poll_for_job(request_id)
                job_id = job.get("id")
                result: dict = {
                    "request_id": request_id,
                    "job_id": job_id,
                    "status": job.get("status"),
                    "completion_status": job.get("completionStatus"),
                    "completed_at": job.get("completedAt"),
                }
                if (job.get("completionStatus") or "").lower() == "success" and job_id:
                    signed_url = await _get_signed_url(job_id)
                    roles, roles_oxygen = await _download_roles(signed_url)
                    result["roles"] = roles
                    result["role_count"] = len(roles)
                    # Reviews-API numeric IDs (role_oxygen_id) for the approval-workflow
                    # tools — merge into role_id_cache.json's 'roles_name_to_oxygen_id'.
                    result["roles_name_to_oxygen_id"] = roles_oxygen
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            # No request_id: list requests only (no polling)
            r = await client.get(
                f"{APS_BASE}/data-connector/v1/accounts/{account_id}/requests",
                headers=hdrs,
                params={"sort": "desc", "limit": 20},
            )
            r.raise_for_status()
            raw = r.json()
            requests_list = raw if isinstance(raw, list) else raw.get("results", [])
            return [TextContent(type="text", text=json.dumps([
                {
                    "id": req.get("id"),
                    "description": req.get("description"),
                    "created_at": req.get("createdAt"),
                    "schedule": req.get("scheduleInterval"),
                    "service_groups": req.get("serviceGroups"),
                    "project_ids": req.get("projectIdList"),
                    "last_queued_at": req.get("lastQueuedAt"),
                }
                for req in requests_list
            ], indent=2))]

        if name == "apply_permission_changes":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            changes = arguments["changes"]

            applied = []
            errors = []

            for change in changes:
                fid = change["folder_id"].strip()
                sid = change["subject_id"]
                stype = change.get("subject_type", "COMPANY")
                actions = change["actions"]

                bare_pid = _to_bare_id(project_id)
                if not actions:
                    url = f"{APS_BASE}/bim360/docs/v1/projects/{bare_pid}/folders/{fid}/permissions:batch-delete"
                    payload = [{"subjectId": sid, "subjectType": stype}]
                else:
                    url = f"{APS_BASE}/bim360/docs/v1/projects/{bare_pid}/folders/{fid}/permissions:batch-create"
                    payload = [{"subjectId": sid, "subjectType": stype, "actions": actions}]

                r = await client.post(url, headers=hdrs, json=payload)
                if r.is_success:
                    applied.append({
                        "folder_id": fid, "subject_id": sid,
                        "subject_type": stype, "actions": actions,
                    })
                else:
                    body = _error_body(r)
                    errors.append({
                        "folder_id": fid, "subject_id": sid,
                        "error": str(r.status_code), "response": body,
                    })

            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "applied_count": len(applied),
                "error_count": len(errors),
                "applied": applied,
                "errors": errors,
            }, indent=2))]

        # ------------------------------------------------------------------
        # Shared logic for bulk add / clone / company-assign
        # ------------------------------------------------------------------
        async def _execute_bulk_assign(
            resolved_projects: list[dict],
            user_emails: list[str],
            default_role: "str | list[str]",
            role_overrides: "dict[str, str | list[str]]",
            dry_run: bool,
        ) -> tuple[list[dict], list[str], bool]:
            """
            Core assign logic reused by bulk_assign_users, clone_user_access,
            and bulk_assign_company_users.
            Returns (results, warnings, api_calls_made).

            A live run is a SINGLE `users:import` per project carrying the resolved
            `roleIds` — membership and roles are applied together in one atomic call
            (verified live: the import honours a valid role UUID, incl. a role no
            member holds yet). Role names resolve via the member walk → the hub-wide
            role cache (lists empty roles) → raw pass-through. The import is
            asynchronous (202 + jobId), so we poll the members list to confirm each
            user landed with their roles; a user unconfirmed within the poll budget is
            reported `submitted` (queued), not failed.
            """
            warnings: list[str] = []
            BATCH = 200
            sem = asyncio.Semaphore(5)
            api_calls_made_flag = [False]

            async def _process_project(proj: dict) -> list[dict]:
                async with sem:
                    pid = proj["id"]
                    pname = proj["name"]
                    role_spec = role_overrides.get(pname.lower(), default_role)
                    role_display = _role_display(role_spec) or "(none)"
                    proj_results: list[dict] = []

                    role_ids: list[str] = []
                    if _as_role_list(role_spec):
                        role_map = await _fetch_project_roles(client, pid, hdrs)
                        # Resolve each friendly role name to its ID: first from the roles a
                        # member already holds, then from the hub-wide role cache (which
                        # DOES list empty/newly-created roles the member walk can't see), and
                        # finally pass an unresolved value through as a raw ID for the ACC
                        # API to validate. roleIds is an array — several roles per user.
                        name_cache = _load_role_name_to_id()
                        role_ids = _resolve_role_ids(role_spec, role_map, name_cache)
                        unknown = _unresolved_role_names(role_spec, role_map, name_cache)
                        if unknown:
                            warnings.append(
                                f"Role name(s) {unknown} not found among {pname}'s members or "
                                f"the role cache — sent to ACC as-is (will be rejected if not a "
                                f"valid role ID). Refresh role_id_cache.json via a Data "
                                f"Connector export if this is a new role."
                            )

                    if dry_run:
                        members = await _get_project_members_map(client, pid, hdrs)
                        for email in user_emails:
                            if email in members:
                                proj_results.append({
                                    "user": email, "project": pname, "role": role_display,
                                    "status": "already_member",
                                    "message": "Already a member — no change",
                                })
                            else:
                                proj_results.append({
                                    "user": email, "project": pname, "role": role_display,
                                    "status": "would_add",
                                    "message": (
                                        f"Would add with role(s) '{role_display}'"
                                        if role_ids else "Would add (no role)"
                                    ),
                                })
                        return proj_results

                    bare_pid = _to_bare_id(pid)

                    # --- Single import per batch, carrying roleIds -------------------
                    # One `users:import` applies membership AND roles together. The call
                    # is async (202 + jobId); we confirm by polling the members list below.
                    # submitted = emails the POST accepted; post_errors = per-email failures.
                    submitted: list[str] = []
                    post_errors: dict[str, str] = {}
                    for i in range(0, len(user_emails), BATCH):
                        batch = user_emails[i : i + BATCH]
                        users_payload = []
                        for email in batch:
                            entry: dict = {"email": email, "products": DEFAULT_PRODUCTS}
                            if role_ids:
                                entry["roleIds"] = role_ids
                            users_payload.append(entry)
                        api_calls_made_flag[0] = True
                        r = await client.post(
                            f"{APS_BASE}/construction/admin/v2/projects/{bare_pid}/users:import",
                            headers=hdrs,
                            json={"users": users_payload, "suppressAdministrativeEmails": False},
                        )
                        if r.is_success:
                            submitted.extend(batch)
                        else:
                            body = _error_body(r)
                            for email in batch:
                                post_errors[email] = f"HTTP {r.status_code}: {body}"

                    # --- Confirm the async job by polling the members list ----------
                    # A user is confirmed when they're a member holding every requested
                    # role. Best-effort within the poll budget; unconfirmed → `submitted`.
                    confirmed: set[str] = set()
                    members: dict[str, dict] = {}
                    role_id_set = set(role_ids)
                    if submitted:
                        for attempt in range(_ASSIGN_POLL_ATTEMPTS):
                            members = await _get_project_members_map(client, pid, hdrs)
                            confirmed = {
                                e for e in submitted
                                if e in members and role_id_set.issubset(
                                    {rr.get("id") for rr in members[e].get("roles", [])}
                                )
                            }
                            if len(confirmed) == len(submitted):
                                break
                            if attempt < _ASSIGN_POLL_ATTEMPTS - 1:
                                await asyncio.sleep(_ASSIGN_POLL_DELAY)

                    # --- One row per requested user ---------------------------------
                    for email in user_emails:
                        if email in post_errors:
                            proj_results.append({
                                "user": email, "project": pname, "role": role_display,
                                "status": "error", "message": post_errors[email],
                            })
                        elif email in confirmed:
                            proj_results.append({
                                "user": email, "project": pname, "role": role_display,
                                "status": "success", "message": "",
                            })
                        else:
                            proj_results.append({
                                "user": email, "project": pname, "role": role_display,
                                "status": "submitted",
                                "message": "Queued (async import) — not confirmed within the "
                                           "poll window; re-check membership shortly",
                            })
                    return proj_results

            gathered = await asyncio.gather(*[_process_project(p) for p in resolved_projects])
            results: list[dict] = []
            for proj_results in gathered:
                results.extend(proj_results)
            return results, warnings, api_calls_made_flag[0]

        # ------------------------------------------------------------------

        if name == "bulk_assign_users":
            region = _norm_region(arguments)
            dry_run = arguments.get("dry_run", True)
            project_names = arguments["project_names"]
            user_emails = _norm_emails(arguments["user_emails"])
            default_role = arguments.get("default_role") or ""
            role_overrides = {k.lower(): v for k, v in (arguments.get("role_overrides") or {}).items()}

            resolved_projects: list[dict] = []
            project_errors: list[dict] = []
            for pname in project_names:
                try:
                    ref = await _resolve_project_ref(
                        client, token, pname, region=region, hub_name=arguments.get("hub_name")
                    )
                    resolved_projects.append({"id": ref["project_id"], "name": ref["project_name"]})
                except ValueError as e:
                    project_errors.append({"project": pname, "error": str(e)})

            warnings: list[str] = []
            invalid_email_set: set[str] = set()
            if resolved_projects:
                app_token = await get_app_token()
                # Roster check across EVERY hub account (the roster is per-hub-account, so
                # a project in a non-default hub must not wrongly skip valid users).
                account_users = await _get_all_accounts_users_map(
                    client, token, app_token, hub_name=arguments.get("hub_name")
                )
                for email in user_emails:
                    if email not in account_users:
                        invalid_email_set.add(email)
                        warnings.append(f"'{email}' not found in account roster — skipping")

            valid_emails = [e for e in user_emails if e not in invalid_email_set]
            results, extra_warnings, api_calls_made = await _execute_bulk_assign(
                resolved_projects, valid_emails, default_role, role_overrides, dry_run
            )
            warnings.extend(extra_warnings)

            for email in invalid_email_set:
                for proj in resolved_projects:
                    role_display = _role_display(role_overrides.get(proj["name"].lower(), default_role))
                    results.append({
                        "user": email, "project": proj["name"], "role": role_display,
                        "status": "error", "message": "Not found in account roster",
                    })

            for pentry in project_errors:
                role_display = _role_display(role_overrides.get(pentry["project"].lower(), default_role))
                for email in user_emails:
                    results.append({
                        "user": email, "project": pentry["project"], "role": role_display,
                        "status": "error", "message": f"Project not found: {pentry['project']}",
                    })

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "bulk_assign")

            summary = {
                k: sum(1 for r in results if r["status"] == k)
                for k in ("success", "submitted", "would_add", "already_member", "error")
            }
            summary["total"] = len(results)
            return [TextContent(type="text", text=json.dumps({
                "dry_run": dry_run, "operation": "bulk_assign_users",
                "summary": summary, "results": results,
                "warnings": warnings, "audit_file": audit_file,
            }, indent=2))]

        if name == "update_user_roles":
            region = _norm_region(arguments)
            dry_run = arguments.get("dry_run", True)
            project_names = arguments["project_names"]
            user_emails = _norm_emails(arguments["user_emails"])
            default_role = arguments["default_role"]
            role_overrides = {k.lower(): v for k, v in (arguments.get("role_overrides") or {}).items()}

            resolved_projects: list[dict] = []
            project_errors: list[dict] = []
            for pname in project_names:
                try:
                    ref = await _resolve_project_ref(
                        client, token, pname, region=region, hub_name=arguments.get("hub_name")
                    )
                    resolved_projects.append({"id": ref["project_id"], "name": ref["project_name"]})
                except ValueError as e:
                    project_errors.append({"project": pname, "error": str(e)})

            invalid_email_set: set[str] = set()
            if resolved_projects:
                app_token = await get_app_token()
                # Roster check across EVERY hub account (see bulk_assign_users).
                account_users = await _get_all_accounts_users_map(
                    client, token, app_token, hub_name=arguments.get("hub_name")
                )
                for email in user_emails:
                    if email not in account_users:
                        invalid_email_set.add(email)

            valid_emails = [e for e in user_emails if e not in invalid_email_set]

            results: list[dict] = []
            warnings: list[str] = []
            for email in invalid_email_set:
                for proj in resolved_projects:
                    role_display = _role_display(role_overrides.get(proj["name"].lower(), default_role))
                    results.append({
                        "user": email, "project": proj["name"], "role": role_display,
                        "status": "error", "message": "Not found in account roster",
                    })

            for pentry in project_errors:
                role_display = _role_display(role_overrides.get(pentry["project"].lower(), default_role))
                for email in user_emails:
                    results.append({
                        "user": email, "project": pentry["project"], "role": role_display,
                        "status": "error", "message": f"Project not found: {pentry['project']}",
                    })

            api_calls_made = False
            for proj in resolved_projects:
                pid = proj["id"]
                pname = proj["name"]
                role_spec = role_overrides.get(pname.lower(), default_role)
                role_display = _role_display(role_spec)

                role_map = await _fetch_project_roles(client, pid, hdrs)
                # Resolve names via the member walk, then the hub-wide role cache (which
                # lists empty/unused roles the walk can't see), else pass through as a raw
                # ID for the ACC API to validate. roleIds is an array (multiple roles).
                name_cache = _load_role_name_to_id()
                role_ids = _resolve_role_ids(role_spec, role_map, name_cache)
                unknown = _unresolved_role_names(role_spec, role_map, name_cache)
                if unknown:
                    warnings.append(
                        f"Role name(s) {unknown} not found among {pname}'s members or the "
                        f"role cache — sent to ACC as-is (will be rejected if not a valid "
                        f"role ID). Refresh role_id_cache.json via a Data Connector export "
                        f"if this is a new role."
                    )

                members = await _get_project_members_map(client, pid, hdrs)
                bare_pid = _to_bare_id(pid)

                for email in valid_emails:
                    member = members.get(email)
                    if not member:
                        results.append({
                            "user": email, "project": pname, "role": role_display,
                            "status": "skipped", "message": "Not a member of this project",
                        })
                        continue

                    user_id = member.get("id") or member.get("userId") or member.get("autodeskId")
                    if not user_id:
                        results.append({
                            "user": email, "project": pname, "role": role_display,
                            "status": "error", "message": "Could not determine user ID from member record",
                        })
                        continue

                    if dry_run:
                        current_role_id = member.get("roleId") or member.get("role") or ""
                        current_role_name = role_map.get(current_role_id) or current_role_id or "(unknown)"
                        results.append({
                            "user": email, "project": pname, "role": role_display,
                            "status": "would_update",
                            "message": f"Would change role from '{current_role_name}' to '{role_display}'",
                        })
                        continue

                    api_calls_made = True
                    r = await client.patch(
                        f"{APS_BASE}/construction/admin/v1/projects/{bare_pid}/users/{user_id}",
                        headers=hdrs,
                        json={"roleIds": role_ids, "products": DEFAULT_PRODUCTS},
                    )
                    if r.is_success:
                        results.append({
                            "user": email, "project": pname, "role": role_display,
                            "status": "success", "message": "",
                        })
                    else:
                        body = _error_body(r)
                        results.append({
                            "user": email, "project": pname, "role": role_display,
                            "status": "error", "message": f"HTTP {r.status_code}: {body}",
                        })

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "update_roles")

            summary = {
                k: sum(1 for r in results if r["status"] == k)
                for k in ("success", "would_update", "skipped", "error")
            }
            summary["total"] = len(results)
            return [TextContent(type="text", text=json.dumps({
                "dry_run": dry_run, "operation": "update_user_roles",
                "summary": summary, "results": results,
                "warnings": warnings, "audit_file": audit_file,
            }, indent=2))]

        if name == "remove_users_from_projects":
            region = _norm_region(arguments)
            dry_run = arguments.get("dry_run", True)
            user_emails = _norm_emails(arguments["user_emails"])
            project_names = arguments.get("project_names") or []

            hub_id, _ = await resolve_hub(client, token, region, hub_name=arguments.get("hub_name"))

            resolved_projects_list: list[dict] = []
            project_errors_list: list[dict] = []
            if project_names:
                for pname in project_names:
                    try:
                        ref = await _resolve_project_ref(
                            client, token, pname, region=region, hub_name=arguments.get("hub_name")
                        )
                        resolved_projects_list.append({"id": ref["project_id"], "name": ref["project_name"]})
                    except ValueError as e:
                        project_errors_list.append({"project": pname, "error": str(e)})
            else:
                # All projects in hub
                all_projs = await get_all_pages(
                    client, f"{APS_BASE}/project/v1/hubs/{hub_id}/projects", hdrs
                )
                resolved_projects_list = [
                    {"id": p["id"], "name": p["attributes"]["name"]} for p in all_projs
                ]

            results: list[dict] = []
            for pentry in project_errors_list:
                for email in user_emails:
                    results.append({
                        "user": email, "project": pentry["project"],
                        "status": "error", "message": f"Project not found: {pentry['project']}",
                    })
            for proj in resolved_projects_list:
                pid = proj["id"]
                pname = proj["name"]
                bare_pid = _to_bare_id(pid)
                members = await _get_project_members_map(client, pid, hdrs)

                for email in user_emails:
                    member = members.get(email)
                    if not member:
                        if project_names:
                            results.append({
                                "user": email, "project": pname,
                                "status": "skipped", "message": "Not a member of this project",
                            })
                        continue

                    user_id = member.get("id") or member.get("userId") or member.get("autodeskId")
                    if not user_id:
                        results.append({
                            "user": email, "project": pname,
                            "status": "error", "message": "Could not determine user ID",
                        })
                        continue

                    if dry_run:
                        results.append({
                            "user": email, "project": pname,
                            "status": "would_remove", "message": "Would remove from project",
                        })
                        continue

                    r = await client.delete(
                        f"{APS_BASE}/construction/admin/v1/projects/{bare_pid}/users/{user_id}",
                        headers=hdrs,
                    )
                    if r.is_success or r.status_code == 204:
                        results.append({
                            "user": email, "project": pname,
                            "status": "success", "message": "Removed",
                        })
                    else:
                        body = _error_body(r)
                        results.append({
                            "user": email, "project": pname,
                            "status": "error", "message": f"HTTP {r.status_code}: {body}",
                        })

            audit_file = None
            if not dry_run and results:
                audit_file = _write_audit_csv(results, "remove_users")

            summary = {
                k: sum(1 for r in results if r["status"] == k)
                for k in ("success", "would_remove", "skipped", "error")
            }
            summary["total"] = len(results)
            return [TextContent(type="text", text=json.dumps({
                "dry_run": dry_run, "operation": "remove_users_from_projects",
                "summary": summary, "results": results,
                "audit_file": audit_file,
            }, indent=2))]

        if name == "clone_user_access":
            dry_run = arguments.get("dry_run", True)
            ref_email = arguments["reference_user_email"].lower().strip()
            target_emails = _norm_emails(arguments["target_user_emails"])

            # Scan projects across EVERY hub (or the hub_name-filtered subset) — the
            # reference user may hold access in a non-default hub, so a single-hub scan
            # would miss it (the wrong-hub fix).
            hubs = await _get_all_hubs_filtered(client, token, arguments.get("hub_name"))
            project_lists = await _gather_bounded(
                8,
                [(lambda h=h: get_all_pages(
                    client, f"{APS_BASE}/project/v1/hubs/{h['id']}/projects", hdrs
                )) for h in hubs],
            )
            all_projs = [p for plist in project_lists for p in plist]

            # Find all projects where the reference user is a member
            # Use a semaphore to limit concurrency across potentially 200+ projects
            sem = asyncio.Semaphore(10)
            ref_project_roles: list[dict] = []

            async def _check_project(proj_raw: dict):
                pid = proj_raw["id"]
                pname = proj_raw["attributes"]["name"]
                async with sem:
                    members = await _get_project_members_map(client, pid, hdrs)
                member = members.get(ref_email)
                if member:
                    role_names = [r["name"] for r in member.get("roles", []) if r.get("name")]
                    ref_project_roles.append({"id": pid, "name": pname, "role_names": role_names})

            await asyncio.gather(*[_check_project(p) for p in all_projs])

            if not ref_project_roles:
                return [TextContent(type="text", text=json.dumps({
                    "error": f"Reference user '{ref_email}' not found in any project.",
                }, indent=2))]

            # Build role_overrides using project names — role names come directly from the API.
            # Clone ALL of the reference user's roles per project (roleIds is an array), not
            # just the first.
            role_overrides: dict[str, list[str]] = {}
            for entry in ref_project_roles:
                role_names = entry["role_names"]
                role_overrides[entry["name"].lower()] = role_names or ["Viewer"]

            results, warnings, api_calls_made = await _execute_bulk_assign(
                ref_project_roles, target_emails, "", role_overrides, dry_run
            )
            warnings.insert(0, f"Reference user '{ref_email}' found in {len(ref_project_roles)} projects.")

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "clone_access")

            summary = {
                k: sum(1 for r in results if r["status"] == k)
                for k in ("success", "submitted", "would_add", "already_member", "error")
            }
            summary["total"] = len(results)
            return [TextContent(type="text", text=json.dumps({
                "dry_run": dry_run, "operation": "clone_user_access",
                "reference_user": ref_email,
                "projects_cloned": len(ref_project_roles),
                "summary": summary, "results": results,
                "warnings": warnings, "audit_file": audit_file,
            }, indent=2))]

        if name == "bulk_assign_company_users":
            region = _norm_region(arguments)
            dry_run = arguments.get("dry_run", True)
            company_name = arguments["company_name"].lower()
            project_names = arguments["project_names"]
            default_role = arguments.get("default_role") or ""
            role_overrides = {k.lower(): v for k, v in (arguments.get("role_overrides") or {}).items()}
            user_filter = _norm_emails(arguments.get("user_filter") or [])

            app_token = await get_app_token()

            # Resolve the company's users across EVERY hub account (the roster is per-hub-
            # account, so a company whose members live in a non-default hub is still found
            # without a hub_name hint — the wrong-hub fix).
            account_users = await _get_all_accounts_users_map(
                client, token, app_token, hub_name=arguments.get("hub_name")
            )
            company_emails = [
                email for email, u in account_users.items()
                if company_name in (u.get("company_name") or u.get("companyName") or "").lower()
            ]

            if not company_emails:
                return [TextContent(type="text", text=json.dumps({
                    "error": f"No users found for company matching '{arguments['company_name']}'. "
                             f"Check company_name or use list_account_users to see available companies.",
                }, indent=2))]

            if user_filter:
                company_emails = [e for e in company_emails if e in set(user_filter)]

            resolved_projects: list[dict] = []
            project_errors: list[dict] = []
            for pname in project_names:
                try:
                    ref = await _resolve_project_ref(
                        client, token, pname, region=region, hub_name=arguments.get("hub_name")
                    )
                    resolved_projects.append({"id": ref["project_id"], "name": ref["project_name"]})
                except ValueError as e:
                    project_errors.append({"project": pname, "error": str(e)})

            results, warnings, api_calls_made = await _execute_bulk_assign(
                resolved_projects, company_emails, default_role, role_overrides, dry_run
            )
            warnings.insert(0, f"Found {len(company_emails)} users for company '{arguments['company_name']}'.")
            for pentry in project_errors:
                role_display = _role_display(role_overrides.get(pentry["project"].lower(), default_role))
                for email in company_emails:
                    results.append({
                        "user": email, "project": pentry["project"], "role": role_display,
                        "status": "error", "message": f"Project not found: {pentry['project']}",
                    })

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "company_assign")

            summary = {
                k: sum(1 for r in results if r["status"] == k)
                for k in ("success", "submitted", "would_add", "already_member", "error")
            }
            summary["total"] = len(results)
            return [TextContent(type="text", text=json.dumps({
                "dry_run": dry_run, "operation": "bulk_assign_company_users",
                "company": arguments["company_name"],
                "users_found": len(company_emails),
                "summary": summary, "results": results,
                "warnings": warnings, "audit_file": audit_file,
            }, indent=2))]

        if name == "list_account_companies":
            hub_id, _ = await resolve_hub(client, token, _norm_region(arguments), hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)
            app_token = await get_app_token()
            companies = await _get_account_companies(client, account_id, app_token)
            listing = [
                {
                    "company_id": c.get("id"),
                    "name": c.get("name"),
                    "trade": c.get("trade"),
                    "status": c.get("status"),
                }
                for c in companies
            ]
            return [TextContent(type="text", text=json.dumps({
                "count": len(listing), "companies": listing,
            }, indent=2))]

        if name == "bulk_add_hub_users":
            dry_run = arguments.get("dry_run", True)
            user_emails = _norm_emails(arguments.get("user_emails") or [])
            company_name = arguments["company_name"]
            default_role = arguments.get("default_role") or ""
            names = {k.lower().strip(): v for k, v in (arguments.get("names") or {}).items()}
            detail = arguments.get("response_detail")
            max_conc = arguments.get("max_concurrency", 8)

            hub_id, _ = await resolve_hub(client, token, _norm_region(arguments), hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)
            app_token = await get_app_token()

            # Company is required on every hub user — resolve name → id up front.
            companies = await _get_account_companies(client, account_id, app_token)
            company_id = _resolve_company_id(companies, company_name)
            if company_id is None:
                return [TextContent(type="text", text=json.dumps({
                    "error": f"Company '{company_name}' not found in the hub directory. "
                             f"Create it first with bulk_add_hub_companies, or check "
                             f"list_account_companies for the exact name.",
                }, indent=2))]

            existing = await _get_account_users_map(client, account_id, app_token)

            results: list[dict] = []
            to_add: list[dict] = []
            for email in user_emails:
                if email in existing:
                    results.append({"email": email, "company": company_name, "role": default_role, "status": "already_exists"})
                    continue
                if dry_run:
                    results.append({"email": email, "company": company_name, "role": default_role, "status": "would_add"})
                    continue
                obj = {"email": email, "company_id": company_id}
                if default_role:
                    obj["default_role"] = default_role
                nm = names.get(email) or {}
                if nm.get("first_name"):
                    obj["first_name"] = nm["first_name"]
                if nm.get("last_name"):
                    obj["last_name"] = nm["last_name"]
                to_add.append(obj)

            api_calls_made = False
            if not dry_run and to_add:
                BATCH = 50  # HQ users/import accepts max 50 per call
                batches = [to_add[i:i + BATCH] for i in range(0, len(to_add), BATCH)]

                async def _import_users_batch(batch):
                    r = await client.post(
                        f"{APS_BASE}/hq/v1/accounts/{account_id}/users/import",
                        headers={**auth_headers(app_token), "Content-Type": "application/json"},
                        json=batch,
                    )
                    return batch, r

                api_calls_made = True
                responses = await _gather_bounded(
                    max_conc, [(lambda b=b: _import_users_batch(b)) for b in batches]
                )
                for batch, r in responses:
                    if not r.is_success:
                        for obj in batch:
                            results.append({"email": obj["email"], "company": company_name, "role": default_role,
                                            "status": "error", "message": f"HTTP {r.status_code}: {_error_body(r)}"})
                        continue
                    body = r.json() if r.content else {}
                    failures = {_import_item_key(it, "email"): it for it in (body.get("failure_items") or [])}
                    for obj in batch:
                        em = obj["email"]
                        if em in failures:
                            results.append({"email": em, "company": company_name, "role": default_role,
                                            "status": "error", "message": _import_item_error(failures[em])})
                        else:
                            results.append({"email": em, "company": company_name, "role": default_role, "status": "added"})

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "add_hub_users")

            summary = {}
            for r in results:
                summary[r["status"]] = summary.get(r["status"], 0) + 1
            summary["total"] = len(results)

            payload = {
                "dry_run": dry_run, "operation": "bulk_add_hub_users",
                "company": company_name, "summary": summary, "audit_file": audit_file,
            }
            _shape_bulk_response(
                payload, results, detail,
                noteworthy=lambda r: r["status"] in ("added", "would_add", "error"),
                failure=lambda r: r["status"] == "error",
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "bulk_add_hub_companies":
            dry_run = arguments.get("dry_run", True)
            companies_in = arguments.get("companies") or []
            detail = arguments.get("response_detail")
            max_conc = arguments.get("max_concurrency", 8)

            hub_id, _ = await resolve_hub(client, token, _norm_region(arguments), hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)
            app_token = await get_app_token()

            existing = await _get_account_companies(client, account_id, app_token)
            existing_names = {(c.get("name") or "").lower().strip() for c in existing}

            results = []
            to_add = []
            for comp in companies_in:
                nm = (comp.get("name") or "").strip()
                trade = (comp.get("trade") or "").strip()
                if not nm or not trade:
                    results.append({"name": nm, "trade": trade, "status": "error",
                                    "message": "both name and trade are required"})
                    continue
                if nm.lower() in existing_names:
                    results.append({"name": nm, "trade": trade, "status": "already_exists"})
                    continue
                if dry_run:
                    results.append({"name": nm, "trade": trade, "status": "would_add"})
                    continue
                to_add.append(comp)

            api_calls_made = False
            if not dry_run and to_add:
                BATCH = 50  # HQ companies/import accepts max 50 per call
                batches = [to_add[i:i + BATCH] for i in range(0, len(to_add), BATCH)]

                async def _import_companies_batch(batch):
                    r = await client.post(
                        f"{APS_BASE}/hq/v1/accounts/{account_id}/companies/import",
                        headers={**auth_headers(app_token), "Content-Type": "application/json"},
                        json=batch,
                    )
                    return batch, r

                api_calls_made = True
                responses = await _gather_bounded(
                    max_conc, [(lambda b=b: _import_companies_batch(b)) for b in batches]
                )
                for batch, r in responses:
                    if not r.is_success:
                        for comp in batch:
                            results.append({"name": comp.get("name"), "trade": comp.get("trade"),
                                            "status": "error", "message": f"HTTP {r.status_code}: {_error_body(r)}"})
                        continue
                    body = r.json() if r.content else {}
                    failures = {_import_item_key(it, "name"): it for it in (body.get("failure_items") or [])}
                    for comp in batch:
                        key = (comp.get("name") or "").lower().strip()
                        if key in failures:
                            results.append({"name": comp.get("name"), "trade": comp.get("trade"),
                                            "status": "error", "message": _import_item_error(failures[key])})
                        else:
                            results.append({"name": comp.get("name"), "trade": comp.get("trade"), "status": "added"})

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "add_hub_companies")

            summary = {}
            for r in results:
                summary[r["status"]] = summary.get(r["status"], 0) + 1
            summary["total"] = len(results)

            payload = {
                "dry_run": dry_run, "operation": "bulk_add_hub_companies",
                "summary": summary, "audit_file": audit_file,
            }
            _shape_bulk_response(
                payload, results, detail,
                noteworthy=lambda r: r["status"] in ("added", "would_add", "error"),
                failure=lambda r: r["status"] == "error",
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "deactivate_hub_users":
            dry_run = arguments.get("dry_run", True)
            user_emails = _norm_emails(arguments.get("user_emails") or [])
            detail = arguments.get("response_detail")
            max_conc = arguments.get("max_concurrency", 8)

            hub_id, _ = await resolve_hub(client, token, _norm_region(arguments), hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)
            app_token = await get_app_token()

            existing = await _get_account_users_map(client, account_id, app_token)
            results = []
            targets = []
            for email in user_emails:
                u = existing.get(email)
                if not u:
                    results.append({"email": email, "status": "not_found"})
                    continue
                if dry_run:
                    results.append({"email": email, "status": "would_deactivate"})
                    continue
                targets.append((email, u.get("id")))

            api_calls_made = False
            if not dry_run and targets:
                async def _deactivate_user(email, uid):
                    r = await client.patch(
                        f"{APS_BASE}/hq/v1/accounts/{account_id}/users/{uid}",
                        headers={**auth_headers(app_token), "Content-Type": "application/json"},
                        json={"status": "inactive"},
                    )
                    return email, r

                api_calls_made = True
                responses = await _gather_bounded(
                    max_conc, [(lambda e=e, i=i: _deactivate_user(e, i)) for e, i in targets]
                )
                for email, r in responses:
                    if r.is_success:
                        results.append({"email": email, "status": "deactivated"})
                    else:
                        results.append({"email": email, "status": "error",
                                        "message": f"HTTP {r.status_code}: {_error_body(r)}"})

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "deactivate_hub_users")

            summary = {}
            for r in results:
                summary[r["status"]] = summary.get(r["status"], 0) + 1
            summary["total"] = len(results)

            payload = {
                "dry_run": dry_run, "operation": "deactivate_hub_users",
                "summary": summary, "audit_file": audit_file,
            }
            _shape_bulk_response(
                payload, results, detail,
                noteworthy=lambda r: r["status"] in ("deactivated", "would_deactivate", "error", "not_found"),
                failure=lambda r: r["status"] in ("error", "not_found"),
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "deactivate_hub_companies":
            dry_run = arguments.get("dry_run", True)
            company_names = arguments.get("company_names") or []
            detail = arguments.get("response_detail")
            max_conc = arguments.get("max_concurrency", 8)

            hub_id, _ = await resolve_hub(client, token, _norm_region(arguments), hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)
            app_token = await get_app_token()

            companies = await _get_account_companies(client, account_id, app_token)
            results = []
            targets = []
            for nm in company_names:
                cid = _resolve_company_id(companies, nm)
                if not cid:
                    results.append({"name": nm, "status": "not_found"})
                    continue
                if dry_run:
                    results.append({"name": nm, "status": "would_deactivate"})
                    continue
                targets.append((nm, cid))

            api_calls_made = False
            if not dry_run and targets:
                async def _deactivate_company(cname, cid):
                    r = await client.patch(
                        f"{APS_BASE}/hq/v1/accounts/{account_id}/companies/{cid}",
                        headers={**auth_headers(app_token), "Content-Type": "application/json"},
                        json={"status": "inactive"},
                    )
                    return cname, r

                api_calls_made = True
                responses = await _gather_bounded(
                    max_conc, [(lambda n=n, i=i: _deactivate_company(n, i)) for n, i in targets]
                )
                for cname, r in responses:
                    if r.is_success:
                        results.append({"name": cname, "status": "deactivated"})
                    else:
                        results.append({"name": cname, "status": "error",
                                        "message": f"HTTP {r.status_code}: {_error_body(r)}"})

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "deactivate_hub_companies")

            summary = {}
            for r in results:
                summary[r["status"]] = summary.get(r["status"], 0) + 1
            summary["total"] = len(results)

            payload = {
                "dry_run": dry_run, "operation": "deactivate_hub_companies",
                "summary": summary, "audit_file": audit_file,
            }
            _shape_bulk_response(
                payload, results, detail,
                noteworthy=lambda r: r["status"] in ("deactivated", "would_deactivate", "error", "not_found"),
                failure=lambda r: r["status"] in ("error", "not_found"),
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "bulk_list_folder_contents":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            # One shared headers dict, mutated in place by the refresher on a 401,
            # so a long batch outlives a token without re-resolving anything.
            b_hdrs = dict(hdrs)
            refresher = _bearer_refresher(b_hdrs)

            include_files = arguments.get("include_files", True)
            max_conc = arguments.get("max_concurrency") or 8
            exclude_lower = {e.strip().lower() for e in (arguments.get("exclude") or [])}
            fields_arg = arguments.get("fields")
            fields_set = {f.strip().lower() for f in fields_arg} if fields_arg else None
            lean_files = fields_set is not None  # caller signalled they want minimal rows
            want_subfolders = fields_set is None or "subfolders" in fields_set
            want_files = include_files and (fields_set is None or "files" in fields_set)
            # The contents payload already carries each subfolder's namingStandardIds,
            # so surfacing them here is free — saves a second pass for an orchestrator
            # that needs both the listing and the naming conventions.
            include_naming = arguments.get("include_naming_standard", False)
            children_of = arguments.get("children_of")
            explicit = arguments.get("folders")
            if bool(children_of) == bool(explicit):
                raise ValueError("Provide exactly one of 'children_of' or 'folders'.")

            targets: list[dict] = []  # {label, id|None, path|None}
            if children_of:
                parent_id, _ = await _resolve_folder(client, token, hub_id, project_id, children_of)
                contents = await get_all_folder_contents(
                    client, project_id, parent_id, b_hdrs, on_unauthorized=refresher
                )
                rx = re.compile(arguments["include_regex"]) if arguments.get("include_regex") else None
                for it in contents:
                    if it["type"] != "folders":
                        continue
                    nm = _folder_name(it["attributes"])
                    if rx and not rx.search(nm):
                        continue
                    if nm.lower() in exclude_lower:
                        continue
                    targets.append({"label": nm, "id": it["id"], "path": None})
            else:
                for f in explicit:
                    targets.append({"label": f, "id": None, "path": f})

            async def _list_one(t):
                try:
                    if t["id"]:
                        fid, fname = t["id"], t["label"]
                    else:
                        fid, fname = await _resolve_folder(client, token, hub_id, project_id, t["path"])
                    items = await get_all_folder_contents(
                        client, project_id, fid, b_hdrs, on_unauthorized=refresher
                    )
                    files = [i for i in items if i["type"] == "items"]
                    row = {
                        "folder": fname,
                        "folder_id": fid,
                        "file_count": len(files),
                    }
                    if want_subfolders:
                        def _sub(i):
                            s = {"name": _folder_name(i["attributes"]), "id": i["id"]}
                            if include_naming:
                                s["naming_standard_ids"] = _naming_standard_ids(i["attributes"])
                            return s
                        row["subfolders"] = [_sub(i) for i in items if i["type"] == "folders"]
                    if want_files:
                        if lean_files:
                            row["files"] = [{
                                "name": i.get("attributes", {}).get("displayName"),
                                "id": i["id"],
                            } for i in files]
                        else:
                            row["files"] = [{
                                "name": i.get("attributes", {}).get("displayName"),
                                "id": i["id"],
                                "last_modified": i.get("attributes", {}).get("lastModifiedTime"),
                                "created_by": i.get("attributes", {}).get("createUserName"),
                            } for i in files]
                    return ("ok", row)
                except APSQuotaError:
                    raise
                except Exception as e:
                    return ("error", {"folder": t["label"], "error": str(e)})

            outcomes = await _gather_bounded(max_conc, [lambda t=t: _list_one(t) for t in targets])
            results = [p for s, p in outcomes if s == "ok"]
            errors = [p for s, p in outcomes if s == "error"]
            # A non-empty folder is noteworthy (keep under 'changes'); failures are
            # carried by the always-present 'errors' array, so under 'summary' the
            # 'results' array is simply omitted (failure=None).
            payload = {
                "project": resolved_name,
                "summary": {"folders_listed": len(results), "errors": len(errors)},
            }
            _shape_bulk_response(
                payload, results, arguments.get("response_detail"),
                noteworthy=lambda r: r.get("file_count", 0) > 0 or bool(r.get("subfolders")),
            )
            payload["errors"] = errors
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "audit_folder_naming_standards":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            b_hdrs = dict(hdrs)
            refresher = _bearer_refresher(b_hdrs)
            max_depth = arguments.get("max_depth")
            if max_depth is None:
                max_depth = 25
            sem = asyncio.Semaphore(max(1, arguments.get("max_concurrency") or 8))

            # Build the list of start folders, each with its own attributes (which
            # carry namingStandardIds) so the root of each subtree is audited too.
            starts: list[tuple[str, str, dict]] = []  # (folder_id, name, attrs)
            folder_arg = arguments.get("folder")
            if folder_arg:
                fid, _ = await _resolve_folder(client, token, hub_id, project_id, folder_arg)
                r = await _request_with_retry(
                    client, "get",
                    f"{APS_BASE}/data/v1/projects/{project_id}/folders/{fid}",
                    headers=b_hdrs, on_unauthorized=refresher,
                )
                r.raise_for_status()
                attrs = r.json().get("data", {}).get("attributes", {})
                starts.append((fid, _folder_name(attrs) or folder_arg, attrs))
            else:
                r = await client.get(
                    f"{APS_BASE}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders",
                    headers=hdrs,
                )
                r.raise_for_status()
                for f in r.json().get("data", []):
                    starts.append((f["id"], _folder_name(f["attributes"]), f["attributes"]))

            walked = await asyncio.gather(*[
                _walk_naming_standards(
                    client, project_id, fid, name_, attrs, b_hdrs, max_depth, sem,
                    on_unauthorized=refresher,
                )
                for fid, name_, attrs in starts
            ])
            rows = [row for sub in walked for row in sub]

            by_standard: dict[str, int] = {}
            for row in rows:
                for sid in row["naming_standard_ids"]:
                    by_standard[sid] = by_standard.get(sid, 0) + 1
            without = sum(1 for row in rows if not row["has_standard"])
            payload = {
                "project": resolved_name,
                "summary": {
                    "total_folders": len(rows),
                    "with_standard": len(rows) - without,
                    "without_standard": without,
                    "by_standard": by_standard,
                },
            }
            _shape_bulk_response(
                payload, rows, arguments.get("response_detail"),
                noteworthy=lambda r: not r["has_standard"],
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "bulk_create_folders":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            b_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            refresher = _bearer_refresher(b_hdrs)

            skip_if_exists = arguments.get("skip_if_exists", True)
            dry_run = arguments.get("dry_run", True)
            cont = arguments.get("continue_on_error", True)
            max_conc = arguments.get("max_concurrency") or 8

            # Dedupe identical (parent, name) pairs (case-insensitive), keeping first.
            seen: set = set()
            deduped: list[dict] = []
            for it in (arguments.get("items") or []):
                parent = (it.get("parent") or "").strip()
                nm = (it.get("name") or "").strip()
                key = (parent, nm.lower())
                if not parent or not nm or key in seen:
                    continue
                seen.add(key)
                deduped.append({"parent": parent, "name": nm})

            # Resolve each unique parent once; cache its child names for skip_if_exists.
            parents: dict = {}
            for parent in {d["parent"] for d in deduped}:
                try:
                    pid, _ = await _resolve_folder(client, token, hub_id, project_id, parent)
                    names: set = set()
                    if skip_if_exists:
                        contents = await get_all_folder_contents(
                            client, project_id, pid, b_hdrs, on_unauthorized=refresher
                        )
                        for c in contents:
                            if c["type"] == "folders":
                                for k in ("name", "displayName"):
                                    v = c["attributes"].get(k)
                                    if v:
                                        names.add(v.lower())
                    parents[parent] = {"id": pid, "names": names, "error": None}
                except APSQuotaError:
                    raise
                except Exception as e:
                    parents[parent] = {"id": None, "names": set(), "error": str(e)}

            results = []
            to_create = []
            for d in deduped:
                p = parents[d["parent"]]
                if p["error"]:
                    results.append({"parent": d["parent"], "name": d["name"], "action": "error", "folder_id": None, "error": p["error"]})
                    continue
                if skip_if_exists and d["name"].lower() in p["names"]:
                    results.append({"parent": d["parent"], "name": d["name"], "action": ("would_exist" if dry_run else "exists"), "folder_id": None, "error": None})
                    continue
                if dry_run:
                    results.append({"parent": d["parent"], "name": d["name"], "action": "would_create", "folder_id": None, "error": None})
                    continue
                to_create.append(d)

            if to_create:
                async def _create_one(d):
                    p = parents[d["parent"]]
                    try:
                        r = await _request_with_retry(
                            client, "post", f"{APS_BASE}/data/v1/projects/{project_id}/folders",
                            headers=b_hdrs, json=_folder_create_payload(d["name"], p["id"]),
                            on_unauthorized=refresher,
                        )
                        if not r.is_success:
                            msg = f"HTTP {r.status_code}: {json.dumps(_error_body(r))[:300]}"
                            if not cont:
                                raise RuntimeError(msg)
                            return {"parent": d["parent"], "name": d["name"], "action": "error", "folder_id": None, "error": msg}
                        return {"parent": d["parent"], "name": d["name"], "action": "created", "folder_id": r.json().get("data", {}).get("id"), "error": None}
                    except APSQuotaError:
                        raise
                    except Exception as e:
                        if not cont:
                            raise
                        return {"parent": d["parent"], "name": d["name"], "action": "error", "folder_id": None, "error": str(e)}

                results.extend(await _gather_bounded(max_conc, [lambda d=d: _create_one(d) for d in to_create]))

            summary = {"created": 0, "exists": 0, "errors": 0}
            for r in results:
                a = r["action"]
                if a in ("created", "would_create"):
                    summary["created"] += 1
                elif a in ("exists", "would_exist"):
                    summary["exists"] += 1
                else:
                    summary["errors"] += 1
            return [TextContent(type="text", text=json.dumps({
                "dry_run": dry_run, "project": resolved_name,
                "summary": summary, "results": results,
            }, indent=2))]

        if name == "bulk_delete_folders":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            b_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            refresher = _bearer_refresher(b_hdrs)

            dry_run = arguments.get("dry_run", True)
            cont = arguments.get("continue_on_error", True)
            max_conc = arguments.get("max_concurrency") or 8

            def _err_row(target, fid, fname, exc):
                return {"folder": fname or target, "folder_id": fid, "action": "error",
                        "file_count": 0, "sample_files": [], "error": str(exc)}

            def _not_found(target):
                return {"folder": target, "folder_id": None, "action": "not_found",
                        "file_count": 0, "sample_files": [], "error": None}

            async def _del_one(target):
                # Resolve (path or URN). Misses → not_found; other failures → error.
                try:
                    fid, fname = await _resolve_folder(client, token, hub_id, project_id, target)
                except APSQuotaError:
                    raise
                except ValueError as e:
                    if "not found" in str(e).lower():
                        return _not_found(target)
                    if not cont:
                        raise
                    return _err_row(target, None, None, e)
                except httpx.HTTPStatusError as e:
                    if e.response is not None and e.response.status_code == 404:
                        return _not_found(target)
                    if not cont:
                        raise
                    return _err_row(target, None, None, e)
                except Exception as e:
                    if not cont:
                        raise
                    return _err_row(target, None, None, e)

                try:
                    count, sample = await _subtree_file_info(
                        client, project_id, fid, b_hdrs, on_unauthorized=refresher
                    )
                    if count > 0:
                        return {"folder": fname, "folder_id": fid, "action": "skipped_has_files",
                                "file_count": count, "sample_files": sample, "error": None}
                    if dry_run:
                        return {"folder": fname, "folder_id": fid, "action": "would_delete",
                                "file_count": 0, "sample_files": [], "error": None}
                    r = await _request_with_retry(
                        client, "patch", f"{APS_BASE}/data/v1/projects/{project_id}/folders/{fid}",
                        headers=b_hdrs, json=_folder_hide_payload(fid), on_unauthorized=refresher,
                    )
                    if not r.is_success:
                        msg = f"HTTP {r.status_code}: {json.dumps(_error_body(r))[:300]}"
                        if not cont:
                            raise RuntimeError(msg)
                        return _err_row(target, fid, fname, msg)
                    return {"folder": fname, "folder_id": fid, "action": "deleted",
                            "file_count": 0, "sample_files": [], "error": None}
                except APSQuotaError:
                    raise
                except Exception as e:
                    if not cont:
                        raise
                    return _err_row(target, fid, fname, e)

            rows = await _gather_bounded(max_conc, [lambda t=t: _del_one(t) for t in (arguments.get("folders") or [])])
            summary = {"deleted": 0, "skipped_has_files": 0, "not_found": 0, "errors": 0}
            for r in rows:
                a = r["action"]
                if a in ("deleted", "would_delete"):
                    summary["deleted"] += 1
                elif a == "skipped_has_files":
                    summary["skipped_has_files"] += 1
                elif a == "not_found":
                    summary["not_found"] += 1
                else:
                    summary["errors"] += 1
            payload = {"dry_run": dry_run, "project": resolved_name, "summary": summary}
            _shape_bulk_response(
                payload, rows, arguments.get("response_detail"),
                noteworthy=lambda r: r["action"] in ("skipped_has_files", "error"),
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "bulk_move_files":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            b_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            refresher = _bearer_refresher(b_hdrs)

            dry_run = arguments.get("dry_run", True)
            cont = arguments.get("continue_on_error", True)
            max_conc = arguments.get("max_concurrency") or 8
            items = arguments.get("items") or []

            # Resolve + list every unique source/destination folder once. The
            # files map enables source-by-name lookup AND 'already_there' idempotency.
            unique_folders: set = set()
            for it in items:
                if it.get("destination"):
                    unique_folders.add(it["destination"])
                if it.get("source"):
                    unique_folders.add(it["source"])
            fcache: dict = {}
            for s in unique_folders:
                try:
                    fid, _ = await _resolve_folder(client, token, hub_id, project_id, s)
                    contents = await get_all_folder_contents(
                        client, project_id, fid, b_hdrs, on_unauthorized=refresher
                    )
                    by_name: dict = {}
                    ids: set = set()
                    for i in contents:
                        if i["type"] == "items":
                            by_name[(i["attributes"].get("displayName") or "").lower()] = i["id"]
                            ids.add(i["id"])
                    fcache[s] = {"id": fid, "error": None, "by_name": by_name, "ids": ids}
                except APSQuotaError:
                    raise
                except Exception as e:
                    fcache[s] = {"id": None, "error": str(e), "by_name": {}, "ids": set()}

            def _mv_row(it, item_id, action, error=None):
                return {"file": it.get("name") or item_id, "item_id": item_id,
                        "from": it.get("source"), "to": it.get("destination"),
                        "action": action, "error": error}

            results = []
            to_move = []  # (it, item_id, dest_id)
            for it in items:
                dest = it.get("destination")
                dentry = fcache.get(dest)
                if not dest or dentry is None or dentry["error"]:
                    results.append(_mv_row(it, it.get("item_id"), "error",
                                           (dentry["error"] if dentry else "destination missing")))
                    continue
                item_id = it.get("item_id")
                if item_id:
                    if item_id in dentry["ids"]:
                        results.append(_mv_row(it, item_id, "already_there"))
                        continue
                else:
                    src, nm = it.get("source"), it.get("name")
                    if not src or not nm:
                        results.append(_mv_row(it, None, "error", "Provide item_id or source+name."))
                        continue
                    sentry = fcache.get(src)
                    if sentry is None or sentry["error"]:
                        results.append(_mv_row(it, None, "error", sentry["error"] if sentry else "source missing"))
                        continue
                    key = nm.strip().lower()
                    if key in sentry["by_name"]:
                        item_id = sentry["by_name"][key]
                    elif key in dentry["by_name"]:
                        results.append(_mv_row(it, dentry["by_name"][key], "already_there"))
                        continue
                    else:
                        results.append(_mv_row(it, None, "not_found"))
                        continue
                if dry_run:
                    results.append(_mv_row(it, item_id, "would_move"))
                    continue
                to_move.append((it, item_id, dentry["id"]))

            if to_move:
                async def _move_one(it, item_id, dest_id):
                    try:
                        r = await _request_with_retry(
                            client, "patch", f"{APS_BASE}/data/v1/projects/{project_id}/items/{item_id}",
                            headers=b_hdrs, json=_reparent_payload("items", item_id, dest_id),
                            on_unauthorized=refresher,
                        )
                        if r.status_code == 403:
                            return _mv_row(it, item_id, "skipped_unmovable",
                                           "403 — likely a cloud-workshared C4R model; move it in the Revit/ACC UI.")
                        if not r.is_success:
                            msg = f"HTTP {r.status_code}: {json.dumps(_error_body(r))[:300]}"
                            if not cont:
                                raise RuntimeError(msg)
                            return _mv_row(it, item_id, "error", msg)
                        return _mv_row(it, item_id, "moved")
                    except APSQuotaError:
                        raise
                    except Exception as e:
                        if not cont:
                            raise
                        return _mv_row(it, item_id, "error", str(e))

                results.extend(await _gather_bounded(max_conc, [lambda a=a: _move_one(*a) for a in to_move]))

            summary = {"moved": 0, "already_there": 0, "skipped_unmovable": 0, "not_found": 0, "errors": 0}
            for r in results:
                a = r["action"]
                if a in ("moved", "would_move"):
                    summary["moved"] += 1
                elif a == "already_there":
                    summary["already_there"] += 1
                elif a == "skipped_unmovable":
                    summary["skipped_unmovable"] += 1
                elif a == "not_found":
                    summary["not_found"] += 1
                else:
                    summary["errors"] += 1
            payload = {"dry_run": dry_run, "project": resolved_name, "summary": summary}
            _shape_bulk_response(
                payload, results, arguments.get("response_detail"),
                noteworthy=lambda r: r["action"] in ("error", "skipped_unmovable", "not_found"),
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "bulk_move_folders":
            hub_id, project_id, resolved_name = await _resolve_project_arg(client, token, arguments)
            b_hdrs = {**hdrs, "Content-Type": "application/vnd.api+json"}
            refresher = _bearer_refresher(b_hdrs)

            dry_run = arguments.get("dry_run", True)
            cont = arguments.get("continue_on_error", True)
            max_conc = arguments.get("max_concurrency") or 8
            items = arguments.get("items") or []

            # Resolve + list every unique destination once; its subfolder ids give
            # 'already_there' idempotency.
            dcache: dict = {}
            for s in {it["destination"] for it in items if it.get("destination")}:
                try:
                    did, _ = await _resolve_folder(client, token, hub_id, project_id, s)
                    contents = await get_all_folder_contents(
                        client, project_id, did, b_hdrs, on_unauthorized=refresher
                    )
                    dcache[s] = {"id": did, "error": None,
                                 "sub_ids": {i["id"] for i in contents if i["type"] == "folders"}}
                except APSQuotaError:
                    raise
                except Exception as e:
                    dcache[s] = {"id": None, "error": str(e), "sub_ids": set()}

            def _mvf_row(folder_label, fid, dest, action, error=None):
                return {"folder": folder_label, "folder_id": fid, "to": dest,
                        "action": action, "error": error}

            async def _movef_one(it):
                folder, dest = it.get("folder"), it.get("destination")
                dentry = dcache.get(dest)
                if not folder or not dest or dentry is None or dentry["error"]:
                    return _mvf_row(folder, None, dest, "error",
                                    (dentry["error"] if dentry else "destination missing"))
                try:
                    fid, fname = await _resolve_folder(client, token, hub_id, project_id, folder)
                except APSQuotaError:
                    raise
                except ValueError as e:
                    if "not found" in str(e).lower():
                        return _mvf_row(folder, None, dest, "not_found")
                    if not cont:
                        raise
                    return _mvf_row(folder, None, dest, "error", str(e))
                except httpx.HTTPStatusError as e:
                    if e.response is not None and e.response.status_code == 404:
                        return _mvf_row(folder, None, dest, "not_found")
                    if not cont:
                        raise
                    return _mvf_row(folder, None, dest, "error", str(e))
                except Exception as e:
                    if not cont:
                        raise
                    return _mvf_row(folder, None, dest, "error", str(e))

                if fid in dentry["sub_ids"]:
                    return _mvf_row(fname, fid, dest, "already_there")
                if dry_run:
                    return _mvf_row(fname, fid, dest, "would_move")
                try:
                    r = await _request_with_retry(
                        client, "patch", f"{APS_BASE}/data/v1/projects/{project_id}/folders/{fid}",
                        headers=b_hdrs, json=_reparent_payload("folders", fid, dentry["id"]),
                        on_unauthorized=refresher,
                    )
                    if not r.is_success:
                        msg = f"HTTP {r.status_code}: {json.dumps(_error_body(r))[:300]}"
                        if not cont:
                            raise RuntimeError(msg)
                        return _mvf_row(fname, fid, dest, "error", msg)
                    return _mvf_row(fname, fid, dest, "moved")
                except APSQuotaError:
                    raise
                except Exception as e:
                    if not cont:
                        raise
                    return _mvf_row(fname, fid, dest, "error", str(e))

            rows = await _gather_bounded(max_conc, [lambda it=it: _movef_one(it) for it in items])
            summary = {"moved": 0, "already_there": 0, "not_found": 0, "errors": 0}
            for r in rows:
                a = r["action"]
                if a in ("moved", "would_move"):
                    summary["moved"] += 1
                elif a == "already_there":
                    summary["already_there"] += 1
                elif a == "not_found":
                    summary["not_found"] += 1
                else:
                    summary["errors"] += 1
            payload = {"dry_run": dry_run, "project": resolved_name, "summary": summary}
            _shape_bulk_response(
                payload, rows, arguments.get("response_detail"),
                noteworthy=lambda r: r["action"] in ("error", "not_found"),
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "list_issues":
            project_id, resolved_name = await _resolve_issue_project(client, token, arguments)
            issue_hdrs = {**hdrs, "x-ads-region": ISSUES_REGION}
            params = {}
            filter_map = {
                "status": "filter[status]",
                "search": "filter[search]",
                "display_id": "filter[displayId]",
                "assigned_to": "filter[assignedTo]",
                "created_by": "filter[createdBy]",
                "due_date": "filter[dueDate]",
                "issue_type_id": "filter[issueTypeId]",
                "issue_subtype_id": "filter[issueSubtypeId]",
                "linked_document_urn": "filter[linkedDocumentUrn]",
                "sort_by": "sortBy",
                "fields": "fields",
            }
            for arg_key, q_key in filter_map.items():
                val = arguments.get(arg_key)
                if val is not None and val != "":
                    params[q_key] = val
            if arguments.get("deleted") is not None:
                params["filter[deleted]"] = str(arguments["deleted"]).lower()
            issues = await _get_all_issues(
                client,
                f"{APS_BASE}/construction/issues/v1/projects/{project_id}/issues",
                issue_hdrs, params, page_limit=100,
            )
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "result_count": len(issues),
                "issues": issues,
            }, indent=2))]

        if name == "create_issue":
            project_id, resolved_name = await _resolve_issue_project(client, token, arguments)
            issue_hdrs = {**hdrs, "x-ads-region": ISSUES_REGION, "Content-Type": "application/json"}
            body = {
                "title": arguments["title"],
                "issueSubtypeId": arguments["issue_subtype_id"],
                "status": arguments["status"],
            }
            body_map = {
                "description": "description",
                "assigned_to": "assignedTo",
                "assigned_to_type": "assignedToType",
                "due_date": "dueDate",
                "start_date": "startDate",
                "location_id": "locationId",
                "location_details": "locationDetails",
                "root_cause_id": "rootCauseId",
                "published": "published",
                "watchers": "watchers",
                "custom_attributes": "customAttributes",
                "gps_coordinates": "gpsCoordinates",
            }
            for arg_key, body_key in body_map.items():
                val = arguments.get(arg_key)
                if val is not None:
                    body[body_key] = val
            r = await client.post(
                f"{APS_BASE}/construction/issues/v1/projects/{project_id}/issues",
                headers=issue_hdrs, json=body,
            )
            if not r.is_success:
                return [TextContent(type="text", text=json.dumps(
                    {"error": r.status_code, "body": _error_body(r)}, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "status": "created",
                "issue": r.json(),
            }, indent=2))]

        if name == "list_issue_types":
            project_id, resolved_name = await _resolve_issue_project(client, token, arguments)
            issue_hdrs = {**hdrs, "x-ads-region": ISSUES_REGION}
            params = {}
            if arguments.get("include_subtypes", True):
                params["include"] = "subtypes"
            if arguments.get("is_active") is not None:
                params["filter[isActive]"] = str(arguments["is_active"]).lower()
            types = await _get_all_issues(
                client,
                f"{APS_BASE}/construction/issues/v1/projects/{project_id}/issue-types",
                issue_hdrs, params, page_limit=200,
            )
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "result_count": len(types),
                "types": types,
            }, indent=2))]

        if name == "list_issue_attribute_definitions":
            project_id, resolved_name = await _resolve_issue_project(client, token, arguments)
            issue_hdrs = {**hdrs, "x-ads-region": ISSUES_REGION}
            params = {}
            if arguments.get("data_type"):
                params["filter[dataType]"] = arguments["data_type"]
            definitions = await _get_all_issues(
                client,
                f"{APS_BASE}/construction/issues/v1/projects/{project_id}/issue-attribute-definitions",
                issue_hdrs, params, page_limit=200,
            )
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "result_count": len(definitions),
                "definitions": definitions,
            }, indent=2))]

        if name == "list_issue_attribute_mappings":
            project_id, resolved_name = await _resolve_issue_project(client, token, arguments)
            issue_hdrs = {**hdrs, "x-ads-region": ISSUES_REGION}
            params = {}
            if arguments.get("attribute_definition_id"):
                params["filter[attributeDefinitionId]"] = arguments["attribute_definition_id"]
            if arguments.get("mapped_item_id"):
                params["filter[mappedItemId]"] = arguments["mapped_item_id"]
            mappings = await _get_all_issues(
                client,
                f"{APS_BASE}/construction/issues/v1/projects/{project_id}/issue-attribute-mappings",
                issue_hdrs, params, page_limit=200,
            )
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "result_count": len(mappings),
                "mappings": mappings,
            }, indent=2))]

        if name == "get_issue":
            project_id, resolved_name = await _resolve_issue_project(client, token, arguments)
            issue_hdrs = {**hdrs, "x-ads-region": ISSUES_REGION}
            issue_id = await _resolve_issue_ref(client, issue_hdrs, project_id, arguments)
            r = await client.get(
                f"{APS_BASE}/construction/issues/v1/projects/{project_id}/issues/{issue_id}",
                headers=issue_hdrs,
            )
            if not r.is_success:
                return [TextContent(type="text", text=json.dumps(
                    {"error": r.status_code, "body": _error_body(r)}, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "issue": r.json(),
            }, indent=2))]

        if name == "update_issue":
            project_id, resolved_name = await _resolve_issue_project(client, token, arguments)
            issue_hdrs = {**hdrs, "x-ads-region": ISSUES_REGION, "Content-Type": "application/json"}
            issue_id = await _resolve_issue_ref(client, issue_hdrs, project_id, arguments)
            body = {}
            body_map = {
                "title": "title",
                "description": "description",
                "issue_subtype_id": "issueSubtypeId",
                "status": "status",
                "assigned_to": "assignedTo",
                "assigned_to_type": "assignedToType",
                "due_date": "dueDate",
                "start_date": "startDate",
                "location_id": "locationId",
                "location_details": "locationDetails",
                "root_cause_id": "rootCauseId",
                "published": "published",
                "watchers": "watchers",
                "custom_attributes": "customAttributes",
                "gps_coordinates": "gpsCoordinates",
            }
            for arg_key, body_key in body_map.items():
                val = arguments.get(arg_key)
                if val is not None:
                    body[body_key] = val
            if not body:
                raise ValueError("Nothing to update — supply at least one field to change.")
            r = await client.patch(
                f"{APS_BASE}/construction/issues/v1/projects/{project_id}/issues/{issue_id}",
                headers=issue_hdrs, json=body,
            )
            if not r.is_success:
                return [TextContent(type="text", text=json.dumps(
                    {"error": r.status_code, "body": _error_body(r)}, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "status": "updated",
                "issue": r.json(),
            }, indent=2))]

        if name == "list_workflows":
            bare_id, resolved_name = await _resolve_review_project(client, token, arguments)
            wf_hdrs = {**hdrs, "x-ads-region": REVIEWS_REGION}
            params: dict = {}
            if arguments.get("status"):
                params["filter[status]"] = arguments["status"]
            if arguments.get("initiator") is not None:
                params["filter[initiator]"] = str(arguments["initiator"]).lower()
            if arguments.get("sort"):
                params["sort"] = arguments["sort"]
            workflows = await _get_all_issues(
                client, WORKFLOWS_URL.format(pid=bare_id), wf_hdrs, params, page_limit=50,
            )
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "result_count": len(workflows),
                "workflows": workflows,
            }, indent=2))]

        if name == "get_workflow":
            bare_id, resolved_name = await _resolve_review_project(client, token, arguments)
            wf_hdrs = {**hdrs, "x-ads-region": REVIEWS_REGION}
            workflow_id = await _resolve_workflow_ref(client, wf_hdrs, bare_id, arguments)
            r = await client.get(
                f"{WORKFLOWS_URL.format(pid=bare_id)}/{workflow_id}", headers=wf_hdrs,
            )
            if not r.is_success:
                return [TextContent(type="text", text=json.dumps(
                    {"error": r.status_code, "body": _error_body(r)}, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "workflow": r.json(),
            }, indent=2))]

        if name == "create_workflow":
            bare_id, resolved_name = await _resolve_review_project(client, token, arguments)
            wf_hdrs = {**hdrs, "x-ads-region": REVIEWS_REGION, "Content-Type": "application/json"}
            directory = await _build_project_directory(client, token, bare_id)
            body = _workflow_create_payload(arguments, directory)
            r = await client.post(
                WORKFLOWS_URL.format(pid=bare_id), headers=wf_hdrs, json=body,
            )
            if not r.is_success:
                return [TextContent(type="text", text=json.dumps(
                    {"error": r.status_code, "body": _error_body(r)}, indent=2))]
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name,
                "status": "created",
                "workflow": r.json(),
            }, indent=2))]

        if name == "bulk_create_workflows":
            bare_id, resolved_name = await _resolve_review_project(client, token, arguments)
            wf_hdrs = {**hdrs, "x-ads-region": REVIEWS_REGION, "Content-Type": "application/json"}
            specs = arguments.get("workflows") or []
            dry_run = arguments.get("dry_run", True)
            continue_on_error = arguments.get("continue_on_error", True)
            max_concurrency = arguments.get("max_concurrency", 8)
            url = WORKFLOWS_URL.format(pid=bare_id)
            directory = await _build_project_directory(client, token, bare_id)

            # Build payloads up front so a bad spec is reported without a POST.
            prepared: list[dict] = []
            for spec in specs:
                wf_name = spec.get("name")
                try:
                    prepared.append({"name": wf_name, "payload": _workflow_create_payload(spec, directory)})
                except ValueError as e:
                    prepared.append({"name": wf_name, "error": str(e)})

            if dry_run:
                results = [
                    {"name": p["name"], "action": "error", "message": p["error"]}
                    if "error" in p else
                    {"name": p["name"], "action": "would_create"}
                    for p in prepared
                ]
            else:
                async def _post_one(p: dict) -> dict:
                    if "error" in p:
                        return {"name": p["name"], "action": "error", "message": p["error"]}
                    try:
                        resp = await client.post(url, headers=wf_hdrs, json=p["payload"])
                    except Exception as e:  # noqa: BLE001
                        if not continue_on_error:
                            raise
                        return {"name": p["name"], "action": "error", "message": str(e)}
                    if resp.status_code == 409:
                        return {"name": p["name"], "action": "already_exists"}
                    if not resp.is_success:
                        return {"name": p["name"], "action": "error",
                                "message": json.dumps(_error_body(resp))}
                    created = resp.json()
                    return {"name": p["name"], "action": "created", "workflow_id": created.get("id")}

                results = await _gather_bounded(
                    max_concurrency, [lambda p=p: _post_one(p) for p in prepared]
                )

            counts: dict = {}
            for r in results:
                counts[r["action"]] = counts.get(r["action"], 0) + 1
            payload: dict = {
                "project": resolved_name,
                "dry_run": dry_run,
                "total": len(results),
                "summary": counts,
            }
            if not dry_run:
                payload["audit_file"] = _write_audit_csv(results, "bulk_create_workflows")
            _shape_bulk_response(
                payload, results, arguments.get("response_detail"),
                noteworthy=lambda r: r["action"] in ("error", "already_exists"),
            )
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
