"""Tests for create_data_request / get_data_request_results (Data Connector)."""
import io
import json
import os
import zipfile

import httpx
import pytest
import respx
from unittest.mock import patch

import aps_mcp
from conftest import (
    FAKE_TOKEN, HUB_ID, PROJECT_ID, BARE_PROJECT_ID, PROJECT_NAME,
    HUB_RESPONSE, PROJECTS_RESPONSE,
)

BASE = aps_mcp.APS_BASE
ACCOUNT = HUB_ID.removeprefix("b.")
DC = f"{BASE}/data-connector/v1/accounts/{ACCOUNT}"
REQ_ID = "11111111-2222-3333-4444-555555555555"
JOB_ID = "99999999-8888-7777-6666-555555555555"
S3 = "https://s3.example.test/extract.zip?sig=1"


@pytest.fixture(autouse=True)
def _no_poll_sleep(monkeypatch):
    monkeypatch.setattr(aps_mcp, "_DC_POLL_INTERVAL", 0)


def _parse(result):
    return json.loads(result[0].text)


def _mock_project(router):
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


async def _call(tool, args):
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        return await aps_mcp.call_tool(tool, args)


# --- create_data_request ----------------------------------------------------

@respx.mock
async def test_create_posts_one_time_request_for_project():
    _mock_project(respx)
    route = respx.post(f"{DC}/requests").mock(return_value=httpx.Response(201, json={"id": REQ_ID}))
    data = _parse(await _call("create_data_request", {
        "project": PROJECT_NAME, "service_groups": ["reviews", "issues"],
    }))
    body = json.loads(route.calls.last.request.content)
    assert body["scheduleInterval"] == "ONE_TIME"
    assert body["serviceGroups"] == ["reviews", "issues"]
    assert body["projectIdList"] == [BARE_PROJECT_ID]
    assert body["sendEmail"] is False
    assert "dateRange" not in body
    assert data["request_id"] == REQ_ID


@respx.mock
async def test_create_activities_with_date_range():
    _mock_project(respx)
    route = respx.post(f"{DC}/requests").mock(return_value=httpx.Response(201, json={"id": REQ_ID}))
    await _call("create_data_request", {
        "project": PROJECT_NAME, "service_groups": ["activities"], "date_range": "PAST_7_DAYS",
    })
    assert json.loads(route.calls.last.request.content)["dateRange"] == "PAST_7_DAYS"


async def test_create_rejects_unknown_group_and_bad_custom_range():
    with pytest.raises(ValueError, match="unknown"):
        aps_mcp._dc_request_payload({"service_groups": ["docs"]}, ["p"])
    with pytest.raises(ValueError, match="CUSTOM"):
        aps_mcp._dc_request_payload({"service_groups": ["activities"], "date_range": "CUSTOM"}, ["p"])


@respx.mock
async def test_create_403_explains_scope():
    _mock_project(respx)
    respx.post(f"{DC}/requests").mock(return_value=httpx.Response(403, json={"detail": "nope"}))
    data = _parse(await _call("create_data_request", {"project": PROJECT_NAME, "service_groups": ["admin"]}))
    assert data["error"] == 403
    assert "read-only" in data["message"]


# --- get_data_request_results -----------------------------------------------

def _mock_job(router, job):
    router.get(f"{DC}/requests/{REQ_ID}/jobs").mock(
        return_value=httpx.Response(200, json={"results": [job] if job else []}))


@respx.mock
async def test_results_downloads_and_unpacks_csv_tables():
    _mock_project(respx)
    _mock_job(respx, {"id": JOB_ID, "status": "complete", "completionStatus": "success"})
    respx.get(f"{DC}/jobs/{JOB_ID}/data/autodesk_data_extract.zip").mock(
        return_value=httpx.Response(200, json={"signedUrl": S3}))
    s3 = respx.get(S3).mock(return_value=httpx.Response(200, content=_zip_bytes({
        "review_documents.csv": "id,review_id,versioned_urn,status\n1,r1,u1,APPROVED\n2,r1,u2,REJECTED\n",
        "reviews.csv": "id,status\nr1,CLOSED\n",
        "../../evil.csv": "a\n1\n",
        "README.html": "<html/>",
    })))
    data = _parse(await _call("get_data_request_results", {"request_id": REQ_ID, "project": PROJECT_NAME}))

    assert "authorization" not in s3.calls.last.request.headers
    tables = {t["table"]: t for t in data["tables"]}
    assert set(tables) == {"review_documents", "reviews", "evil"}
    assert tables["review_documents"]["rows"] == 2
    assert tables["review_documents"]["columns"] == ["id", "review_id", "versioned_urn", "status"]
    out_dir = os.path.join(aps_mcp._data_connector_dir(), JOB_ID)
    assert data["folder"] == out_dir
    # zip-slip: the traversal entry lands inside the job folder, nothing outside it
    assert os.path.dirname(tables["evil"]["file"]) == out_dir
    assert not os.path.exists(os.path.join(aps_mcp._data_dir(), "evil.csv"))


@respx.mock
async def test_results_still_running_returns_running():
    respx.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    _mock_job(respx, {"id": JOB_ID, "status": "running"})
    data = _parse(await _call("get_data_request_results", {"request_id": REQ_ID, "wait_seconds": 0}))
    assert data["status"] == "running"


@respx.mock
async def test_results_failed_job_reported():
    respx.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    _mock_job(respx, {"id": JOB_ID, "status": "complete", "completionStatus": "failed"})
    data = _parse(await _call("get_data_request_results", {"request_id": REQ_ID}))
    assert data["error"] == "job_not_successful"


async def test_results_rejects_unsafe_job_id():
    result = await _call("get_data_request_results", {"request_id": REQ_ID, "job_id": "../x"})
    assert result.isError
