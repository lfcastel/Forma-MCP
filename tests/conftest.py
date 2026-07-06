"""
Shared fixtures and env setup for all tests.
Must be imported before aps_mcp to satisfy the env-var check at module load.
"""
import os
import sys

# Fake credentials so the module-level os.environ[] calls don't blow up on import
os.environ.setdefault("APS_CLIENT_ID", "test_client_id")
os.environ.setdefault("APS_CLIENT_SECRET", "test_client_secret")

# Make the project root importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

# ---------------------------------------------------------------------------
# Shared fake IDs used across all test modules
# ---------------------------------------------------------------------------

FAKE_TOKEN = "fake_3leg_token"
FAKE_APP_TOKEN = "fake_2leg_token"

HUB_ID = "b.test-hub-id"
ACCOUNT_ID = "test-hub-id"        # HUB_ID without "b." prefix
PROJECT_ID = "b.test-proj-id"
BARE_PROJECT_ID = "test-proj-id"  # PROJECT_ID without "b." prefix
PROJECT_NAME = "Test Project"

ROLE_ID_VIEWER = "role-viewer-id"
ROLE_ID_EDITOR = "role-editor-id"
ROLE_NAME_VIEWER = "Viewer"
ROLE_NAME_EDITOR = "Editor"

USER_A_EMAIL = "alice@example.com"
USER_A_ID = "user-id-alice"
USER_B_EMAIL = "bob@example.com"
USER_B_ID = "user-id-bob"

COMPANY_NAME = "Acme Corp"
COMPANY_ID = "company-acme-id"

# Hub-onboarding test fixtures
NEW_USER_EMAIL = "carol@example.com"      # not in ACCOUNT_USERS_RESPONSE
NEW_COMPANY_NAME = "Beta Builders"        # not in ACCOUNT_COMPANIES_RESPONSE


# ---------------------------------------------------------------------------
# Common mock API responses
# ---------------------------------------------------------------------------

HUB_RESPONSE = {
    "data": [{
        "id": HUB_ID,
        "attributes": {"name": "Test Hub", "region": "EMEA", "hubType": "autodesk.bim360:Account"},
    }]
}

PROJECTS_RESPONSE = {
    "data": [{
        "id": PROJECT_ID,
        "attributes": {"name": PROJECT_NAME, "status": "active", "projectType": "ACC"},
    }],
    "meta": {"pagination": {"totalResults": 1}},
}

ACCOUNT_USERS_RESPONSE = [
    {
        "id": USER_A_ID, "email": USER_A_EMAIL, "first_name": "Alice", "last_name": "Test",
        "company_name": COMPANY_NAME, "status": "active", "last_sign_in": "2026-04-01",
    },
    {
        "id": USER_B_ID, "email": USER_B_EMAIL, "first_name": "Bob", "last_name": "Test",
        "company_name": COMPANY_NAME, "status": "active", "last_sign_in": "2026-04-10",
    },
]

# Response for _fetch_project_roles  (GET /projects/{project_id}/users)
PROJECT_ROLES_RESPONSE = {
    "results": [
        {
            "email": USER_A_EMAIL,
            "roles": [
                {"id": ROLE_ID_VIEWER, "name": ROLE_NAME_VIEWER},
                {"id": ROLE_ID_EDITOR, "name": ROLE_NAME_EDITOR},
            ],
        }
    ]
}

# Response for _get_project_members_map AND _fetch_project_roles
# (both call GET /construction/admin/v1/projects/{id}/users)
# Includes both 'roles' (for _fetch_project_roles) and member fields (for _get_project_members_map).
PROJECT_MEMBERS_RESPONSE = {
    "results": [
        {
            "id": USER_A_ID,
            "email": USER_A_EMAIL,
            "name": "Alice Test",
            "roleId": ROLE_ID_VIEWER,
            "status": "active",
            "companyName": COMPANY_NAME,
            "roles": [
                {"id": ROLE_ID_VIEWER, "name": ROLE_NAME_VIEWER},
                {"id": ROLE_ID_EDITOR, "name": ROLE_NAME_EDITOR},
            ],
        }
    ]
}

IMPORT_SUCCESS_RESPONSE = [
    {"email": USER_A_EMAIL, "success": True, "message": ""},
    {"email": USER_B_EMAIL, "success": True, "message": ""},
]

# Account company directory: GET construction/admin/v1/accounts/{id}/companies
# returns a {pagination, results} envelope.
ACCOUNT_COMPANIES_RESPONSE = {
    "pagination": {"limit": 200, "offset": 0, "totalResults": 1},
    "results": [
        {"id": COMPANY_ID, "name": COMPANY_NAME, "trade": "General Contractor", "status": "active"},
    ],
}

# HQ bulk-import envelope (POST users/import and companies/import share this shape).
USER_IMPORT_SUCCESS_RESPONSE = {
    "success": 1, "failure": 0,
    "success_items": [{"email": NEW_USER_EMAIL}],
    "failure_items": [],
}
COMPANY_IMPORT_SUCCESS_RESPONSE = {
    "success": 1, "failure": 0,
    "success_items": [{"name": NEW_COMPANY_NAME}],
    "failure_items": [],
}
