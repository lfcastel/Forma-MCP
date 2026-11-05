"""
Build & Docs workflow tools: RFIs, Submittals, Transmittals, File packages.
All APS calls are intercepted by respx; auth is patched.
"""
import json
from unittest.mock import patch

import httpx
import pytest
import respx

import aps_mcp
from conftest import (
    FAKE_TOKEN, HUB_ID, BARE_PROJECT_ID, PROJECT_NAME, HUB_RESPONSE, PROJECTS_RESPONSE,
)

BASE = aps_mcp.APS_BASE
RFIS = f"{BASE}/construction/rfis/v3/projects/{BARE_PROJECT_ID}"
SUBS = f"{BASE}/construction/submittals/v2/projects/{BARE_PROJECT_ID}"
TRANS = f"{BASE}/construction/transmittals/v1/projects/{BARE_PROJECT_ID}"
PKGS = f"{BASE}/construction/packages/v1/projects/{BARE_PROJECT_ID}"


def _parse(result):
    assert result and result[0].type == "text"
    return json.loads(result[0].text)


def _page(results, total=None, limit=10, offset=0):
    return {"results": results,
            "pagination": {"limit": limit, "offset": offset,
                           "totalResults": len(results) if total is None else total}}


def _resolve(router):
    router.get(f"{BASE}/project/v1/hubs").mock(return_value=httpx.Response(200, json=HUB_RESPONSE))
    router.get(f"{BASE}/project/v1/hubs/{HUB_ID}/projects").mock(
        return_value=httpx.Response(200, json=PROJECTS_RESPONSE))


async def _call(name, args):
    with patch("aps_mcp.get_access_token", return_value=FAKE_TOKEN):
        return await aps_mcp.call_tool(name, {"project_name": PROJECT_NAME, **args})


def test_tools_registered_and_unique():
    names = [t.name for t in aps_mcp._BUILD_TOOLS]
    assert len(names) == len(set(names)) == 18
    assert {"list_rfis", "create_rfi", "list_submittals", "list_transmittals", "list_file_packages"} <= set(names)


# ---------------------------------------------------------------------------
# RFIs
# ---------------------------------------------------------------------------

@respx.mock
async def test_list_rfis_builds_search_body_and_paginates():
    _resolve(respx)
    route = respx.post(f"{RFIS}/search:rfis").mock(side_effect=[
        httpx.Response(200, json=_page([{"id": "r1"}, {"id": "r2"}], total=3)),
        httpx.Response(200, json=_page([{"id": "r3"}], total=3)),
    ])
    data = _parse(await _call("list_rfis", {
        "status": "open,answered", "search": "duct", "assigned_to": ["U1"],
        "sort_by": "dueDate", "sort_order": "asc", "fields": ["id", "title"],
    }))
    assert data["result_count"] == 3 and [r["id"] for r in data["rfis"]] == ["r1", "r2", "r3"]
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert first["filter"] == {"status": ["open", "answered"], "assignedTo": ["U1"]}
    assert first["search"] == "duct" and first["fields"] == ["id", "title"]
    assert first["sort"] == [{"field": "dueDate", "order": "ASC"}]
    assert (first["offset"], second["offset"], first["limit"]) == (0, 2, 200)
    assert route.calls[0].request.headers["x-ads-region"] == aps_mcp.BUILD_REGION


@respx.mock
async def test_get_rfi_by_custom_identifier_with_comments():
    _resolve(respx)
    respx.post(f"{RFIS}/search:rfis").mock(return_value=httpx.Response(
        200, json=_page([{"id": "r1", "customIdentifier": "011"}, {"id": "r2", "customIdentifier": "012"}])))
    respx.get(f"{RFIS}/rfis/r2").mock(return_value=httpx.Response(200, json={"id": "r2", "title": "Wall"}))
    respx.get(f"{RFIS}/rfis/r2/comments").mock(return_value=httpx.Response(200, json=_page([{"body": "hi"}])))
    data = _parse(await _call("get_rfi", {"custom_identifier": "012"}))
    assert data["rfi"]["id"] == "r2"
    assert data["comments"] == [{"body": "hi"}]


@respx.mock
async def test_get_rfi_unknown_custom_identifier_errors():
    _resolve(respx)
    respx.post(f"{RFIS}/search:rfis").mock(return_value=httpx.Response(200, json=_page([])))
    with pytest.raises(ValueError, match="No RFI with custom identifier"):
        await _call("get_rfi", {"custom_identifier": "999"})


@respx.mock
async def test_create_rfi_maps_fields():
    _resolve(respx)
    route = respx.post(f"{RFIS}/rfis").mock(return_value=httpx.Response(201, json={"id": "new"}))
    data = _parse(await _call("create_rfi", {
        "title": "Pipe size", "status": "draft", "question": "Correct size?",
        "assigned_to": ["U1"], "discipline": "Mechanical", "due_date": "2026-12-01T12:00:00.000Z",
    }))
    assert data["status"] == "created" and data["rfi"]["id"] == "new"
    body = json.loads(route.calls[0].request.content)
    assert body == {"title": "Pipe size", "status": "draft", "question": "Correct size?",
                    "assignedTo": [{"id": "U1", "type": "user"}], "discipline": ["Mechanical"],
                    "dueDate": "2026-12-01T12:00:00.000Z"}


@respx.mock
async def test_create_rfi_surfaces_api_error():
    _resolve(respx)
    respx.post(f"{RFIS}/rfis").mock(return_value=httpx.Response(403, json={"detail": "no role"}))
    data = _parse(await _call("create_rfi", {"title": "x", "status": "draft"}))
    assert data == {"error": 403, "body": {"detail": "no role"}}


@respx.mock
async def test_update_rfi_close_with_official_response():
    _resolve(respx)
    route = respx.patch(f"{RFIS}/rfis/r1").mock(return_value=httpx.Response(200, json={"id": "r1", "status": "closed"}))
    data = _parse(await _call("update_rfi", {
        "rfi_id": "r1", "status": "closed", "official_response": "Use 50mm",
        "official_response_status": "answered",
    }))
    assert data["status"] == "updated"
    assert json.loads(route.calls[0].request.content) == {
        "status": "closed", "officialResponse": "Use 50mm", "officialResponseStatus": "answered"}


@respx.mock
async def test_update_rfi_requires_a_field():
    _resolve(respx)
    with pytest.raises(ValueError, match="Nothing to update"):
        await _call("update_rfi", {"rfi_id": "r1"})


@respx.mock
async def test_add_rfi_comment():
    _resolve(respx)
    route = respx.post(f"{RFIS}/rfis/r1/comments").mock(return_value=httpx.Response(201, json={"id": "c1"}))
    data = _parse(await _call("add_rfi_comment", {"rfi_id": "r1", "body": "Checked on site"}))
    assert data["comment"]["id"] == "c1"
    assert json.loads(route.calls[0].request.content) == {"body": "Checked on site"}


@respx.mock
async def test_list_rfi_types_with_permissions():
    _resolve(respx)
    respx.get(f"{RFIS}/rfi-types").mock(return_value=httpx.Response(200, json=_page([{"id": "t1"}])))
    respx.get(f"{RFIS}/users/me").mock(return_value=httpx.Response(200, json={"workflowType": "EU"}))
    data = _parse(await _call("list_rfi_types", {}))
    assert data["types"] == [{"id": "t1"}] and data["me"]["workflowType"] == "EU"


async def test_unsafe_rfi_id_rejected_before_any_request():
    with patch("aps_mcp.get_access_token") as gat:
        result = await aps_mcp.call_tool("get_rfi", {"project": "x", "rfi_id": "r1/../../x"})
    assert result.isError is True
    gat.assert_not_called()


# ---------------------------------------------------------------------------
# Submittals
# ---------------------------------------------------------------------------

@respx.mock
async def test_list_submittals_maps_filters_and_paginates():
    _resolve(respx)
    route = respx.get(f"{SUBS}/items").mock(side_effect=[
        httpx.Response(200, json=_page([{"id": "s1"}], total=2, limit=50)),
        httpx.Response(200, json=_page([{"id": "s2"}], total=2, limit=50, offset=1)),
    ])
    data = _parse(await _call("list_submittals", {"state_id": "mgr-1", "package_id": "p1", "search": "door"}))
    assert data["result_count"] == 2
    q = route.calls[0].request.url.params
    assert q["filter[stateId]"] == "mgr-1" and q["filter[packageId]"] == "p1"
    assert q["search"] == "door" and q["limit"] == "50"


@respx.mock
async def test_get_submittal_includes_steps_with_tasks():
    _resolve(respx)
    respx.get(f"{SUBS}/items/s1").mock(return_value=httpx.Response(200, json={"id": "s1"}))
    respx.get(f"{SUBS}/items/s1/steps").mock(return_value=httpx.Response(200, json=_page([{"id": "st1"}])))
    respx.get(f"{SUBS}/items/s1/steps/st1/tasks").mock(
        return_value=httpx.Response(200, json=_page([{"id": "tk1", "status": "open"}])))
    data = _parse(await _call("get_submittal", {"submittal_id": "s1"}))
    assert data["steps"][0]["tasks"][0]["id"] == "tk1"
    assert "revisions" not in data and "attachments" not in data


@respx.mock
async def test_create_submittal_body():
    _resolve(respx)
    route = respx.post(f"{SUBS}/items").mock(return_value=httpx.Response(201, json={"id": "new"}))
    await _call("create_submittal", {
        "title": "Door hardware", "type_id": "t1", "spec_id": "sp1", "state_id": "sbc-1",
        "manager": "U1", "manager_type": "user",
    })
    assert json.loads(route.calls[0].request.content) == {
        "title": "Door hardware", "typeId": "t1", "specId": "sp1", "stateId": "sbc-1",
        "manager": "U1", "managerType": "1"}


@respx.mock
async def test_update_submittal_never_sends_state():
    _resolve(respx)
    route = respx.patch(f"{SUBS}/items/s1").mock(return_value=httpx.Response(200, json={"id": "s1"}))
    await _call("update_submittal", {"submittal_id": "s1", "title": "New", "state_id": "cmp"})
    assert json.loads(route.calls[0].request.content) == {"title": "New"}


@respx.mock
async def test_list_submittal_packages_single_and_list():
    _resolve(respx)
    respx.get(f"{SUBS}/packages/p1").mock(return_value=httpx.Response(200, json={"id": "p1"}))
    respx.get(f"{SUBS}/packages").mock(return_value=httpx.Response(200, json=_page([{"id": "p1"}, {"id": "p2"}])))
    assert _parse(await _call("list_submittal_packages", {"package_id": "p1"}))["package"]["id"] == "p1"
    assert _parse(await _call("list_submittal_packages", {}))["result_count"] == 2


@respx.mock
async def test_list_submittal_types_with_responses():
    _resolve(respx)
    respx.get(f"{SUBS}/item-types").mock(return_value=httpx.Response(200, json=_page([{"id": "t1"}])))
    respx.get(f"{SUBS}/responses").mock(return_value=httpx.Response(200, json=_page([{"id": "a1"}])))
    data = _parse(await _call("list_submittal_types", {}))
    assert data["item_types"] == [{"id": "t1"}] and data["responses"] == [{"id": "a1"}]


# ---------------------------------------------------------------------------
# Transmittals & file packages
# ---------------------------------------------------------------------------

@respx.mock
async def test_list_and_get_transmittal():
    _resolve(respx)
    respx.get(f"{TRANS}/transmittals").mock(return_value=httpx.Response(200, json=_page([{"id": "tr1"}])))
    respx.get(f"{TRANS}/transmittals/tr1").mock(return_value=httpx.Response(200, json={"id": "tr1"}))
    docs = respx.get(f"{TRANS}/transmittals/tr1/documents").mock(
        return_value=httpx.Response(200, json=_page([{"urn": "u1"}])))
    rec = respx.get(f"{TRANS}/transmittals/tr1/recipients").mock(return_value=httpx.Response(200, json=_page([])))
    assert _parse(await _call("list_transmittals", {}))["result_count"] == 1
    data = _parse(await _call("get_transmittal", {"transmittal_id": "tr1", "include": ["documents"]}))
    assert data["documents"] == [{"urn": "u1"}] and "recipients" not in data
    assert docs.called and not rec.called


@respx.mock
async def test_list_file_packages_and_resources():
    _resolve(respx)
    respx.get(f"{PKGS}/packages").mock(return_value=httpx.Response(200, json=_page([{"id": "fp1"}])))
    respx.get(f"{PKGS}/packages/fp1/resources").mock(
        return_value=httpx.Response(200, json=_page([{"name": "A-101.pdf"}])))
    assert _parse(await _call("list_file_packages", {}))["packages"] == [{"id": "fp1"}]
    data = _parse(await _call("list_file_packages", {"package_id": "fp1"}))
    assert data["resources"] == [{"name": "A-101.pdf"}]


# ---------------------------------------------------------------------------
# Submittal transitions & reviews
# ---------------------------------------------------------------------------

ITEM_WITH_ACTIONS = {
    "id": "s1", "stateId": "sbc-1",
    "permittedActions": [
        {"id": "Item::retrieve"},
        {"id": "Item::wf_transition", "mandatoryFields": ["stateId"], "transitions": [
            {"id": "sbc-1::mgr-1", "name": "Submit to Manager",
             "stateTo": {"id": "mgr-1"}, "transitionFields": ["mailNote", "manager", "managerType"],
             "mandatoryFields": ["manager"]},
            {"id": "sbc-1::void", "name": "Void", "stateTo": {"id": "void"},
             "transitionFields": [], "mandatoryFields": []},
        ]},
    ],
}


def test_submittal_actor_type_codes():
    assert [aps_mcp._submittal_actor_type(v) for v in ("user", "Company", "role", "2")] == ["1", "2", "3", "2"]
    with pytest.raises(ValueError):
        aps_mcp._submittal_actor_type("team")


def test_submittal_transitions_summary():
    out = aps_mcp._submittal_transitions(ITEM_WITH_ACTIONS)
    assert out["can_transition"] is True
    to_mgr = next(t for t in out["transitions"] if t["to"] == "mgr-1")
    assert to_mgr["required_fields"] == ["manager"]
    assert to_mgr["optional_fields"] == ["mailNote", "managerType"]
    assert aps_mcp._submittal_transitions({"permittedActions": []}) == {"can_transition": False, "transitions": []}


@respx.mock
async def test_get_submittal_reports_allowed_transitions():
    _resolve(respx)
    respx.get(f"{SUBS}/items/s1").mock(return_value=httpx.Response(200, json=ITEM_WITH_ACTIONS))
    data = _parse(await _call("get_submittal", {"submittal_id": "s1", "include_steps": False}))
    assert {t["to"] for t in data["allowed_transitions"]["transitions"]} == {"mgr-1", "void"}


@respx.mock
async def test_transition_submittal_posts_when_allowed():
    _resolve(respx)
    respx.get(f"{SUBS}/items/s1").mock(return_value=httpx.Response(200, json=ITEM_WITH_ACTIONS))
    route = respx.post(f"{SUBS}/items/s1:transition").mock(
        return_value=httpx.Response(200, json={"id": "s1", "stateId": "mgr-1"}))
    data = _parse(await _call("transition_submittal", {
        "submittal_id": "s1", "state_id": "mgr-1", "manager": "U1", "manager_type": "user",
        "mail_note": "Please review",
    }))
    assert data["status"] == "transitioned" and data["transition"] == "Submit to Manager"
    assert json.loads(route.calls[0].request.content) == {
        "stateId": "mgr-1", "manager": "U1", "managerType": "1", "mailNote": "Please review"}


@respx.mock
async def test_transition_submittal_refuses_disallowed_target():
    _resolve(respx)
    respx.get(f"{SUBS}/items/s1").mock(return_value=httpx.Response(200, json=ITEM_WITH_ACTIONS))
    route = respx.post(f"{SUBS}/items/s1:transition").mock(return_value=httpx.Response(200, json={}))
    data = _parse(await _call("transition_submittal", {"submittal_id": "s1", "state_id": "sbc-2"}))
    assert data["error"] == "transition_not_allowed"
    assert not route.called


@respx.mock
async def test_transition_submittal_refuses_missing_required_field():
    _resolve(respx)
    respx.get(f"{SUBS}/items/s1").mock(return_value=httpx.Response(200, json=ITEM_WITH_ACTIONS))
    route = respx.post(f"{SUBS}/items/s1:transition").mock(return_value=httpx.Response(200, json={}))
    data = _parse(await _call("transition_submittal", {"submittal_id": "s1", "state_id": "mgr-1"}))
    assert data["error"] == "missing_required_fields" and "manager" in data["message"]
    assert not route.called


@respx.mock
async def test_transition_submittal_close_resolves_response_by_name():
    _resolve(respx)
    item = {"id": "s1", "permittedActions": [{"id": "Item::wf_transition", "mandatoryFields": ["stateId"],
            "transitions": [{"name": "Close", "stateTo": {"id": "sbc-2"}, "mandatoryFields": ["responseId"],
                             "transitionFields": ["responseId", "responseComment"]}]}]}
    respx.get(f"{SUBS}/items/s1").mock(return_value=httpx.Response(200, json=item))
    respx.get(f"{SUBS}/responses").mock(return_value=httpx.Response(200, json=_page(
        [{"id": "a1", "value": "Approved"}, {"id": "a2", "value": "Revise and resubmit"}])))
    route = respx.post(f"{SUBS}/items/s1:transition").mock(return_value=httpx.Response(200, json={"id": "s1"}))
    await _call("transition_submittal", {"submittal_id": "s1", "state_id": "sbc-2",
                                         "response": "approved", "response_comment": "OK"})
    assert json.loads(route.calls[0].request.content) == {
        "stateId": "sbc-2", "responseComment": "OK", "responseId": "a1"}


@respx.mock
async def test_unknown_response_name_lists_known():
    _resolve(respx)
    respx.get(f"{SUBS}/items/s1").mock(return_value=httpx.Response(200, json=ITEM_WITH_ACTIONS))
    respx.get(f"{SUBS}/responses").mock(return_value=httpx.Response(200, json=_page([{"id": "a1", "value": "Approved"}])))
    with pytest.raises(ValueError, match="Known responses"):
        await _call("transition_submittal", {"submittal_id": "s1", "state_id": "void", "response": "Nope"})


@respx.mock
async def test_submit_review_auto_detects_my_task():
    _resolve(respx)
    respx.get(f"{SUBS}/responses").mock(return_value=httpx.Response(200, json=_page([{"id": "a1", "value": "Approved"}])))
    respx.get(f"{SUBS}/items/s1/steps").mock(return_value=httpx.Response(200, json=_page([{"id": "st1"}, {"id": "st2"}])))
    respx.get(f"{SUBS}/items/s1/steps/st1/tasks").mock(return_value=httpx.Response(200, json=_page(
        [{"id": "tk-other", "permittedActions": [{"id": "Task::retrieve"}]}])))
    respx.get(f"{SUBS}/items/s1/steps/st2/tasks").mock(return_value=httpx.Response(200, json=_page(
        [{"id": "tk-mine", "permittedActions": [{"id": "Task::retrieve"}, {"id": "Task::close"}]}])))
    route = respx.post(f"{SUBS}/items/s1/steps/st2/tasks/tk-mine:close").mock(
        return_value=httpx.Response(200, json={"id": "tk-mine", "status": "closed"}))
    data = _parse(await _call("submit_submittal_review", {
        "submittal_id": "s1", "response": "Approved", "response_comment": "Looks good"}))
    assert data["status"] == "review_submitted" and data["task_id"] == "tk-mine"
    assert json.loads(route.calls[0].request.content) == {"responseId": "a1", "responseComment": "Looks good"}


@respx.mock
async def test_submit_review_without_open_task_posts_nothing():
    _resolve(respx)
    respx.get(f"{SUBS}/items/s1/steps").mock(return_value=httpx.Response(200, json=_page([{"id": "st1"}])))
    respx.get(f"{SUBS}/items/s1/steps/st1/tasks").mock(return_value=httpx.Response(200, json=_page(
        [{"id": "tk1", "permittedActions": [{"id": "Task::retrieve"}]}])))
    data = _parse(await _call("submit_submittal_review", {"submittal_id": "s1", "response_id": "a1"}))
    assert data["error"] == "no_open_task"
