"""
Tests for the client-side rate limiter that paces every Data Management request
through a per-endpoint token bucket (APS limits are per endpoint, per client ID,
per minute).

The buckets use `time.monotonic()` for refill and `asyncio.sleep` to wait, so
tests pin the clock and mock the sleep to assert on exact waits.
"""
import asyncio
import pytest
import respx
import httpx
from unittest.mock import patch, AsyncMock

import aps_mcp
from conftest import FAKE_TOKEN, HUB_ID, PROJECT_ID, HUB_RESPONSE

BASE = aps_mcp.APS_BASE
FOLDER = "urn:adsk.wipprod:fs.folder:co.abc123"
CONTENTS_URL = f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER}/contents"


@pytest.fixture
def pacing_on(monkeypatch):
    """Re-enable pacing (conftest turns it off) against a fresh bucket registry."""
    monkeypatch.setattr(aps_mcp, "_RATE_LIMITING_ENABLED", True)
    monkeypatch.setattr(aps_mcp, "_rate_buckets", {})
    monkeypatch.setattr(aps_mcp, "_RATE_LIMIT_HEADROOM", 1.0)  # exact published rates


@pytest.fixture
def clock(monkeypatch):
    """Frozen `time.monotonic()` the test advances by hand."""
    now = [1000.0]
    monkeypatch.setattr(aps_mcp.time, "monotonic", lambda: now[0])
    return now


# ---------------------------------------------------------------------------
# Endpoint normalisation → rate-limit table key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,url,expected", [
    ("GET", CONTENTS_URL, ("GET", "/projects/{id}/folders/{id}/contents")),
    ("GET", f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER}",
     ("GET", "/projects/{id}/folders/{id}")),
    ("PATCH", f"{BASE}/data/v1/projects/{PROJECT_ID}/folders/{FOLDER}",
     ("PATCH", "/projects/{id}/folders/{id}")),
    ("POST", f"{BASE}/data/v1/projects/{PROJECT_ID}/folders",
     ("POST", "/projects/{id}/folders")),
    ("GET", f"{BASE}/project/v1/hubs", ("GET", "/hubs")),
    ("GET", f"{BASE}/project/v1/hubs/{HUB_ID}/projects/{PROJECT_ID}/topFolders",
     ("GET", "/hubs/{id}/projects/{id}/topFolders")),
    ("GET", f"{BASE}/data/v1/projects/{PROJECT_ID}/items/urn:adsk.wipprod:dm.lineage:xyz/versions",
     ("GET", "/projects/{id}/items/{id}/versions")),
    ("get", f"{CONTENTS_URL}?page[limit]=200&page[number]=2",
     ("GET", "/projects/{id}/folders/{id}/contents")),   # query string ignored, method upper-cased
])
def test_dm_endpoint_key_normalises_ids(method, url, expected):
    assert aps_mcp._dm_endpoint_key(method, url) == expected


@pytest.mark.parametrize("url", [
    f"{BASE}/construction/admin/v1/projects/abc/users",
    f"{BASE}/hq/v1/accounts/abc/users",
    f"{BASE}/construction/issues/v1/projects/abc/issues",
    f"{BASE}/bim360/docs/v1/projects/abc/folders/x/permissions",
    f"{BASE}/authentication/v2/token",
])
def test_dm_endpoint_key_ignores_non_data_management(url):
    """Only Data Management (`/project/v1`, `/data/v1`) is paced."""
    assert aps_mcp._dm_endpoint_key("GET", url) is None


def test_bucket_for_uses_published_limits_with_headroom(pacing_on, monkeypatch):
    monkeypatch.setattr(aps_mcp, "_RATE_LIMIT_HEADROOM", 0.8)
    contents = aps_mcp._bucket_for(("GET", "/projects/{id}/folders/{id}/contents"))
    hubs = aps_mcp._bucket_for(("GET", "/hubs"))
    unknown = aps_mcp._bucket_for(("DELETE", "/projects/{id}/something"))
    assert contents.rate == pytest.approx(300 * 0.8 / 60)
    assert hubs.rate == pytest.approx(50 * 0.8 / 60)
    assert unknown.rate == pytest.approx(aps_mcp._DM_DEFAULT_LIMIT * 0.8 / 60)
    # Same key → same bucket instance (state is shared across callers).
    assert aps_mcp._bucket_for(("GET", "/hubs")) is hubs


# ---------------------------------------------------------------------------
# _TokenBucket
# ---------------------------------------------------------------------------

def test_token_bucket_allows_burst_then_paces(clock):
    b = aps_mcp._TokenBucket(rate_per_min=60, burst_seconds=3)   # 1/s, burst of 3
    assert [b.reserve() for _ in range(3)] == [0.0, 0.0, 0.0]     # burst is free
    # 4th, 5th, 6th callers are queued one refill-interval apart.
    assert b.reserve() == pytest.approx(1.0)
    assert b.reserve() == pytest.approx(2.0)
    assert b.reserve() == pytest.approx(3.0)


def test_token_bucket_refills_over_time(clock):
    b = aps_mcp._TokenBucket(rate_per_min=60, burst_seconds=3)
    for _ in range(3):
        b.reserve()
    clock[0] += 2.0                       # 2 tokens refilled
    assert b.reserve() == 0.0
    assert b.reserve() == 0.0
    assert b.reserve() == pytest.approx(1.0)


def test_token_bucket_penalize_blocks_everyone(clock):
    """A 429 drains the bucket and holds every caller until Retry-After elapses."""
    b = aps_mcp._TokenBucket(rate_per_min=300, burst_seconds=10)
    assert b.reserve() == 0.0
    b.penalize(25)
    assert b.reserve() == pytest.approx(25.0)
    clock[0] += 25.0
    # Block lifted, but the drained bucket still paces at the refill rate.
    assert b.reserve() <= 1.0


# ---------------------------------------------------------------------------
# httpx event hooks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_request_hook_paces_data_management_calls(pacing_on, clock):
    """Requests past the burst allowance sleep for their reservation."""
    respx.get(CONTENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    bucket = aps_mcp._bucket_for(("GET", "/projects/{id}/folders/{id}/contents"))
    bucket.tokens = 1.0                   # one free token left, then 5/s pacing

    sleep = AsyncMock()
    with patch("aps_mcp.asyncio.sleep", sleep):
        async with aps_mcp._make_client() as client:
            await client.get(CONTENTS_URL)   # free
            await client.get(CONTENTS_URL)   # queued 1 interval (0.2 s)
            await client.get(CONTENTS_URL)   # queued 2 intervals (0.4 s)

    waits = [c.args[0] for c in sleep.await_args_list]
    assert waits == [pytest.approx(0.2), pytest.approx(0.4)]


@pytest.mark.asyncio
@respx.mock
async def test_request_hook_leaves_other_apis_alone(pacing_on, clock):
    url = f"{BASE}/construction/admin/v1/projects/abc/users"
    respx.get(url).mock(return_value=httpx.Response(200, json={"results": []}))
    sleep = AsyncMock()
    with patch("aps_mcp.asyncio.sleep", sleep):
        async with aps_mcp._make_client() as client:
            for _ in range(20):
                await client.get(url)
    sleep.assert_not_awaited()
    assert aps_mcp._rate_buckets == {}


@pytest.mark.asyncio
@respx.mock
async def test_response_hook_penalizes_bucket_on_429(pacing_on, clock):
    """A 429 seen by ANY call site (even a raw client.get) blocks that endpoint's
    bucket for Retry-After, so the next caller waits instead of also being 429'd."""
    respx.get(CONTENTS_URL).mock(
        return_value=httpx.Response(429, json={"developerMessage": "Quota limit exceeded."},
                                    headers={"Retry-After": "25"})
    )
    async with aps_mcp._make_client() as client:
        r = await client.get(CONTENTS_URL)
    assert r.status_code == 429

    bucket = aps_mcp._bucket_for(("GET", "/projects/{id}/folders/{id}/contents"))
    assert bucket.reserve() == pytest.approx(25.0)
    # A different endpoint is unaffected.
    assert aps_mcp._bucket_for(("GET", "/hubs")).reserve() == 0.0


@pytest.mark.asyncio
@respx.mock
async def test_pacing_disabled_switch(clock):
    """With pacing off (the test default) no bucket is consulted at all."""
    respx.get(CONTENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    sleep = AsyncMock()
    with patch("aps_mcp.asyncio.sleep", sleep):
        async with aps_mcp._make_client() as client:
            for _ in range(100):
                await client.get(CONTENTS_URL)
    sleep.assert_not_awaited()
    assert aps_mcp._rate_buckets == {}


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_uses_paced_client(pacing_on, clock):
    """The tool dispatcher's client is the paced one: `list_hubs` consumes a token."""
    respx.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        await aps_mcp.call_tool("list_hubs", {})
    hubs = aps_mcp._bucket_for(("GET", "/hubs"))
    assert hubs.tokens == pytest.approx(hubs.capacity - 1)


# ---------------------------------------------------------------------------
# Walker concurrency guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_walk_project_files_bounds_in_flight_listings(monkeypatch):
    """A wide tree never has more than _WALK_CONCURRENCY listings in flight."""
    monkeypatch.setattr(aps_mcp, "_WALK_CONCURRENCY", 3)
    in_flight = [0]
    peak = [0]
    release = asyncio.Event()

    def folder(i):
        return {"type": "folders", "id": f"sub{i}", "attributes": {"name": f"Sub {i}"}}

    async def contents_handler(request):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        await asyncio.sleep(0)                  # let siblings start
        await release.wait()
        in_flight[0] -= 1
        if request.url.path.endswith("/root/contents"):
            return httpx.Response(200, json={"data": [folder(i) for i in range(12)]})
        return httpx.Response(200, json={"data": [
            {"type": "items", "id": "f", "attributes": {"displayName": "x.pdf"}},
        ]})

    respx.get(url__regex=r".*/folders/[^/]+/contents").mock(side_effect=contents_handler)

    async def run():
        async with httpx.AsyncClient() as client:
            return await aps_mcp._walk_project_files(
                client, PROJECT_ID, {}, [{"id": "root", "attributes": {"name": "Root"}}]
            )

    task = asyncio.create_task(run())
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    files = await task

    assert len(files) == 12
    assert peak[0] <= 3
