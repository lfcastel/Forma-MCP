import os
import io
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
from mcp.types import Tool, TextContent

APS_CLIENT_ID = os.environ["APS_CLIENT_ID"]
APS_CLIENT_SECRET = os.environ["APS_CLIENT_SECRET"]
APS_BASE = "https://developer.api.autodesk.com"
REDIRECT_URI = "http://localhost:8080/oauth/callback"
SCOPES = "data:read data:write data:create account:read account:write"

# tokens.json sits next to this script
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")

app = Server("aps-mcp")

# Default product access granted to users added/updated via bulk tools.
# docs = document management (core); insight = required by the Admin API.
DEFAULT_PRODUCTS = [
    {"key": "docs", "access": "member"},
    {"key": "insight", "access": "member"},
]

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


def _to_bare_id(aps_id: str) -> str:
    """Strip the 'b.' prefix from APS hub/project IDs."""
    return aps_id.removeprefix("b.")


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


async def get_all_folder_contents(
    client: httpx.AsyncClient,
    project_id: str,
    folder_id: str,
    headers: dict,
    *,
    raise_on_error: bool = True,
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
        res = await _request_with_retry(client, "get", url, headers=headers, params=params)
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


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 5,
    **kwargs,
) -> httpx.Response:
    """Issue an HTTP request and retry on 429 using Retry-After or exponential backoff."""
    for attempt in range(max_retries + 1):
        r = await getattr(client, method)(url, **kwargs)
        if r.status_code != 429 or attempt == max_retries:
            return r
        wait = int(r.headers.get("Retry-After", min(2 ** attempt, 60)))
        await asyncio.sleep(wait)
    return r


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


def _resolve_role_id(role_name: str, role_map: dict[str, str]) -> str | None:
    """Case-insensitive role name → role ID lookup."""
    name_lower = role_name.lower()
    for rid, rname in role_map.items():
        if rname.lower() == name_lower:
            return rid
    return None


def _write_audit_csv(rows: list[dict], operation: str) -> str:
    """Write audit rows to a timestamped CSV in the audit_logs/ folder. Returns file path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_logs")
    os.makedirs(audit_dir, exist_ok=True)
    filepath = os.path.join(audit_dir, f"audit_{operation}_{timestamp}.csv")
    if rows:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
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
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
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
                    "project_name": {"type": "string", "description": "Project name (partial match ok)."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": ["project_name"],
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
                    "project_name": {"type": "string"},
                    "folder_path": {"type": "string", "description": "Slash-separated path e.g. 'Project Files/Drawings', or a raw folder URN for faster resolution."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": ["project_name", "folder_path"],
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
                    "project_name": {"type": "string"},
                    "folder_path": {
                        "type": "string",
                        "description": "Slash-separated path e.g. 'Project Files/Drawings/My Folder', or a raw folder URN for faster resolution.",
                    },
                    "new_name": {"type": "string", "description": "New display name for the folder."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name", "folder_path", "new_name"],
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
                    "project_name": {"type": "string"},
                    "folder_path": {
                        "type": "string",
                        "description": "Slash-separated path to the folder containing the file, or a raw folder URN for faster resolution.",
                    },
                    "file_name": {"type": "string", "description": "Current display name of the file."},
                    "new_name": {"type": "string", "description": "New display name for the file."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name", "folder_path", "file_name", "new_name"],
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
                    "project_name": {"type": "string"},
                    "query": {"type": "string", "description": "Folder name substring to search for."},
                    "folder_path": {"type": "string", "description": "Limit search to this folder and its descendants (optional). Accepts a slash-separated path or a raw folder URN."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name", "query"],
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
                    "project_name": {"type": "string"},
                    "parent_folder_path": {
                        "type": "string",
                        "description": "Slash-separated path to the parent folder, e.g. 'Project Files/Drawings', or a raw folder URN for faster resolution.",
                    },
                    "folder_name": {"type": "string", "description": "Name for the new folder."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name", "parent_folder_path", "folder_name"],
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
                    "project_name": {"type": "string"},
                    "folder_path": {
                        "type": "string",
                        "description": "Slash-separated path e.g. 'Project Files/Drawings/Old Folder', or a raw folder URN.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), preview without making changes.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name", "folder_path"],
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
                    "project_name": {"type": "string"},
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
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name", "source_folder_path", "file_name", "destination_folder_path"],
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
                    "project_name": {"type": "string"},
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
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name", "folder_path", "destination_parent_path"],
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
                    "project_name": {"type": "string"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": ["project_name"],
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
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
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
                    "project_name": {"type": "string"},
                    "since_date": {"type": "string", "description": "YYYY-MM-DD, defaults to 7 days ago"},
                    "limit": {"type": "integer", "description": "Max items (default 50)"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": ["project_name"],
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
                    "project_name": {"type": "string"},
                    "query": {"type": "string", "description": "Filename substring"},
                    "folder_path": {"type": "string", "description": "Limit search to this folder (optional). Accepts a slash-separated path or a raw folder URN."},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region (e.g. EMEA, US). Defaults to EMEA."},
                },
                "required": ["project_name", "query"],
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
                    "project_name": {"type": "string"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name"],
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
                    "project_name": {"type": "string"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name"],
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
                    "project_name": {"type": "string"},
                    "folder_path": {
                        "type": "string",
                        "description": "Root folder to scan, e.g. 'Project Files/20_SHARED_Extern', or a raw folder URN for faster resolution.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Folder levels to scan below the root (default 2).",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name", "folder_path"],
            },
        ),
        Tool(
            name="create_role_data_export",
            description=(
                "STEP 1 of 2 for Forma role resolution. "
                "Create a one-time Data Connector export (admin service group) to retrieve "
                "Forma project roles. Forma does not expose a direct roles endpoint — this "
                "async export is the only supported approach. "
                "Rate limited to 24 jobs per hub per day. "
                "After calling this, immediately call get_data_connector_requests (STEP 2) "
                "to poll until the job is complete, then proceed to export_permission_matrix."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_name": {"type": "string"},
                    "send_email": {
                        "type": "boolean",
                        "description": "Send completion email. Default: true.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["project_name"],
            },
        ),
        Tool(
            name="get_data_connector_requests",
            description=(
                "STEP 2 of 2 for Forma role resolution. "
                "Lists Data Connector requests and, when a completed job is found, immediately "
                "downloads and parses the role CSV from the signed ZIP URL (valid only 60 s). "
                "Pass request_id (returned by create_role_data_export) to wait for that specific "
                "job — the tool polls internally every 15 s (up to 10 min) so you never need to "
                "call it repeatedly. Parsed roles are returned as a role_id→name map ready for "
                "export_permission_matrix."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
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
                    "project_name": {"type": "string"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
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
                "required": ["project_name", "changes"],
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
                        "type": "string",
                        "description": "Role name applied to all projects unless overridden by role_overrides.",
                    },
                    "role_overrides": {
                        "type": "object",
                        "description": "Per-project role map: {\"Project A\": \"Editor\", \"Project B\": \"Viewer\"}.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), return a preview without making any changes.",
                    },
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
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
                        "type": "string",
                        "description": "New role name to apply across all projects unless overridden.",
                    },
                    "role_overrides": {
                        "type": "object",
                        "description": "Per-project role map: {\"Project A\": \"Admin\"}.",
                    },
                    "dry_run": {"type": "boolean"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
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
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
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
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
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
                    "default_role": {"type": "string"},
                    "role_overrides": {"type": "object"},
                    "user_filter": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: restrict to these emails within the company.",
                    },
                    "dry_run": {"type": "boolean"},
                    "hub_name": {"type": "string", "description": "Hub display name (partial match ok, e.g. 'BAC - EU Hub'). Use when multiple hubs share the same region."},
                    "region": {"type": "string", "description": "Hub region. Defaults to EMEA."},
                },
                "required": ["company_name", "project_names", "default_role"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
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

        if name == "list_top_folders":
            hub_id, project_id, resolved_name = await resolve_project(client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name"))
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
            hub_id, project_id, resolved_name = await resolve_project(client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name"))
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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

            payload = {
                "jsonapi": {"version": "1.0"},
                "data": {
                    "type": "items",
                    "id": item_id,
                    "relationships": {
                        "parent": {"data": {"type": "folders", "id": dest_folder_id}},
                    },
                },
            }
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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

            payload = {
                "jsonapi": {"version": "1.0"},
                "data": {
                    "type": "folders",
                    "id": folder_id,
                    "relationships": {
                        "parent": {"data": {"type": "folders", "id": dest_parent_id}},
                    },
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
                "name": folder_name,
                "from": folder_path,
                "to": destination_parent_path,
                "destination_parent_id": dest_parent_id,
                "status": "moved",
            }, indent=2))]

        if name == "create_folder":
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
            parent_path = arguments["parent_folder_path"]
            folder_name = arguments["folder_name"].strip()
            if not folder_name:
                raise ValueError("folder_name cannot be empty.")

            parent_id, _ = await _resolve_folder(client, token, hub_id, project_id, parent_path)

            payload = {
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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

            payload = {
                "jsonapi": {"version": "1.0"},
                "data": {
                    "type": "folders",
                    "id": folder_id,
                    "attributes": {"hidden": True},
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
                "folder_name": folder_name,
                "status": "deleted",
                "note": "Folder is hidden (soft-deleted). It can be restored by an ACC admin.",
            }, indent=2))]

        if name == "list_project_members":
            hub_id, project_id, resolved_name = await resolve_project(client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name"))
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
            hub_id, project_id, resolved_name = await resolve_project(client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name"))
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
            hub_id, project_id, resolved_name = await resolve_project(client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name"))
            query = arguments["query"].lower()
            folder_path = arguments.get("folder_path")

            if folder_path:
                folder_id, _ = await _resolve_folder(client, token, hub_id, project_id, folder_path)
                start_folders = [{"id": folder_id, "attributes": {"displayName": folder_path}}]
            else:
                res = await client.get(
                    f"{APS_BASE}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders",
                    headers=hdrs,
                )
                res.raise_for_status()
                start_folders = res.json().get("data", [])

            results: list[dict] = []

            async def search_folder(folder_id: str, path: str, depth: int = 0):
                if depth > 8:
                    return
                contents = await get_all_folder_contents(
                    client, project_id, folder_id, hdrs, raise_on_error=False
                )
                tasks = []
                for item in contents:
                    a = item.get("attributes", {})
                    if item["type"] == "folders":
                        tasks.append(search_folder(item["id"], f"{path}/{_folder_name(a)}", depth + 1))
                        continue
                    display_name = a.get("displayName", "")
                    if query in display_name.lower():
                        results.append({
                            "name": display_name,
                            "path": f"{path}/{display_name}",
                            "id": item["id"],
                            "last_modified": a.get("lastModifiedTime"),
                            "modified_by": a.get("lastModifiedUserName"),
                        })
                await asyncio.gather(*tasks)

            await asyncio.gather(*[
                search_folder(f["id"], _folder_name(f["attributes"])) for f in start_folders
            ])
            results.sort(key=lambda x: x.get("last_modified") or "", reverse=True)
            return [TextContent(type="text", text=json.dumps({
                "project": resolved_name, "query": query,
                "result_count": len(results), "files": results,
            }, indent=2))]

        if name == "find_folder":
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
            query = arguments["query"].lower()
            folder_path = arguments.get("folder_path")

            if folder_path:
                folder_id, _ = await _resolve_folder(client, token, hub_id, project_id, folder_path)
                start_folders = [{"id": folder_id, "attributes": {"displayName": folder_path}}]
            else:
                res = await client.get(
                    f"{APS_BASE}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders",
                    headers=hdrs,
                )
                res.raise_for_status()
                start_folders = res.json().get("data", [])

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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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

            async def _download_roles(signed_url: str) -> dict[str, str]:
                dl = await client.get(signed_url, timeout=120)
                dl.raise_for_status()
                roles: dict[str, str] = {}
                zf = zipfile.ZipFile(io.BytesIO(dl.content))
                for zfname in zf.namelist():
                    if "role" in zfname.lower() and zfname.endswith(".csv"):
                        csv_text = zf.read(zfname).decode("utf-8-sig")
                        for row in csv.DictReader(io.StringIO(csv_text)):
                            rid = row.get("role_id") or row.get("roleId") or row.get("id", "")
                            rname = row.get("name") or row.get("role_name") or row.get("roleName", "")
                            if rid and rname:
                                roles[rid] = rname
                return roles

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
                    roles = await _download_roles(signed_url)
                    result["roles"] = roles
                    result["role_count"] = len(roles)
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
            hub_id, project_id, resolved_name = await resolve_project(
                client, token, arguments["project_name"], region=_norm_region(arguments), hub_name=arguments.get("hub_name")
            )
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
            default_role: str,
            role_overrides: dict[str, str],
            dry_run: bool,
        ) -> tuple[list[dict], list[str], bool]:
            """
            Core assign logic reused by bulk_assign_users, clone_user_access,
            and bulk_assign_company_users.
            Returns (results, warnings, api_calls_made).
            NOTE: request body format for users:import may need adjustment
            against the ACC Admin API v1 spec during implementation.
            """
            warnings: list[str] = []
            BATCH = 200
            sem = asyncio.Semaphore(5)
            api_calls_made_flag = [False]

            async def _process_project(proj: dict) -> list[dict]:
                async with sem:
                    pid = proj["id"]
                    pname = proj["name"]
                    role_name = role_overrides.get(pname.lower(), default_role)
                    proj_results: list[dict] = []

                    role_id: str | None = None
                    if role_name:
                        role_map = await _fetch_project_roles(client, pid, hdrs)
                        role_id = _resolve_role_id(role_name, role_map)
                        if not role_id:
                            available = list(role_map.values()) or ["(none found — project may have no members yet)"]
                            for email in user_emails:
                                proj_results.append({
                                    "user": email, "project": pname, "role": role_name,
                                    "status": "error",
                                    "message": f"Role '{role_name}' not found. Available: {available}",
                                })
                            return proj_results

                    if dry_run:
                        members = await _get_project_members_map(client, pid, hdrs)
                        for email in user_emails:
                            if email in members:
                                proj_results.append({
                                    "user": email, "project": pname, "role": role_name or "(none)",
                                    "status": "already_member",
                                    "message": "Already a member — no change",
                                })
                            else:
                                proj_results.append({
                                    "user": email, "project": pname, "role": role_name or "(none)",
                                    "status": "would_add",
                                    "message": f"Would add with role '{role_name}'" if role_name else "Would add (no role)",
                                })
                        return proj_results

                    bare_pid = _to_bare_id(pid)
                    for i in range(0, len(user_emails), BATCH):
                        batch = user_emails[i : i + BATCH]
                        users_payload = []
                        for email in batch:
                            entry: dict = {
                                "email": email,
                                "products": DEFAULT_PRODUCTS,
                            }
                            if role_id:
                                entry["roleIds"] = [role_id]
                            users_payload.append(entry)

                        api_calls_made_flag[0] = True
                        r = await client.post(
                            f"{APS_BASE}/construction/admin/v2/projects/{bare_pid}/users:import",
                            headers=hdrs,
                            json={"users": users_payload, "suppressAdministrativeEmails": False},
                        )
                        if r.is_success:
                            resp = r.json()
                            items = _extract_response_items(resp)
                            if items:
                                for item in items:
                                    email_resp = (item.get("email") or "").lower()
                                    ok = item.get("success", True)
                                    proj_results.append({
                                        "user": email_resp or "(unknown)",
                                        "project": pname, "role": role_name or "(none)",
                                        "status": "success" if ok else "error",
                                        "message": item.get("message") or item.get("error") or "",
                                    })
                            else:
                                for email in batch:
                                    proj_results.append({
                                        "user": email, "project": pname, "role": role_name or "(none)",
                                        "status": "success", "message": "",
                                    })
                        else:
                            body = _error_body(r)
                            for email in batch:
                                proj_results.append({
                                    "user": email, "project": pname, "role": role_name or "(none)",
                                    "status": "error", "message": f"HTTP {r.status_code}: {body}",
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

            hub_id, _ = await resolve_hub(client, token, region, hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)

            resolved_projects: list[dict] = []
            project_errors: list[dict] = []
            for pname in project_names:
                try:
                    _, pid, rname = await resolve_project(client, token, pname, hub_id=hub_id)
                    resolved_projects.append({"id": pid, "name": rname})
                except ValueError as e:
                    project_errors.append({"project": pname, "error": str(e)})

            warnings: list[str] = []
            invalid_email_set: set[str] = set()
            if resolved_projects:
                app_token = await get_app_token()
                account_users = await _get_account_users_map(client, account_id, app_token)
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
                    role_name = role_overrides.get(proj["name"].lower(), default_role)
                    results.append({
                        "user": email, "project": proj["name"], "role": role_name,
                        "status": "error", "message": "Not found in account roster",
                    })

            for pentry in project_errors:
                role_name = role_overrides.get(pentry["project"].lower(), default_role)
                for email in user_emails:
                    results.append({
                        "user": email, "project": pentry["project"], "role": role_name,
                        "status": "error", "message": f"Project not found: {pentry['project']}",
                    })

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "bulk_assign")

            summary = {
                k: sum(1 for r in results if r["status"] == k)
                for k in ("success", "would_add", "already_member", "error")
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

            hub_id, _ = await resolve_hub(client, token, region, hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)

            resolved_projects: list[dict] = []
            project_errors: list[dict] = []
            for pname in project_names:
                try:
                    _, pid, rname = await resolve_project(client, token, pname, hub_id=hub_id)
                    resolved_projects.append({"id": pid, "name": rname})
                except ValueError as e:
                    project_errors.append({"project": pname, "error": str(e)})

            invalid_email_set: set[str] = set()
            if resolved_projects:
                app_token = await get_app_token()
                account_users = await _get_account_users_map(client, account_id, app_token)
                for email in user_emails:
                    if email not in account_users:
                        invalid_email_set.add(email)

            valid_emails = [e for e in user_emails if e not in invalid_email_set]

            results: list[dict] = []
            for email in invalid_email_set:
                for proj in resolved_projects:
                    role_name = role_overrides.get(proj["name"].lower(), default_role)
                    results.append({
                        "user": email, "project": proj["name"], "role": role_name,
                        "status": "error", "message": "Not found in account roster",
                    })

            for pentry in project_errors:
                role_name = role_overrides.get(pentry["project"].lower(), default_role)
                for email in user_emails:
                    results.append({
                        "user": email, "project": pentry["project"], "role": role_name,
                        "status": "error", "message": f"Project not found: {pentry['project']}",
                    })

            api_calls_made = False
            for proj in resolved_projects:
                pid = proj["id"]
                pname = proj["name"]
                role_name = role_overrides.get(pname.lower(), default_role)

                role_map = await _fetch_project_roles(client, pid, hdrs)
                role_id = _resolve_role_id(role_name, role_map)
                if not role_id:
                    available = list(role_map.values()) or ["(none found)"]
                    for email in valid_emails:
                        results.append({
                            "user": email, "project": pname, "role": role_name,
                            "status": "error",
                            "message": f"Role '{role_name}' not found. Available: {available}",
                        })
                    continue

                members = await _get_project_members_map(client, pid, hdrs)
                bare_pid = _to_bare_id(pid)

                for email in valid_emails:
                    member = members.get(email)
                    if not member:
                        results.append({
                            "user": email, "project": pname, "role": role_name,
                            "status": "skipped", "message": "Not a member of this project",
                        })
                        continue

                    user_id = member.get("id") or member.get("userId") or member.get("autodeskId")
                    if not user_id:
                        results.append({
                            "user": email, "project": pname, "role": role_name,
                            "status": "error", "message": "Could not determine user ID from member record",
                        })
                        continue

                    if dry_run:
                        current_role_id = member.get("roleId") or member.get("role") or ""
                        current_role_name = role_map.get(current_role_id) or current_role_id or "(unknown)"
                        results.append({
                            "user": email, "project": pname, "role": role_name,
                            "status": "would_update",
                            "message": f"Would change role from '{current_role_name}' to '{role_name}'",
                        })
                        continue

                    api_calls_made = True
                    r = await client.patch(
                        f"{APS_BASE}/construction/admin/v1/projects/{bare_pid}/users/{user_id}",
                        headers=hdrs,
                        json={"roleIds": [role_id], "products": DEFAULT_PRODUCTS},
                    )
                    if r.is_success:
                        results.append({
                            "user": email, "project": pname, "role": role_name,
                            "status": "success", "message": "",
                        })
                    else:
                        body = _error_body(r)
                        results.append({
                            "user": email, "project": pname, "role": role_name,
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
                "audit_file": audit_file,
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
                        _, pid, rname = await resolve_project(client, token, pname, hub_id=hub_id)
                        resolved_projects_list.append({"id": pid, "name": rname})
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
            region = _norm_region(arguments)
            dry_run = arguments.get("dry_run", True)
            ref_email = arguments["reference_user_email"].lower().strip()
            target_emails = _norm_emails(arguments["target_user_emails"])

            hub_id, _ = await resolve_hub(client, token, region, hub_name=arguments.get("hub_name"))
            all_projs = await get_all_pages(
                client, f"{APS_BASE}/project/v1/hubs/{hub_id}/projects", hdrs
            )

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

            # Build role_overrides using project names — role names come directly from the API
            role_overrides: dict[str, str] = {}
            for entry in ref_project_roles:
                role_names = entry["role_names"]
                # Use the first role; warn below if the user holds multiple roles in a project
                role_overrides[entry["name"].lower()] = role_names[0] if role_names else "Viewer"

            results, warnings, api_calls_made = await _execute_bulk_assign(
                ref_project_roles, target_emails, "", role_overrides, dry_run
            )
            warnings.insert(0, f"Reference user '{ref_email}' found in {len(ref_project_roles)} projects.")
            for entry in ref_project_roles:
                if len(entry["role_names"]) > 1:
                    warnings.append(
                        f"Project '{entry['name']}': reference user holds multiple roles "
                        f"{entry['role_names']} — only '{entry['role_names'][0]}' was cloned."
                    )

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "clone_access")

            summary = {
                k: sum(1 for r in results if r["status"] == k)
                for k in ("success", "would_add", "already_member", "error")
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

            hub_id, _ = await resolve_hub(client, token, region, hub_name=arguments.get("hub_name"))
            account_id = _to_bare_id(hub_id)
            app_token = await get_app_token()

            # Find company by name across all account companies (HQ API)
            account_users = await _get_account_users_map(client, account_id, app_token)
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
                    _, pid, rname = await resolve_project(client, token, pname, hub_id=hub_id)
                    resolved_projects.append({"id": pid, "name": rname})
                except ValueError as e:
                    project_errors.append({"project": pname, "error": str(e)})

            results, warnings, api_calls_made = await _execute_bulk_assign(
                resolved_projects, company_emails, default_role, role_overrides, dry_run
            )
            warnings.insert(0, f"Found {len(company_emails)} users for company '{arguments['company_name']}'.")
            for pentry in project_errors:
                role_name = role_overrides.get(pentry["project"].lower(), default_role)
                for email in company_emails:
                    results.append({
                        "user": email, "project": pentry["project"], "role": role_name,
                        "status": "error", "message": f"Project not found: {pentry['project']}",
                    })

            audit_file = None
            if not dry_run and api_calls_made:
                audit_file = _write_audit_csv(results, "company_assign")

            summary = {
                k: sum(1 for r in results if r["status"] == k)
                for k in ("success", "would_add", "already_member", "error")
            }
            summary["total"] = len(results)
            return [TextContent(type="text", text=json.dumps({
                "dry_run": dry_run, "operation": "bulk_assign_company_users",
                "company": arguments["company_name"],
                "users_found": len(company_emails),
                "summary": summary, "results": results,
                "warnings": warnings, "audit_file": audit_file,
            }, indent=2))]

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
