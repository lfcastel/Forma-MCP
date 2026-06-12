# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP (Model Context Protocol) stdio server for managing an Autodesk Construction Cloud (ACC) environment via the Autodesk Platform Services (APS) API. Runs as a stdio subprocess registered in Claude Code's MCP config.

- **`aps_mcp.py`** — 26 tools for navigation, folder/file operations, permission auditing, and bulk user management

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

All 26 tools are registered via `@app.call_tool`. The key layers:

- **Name → ID resolution**: `resolve_hub()`, `resolve_project()` (fuzzy matching: exact first, then partial), `_resolve_folder_with_hub()` (recursive path traversal)
- **Pagination**: `get_all_pages()` handles limit/offset + `meta.pagination` endpoints (200 items/page). `get_all_folder_contents()` handles the Data Management `folders/{id}/contents` endpoint, which instead pages via JSON:API `links.next` — every folder-listing/lookup site (`list_folder_contents`, folder-path resolution, file lookups in `rename_file`/`move_file`, `find_files`/`find_folder`, permission walker) routes through it so folders with >200 items aren't truncated
- **Rate/quota limits (429)**: `_request_with_retry()` retries transient 429s with a short capped back-off, but fails fast with `APSQuotaError` on a hard quota ("Quota limit exceeded") or once retries are exhausted. `call_tool()` wraps the whole dispatch and converts any 429 into a `CallToolResult` with `isError=True` and a readable `quota_exceeded` JSON body (incl. `retry_after_seconds`) — so the client LLM is told it can't proceed (and why) instead of hanging, and the failure is flagged at the protocol level rather than looking like a successful call
- **Permission tree walker**: `_walk_folder_tree()` recurses with an asyncio semaphore to limit concurrency; `_get_folder_perms()` fetches per-folder
- **Bulk user tools**: `_execute_bulk_assign()` is the shared core for `bulk_assign_users`, `clone_user_access`, and `bulk_assign_company_users`. All bulk tools default to `dry_run=True` and generate a timestamped audit CSV when run live.

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
