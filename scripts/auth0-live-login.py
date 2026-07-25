"""Interactive Auth0 Authorization Code + PKCE login for the Leaf SPA.

The access token is written under the current user's own profile directory
(``%LOCALAPPDATA%\\leaf-grants`` on Windows, ``~/.leaf-grants`` elsewhere) and is
never printed. That location is owned by the user and is NOT a world-writable
directory like ``C:\\tmp``, so no other local principal can pre-plant the
directory, replace it via ``DELETE_CHILD`` on the parent, or hold an open read
handle through a permission window -- the races that a token under ``C:\\tmp``
was exposed to. The receipt contains only non-secret verification metadata.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
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
def _default_token_dir() -> Path:
    """Per-user, non-world-writable directory for the token.

    ``%LOCALAPPDATA%`` (Windows) and ``~`` (POSIX) are owned by the current user,
    so no other local principal can pre-plant the directory, replace it via
    ``DELETE_CHILD`` on the parent, or otherwise race the token write -- unlike a
    shared root such as ``C:\\tmp``.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "leaf-grants"
    return Path(os.path.expanduser("~")) / ".leaf-grants"


TOKEN_PATH = Path(
    os.environ.get("LEAF_AUTH0_TOKEN_PATH")
    or _default_token_dir() / "auth0-access-token.txt"
)
RECEIPT_PATH = Path(
    os.environ.get(
        "LEAF_AUTH0_RECEIPT_PATH",
        r"C:\tmp\leaf-web-demo\docs\auth0-live-login-receipt.json",
    )
)


def _lock_dir_owner_only(directory: Path) -> None:
    """Lock ``directory`` so only the current user may access it, and so files
    CREATED inside it inherit that owner-only access.

    This is the key to closing the creation-time window: if the token file's
    parent is already owner-only with an INHERITABLE owner ACE, the file is
    owner-only the instant it is created, so no other local account ever has a
    window to open a read handle before the DACL is tightened. On Windows,
    ``/inheritance:r`` drops inherited ACEs and ``/grant:r "<user>:(OI)(CI)F"``
    grants the user full control, inheritable by files (OI) and subdirs (CI).
    Fail-closed: raise if the lock cannot be set.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        user = os.environ.get("USERNAME") or ""
        if not user:
            raise RuntimeError("USERNAME not set; cannot scope ACL to owner")
        # /inheritance:r severs inherited ACEs and /grant:r sets the current user
        # as the only principal, inheritable by files (OI) and subdirs (CI). The
        # directory lives under the user's own profile, so it cannot be
        # pre-planted with third-party ACEs by another local principal; the
        # read-back below still fails closed if any broad principal survives.
        res = subprocess.run(
            ["icacls", str(directory), "/inheritance:r",
             "/grant:r", f"{user}:(OI)(CI)F"],
            capture_output=True,
        )
        if res.returncode != 0:
            raise RuntimeError(
                "icacls (dir) failed: " + res.stderr.decode("utf-8", "replace").strip()
            )
        _assert_no_broad_access(directory)
    else:
        os.chmod(directory, 0o700)


# Well-known principals that must never retain access to the token directory.
_BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "Authenticated Users",
                     "NT AUTHORITY\\Authenticated Users", "Users")


def _assert_no_broad_access(directory: Path) -> None:
    """Read the DACL back and FAIL CLOSED if any broad principal still has access.

    ``/grant:r`` only replaces the named user's ACE, so this verifies the DACL we
    intended is the DACL that is actually in force before a secret is written.
    """
    res = subprocess.run(["icacls", str(directory)], capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(
            "icacls (verify) failed: " + res.stderr.decode("utf-8", "replace").strip()
        )
    out = res.stdout.decode("utf-8", "replace")
    for principal in _BROAD_PRINCIPALS:
        if principal + ":" in out:
            raise RuntimeError(
                f"token directory DACL still grants access to {principal!r}: {out.strip()}"
            )


def write_token_owner_only(path: Path, token: str) -> None:
    """Write ``token`` to ``path`` readable only by the current user, FAIL-CLOSED.

    The default token location sits under C:\\tmp, whose inherited ACL grants
    read to BUILTIN\\Users and modify to sandbox principals, so any other local
    account could read (or replace) a live bearer token. Hardening:

    * Lock the PARENT directory owner-only with an inheritable owner ACE FIRST,
      so the token file is owner-only from the instant it is created -- there is
      no window where it carries the broad inherited ACL for another account to
      grab a read handle through (icacls-after-create left exactly that window).
    * Remove any existing file, so a pre-planted symlink/junction or a stale
      token cannot redirect the write or survive it (dropping a reparse point
      deletes the link, not its target).
    * Create the file EXCLUSIVELY (O_CREAT|O_EXCL) inside the locked directory so
      a race that re-creates the path is caught instead of followed, and write
      the secret through that same handle.
    * If anything fails, DELETE the file and raise -- never leave a readable
      token behind (the original version warned and wrote it anyway).
    """
    _lock_dir_owner_only(path.parent)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    # The file inherits the parent's owner-only ACE at creation (Windows) / is
    # created 0600 (POSIX): owner-only before a single byte is written.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        os.write(fd, token.encode("utf-8"))
    except BaseException:
        # Fail closed: never leave a token we could not protect.
        os.close(fd)
        fd = None
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)


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

# Write the token owner-only and fail-closed: locked before the secret lands,
# and deleted rather than left exposed if the lock cannot be set.
write_token_owner_only(TOKEN_PATH, access_token)

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
