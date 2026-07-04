"""
Assign the project role "Internal" to a single user on every ACTIVE ACC project.

Performs the exact same operation as the aps_mcp `bulk_assign_users` tool, per project:
  POST construction/admin/v2/projects/{projectId}/users:import
  body: {"users":[{"email", "products":[docs,insight member], "roleIds":[<Internal>]}], ...}
...but is driven off the authoritative active-project list (active_projects.json, from the
ACC Admin API) keyed on project IDs, so it lists projects ONCE (no per-name re-listing that
trips Autodesk's rate limiter) and never touches archived projects.

Idempotent: if the target already holds the Internal role on a project, it is skipped.

Modes:
  python assign_internal_role.py --dry-run            (default; READ-ONLY preview)
  python assign_internal_role.py --execute --limit 1  (canary: write to 1 project)
  python assign_internal_role.py --execute            (full run)

Writes a timestamped audit CSV. Reuses tokens.json (refresh only; no browser).
"""
import os
import csv
import json
import time
import base64
import asyncio
import argparse
from datetime import datetime, timezone

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "tokens.json")
ACTIVE_FILE = os.path.join(HERE, "active_projects.json")

TARGET_EMAIL = "nena.vanleekwyck@arcade-eng.com"
ROLE_NAME = "Internal"
APS_BASE = "https://developer.api.autodesk.com"

# Same product access the MCP bulk tool grants; required so the role assignment sticks.
DEFAULT_PRODUCTS = [
    {"key": "docs", "access": "member"},
    {"key": "insight", "access": "member"},
]

CONCURRENCY = 5


# --------------------------------------------------------------------------- creds
def _load_aps_creds() -> tuple[str, str]:
    cid = os.environ.get("APS_CLIENT_ID")
    csec = os.environ.get("APS_CLIENT_SECRET")
    if cid and csec:
        return cid, csec
    with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as f:
        cfg = json.load(f)

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
    raise SystemExit("APS_CLIENT_ID/SECRET not found in env or ~/.claude.json")


APS_CLIENT_ID, APS_CLIENT_SECRET = _load_aps_creds()


# --------------------------------------------------------------------------- token
async def get_access_token() -> str:
    now = time.time()
    with open(TOKEN_FILE) as f:
        stored = json.load(f)
    if stored.get("access_token") and now < stored.get("expires_at", 0) - 60:
        return stored["access_token"]
    rt = stored.get("refresh_token")
    if not rt:
        raise SystemExit("Access token expired and no refresh_token; re-auth via make_project_admin.py first.")
    creds = base64.b64encode(f"{APS_CLIENT_ID}:{APS_CLIENT_SECRET}".encode()).decode()
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{APS_BASE}/authentication/v2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Authorization": f"Basic {creds}"},
            data={"grant_type": "refresh_token", "refresh_token": rt},
        )
        r.raise_for_status()
        data = r.json()
    stored = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", rt),
        "expires_at": now + data.get("expires_in", 3600),
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(stored, f, indent=2)
    return stored["access_token"]


def hdr(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _req(client, method, url, tok, **kw):
    """HTTP with 429/5xx + transient network-error retry and exponential backoff."""
    delay = 2.0
    last_exc = None
    for attempt in range(8):
        try:
            r = await client.request(method, url, headers=hdr(tok), **kw)
        except (httpx.TransportError, httpx.HTTPError, OSError) as e:
            # transient DNS/connection/read errors -> back off and retry
            last_exc = e
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            ra = r.headers.get("Retry-After")
            wait = float(ra) if (ra and ra.isdigit()) else delay
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        return r
    if last_exc is not None:
        raise last_exc
    return r  # last response (still failing)


# --------------------------------------------------------------------------- project ops
async def fetch_members(client, tok, bare_id):
    """Return (role_map rid->name, members_by_email) by paging the Admin API users list."""
    role_map, members = {}, {}
    params = {"limit": 200, "offset": 0}
    while True:
        r = await _req(client, "GET",
                       f"{APS_BASE}/construction/admin/v1/projects/{bare_id}/users",
                       tok, params=params)
        if not r.is_success:
            raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {r.text[:200]}",
                                        request=r.request, response=r)
        data = r.json()
        users = data if isinstance(data, list) else data.get("results", data.get("data", []))
        for u in users:
            email = (u.get("email") or "").lower()
            if email:
                members[email] = u
            for role in u.get("roles", []):
                rid, rname = role.get("id", ""), role.get("name", "")
                if rid and rname:
                    role_map[rid] = rname
        if len(users) < params["limit"]:
            break
        params["offset"] += params["limit"]
    return role_map, members


def resolve_role_id(role_map, name):
    nl = name.lower()
    for rid, rname in role_map.items():
        if rname.lower() == nl:
            return rid
    return None


async def process_project(client, tok, proj, execute, suppress_emails, sem):
    """Return an audit row dict for one project."""
    pid = proj["id"]
    bare = pid.removeprefix("b.")
    pname = proj["name"]
    row = {"project_id": pid, "project": pname, "user": TARGET_EMAIL,
           "role": ROLE_NAME, "status": "", "message": ""}
    async with sem:
        try:
            role_map, members = await fetch_members(client, tok, bare)
        except Exception as e:
            row["status"] = "error"; row["message"] = f"members fetch failed: {type(e).__name__}: {e}"
            return row

        role_id = resolve_role_id(role_map, ROLE_NAME)
        if not role_id:
            row["status"] = "error"
            row["message"] = f"Role '{ROLE_NAME}' not found. Available: {sorted(set(role_map.values())) or '(none)'}"
            return row

        existing = members.get(TARGET_EMAIL.lower())
        if existing:
            has_role = any((r.get("name") or "").lower() == ROLE_NAME.lower()
                           for r in existing.get("roles", []))
            if has_role:
                row["status"] = "skip_already_has_role"
                row["message"] = "already a member with Internal role"
                return row

        if not execute:
            row["status"] = "would_add"
            row["message"] = ("member, would add role" if existing else "not a member, would import with Internal role")
            return row

        entry = {"email": TARGET_EMAIL, "products": DEFAULT_PRODUCTS, "roleIds": [role_id]}
        try:
            r = await _req(client, "POST",
                           f"{APS_BASE}/construction/admin/v2/projects/{bare}/users:import",
                           tok, json={"users": [entry], "suppressAdministrativeEmails": suppress_emails})
        except Exception as e:
            row["status"] = "error"; row["message"] = f"import request failed: {type(e).__name__}: {e}"
            return row
        if r.is_success:
            resp = r.json()
            items = resp if isinstance(resp, list) else resp.get("results", [])
            ok = items[0].get("success", True) if items else True
            row["status"] = "success" if ok else "error"
            row["message"] = (items[0].get("message") or items[0].get("error") or "") if items else ""
        else:
            try:
                body = r.json()
            except Exception:
                body = r.text
            row["status"] = "error"; row["message"] = f"HTTP {r.status_code}: {body}"
        return row


async def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="Process only the first N active projects (canary).")
    ap.add_argument("--send-emails", action="store_true",
                    help="Send ACC administrative invite emails (default: suppressed).")
    args = ap.parse_args()
    execute = args.execute
    suppress_emails = not args.send_emails

    active = json.load(open(ACTIVE_FILE, encoding="utf-8"))
    if args.limit:
        active = active[: args.limit]

    mode = "EXECUTE (writing)" if execute else "DRY-RUN (read-only)"
    print(f"[Mode] {mode} | target={TARGET_EMAIL} | role={ROLE_NAME} | "
          f"projects={len(active)} | suppress_emails={suppress_emails}", flush=True)

    tok = await get_access_token()
    sem = asyncio.Semaphore(CONCURRENCY)
    rows = []
    async with httpx.AsyncClient(timeout=60) as client:
        tasks = [process_project(client, tok, p, execute, suppress_emails, sem) for p in active]
        done = 0
        for coro in asyncio.as_completed(tasks):
            rows.append(await coro)
            done += 1
            if done % 25 == 0 or done == len(tasks):
                print(f"  [{done}/{len(tasks)}] processed", flush=True)

    from collections import Counter
    tally = Counter(r["status"] for r in rows)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    audit = os.path.join(HERE, f"audit_assign_internal_{'exec' if execute else 'dryrun'}_{ts}.csv")
    with open(audit, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    print("\n===== SUMMARY =====", flush=True)
    for k, v in sorted(tally.items()):
        print(f"  {k:26s} {v}", flush=True)
    errors = [r for r in rows if r["status"] == "error"]
    if errors:
        print(f"\n  first errors ({min(10, len(errors))} of {len(errors)}):", flush=True)
        for r in errors[:10]:
            print(f"    - {r['project']}: {r['message'][:160]}", flush=True)
    print(f"\nAudit CSV: {audit}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
