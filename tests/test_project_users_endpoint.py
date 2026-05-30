"""
Quick integration test: call GET construction/admin/v1/projects/{projectId}/users
and print the roles found on each user.

Run from the repo root with:
    python tests/test_project_users_endpoint.py

Reads the cached 3-legged token from tokens.json (written by aps_mcp.py).
Make sure the MCP server has been authorised at least once before running this.
"""

import sys
import os
import json
import time
import asyncio

import httpx

APS_BASE = "https://developer.api.autodesk.com"
PROJECT_ID = ""  # fill in your own project ID (with "b." prefix) before running
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "tokens.json")


def _load_token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sys.exit("tokens.json not found or invalid — run the MCP server once to authorise first.")

    expires_at = data.get("expires_at", 0)
    if time.time() > expires_at - 60:
        sys.exit("Cached token has expired — trigger any MCP tool call first to refresh it.")

    return data["access_token"]


async def main():
    token = _load_token()
    bare_id = PROJECT_ID.removeprefix("b.")
    url = f"{APS_BASE}/construction/admin/v1/projects/{bare_id}/users"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": 200, "offset": 0}
    all_users = []

    async with httpx.AsyncClient() as client:
        while True:
            r = await client.get(url, headers=headers, params=params)
            print(f"GET {url}  →  {r.status_code}")
            if not r.is_success:
                print("Response body:", r.text)
                break
            data = r.json()
            users = data.get("results", data.get("data", []))
            all_users.extend(users)
            if len(users) < params["limit"]:
                break
            params["offset"] += params["limit"]

    print(f"\nTotal users returned: {len(all_users)}\n")

    role_map: dict[str, str] = {}
    for user in all_users:
        email = user.get("email", "(no email)")
        roles = user.get("roles", [])
        role_names = [r.get("name", r.get("id", "?")) for r in roles]
        print(f"  {email}: {role_names}")
        for role in roles:
            rid = role.get("id", "")
            rname = role.get("name", "")
            if rid and rname:
                role_map[rid] = rname

    print(f"\nDistinct roles found ({len(role_map)}):")
    for rid, rname in role_map.items():
        print(f"  {rid}  →  {rname}")


if __name__ == "__main__":
    asyncio.run(main())
