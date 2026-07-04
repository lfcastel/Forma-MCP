"""
Grant a single user Project Admin (project-level admin access) on every ACC project.

Reuses the OAuth 3-legged pattern from aps_mcp.py / archive_projects.py (tokens.json).

Modes:
  python make_project_admin.py --discover
      READ-ONLY. Resolve EMEA hub, list active (non-archived) projects, and for each
      project GET its members. Reports:
        - total active projects
        - for each project: whether TARGET_EMAIL is already a member and their current
          accessLevels
        - a sample of the exact accessLevels string(s) used by existing admins in the hub
          (so we confirm the real enum value before writing anything)
      Writes discovery_report.json. Makes NO changes.

  python make_project_admin.py --execute [--limit N] [--access-value projectAdmin]
      Grant admin to TARGET_EMAIL on each active project:
        - if already admin  -> skip
        - if member (not admin) -> PATCH .../users/{userId} accessLevels=[access_value]
        - if not a member -> POST .../users:import with accessLevels=[access_value]
      Checkpointed via audit CSV (resumable): rows already 'done' are skipped.

Always run --discover first, confirm the enum + counts, then --execute --limit 1 as a
canary and verify before the full run.
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
TOKEN_FILE = os.path.join(HERE, "tokens.json")
REPORT_FILE = os.path.join(HERE, "discovery_report.json")

TARGET_EMAIL = "nena.vanleekwyck@arcade-eng.com"

# Product access granted to NEW members we import (existing members are left untouched).
DEFAULT_PRODUCTS = [
    {"key": "docs", "access": "member"},
    {"key": "insight", "access": "member"},
]


# ---------------------------------------------------------------------------
# Credentials (env or ~/.claude.json mcpServers, same as archive_projects.py)
# ---------------------------------------------------------------------------
def _load_aps_creds() -> tuple[str, str]:
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


# ---------------------------------------------------------------------------
# 3-legged token handling
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


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Hub / project / member helpers
# ---------------------------------------------------------------------------
async def resolve_hub(client: httpx.AsyncClient, token: str, region: str = "EMEA") -> str:
    res = await client.get(f"{APS_BASE}/project/v1/hubs", headers=auth_headers(token))
    res.raise_for_status()
    hubs = res.json().get("data", [])
    for h in hubs:
        if (h["attributes"].get("region") or "").upper() == region.upper():
            return h["id"]
    if not hubs:
        raise SystemExit("No hubs found for this account.")
    return hubs[0]["id"]


async def list_all_projects(client: httpx.AsyncClient, token: str, hub_id: str) -> list[dict]:
    projects: list[dict] = []
    url = f"{APS_BASE}/project/v1/hubs/{hub_id}/projects"
    params: dict | None = {"page[limit]": 200}
    while url:
        res = await client.get(url, headers=auth_headers(token), params=params)
        res.raise_for_status()
        body = res.json()
        projects.extend(body.get("data", []))
        url = body.get("links", {}).get("next", {}).get("href")
        params = None
    return projects


async def get_project_members(client: httpx.AsyncClient, token: str, project_id: str) -> list[dict]:
    """GET all members of a project via the Admin API."""
    bare = project_id.removeprefix("b.")
    members: list[dict] = []
    limit, offset = 200, 0
    while True:
        res = await client.get(
            f"{APS_BASE}/construction/admin/v1/projects/{bare}/users",
            headers=auth_headers(token),
            params={"limit": limit, "offset": offset},
        )
        res.raise_for_status()
        data = res.json()
        page = data.get("results", data.get("data", [])) if isinstance(data, dict) else data
        members.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return members


def _is_admin(member: dict, access_values: set[str]) -> bool:
    levels = {str(a).lower() for a in (member.get("accessLevels") or [])}
    return bool(levels & {a.lower() for a in access_values})


# ---------------------------------------------------------------------------
# Discovery (read-only)
# ---------------------------------------------------------------------------
async def discover(region: str) -> None:
    print("[Auth] Acquiring 3-legged token...", flush=True)
    token = await get_access_token()
    print("[Auth] Token OK", flush=True)

    async with httpx.AsyncClient(timeout=40.0) as client:
        hub_id = await resolve_hub(client, token, region)
        print(f"[Hub] {hub_id}", flush=True)

        all_projects = await list_all_projects(client, token, hub_id)
        active = [p for p in all_projects
                  if (p["attributes"].get("status") or "").lower() == "active"]
        print(f"[Projects] {len(all_projects)} total, {len(active)} active", flush=True)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hub_id": hub_id,
            "target_email": TARGET_EMAIL,
            "total_projects": len(all_projects),
            "active_projects": len(active),
            "observed_access_level_values": {},   # value -> count seen across all members
            "projects": [],
        }

        access_value_counts: dict[str, int] = {}
        need_add = need_update = already_admin = errors = 0

        for i, p in enumerate(active, 1):
            pid = p["id"]
            pname = p["attributes"]["name"]
            entry = {"id": pid, "name": pname}
            try:
                members = await get_project_members(client, token, pid)
            except httpx.HTTPStatusError as e:
                entry["error"] = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
                errors += 1
                report["projects"].append(entry)
                print(f"[{i}/{len(active)}] ERROR {pname}: {entry['error']}", flush=True)
                continue

            # Tally every accessLevels value we see (to learn the real enum)
            for m in members:
                for lvl in (m.get("accessLevels") or []):
                    access_value_counts[str(lvl)] = access_value_counts.get(str(lvl), 0) + 1

            target = next(
                (m for m in members
                 if (m.get("email") or "").lower() == TARGET_EMAIL.lower()), None)

            if target is None:
                entry["target_status"] = "not_member"
                need_add += 1
            else:
                entry["target_user_id"] = target.get("id") or target.get("autodeskId") or target.get("userId")
                entry["target_access_levels"] = target.get("accessLevels") or []
                # Treat any of these as "admin" for reporting; confirmed value chosen later.
                if _is_admin(target, {"projectAdmin", "admin", "project_admin"}):
                    entry["target_status"] = "already_admin"
                    already_admin += 1
                else:
                    entry["target_status"] = "member_needs_admin"
                    need_update += 1

            report["projects"].append(entry)
            print(f"[{i}/{len(active)}] {entry['target_status']:20s} {pname}", flush=True)

        report["observed_access_level_values"] = access_value_counts
        report["summary"] = {
            "need_add": need_add,
            "need_update": need_update,
            "already_admin": already_admin,
            "errors": errors,
        }

        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        print("\n===== DISCOVERY SUMMARY =====", flush=True)
        print(f"Active projects:            {len(active)}", flush=True)
        print(f"  Nena not a member:        {need_add}", flush=True)
        print(f"  Nena member, needs admin: {need_update}", flush=True)
        print(f"  Nena already admin:       {already_admin}", flush=True)
        print(f"  Member-list errors:       {errors}", flush=True)
        print(f"\nObserved accessLevels values across all members (value: count):", flush=True)
        for v, c in sorted(access_value_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {v!r}: {c}", flush=True)
        print(f"\nFull report written to: {REPORT_FILE}", flush=True)


async def main():
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--discover", action="store_true", help="Read-only: report state, no changes")
    g.add_argument("--execute", action="store_true", help="Grant admin (writes)")
    parser.add_argument("--region", default="EMEA")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--access-value", default="projectAdmin",
                        help="Exact accessLevels enum value to grant (confirm via --discover first)")
    args = parser.parse_args()

    if args.discover:
        await discover(args.region)
    else:
        print("Execute mode is intentionally not wired up yet — run --discover first, "
              "confirm the accessLevels enum value + counts, then this script will be "
              "extended to write.", flush=True)
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
