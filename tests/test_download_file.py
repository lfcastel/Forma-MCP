"""
Tests for the download_file tool (DM version -> OSS signeds3download -> S3 GET).
All APS calls are intercepted by respx; auth is patched.
"""
import hashlib
import json
import os
import urllib.parse

import httpx
import pytest
import respx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, HUB_ID, PROJECT_ID, PROJECT_NAME, HUB_RESPONSE, PROJECTS_RESPONSE,
)

BASE = aps_mcp.APS_BASE
FOLDER_ID = "urn:adsk.wipprod:fs.folder:co.docs"
ITEM_ID = "urn:adsk.wipprod:dm.lineage:item-abc"
V1 = "urn:adsk.wipprod:fs.file:vf.item-abc?version=1"
V2 = "urn:adsk.wipprod:fs.file:vf.item-abc?version=2"
OBJ_KEY = "0000-test-object.xlsx"
STORAGE = f"urn:adsk.objects:os.object:wip.dm.prod/{OBJ_KEY}"
SIGNED_ROUTE = f"{BASE}/oss/v2/buckets/wip.dm.prod/objects/{OBJ_KEY}/signeds3download"
S3_URL = "https://cdn.example-s3.test/obj?Signature=abc"
CONTENT = b"tracker-bytes" * 100



def _version(vid, n, storage=STORAGE, size=len(CONTENT)):
    return {
        "type": "versions", "id": vid,
        "attributes": {"displayName": "MIDP Tracker.xlsx", "versionNumber": n,
                       "fileType": "xlsx", "storageSize": size},
        "relationships": {"item": {"data": {"id": ITEM_ID}},
                          "storage": {"data": {"id": storage}} if storage else {}},
    }


def _parse(result):
    return json.loads(result[0].text)


def _mock_project(router):
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))


def _mock_signed_and_s3(router, sha1=None):
    signed = router.get(SIGNED_ROUTE).mock(return_value=httpx.Response(200, json={
        "status": "complete", "url": S3_URL, "size": len(CONTENT),
        "sha1": sha1 if sha1 is not None else hashlib.sha1(CONTENT).hexdigest(),
    }))
    s3 = router.get(S3_URL).mock(return_value=httpx.Response(200, content=CONTENT))
    return signed, s3


async def _call(args):
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        return await aps_mcp.call_tool("download_file", {"project_name": PROJECT_NAME, **args})


@respx.mock
async def test_download_by_folder_and_name_gets_tip_and_saves_file():
    _mock_project(respx)
    respx.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": FOLDER_ID, "type": "folders", "attributes": {"name": "Docs", "displayName": "Docs"}}]}))
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER_ID}/contents").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": ITEM_ID, "type": "items", "attributes": {"displayName": "MIDP Tracker.xlsx"}}]}))
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}/tip").mock(
        return_value=httpx.Response(200, json={"data": _version(V2, 2)}))
    signed, s3 = _mock_signed_and_s3(respx)

    data = _parse(await _call({"folder_path": "Docs", "file_name": "midp tracker.xlsx"}))

    assert data["status"] == "downloaded"
    assert data["version_number"] == 2
    assert data["size_bytes"] == len(CONTENT)
    assert data["saved_to"] == os.path.join(aps_mcp._downloads_dir(), "MIDP Tracker.v2.xlsx")
    with open(data["saved_to"], "rb") as f:
        assert f.read() == CONTENT
    if hasattr(os, "getuid"):
        assert os.stat(data["saved_to"]).st_mode & 0o077 == 0
    # The signed S3 URL is fetched without the APS bearer token.
    assert "authorization" not in s3.calls.last.request.headers
    assert signed.calls.last.request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"


@respx.mock
async def test_download_specific_version_number():
    _mock_project(respx)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}/versions").mock(
        return_value=httpx.Response(200, json={"data": [_version(V2, 2), _version(V1, 1)]}))
    _mock_signed_and_s3(respx)

    data = _parse(await _call({"item_id": ITEM_ID, "version_number": 1}))
    assert data["status"] == "downloaded"
    assert data["version_urn"] == V1
    assert data["saved_to"].endswith("MIDP Tracker.v1.xlsx")


@respx.mock
async def test_download_unknown_version_number_lists_available():
    _mock_project(respx)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}/versions").mock(
        return_value=httpx.Response(200, json={"data": [_version(V2, 2), _version(V1, 1)]}))
    with pytest.raises(ValueError, match=r"Available versions: \[1, 2\]"):
        await _call({"item_id": ITEM_ID, "version_number": 7})


@respx.mock
async def test_download_by_version_urn_encodes_it_in_the_path():
    _mock_project(respx)
    route = respx.get(
        f"{BASE}/data/v1/projects/{PROJECT_ID}/versions/{urllib.parse.quote(V1, safe='')}"
    ).mock(return_value=httpx.Response(200, json={"data": _version(V1, 1)}))
    _mock_signed_and_s3(respx)

    data = _parse(await _call({"version_urn": V1}))
    assert route.called
    assert data["status"] == "downloaded"


@respx.mock
async def test_download_refuses_file_over_size_cap_before_fetching():
    _mock_project(respx)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}/tip").mock(
        return_value=httpx.Response(200, json={"data": _version(V2, 2, size=5 * 1024 * 1024)}))
    signed, s3 = _mock_signed_and_s3(respx)

    data = _parse(await _call({"item_id": ITEM_ID, "max_size_mb": 1}))
    assert data["error"] == "too_large"
    assert not signed.called and not s3.called


@respx.mock
async def test_download_stream_aborts_when_body_exceeds_cap():
    _mock_project(respx)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}/tip").mock(
        return_value=httpx.Response(200, json={"data": _version(V2, 2, size=None)}))
    respx.get(SIGNED_ROUTE).mock(return_value=httpx.Response(200, json={"status": "complete", "url": S3_URL}))
    respx.get(S3_URL).mock(return_value=httpx.Response(200, content=b"x" * (1024 * 1024 + 1)))

    with pytest.raises(ValueError, match="exceeds max_size_mb"):
        await _call({"item_id": ITEM_ID, "max_size_mb": 1})
    leftovers = os.listdir(aps_mcp._downloads_dir())
    assert leftovers == []  # temp file cleaned up


@respx.mock
async def test_download_without_storage_reports_no_storage():
    _mock_project(respx)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}/tip").mock(
        return_value=httpx.Response(200, json={"data": _version(V2, 2, storage=None)}))
    data = _parse(await _call({"item_id": ITEM_ID}))
    assert data["error"] == "no_storage"


@respx.mock
async def test_download_403_on_signed_url_reports_permission_hint():
    _mock_project(respx)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}/tip").mock(
        return_value=httpx.Response(200, json={"data": _version(V2, 2)}))
    respx.get(SIGNED_ROUTE).mock(return_value=httpx.Response(403, json={"reason": "denied"}))
    data = _parse(await _call({"item_id": ITEM_ID}))
    assert data["error"] == 403
    assert "permission" in data["message"]


@respx.mock
async def test_download_checksum_mismatch_removes_file():
    _mock_project(respx)
    respx.get(f"{BASE}/data/v1/projects/{PROJECT_ID}/items/{ITEM_ID}/tip").mock(
        return_value=httpx.Response(200, json={"data": _version(V2, 2)}))
    _mock_signed_and_s3(respx, sha1="0" * 40)
    data = _parse(await _call({"item_id": ITEM_ID}))
    assert data["error"] == "checksum_mismatch"
    assert os.listdir(aps_mcp._downloads_dir()) == []


async def test_download_rejects_unsafe_item_id_without_request():
    result = await _call({"item_id": "urn:adsk.wipprod:dm.lineage:x/../../evil"})
    assert result.isError


def test_safe_download_name_strips_directories_and_reserved_chars():
    assert aps_mcp._safe_download_name("../../etc/passwd", None) == "passwd"
    assert aps_mcp._safe_download_name("a\\b\\c:d?.pdf", 3) == "c_d_.v3.pdf"
    assert aps_mcp._safe_download_name("", 1) == "download.v1"
    assert aps_mcp._safe_download_name(".hidden", None) == "hidden"


def test_parse_storage_urn_rejects_foreign_ids():
    assert aps_mcp._parse_storage_urn(STORAGE) == ("wip.dm.prod", OBJ_KEY)
    with pytest.raises(ValueError):
        aps_mcp._parse_storage_urn("urn:adsk.wipprod:fs.file:vf.x")
    with pytest.raises(ValueError):
        aps_mcp._parse_storage_urn("urn:adsk.objects:os.object:../x")
