"""
Archive ACC projects listed in archive_decisions.csv.

Reuses tokens.json + OAuth refresh pattern from aps_mcp.py.
Per row with Decision=ARCHIVE and ArchiveStatus != 'done':
  1. Look up project ID by name (cached list)
  2. PATCH /construction/admin/v1/projects/{id} with status=archived
  3. Update CSV row immediately (checkpoint = resumable)

Usage:
  python archive_projects.py --dry-run     # preview only, no PATCH
  python archive_projects.py --execute     # live run, archives projects
  python archive_projects.py --execute --limit 5   # only first 5 unprocessed rows
"""
import os
import sys
import csv
import json
import time
import base64
import asyncio
import argparse
import webbrowser
import urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "archive_decisions.csv")
TOKEN_FILE = os.path.join(HERE, "tokens.json")

def _load_aps_creds() -> tuple[str, str]:
    """Read APS credentials from env vars, falling back to ~/.claude.json MCP env."""
    cid = os.environ.get("APS_CLIENT_ID")
    csec = os.environ.get("APS_CLIENT_SECRET")
    if cid and csec:
        return cid, csec
    claude_cfg = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_cfg, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise SystemExit(f"No APS creds in env and could not read {claude_cfg}: {e}")
    # Walk the config: look for any mcpServers entry with APS_CLIENT_ID in its env
    def walk(node):
        if isinstance(node, dict):
            env = node.get("env")
            if isinstance(env, dict) and "APS_CLIENT_ID" in env and "APS_CLIENT_SECRET" in env:
                yield env["APS_CLIENT_ID"], env["APS_CLIENT_SECRET"]
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)
    for cid, csec in walk(cfg):
        return cid, csec
    raise SystemExit("APS_CLIENT_ID/SECRET not found in env or ~/.claude.json mcpServers")


APS_CLIENT_ID, APS_CLIENT_SECRET = _load_aps_creds()
APS_BASE = "https://developer.api.autodesk.com"
REDIRECT_URI = "http://localhost:8080/oauth/callback"
SCOPES = "data:read data:write data:create account:read account:write"


def _load_tokens() -> dict:
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tokens(data: dict) -> None:
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _build_auth_url() -> str:
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": APS_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    })
    return f"{APS_BASE}/authentication/v2/authorize?{params}"


def _wait_for_callback() -> str:
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

        def log_message(self, *a):
            pass

    server = HTTPServer(("localhost", 8080), Handler)
    server.timeout = 120
    while "code" not in received:
        server.handle_request()
    server.server_close()
    return received["code"]


async def _refresh_tokens(refresh_token: str) -> dict:
    creds = base64.b64encode(f"{APS_CLIENT_ID}:{APS_CLIENT_SECRET}".encode()).decode()
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{APS_BASE}/authentication/v2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        res.raise_for_status()
        return res.json()


async def _exchange_code(code: str) -> dict:
    creds = base64.b64encode(f"{APS_CLIENT_ID}:{APS_CLIENT_SECRET}".encode()).decode()
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


async def get_access_token() -> str:
    now = time.time()
    stored = _load_tokens()
    if stored.get("access_token") and now < stored.get("expires_at", 0) - 60:
        return stored["access_token"]
    if stored.get("refresh_token"):
        try:
            data = await _refresh_tokens(stored["refresh_token"])
            stored = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", stored["refresh_token"]),
                "expires_at": now + data.get("expires_in", 3600),
            }
            _save_tokens(stored)
            return stored["access_token"]
        except httpx.HTTPStatusError:
            pass
    print("[Auth] Opening browser for full re-auth...", flush=True)
    webbrowser.open(_build_auth_url())
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


async def resolve_hub(client: httpx.AsyncClient, token: str, region: str = "EMEA") -> str:
    res = await client.get(
        f"{APS_BASE}/project/v1/hubs",
        headers={"Authorization": f"Bearer {token}", "x-user-id": ""},
    )
    res.raise_for_status()
    hubs = res.json()["data"]
    for h in hubs:
        if h["attributes"].get("region", "").upper() == region.upper():
            return h["id"]
    return hubs[0]["id"]


async def list_all_projects(client: httpx.AsyncClient, token: str, hub_id: str) -> list[dict]:
    """Fetch every project in the hub, paginated."""
    projects: list[dict] = []
    url = f"{APS_BASE}/project/v1/hubs/{hub_id}/projects"
    params: dict = {"page[limit]": 200}
    while url:
        res = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        res.raise_for_status()
        body = res.json()
        projects.extend(body.get("data", []))
        next_link = body.get("links", {}).get("next", {}).get("href")
        url = next_link
        params = None
    return projects


def _norm(s: str) -> str:
    return "".join(s.lower().split())


def build_project_index(projects: list[dict]) -> dict[str, dict]:
    """Index projects by normalized name for fast lookup."""
    idx = {}
    for p in projects:
        name = p["attributes"]["name"]
        idx[_norm(name)] = {
            "id": p["id"],
            "name": name,
            "status": p["attributes"].get("status"),
        }
    return idx


_app_token_cache: dict = {"token": None, "expires_at": 0.0}


async def get_app_token(client: httpx.AsyncClient) -> str:
    """2-legged client_credentials token for HQ Admin API (app-only endpoints)."""
    now = time.time()
    if _app_token_cache["token"] and now < _app_token_cache["expires_at"] - 60:
        return _app_token_cache["token"]
    creds = base64.b64encode(f"{APS_CLIENT_ID}:{APS_CLIENT_SECRET}".encode()).decode()
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
    return data["access_token"]


async def archive_project(client: httpx.AsyncClient, app_token: str, hub_id: str, project_id: str) -> tuple[bool, str]:
    """PATCH the project to status=archived via HQ Admin API. Returns (success, message)."""
    account_id = hub_id.removeprefix("b.")
    bare_pid = project_id.removeprefix("b.")
    url = f"{APS_BASE}/hq/v1/accounts/{account_id}/projects/{bare_pid}"
    res = await client.patch(
        url,
        headers={
            "Authorization": f"Bearer {app_token}",
            "Content-Type": "application/json",
        },
        json={"status": "archived"},
    )
    if 200 <= res.status_code < 300:
        return True, f"HTTP {res.status_code}"
    return False, f"HTTP {res.status_code}: {res.text[:300]}"


def read_csv_rows() -> tuple[list[str], list[dict]]:
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames)
    # Ensure ArchiveStatus + ArchivedAt columns exist
    if "ArchiveStatus" not in fieldnames:
        fieldnames.append("ArchiveStatus")
        for r in rows:
            r["ArchiveStatus"] = ""
    if "ArchivedAt" not in fieldnames:
        fieldnames.append("ArchivedAt")
        for r in rows:
            r["ArchivedAt"] = ""
    return fieldnames, rows


def write_csv_rows(fieldnames: list[str], rows: list[dict]) -> None:
    tmp_path = CSV_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, CSV_PATH)


async def main():
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N projects this run")
    parser.add_argument("--region", default="EMEA")
    args = parser.parse_args()

    print(f"[Init] Loading CSV: {CSV_PATH}")
    fieldnames, rows = read_csv_rows()
    pending = [r for r in rows
               if r.get("Decision") == "ARCHIVE"
               and r.get("ArchiveStatus") not in ("done", "skipped")]
    print(f"[Init] {len(rows)} total rows, {len(pending)} pending ARCHIVE rows")
    if args.limit:
        pending = pending[: args.limit]
        print(f"[Init] Limited to first {len(pending)} this run")

    print("[Auth] Acquiring token...")
    token = await get_access_token()
    print("[Auth] Token OK")

    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"[Hub] Resolving hub for region={args.region}...")
        hub_id = await resolve_hub(client, token, args.region)
        print(f"[Hub] {hub_id}")

        print("[Projects] Fetching full project list (cached)...")
        projects = await list_all_projects(client, token, hub_id)
        idx = build_project_index(projects)
        print(f"[Projects] {len(projects)} total projects in hub")

        # First pass: lookup all project IDs and flag missing
        missing_names = []
        for r in pending:
            key = _norm(r["Project"])
            if key not in idx:
                missing_names.append(r["Project"])
        if missing_names:
            print(f"\n[Lookup] WARNING: {len(missing_names)} project(s) not found in hub:")
            for n in missing_names[:20]:
                print(f"   - {n}")
            if len(missing_names) > 20:
                print(f"   ... and {len(missing_names) - 20} more")

        if args.dry_run:
            print("\n[Dry-run] No PATCH calls will be made.")
            print(f"[Dry-run] Would archive {len(pending) - len(missing_names)} projects.")
            return

        # Execute mode
        print(f"\n[Execute] Starting archival of {len(pending)} projects...")
        success_count = 0
        fail_count = 0
        skip_count = 0
        for i, r in enumerate(pending, 1):
            name = r["Project"]
            key = _norm(name)
            if key not in idx:
                r["ArchiveStatus"] = "not_found"
                r["ArchivedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                write_csv_rows(fieldnames, rows)
                print(f"[{i}/{len(pending)}] SKIP (not in hub): {name}")
                skip_count += 1
                continue

            proj = idx[key]
            if proj.get("status") == "archived":
                r["ArchiveStatus"] = "already_archived"
                r["ArchivedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                write_csv_rows(fieldnames, rows)
                print(f"[{i}/{len(pending)}] ALREADY ARCHIVED: {name}")
                skip_count += 1
                continue

            # Refresh user token if close to expiry (used for hub/projects listing)
            stored = _load_tokens()
            if time.time() >= stored.get("expires_at", 0) - 60:
                token = await get_access_token()
            # Get/refresh 2-legged app token (used for HQ Admin PATCH)
            app_token = await get_app_token(client)

            ok, msg = await archive_project(client, app_token, hub_id, proj["id"])
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if ok:
                r["ArchiveStatus"] = "done"
                r["ArchivedAt"] = ts
                success_count += 1
                print(f"[{i}/{len(pending)}] OK: {name}  ({msg})")
            else:
                r["ArchiveStatus"] = "failed"
                r["ArchivedAt"] = ts
                r["Notes"] = (r.get("Notes", "") + f" | {msg}").strip(" |")
                fail_count += 1
                print(f"[{i}/{len(pending)}] FAIL: {name}  -- {msg}")
            write_csv_rows(fieldnames, rows)

            # Soft rate limit: 200ms between calls
            await asyncio.sleep(0.2)

        print(f"\n[Done] success={success_count} fail={fail_count} skip={skip_count}")


if __name__ == "__main__":
    asyncio.run(main())
