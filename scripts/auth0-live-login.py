"""Interactive Auth0 Authorization Code + PKCE login for the Leaf SPA.

The access token is written to C:\\tmp\\leaf-grants and is never printed.  The
receipt contains only non-secret verification metadata and namespaced claims.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


ISSUER = "https://leafautomation.us.auth0.com"
AUDIENCE = "https://api.leafdesign.ai"
CLIENT_ID = "zkJjr0ZFtcyQjyJ8e4zdkdgzoMaVWt5O"
REDIRECT_URI = os.environ.get("LEAF_AUTH0_REDIRECT_URI", "http://localhost:8080")
TOKEN_PATH = Path(r"C:\tmp\leaf-grants\auth0-access-token.txt")
RECEIPT_PATH = Path(r"C:\tmp\leaf-web-demo\docs\auth0-live-login-receipt.json")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


verifier = b64url(secrets.token_bytes(64))
challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
state = secrets.token_urlsafe(32)
params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "scope": "openid profile email",
    "audience": AUDIENCE,
    "connection": "google-oauth2",
    "code_challenge": challenge,
    "code_challenge_method": "S256",
    "state": state,
}
authorize_url = f"{ISSUER}/authorize?{urllib.parse.urlencode(params)}"
callback: dict[str, str] = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        callback.update({key: values[0] for key, values in query.items() if values})
        ok = callback.get("state") == state and "code" in callback
        body = (
            "Leaf Auth0 login received. You can return to Codex."
            if ok
            else "Leaf Auth0 login failed. You can return to Codex for details."
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, _format: str, *_args: object) -> None:
        return


print(f"AUTH0_AUTHORIZE_URL={authorize_url}", flush=True)
if os.environ.get("LEAF_AUTH0_OPEN_BROWSER") == "1":
    webbrowser.open(authorize_url)
redirect = urllib.parse.urlparse(REDIRECT_URI)
if redirect.scheme != "http" or redirect.hostname not in {"localhost", "127.0.0.1"}:
    raise SystemExit("LEAF_AUTH0_REDIRECT_URI must use http://localhost or http://127.0.0.1")
callback_port = redirect.port
if callback_port is None:
    raise SystemExit("LEAF_AUTH0_REDIRECT_URI must include an explicit port")
server = HTTPServer(("127.0.0.1", callback_port), CallbackHandler)
server.timeout = 300
server.handle_request()
server.server_close()

if callback.get("state") != state:
    raise SystemExit("Auth0 callback state mismatch or timeout")
if "error" in callback:
    raise SystemExit(f"Auth0 callback error: {callback['error']}")
if "code" not in callback:
    raise SystemExit("Auth0 callback did not include an authorization code")

token_body = urllib.parse.urlencode(
    {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": callback["code"],
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }
).encode("ascii")
request = urllib.request.Request(
    f"{ISSUER}/oauth/token",
    data=token_body,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        token_response = json.load(response)
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", errors="replace")
    raise SystemExit(f"Auth0 token exchange failed ({exc.code}): {detail[:500]}") from exc

access_token = token_response.get("access_token")
if not isinstance(access_token, str) or not access_token:
    raise SystemExit("Auth0 token response did not contain an access token")

TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
TOKEN_PATH.write_text(access_token, encoding="utf-8")

payload_segment = access_token.split(".")[1]
payload_segment += "=" * (-len(payload_segment) % 4)
payload = json.loads(base64.urlsafe_b64decode(payload_segment))
namespace = "https://leafdesign.ai/"
receipt = {
    "issuer": payload.get("iss"),
    "audience": payload.get("aud"),
    "subject": payload.get("sub"),
    "authorized_party": payload.get("azp"),
    "expires_at": payload.get("exp"),
    "tenant_id": payload.get(namespace + "tenant_id"),
    "org_id": payload.get(namespace + "org_id"),
    "tier": payload.get(namespace + "tier"),
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "token_file": str(TOKEN_PATH),
}
RECEIPT_PATH.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(f"AUTH0_LOGIN_RECEIPT={RECEIPT_PATH}", flush=True)
