"""
Check that all pending-ARCHIVE projects in archive_decisions.csv have no files.

For each row with Decision=ARCHIVE and ArchiveStatus != 'done':
  1. Look up project ID by name (from the hub project list)
  2. Recursively walk topFolders -> folders/contents until a file (type=items) is found
  3. Record has_files=True (with sample file path) or has_files=False

Concurrency: up to 8 projects in flight simultaneously.

Usage:
  python check_empty_projects.py
Outputs:
  project_files_check.csv  (Project, ProjectId, Status, FileCount, SampleFile, Error)
"""
import os
import csv
import time
import asyncio
from collections import defaultdict

import httpx

from archive_projects import (
    APS_BASE,
    CSV_PATH,
    _norm,
    build_project_index,
    get_access_token,
    list_all_projects,
    resolve_hub,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "project_files_check.csv")

CONCURRENCY = 8
REGION = "EMEA"


async def get_top_folders(client, token, hub_id, project_id):
    res = await client.get(
        f"{APS_BASE}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders",
        headers={"Authorization": f"Bearer {token}"},
    )
    res.raise_for_status()
    return res.json().get("data", [])


async def get_folder_contents(client, token, project_id, folder_id):
    res = await client.get(
        f"{APS_BASE}/data/v1/projects/{project_id}/folders/{folder_id}/contents",
        headers={"Authorization": f"Bearer {token}"},
    )
    res.raise_for_status()
    return res.json().get("data", [])


async def check_project(client, token, hub_id, project_name, project_id, sem):
    """Return dict with has_files, file_count, sample_file, error."""
    async with sem:
        try:
            top = await get_top_folders(client, token, hub_id, project_id)
        except httpx.HTTPStatusError as e:
            return {"status": "error", "file_count": 0, "sample": "", "error": f"topFolders HTTP {e.response.status_code}"}
        except Exception as e:  # noqa
            return {"status": "error", "file_count": 0, "sample": "", "error": f"topFolders: {e}"}

        file_count = 0
        sample = ""

        # BFS through folders, stop early if we hit a threshold
        # But count all files across all top folders for a fuller picture.
        stack = [(f["id"], f["attributes"]["displayName"]) for f in top]

        while stack:
            folder_id, path = stack.pop()
            try:
                items = await get_folder_contents(client, token, project_id, folder_id)
            except httpx.HTTPStatusError as e:
                # 403 or 404 on a subfolder — skip but don't fail whole project
                continue
            except Exception:
                continue
            for it in items:
                if it.get("type") == "folders":
                    stack.append((it["id"], f"{path}/{it['attributes'].get('displayName', '?')}"))
                elif it.get("type") == "items":
                    file_count += 1
                    if not sample:
                        sample = f"{path}/{it['attributes'].get('displayName', '?')}"
                    # No early exit — we want the count
        return {
            "status": "has_files" if file_count > 0 else "empty",
            "file_count": file_count,
            "sample": sample,
            "error": "",
        }


async def main():
    print(f"[Init] Loading CSV: {CSV_PATH}")
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    pending = [r for r in rows
               if r.get("Decision") == "ARCHIVE"
               and r.get("ArchiveStatus") not in ("done", "skipped")]
    print(f"[Init] {len(rows)} total rows, {len(pending)} pending ARCHIVE rows to check")

    print("[Auth] Acquiring token...")
    token = await get_access_token()

    async with httpx.AsyncClient(timeout=60.0) as client:
        print(f"[Hub] Resolving hub for region={REGION}...")
        hub_id = await resolve_hub(client, token, REGION)
        print(f"[Hub] {hub_id}")

        print("[Projects] Fetching full project list...")
        projects = await list_all_projects(client, token, hub_id)
        idx = build_project_index(projects)
        print(f"[Projects] {len(projects)} projects in hub")

        # Match CSV rows to hub project IDs
        to_check = []
        not_found = []
        for r in pending:
            key = _norm(r["Project"])
            if key in idx:
                to_check.append((r["Project"], idx[key]["id"]))
            else:
                not_found.append(r["Project"])

        print(f"[Check] {len(to_check)} projects to check, {len(not_found)} not found in hub (skipped)")

        sem = asyncio.Semaphore(CONCURRENCY)
        t0 = time.time()

        async def run_one(name, pid):
            result = await check_project(client, token, hub_id, name, pid, sem)
            return name, pid, result

        tasks = [asyncio.create_task(run_one(n, p)) for n, p in to_check]

        results = []
        done_count = 0
        for coro in asyncio.as_completed(tasks):
            name, pid, res = await coro
            done_count += 1
            marker = "EMPTY" if res["status"] == "empty" else ("FILES" if res["status"] == "has_files" else "ERR")
            extra = f" ({res['file_count']} files)" if res["file_count"] else ""
            print(f"[{done_count}/{len(tasks)}] {marker:5} {name}{extra}")
            results.append({
                "Project": name,
                "ProjectId": pid,
                "Status": res["status"],
                "FileCount": res["file_count"],
                "SampleFile": res["sample"],
                "Error": res["error"],
            })

        # Also record not-found projects
        for name in not_found:
            results.append({
                "Project": name,
                "ProjectId": "",
                "Status": "not_in_hub",
                "FileCount": 0,
                "SampleFile": "",
                "Error": "",
            })

        dt = time.time() - t0
        print(f"\n[Done] Checked {len(to_check)} projects in {dt:.1f}s")

        # Sort: has_files first (attention needed), then empty, then errors/not-found
        order = {"has_files": 0, "error": 1, "empty": 2, "not_in_hub": 3}
        results.sort(key=lambda r: (order.get(r["Status"], 9), r["Project"]))

        # Write output
        with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["Project", "ProjectId", "Status", "FileCount", "SampleFile", "Error"])
            w.writeheader()
            w.writerows(results)
        print(f"[Out] Wrote {OUT_PATH}")

        # Summary
        by_status = defaultdict(int)
        for r in results:
            by_status[r["Status"]] += 1
        print("\n[Summary]")
        for status in ("has_files", "empty", "error", "not_in_hub"):
            print(f"  {status:12}  {by_status.get(status, 0)}")


if __name__ == "__main__":
    asyncio.run(main())
