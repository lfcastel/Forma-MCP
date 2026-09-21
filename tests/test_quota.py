"""
Tests for 429 rate/quota handling.

APS signals an ordinary per-minute rate limit with a 429 whose body reads
"Quota limit exceeded." — so a 429 is always retried after the `Retry-After`
period (never failed fast on the wording). Only once retries are exhausted
does the tool surface a clear, user-facing result (not an opaque exception).
"""
import json
import pytest
import respx
import httpx
from unittest.mock import patch, AsyncMock

import aps_mcp
from conftest import FAKE_TOKEN, HUB_RESPONSE

BASE = aps_mcp.APS_BASE

# The canonical APS rate-limit 429 body (from the APS Rate Limits and Quotas docs).
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
async def test_request_with_retry_waits_retry_after_on_quota_body_then_succeeds():
    """A "Quota limit exceeded." 429 is a rate limit: wait the full Retry-After, retry."""
    route = respx.get(f"{BASE}/anything")
    route.side_effect = [
        httpx.Response(429, json=QUOTA_BODY, headers={"Retry-After": "25"}),
        httpx.Response(200, json={"ok": True}),
    ]
    sleep = AsyncMock()
    with patch("aps_mcp.asyncio.sleep", sleep):
        async with httpx.AsyncClient() as client:
            r = await aps_mcp._request_with_retry(client, "get", f"{BASE}/anything")

    assert r.status_code == 200
    assert route.call_count == 2
    sleep.assert_awaited_once_with(25)   # the full period, not a 10 s cap


@respx.mock
async def test_request_with_retry_caps_retry_after():
    """An absurd Retry-After is capped so one request can't block for minutes."""
    route = respx.get(f"{BASE}/anything")
    route.side_effect = [
        httpx.Response(429, json=QUOTA_BODY, headers={"Retry-After": "900"}),
        httpx.Response(200, json={"ok": True}),
    ]
    sleep = AsyncMock()
    with patch("aps_mcp.asyncio.sleep", sleep):
        async with httpx.AsyncClient() as client:
            await aps_mcp._request_with_retry(client, "get", f"{BASE}/anything")

    sleep.assert_awaited_once_with(aps_mcp._RETRY_AFTER_CAP)


@respx.mock
async def test_request_with_retry_backs_off_without_retry_after_header():
    """No Retry-After → a short exponential back-off (5 s, 10 s, …), then retry."""
    route = respx.get(f"{BASE}/anything")
    route.side_effect = [
        httpx.Response(429, json=TRANSIENT_BODY),
        httpx.Response(429, json=TRANSIENT_BODY),
        httpx.Response(200, json={"ok": True}),
    ]
    sleep = AsyncMock()
    with patch("aps_mcp.asyncio.sleep", sleep):
        async with httpx.AsyncClient() as client:
            r = await aps_mcp._request_with_retry(client, "get", f"{BASE}/anything")

    assert r.status_code == 200
    assert route.call_count == 3
    assert [c.args[0] for c in sleep.await_args_list] == [5, 10]


@respx.mock
async def test_request_with_retry_raises_after_exhausting_retries():
    """Persistent 429s eventually raise APSQuotaError carrying the Retry-After hint."""
    route = respx.get(f"{BASE}/anything").mock(
        return_value=httpx.Response(429, json=QUOTA_BODY, headers={"Retry-After": "30"})
    )
    with patch("aps_mcp.asyncio.sleep", AsyncMock()):
        async with httpx.AsyncClient() as client:
            with pytest.raises(aps_mcp.APSQuotaError) as exc:
                await aps_mcp._request_with_retry(
                    client, "get", f"{BASE}/anything", max_retries=2
                )

    assert route.call_count == 3          # initial + 2 retries
    assert exc.value.retry_after == 30
    assert "quota" in str(exc.value).lower()


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

    # Quota errors come back as a CallToolResult flagged isError=True, with the
    # readable JSON still in the content so the client LLM can act on it.
    assert isinstance(result, aps_mcp.CallToolResult)
    assert result.isError is True
    data = json.loads(result.content[0].text)
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
