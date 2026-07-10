# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP (Model Context Protocol) stdio server for managing an Autodesk Construction Cloud (ACC) environment via the Autodesk Platform Services (APS) API. Runs as a stdio subprocess registered in Claude Code's MCP config.

- **`aps_mcp.py`** — 47 tools for navigation, folder/file operations, permission auditing, bulk user management, hub-level directory onboarding, bulk folder reorg, and ACC Issues (list/get/create/update + types & custom-field metadata)

## Commands

```bash
# Activate virtual environment (Windows)
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_bulk_tools.py -v

# Run a single test by name
python -m pytest tests/test_bulk_tools.py::test_bulk_assign_dry_run_returns_would_add -v
```

`pytest.ini` sets `asyncio_mode = auto` and `testpaths = tests`.

## Architecture

### Authentication

- **3-legged OAuth** (user context): navigation, folder operations, folder permissions. Tokens cached in `tokens.json` with auto-refresh. First use opens a browser and spins up a local callback server on port 8080.
- **2-legged OAuth** (client credentials): account-level user/company lookups. Token cached in memory per process.

Environment variables required: `APS_CLIENT_ID`, `APS_CLIENT_SECRET`.

### `aps_mcp.py` (2,000+ lines)

All 47 tools are registered via `@app.call_tool`. The key layers:

- **Name → ID resolution**: `resolve_hub()`, `resolve_project()` (fuzzy matching: exact first, then partial — the internal name-in-one-hub resolver used by the bulk tools), `_resolve_folder_with_hub()` (recursive path traversal)
- **Project reference resolution** (`resolve_project` tool + the shared `project` param): `_resolve_project_ref()` resolves a project from a **name, a bare/`b.`-prefixed ID, or a full ACC URL** and returns it *with its owning hub* in one shot. `_extract_uuid()` (regex) routes the query: a UUID → **ID/URL fast path** (`_resolve_by_id()` calls ACC Admin `GET /construction/admin/v1/projects/{bare_uuid}?fields=accountId,name,platform` with a **2-legged** token and **no `Region` header** — auto-routed across regions — then maps `accountId` → hub via `list_hubs`; a 404 is a clean not-found with no region retry, and a 401/403 scope failure falls back to `_resolve_id_by_enumeration()`); otherwise `_resolve_by_name()` **fans out across every hub** (the wrong-hub fix for the two EMEA hubs) and partial-matches, returning `{ambiguous, candidates}` on collision instead of guessing. `b.` is stripped for the Admin call and always re-added on output (`_to_bare_id()`/`_ensure_b_prefix()`). Every single-project tool takes an optional `project` param (name/ID/URL, preferred) alongside the backward-compatible `project_name` alias, both funnelled through `_resolve_project_arg()` at the tool's entry so a pasted URL pins the exact hub with no region guesswork. The multi-project bulk-*user* tools (`project_names` arrays) still resolve names within one hub and are unchanged.
- **Pagination**: `get_all_pages()` handles limit/offset + `meta.pagination` endpoints (200 items/page). `get_all_folder_contents()` handles the Data Management `folders/{id}/contents` endpoint, which instead pages via JSON:API `links.next` — every folder-listing/lookup site (`list_folder_contents`, folder-path resolution, file lookups in `rename_file`/`move_file`, `find_files`/`find_folder`, permission walker) routes through it so folders with >200 items aren't truncated
- **Rate/quota limits (429), 503, token expiry (401)**: `_request_with_retry()` retries transient 429s/503s with a short capped back-off, but fails fast with `APSQuotaError` on a hard quota ("Quota limit exceeded") or once retries are exhausted. With an `on_unauthorized` callback (used by the bulk folder tools via `_bearer_refresher`), a single 401 triggers a one-time token refresh — mutating a shared headers dict in place — then retries, so a long batch can outlive a token (`_force_refresh_access_token()` force-refreshes ignoring the cached-token window). `call_tool()` wraps the whole dispatch and converts any 429 into a `CallToolResult` with `isError=True` and a readable `quota_exceeded` JSON body (incl. `retry_after_seconds`) — so the client LLM is told it can't proceed (and why) instead of hanging, and the failure is flagged at the protocol level rather than looking like a successful call
- **Permission tree walker**: `_walk_folder_tree()` recurses with an asyncio semaphore to limit concurrency; `_get_folder_perms()` fetches per-folder
- **Recursive file walk**: `_resolve_start_folders()` returns the walk roots (a resolved `folder_path`, else the project's top folders) and `_walk_project_files()` recurses those roots (depth ≤ 8, best-effort — sub-folder listing errors are skipped) collecting files with full display-name paths, newest first. `find_files` passes a substring predicate; `list_all_files` passes none (lists everything). `find_folder` shares `_resolve_start_folders()` but keeps its own folder-only walk. `list_all_files` takes an optional `folder_path` — omit for the whole project, pass it for a subtree. `export_deliverables_manifest` reuses the same walker but returns a **plain-text, filename-only** manifest — deduplicated and sorted A→Z, all metadata (ids/paths/dates) stripped — so it stays token-lean for cross-checking against an external deliverable list (e.g. an Excel checklist); it takes an optional `extensions` filter (dots/case ignored) and the shared `folder_path`.
- **Bulk user tools**: `_execute_bulk_assign()` is the shared core for `bulk_assign_users`, `clone_user_access`, and `bulk_assign_company_users`. All bulk tools default to `dry_run=True` and generate a timestamped audit CSV when run live.
- **Hub-directory tools**: `list_account_companies`, `bulk_add_hub_users`, `bulk_add_hub_companies`, `deactivate_hub_users`, `deactivate_hub_companies` operate on the **account** member/company directory (not a project) — the onboarding step that precedes project assignment. All use the **2-legged** HQ API (`get_app_token()` + `_to_bare_id(hub_id)` = account id). Reads/idempotency come from `_get_account_users_map()` and the new `_get_account_companies()` (+ `_resolve_company_id()`); `bulk_add_hub_users` resolves `company_name` → `company_id` and errors if the company doesn't exist. Imports POST to `hq/v1/accounts/{id}/users/import` and `.../companies/import` **batched ≤50** via `_gather_bounded()`, mapping the `{success, failure, success_items, failure_items}` envelope back to per-row `added`/`error`/`already_exists` via `_import_item_key()`/`_import_item_error()`. Deactivation is a soft-offboard `PATCH .../{id}` `{status: inactive}` (no hard delete). All mutating tools default to `dry_run=True`, write a timestamped audit CSV live, and support `response_detail` via `_shape_bulk_response()`. `_write_audit_csv()` now takes the union of row keys so heterogeneous result rows serialize cleanly.
- **Bulk folder tools**: `bulk_list_folder_contents` (read-only audit; `children_of` lists every immediate subfolder's contents in one call; `include_naming_standard` tags each subfolder row with its `naming_standard_ids` straight from the same `contents` payload — free, so an orchestrator gets listing + conventions in one sweep instead of also calling `audit_folder_naming_standards`), `bulk_create_folders` (idempotent; `skip_if_exists` lists each parent once and reports `exists` instead of duplicating), `bulk_delete_folders` (file-safe soft-delete via `_subtree_file_info()`, which counts/samples files in a subtree — `skipped_has_files` rows surface the stuck cloud-workshared models). Deliberately policy-free primitives: the orchestrating prompt decides which folders are standard/legacy/anomaly. Both mutating tools default to `dry_run=True`, take `continue_on_error` (default true) and a `max_concurrency` cap (default 8, via `_gather_bounded()`), and share the single tools' create/hide payload builders (`_folder_create_payload`/`_folder_hide_payload`).
- **Naming-standards audit**: `audit_folder_naming_standards` is a read-only tree walk (`_walk_naming_standards()`, bounded by a shared semaphore) that reports each folder's assigned naming convention. A folder's standard lives in `attributes.extension.data.namingStandardIds` (read via `_naming_standard_ids()`), and every subfolder's value is already in its parent's `contents` listing — so the walk costs one listing per folder and **no** per-folder GET (the start folder is the lone exception: its own attributes come from a single folder GET, or from the `topFolders` payload when auditing the whole project). The summary groups folders by standard id (`by_standard`) and counts the gaps; `response_detail` (shared `_shape_bulk_response()`) treats a folder with no standard as the noteworthy/failure row (so `changes` returns only the gaps, `summary` surfaces them under `failures`). This reports the standard each folder *enforces*, not whether existing files comply.
- **Bulk move tools**: `bulk_move_files` and `bulk_move_folders` reparent items/folders via the shared `_reparent_payload()` (also used by the single `move_file`/`move_folder`). Both resolve+list unique source/destination folders once up front, then fan out the PATCHes bounded. Idempotent: a file/folder already at its destination is reported `already_there`. `bulk_move_files` surfaces a 403 on a file move as `skipped_unmovable` (cloud-workshared C4R models the API can't relocate) rather than a generic error; files can be addressed by raw `item_id` or by `source`+`name`.
- **Output verbosity (`response_detail`)**: `bulk_move_files`, `bulk_move_folders`, `bulk_delete_folders`, `bulk_list_folder_contents`, and `audit_folder_naming_standards` take `response_detail` ∈ {`summary`, `changes`, `full`} (default `changes`), applied as pure post-processing by the shared `_shape_bulk_response()` helper — it gates the `results` array only; `summary` counts are computed before filtering and never change. `full` echoes every row; `changes` keeps only the per-tool "noteworthy" rows (the things needing action — drops the success/no-op noise); `summary` omits `results` but still surfaces a `failures` array so locked/stuck rows are never lost (for the audit tool, failures travel in its always-present separate `errors` array, so `summary` simply omits `results`). The audit tool additionally takes `fields: ["files","subfolders"]` to restrict each row to those sections and emit lean file entries (id+name only) for URN harvesting. This is response-shaping only — it does not touch the move/delete/list logic or the 429/idempotency story.
- **Issues tools**: `list_issues`, `create_issue`, `list_issue_types`, `list_issue_attribute_definitions`, `list_issue_attribute_mappings` wrap the `construction/issues/v1/projects/{bareProjectId}/...` API (user-scoped 3-legged). Two dedicated helpers back them: `_resolve_issue_project()` runs the standard `_resolve_project_arg()` then `_to_bare_id()`s the result (the Issues API wants the bare UUID), and `_get_all_issues()` paginates the Issues envelope, which puts `pagination.totalResults` at the **top level** (not under `meta` like `get_all_pages`), so reusing `get_all_pages` would silently stop after the first page. Every call sends a constant `x-ads-region: {ISSUES_REGION}` header (`ISSUES_REGION = "EMEA"`, module-level — all Issues traffic is EMEA; one-line change if a US hub is added). `list_issues` maps friendly snake_case params onto `filter[...]`/`sortBy`/`fields` query keys (only when provided) and auto-paginates. `create_issue` **posts immediately (no `dry_run`, deliberately diverging from the other mutating tools)**, building the JSON body from only the supplied keys (snake_case → API camelCase), requiring `title`/`issue_subtype_id`/`status`, and returning the created issue or `{error, body}`. The API's "type"/"subtype" = the UI's "category"/"type"; `list_issue_types` (with `include=subtypes`, `page_limit=200`) is the source of a valid `issue_subtype_id`. The single-issue tools `get_issue` (GET) and `update_issue` (PATCH) both take either `issue_id` (UUID) or `display_id` (the friendly number) via the shared `_resolve_issue_ref()` helper (a `display_id` costs one `filter[displayId]` lookup). `update_issue` reuses the same snake_case→camelCase `body_map` as `create_issue` (all fields optional; errors if the body is empty) and posts immediately. **There is deliberately no `delete_issue` tool:** the ACC Issues API (v1) exposes no delete route — `deleted` is read-only on the PATCH body (a `{"deleted": true}` PATCH 400s), and a raw HTTP DELETE is rejected (403). Issues can only be deleted in the ACC UI; `list_issues` can still surface UI-deleted issues via `filter[deleted]`.

### Tests

Located in `tests/`. Uses `respx` for HTTP mocking and `unittest.mock.patch` to replace auth calls. `conftest.py` provides shared fixtures (fake tokens, IDs, mock API response bodies). Tests cover dry-run vs execute modes, role resolution edge cases, partial failures, and clone workflows.

## Key Documentation

- `README.md` — Setup, OAuth app registration, Claude Code config snippet, tools reference table, and Mermaid flowchart of all tools with API endpoints and auth modes

## Public Repo — Sensitive Data Check

This repository is **publicly visible**. Before committing or helping stage any file, verify it contains no sensitive data. Block and warn if any of the following are detected:

- **Credentials / secrets** — API keys, client secrets, tokens, passwords (including in `.env` files or any file not in `.gitignore`)
- **Real user data** — names, email addresses, or user IDs of actual people
- **Real Autodesk IDs** — hub IDs, project IDs, folder URNs, or account IDs that belong to a live environment
- **Internal URLs or hostnames** — ACC project links, internal dashboard URLs, VPN hostnames

The safe list (fake/placeholder data in tests, public API base URLs like `developer.api.autodesk.com`) is fine to commit. When in doubt, ask before staging.

## Documentation Maintenance

After **any** code change that impacts user-facing behaviour, update `README.md` before finishing. This includes, but is not limited to:

- **New or removed tools** — add/remove the row in the Tools reference table, the node in the Mermaid flowchart, and update the tool count in the repo layout section and in `CLAUDE.md`.
- **New or changed tool parameters** — update the relevant table rows, add or adjust example prompts, and update any callout notes (e.g. the multi-hub `hub_name` note).
- **Auth flow changes** — update the Authentication section and the Auth column in the tools tables.
- **New known limitations or resolved limitations** — add/remove entries in the Known Limitations section.
- **Architecture changes** — update the Architecture section in `CLAUDE.md` to match.
