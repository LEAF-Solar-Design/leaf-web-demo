"""Verify the Leaf server against a real Auth0 access token without printing it."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"


def _default_token_dir() -> Path:
    """Same per-user, non-world-writable default as auth0-live-login.py, so the
    login script's written token is found here by default. Keep both in sync."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "leaf-grants"
    return Path(os.path.expanduser("~")) / ".leaf-grants"


# Mirror auth0-live-login.py exactly: the profile-based default plus the
# LEAF_AUTH0_TOKEN_PATH override. A hardcoded C:\tmp path both broke the default
# login->verify flow (login now writes under the profile) and, if restored via
# the override, would reintroduce the shared-parent race.
TOKEN_PATH = Path(
    os.environ.get("LEAF_AUTH0_TOKEN_PATH")
    or _default_token_dir() / "auth0-access-token.txt"
)
RECEIPT_PATH = ROOT / "docs" / "auth0-live-server-receipt.json"

os.environ["LEAF_AUTH_LIVE"] = "1"
os.environ["LEAF_AUTH0_ISSUER"] = "https://leafautomation.us.auth0.com/"
os.environ["LEAF_AUTH0_AUDIENCE"] = "https://api.leafdesign.ai"
os.environ["LEAF_AUTH0_JWKS_URL"] = (
    "https://leafautomation.us.auth0.com/.well-known/jwks.json"
)
os.environ.pop("LEAF_AUTH0_JWKS_FILE", None)
os.environ["APS_LIVE"] = "0"

os.chdir(SERVER)
sys.path.insert(0, str(SERVER))

from fastapi.testclient import TestClient  # noqa: E402
from app import app  # noqa: E402


token = TOKEN_PATH.read_text(encoding="utf-8").strip()
headers = {"Authorization": f"Bearer {token}"}
client = TestClient(app)

unauthenticated = client.get("/api/session")
session = client.get("/api/session", headers=headers)
entitlements = client.get("/api/entitlements", headers=headers)
author_denied = client.post(
    "/api/author",
    headers=headers,
    json={"description": "verification-only authoring request"},
)

session_body = session.json()
entitlements_body = entitlements.json()
denied_body = author_denied.json()

assert unauthenticated.status_code == 401, unauthenticated.text
assert session.status_code == 200, session.text
assert session_body.get("tenant_id"), session_body
assert session_body.get("tier") == "hosted_starter", session_body
assert entitlements.status_code == 200, entitlements.text
assert entitlements_body.get("entitlements", {}).get("run_read") is True
assert entitlements_body.get("entitlements", {}).get("build") is False
assert author_denied.status_code == 403, author_denied.text
assert denied_body.get("entitlement_required") is True, denied_body
assert denied_body.get("required") == "build", denied_body

receipt = {
    "auth_live": True,
    "issuer": os.environ["LEAF_AUTH0_ISSUER"],
    "audience": os.environ["LEAF_AUTH0_AUDIENCE"],
    "matrix": {
        "no_bearer": unauthenticated.status_code,
        "real_token_session": session.status_code,
        "tier_denied_author": author_denied.status_code,
    },
    "echo": {
        "tenant_id": session_body.get("tenant_id"),
        "org_id": session_body.get("org_id"),
        "tier": session_body.get("tier"),
    },
    "entitlements": entitlements_body.get("entitlements"),
    "denied": {
        "entitlement_required": denied_body.get("entitlement_required"),
        "required": denied_body.get("required"),
        "tier": denied_body.get("tier"),
    },
    "token_file": str(TOKEN_PATH),
}
RECEIPT_PATH.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(f"AUTH0_LIVE_VERIFY_RECEIPT={RECEIPT_PATH}")
