"""
Unit tests for the pure helper functions in aps_mcp.py.
No HTTP calls, no auth — pure logic only.
"""
import os
import json
import pytest
import httpx
import respx
import asyncio
import tempfile
from unittest.mock import patch, AsyncMock

import aps_mcp
from conftest import ROLE_ID_VIEWER, ROLE_ID_EDITOR, ROLE_NAME_VIEWER, ROLE_NAME_EDITOR


# ---------------------------------------------------------------------------
# _resolve_role_id
# ---------------------------------------------------------------------------

ROLE_MAP = {
    ROLE_ID_VIEWER: ROLE_NAME_VIEWER,
    ROLE_ID_EDITOR: ROLE_NAME_EDITOR,
    "role-admin-id": "Project Admin",
}


def test_resolve_role_id_exact_match():
    result = aps_mcp._resolve_role_id("Viewer", ROLE_MAP)
    assert result == ROLE_ID_VIEWER


def test_resolve_role_id_case_insensitive():
    result = aps_mcp._resolve_role_id("viewer", ROLE_MAP)
    assert result == ROLE_ID_VIEWER

    result = aps_mcp._resolve_role_id("EDITOR", ROLE_MAP)
    assert result == ROLE_ID_EDITOR


def test_resolve_role_id_not_found():
    result = aps_mcp._resolve_role_id("NonExistentRole", ROLE_MAP)
    assert result is None


def test_resolve_role_id_empty_map():
    result = aps_mcp._resolve_role_id("Viewer", {})
    assert result is None


# ---------------------------------------------------------------------------
# _folder_name / _folder_name_matches  (Forma uses `name` as the UI label)
# ---------------------------------------------------------------------------

def test_folder_name_prefers_name_over_display_name():
    attrs = {"name": "Drawings", "displayName": "Drawings (internal)"}
    assert aps_mcp._folder_name(attrs) == "Drawings"


def test_folder_name_falls_back_to_display_name():
    attrs = {"displayName": "Only Display"}
    assert aps_mcp._folder_name(attrs) == "Only Display"


def test_folder_name_empty_when_neither_present():
    assert aps_mcp._folder_name({}) == ""


def test_fmt_folder_returns_name():
    folder = {"id": "urn:folder:1", "attributes": {"name": "Drawings", "displayName": "Drawings (internal)"}}
    assert aps_mcp._fmt_folder(folder) == {"id": "urn:folder:1", "name": "Drawings", "type": "folder"}


def test_folder_name_matches_either_label_case_insensitive():
    attrs = {"name": "Drawings", "displayName": "Drawings (internal)"}
    # matches on the UI `name`
    assert aps_mcp._folder_name_matches(attrs, "drawings")
    # also still matches a path typed from the old `displayName`
    assert aps_mcp._folder_name_matches(attrs, "drawings (internal)")
    # non-match
    assert not aps_mcp._folder_name_matches(attrs, "models")


# ---------------------------------------------------------------------------
# _naming_standard_ids  (folder's assigned naming convention)
# ---------------------------------------------------------------------------

def test_naming_standard_ids_reads_from_extension_data():
    attrs = {"extension": {"data": {"namingStandardIds": ["std-a"]}}}
    assert aps_mcp._naming_standard_ids(attrs) == ["std-a"]


def test_naming_standard_ids_empty_when_absent():
    assert aps_mcp._naming_standard_ids({"name": "f"}) == []
    assert aps_mcp._naming_standard_ids({"extension": {"data": {}}}) == []


def test_naming_standard_ids_drops_falsy_entries():
    attrs = {"extension": {"data": {"namingStandardIds": ["", None, "std-b"]}}}
    assert aps_mcp._naming_standard_ids(attrs) == ["std-b"]


# ---------------------------------------------------------------------------
# _write_audit_csv
# ---------------------------------------------------------------------------

def test_write_audit_csv_creates_file(tmp_path, monkeypatch):
    # Redirect the output directory to tmp_path so we don't litter the project
    monkeypatch.setattr(
        aps_mcp, "__file__",
        str(tmp_path / "aps_mcp.py"),
    )

    rows = [
        {"user": "alice@example.com", "project": "Test Project", "role": "Viewer", "status": "success", "message": ""},
        {"user": "bob@example.com",   "project": "Test Project", "role": "Viewer", "status": "error",   "message": "HTTP 403"},
    ]
    filepath = aps_mcp._write_audit_csv(rows, "test_op")

    assert os.path.isfile(filepath)
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    assert "alice@example.com" in content
    assert "bob@example.com" in content
    assert "HTTP 403" in content
    # Header row must be present
    assert "user" in content
    assert "status" in content


def test_write_audit_csv_empty_rows_still_returns_path(tmp_path, monkeypatch):
    monkeypatch.setattr(aps_mcp, "__file__", str(tmp_path / "aps_mcp.py"))
    filepath = aps_mcp._write_audit_csv([], "empty_op")
    # Should return a path (file may not exist if rows is empty — that's fine)
    assert isinstance(filepath, str)
    assert "empty_op" in filepath


# ---------------------------------------------------------------------------
# _to_bare_id
# ---------------------------------------------------------------------------

def test_to_bare_id_strips_prefix():
    assert aps_mcp._to_bare_id("b.abc123") == "abc123"


def test_to_bare_id_no_prefix_unchanged():
    assert aps_mcp._to_bare_id("abc123") == "abc123"


def test_to_bare_id_double_prefix_strips_one():
    assert aps_mcp._to_bare_id("b.b.abc123") == "b.abc123"


# ---------------------------------------------------------------------------
# _extract_response_items
# ---------------------------------------------------------------------------

def test_extract_response_items_bare_list():
    assert aps_mcp._extract_response_items([1, 2, 3]) == [1, 2, 3]


def test_extract_response_items_results_key():
    assert aps_mcp._extract_response_items({"results": ["a", "b"]}) == ["a", "b"]


def test_extract_response_items_data_key():
    assert aps_mcp._extract_response_items({"data": ["x"]}) == ["x"]


def test_extract_response_items_unknown_keys():
    assert aps_mcp._extract_response_items({"other": [1]}) == []


# ---------------------------------------------------------------------------
# _norm_emails / _norm_region
# ---------------------------------------------------------------------------

def test_norm_emails_lowercases_and_strips():
    assert aps_mcp._norm_emails(["  ALICE@EXAMPLE.COM  ", "Bob@EXAMPLE.COM"]) == ["alice@example.com", "bob@example.com"]


def test_norm_region_defaults_to_emea():
    assert aps_mcp._norm_region({}) == "EMEA"


def test_norm_region_uppercases():
    assert aps_mcp._norm_region({"region": "us"}) == "US"


# ---------------------------------------------------------------------------
# _request_with_retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_with_retry_retries_on_429():
    call_count = 0

    async def fake_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient() as client:
        with patch.object(client, "get", side_effect=fake_get), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            r = await aps_mcp._request_with_retry(client, "get", "https://example.com")

    assert r.status_code == 200
    assert call_count == 2


@pytest.mark.asyncio
async def test_request_with_retry_raises_after_max_retries():
    # Persistent 429s (transient wording, no quota) eventually fail fast with a
    # clear APSQuotaError rather than returning the raw 429 for callers to handle.
    async def always_429(url, **kwargs):
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})

    async with httpx.AsyncClient() as client:
        with patch.object(client, "get", side_effect=always_429), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(aps_mcp.APSQuotaError):
                await aps_mcp._request_with_retry(client, "get", "https://example.com", max_retries=2)
