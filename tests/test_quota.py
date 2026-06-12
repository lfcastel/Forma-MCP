"""
Tests for 429 rate/quota handling.

A hard "Quota limit exceeded" 429 should fail fast with a clear, user-facing
result (not hang on retries, not raise an opaque exception). Transient 429s
should retry briefly and then succeed.
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch, AsyncMock

import aps_mcp
from conftest import FAKE_TOKEN, HUB_RESPONSE

BASE = aps_mcp.APS_BASE

QUOTA_BODY = {
    "developerMessage": "Quota limit exceeded.",
    "errorCode": "AUTH-012",
}
TRANSIENT_BODY = {"developerMessage": "Rate limited, slow down."}

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# _request_with_retry
# ---------------------------------------------------------------------------

@respx.mock
async def test_request_with_retry_fails_fast_on_quota():
    """A quota 429 raises APSQuotaError immediately — no retries, no sleeping."""
    route = respx.get(f"{BASE}/anything").mock(
        return_value=httpx.Response(429, json=QUOTA_BODY, headers={"Retry-After": "60"})
    )
    sleep = AsyncMock()
    with patch("aps_mcp.asyncio.sleep", sleep):
        async with httpx.AsyncClient() as client:
            with pytest.raises(aps_mcp.APSQuotaError) as exc:
                await aps_mcp._request_with_retry(client, "get", f"{BASE}/anything")

    assert route.call_count == 1          # failed fast, did not retry
    sleep.assert_not_awaited()            # never blocked
    assert exc.value.retry_after == 60
    assert "quota" in str(exc.value).lower()


@respx.mock
async def test_request_with_retry_retries_transient_then_succeeds():
    """A transient 429 (no quota wording) backs off briefly, then succeeds."""
    route = respx.get(f"{BASE}/anything")
    route.side_effect = [
        httpx.Response(429, json=TRANSIENT_BODY, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"ok": True}),
    ]
    sleep = AsyncMock()
    with patch("aps_mcp.asyncio.sleep", sleep):
        async with httpx.AsyncClient() as client:
            r = await aps_mcp._request_with_retry(client, "get", f"{BASE}/anything")

    assert r.status_code == 200
    assert route.call_count == 2
    sleep.assert_awaited_once()


@respx.mock
async def test_request_with_retry_raises_after_exhausting_retries():
    """Persistent transient 429s eventually fail fast with APSQuotaError."""
    respx.get(f"{BASE}/anything").mock(
        return_value=httpx.Response(429, json=TRANSIENT_BODY, headers={"Retry-After": "0"})
    )
    with patch("aps_mcp.asyncio.sleep", AsyncMock()):
        async with httpx.AsyncClient() as client:
            with pytest.raises(aps_mcp.APSQuotaError):
                await aps_mcp._request_with_retry(
                    client, "get", f"{BASE}/anything", max_retries=2
                )


# ---------------------------------------------------------------------------
# call_tool wrapper
# ---------------------------------------------------------------------------

@respx.mock
async def test_call_tool_quota_returns_friendly_result():
    """A 429 surfacing from a direct request becomes a clean tool result."""
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(429, json=QUOTA_BODY, headers={"Retry-After": "30"})
    )
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_hubs", {})

    data = json.loads(result[0].text)
    assert data["error"] == "quota_exceeded"
    assert data["status"] == 429
    assert data["retry_after_seconds"] == 30
    assert "quota" in data["message"].lower()


@respx.mock
async def test_call_tool_success_unaffected():
    """Non-429 responses still return normally through the wrapper."""
    respx.get(f"{BASE}/project/v1/hubs").mock(
        return_value=httpx.Response(200, json=HUB_RESPONSE)
    )
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        result = await aps_mcp.call_tool("list_hubs", {})

    data = json.loads(result[0].text)
    assert isinstance(data, list) and data[0]["name"] == "Test Hub"
