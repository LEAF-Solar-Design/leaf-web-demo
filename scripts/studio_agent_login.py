"""Approve a public Studio login challenge using an environment DPAPI package.

No credential access at import time. Output is deliberately limited to receipts;
provider bodies, exception strings, secret material and JWTs are never emitted.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

ISSUER = "https://leafautomation.us.auth0.com/"
NS = "https://leafdesign.ai/"
TARGETS = {
    "staging": ("https://platform-staging.leafdesign.ai", "https://api.leafdesign.ai"),
    "production": ("https://platform.leafdesign.ai", "https://api.leafdesign.ai"),
}
MAX_RESPONSE = 131072


class LoginFailure(Exception):
    pass


class ReceiptParser(argparse.ArgumentParser):
    def error(self, message):
        # Argument errors must not echo arbitrary input back to a log/console.
        raise LoginFailure()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib must not follow even a same-origin redirect with credentials.
        raise LoginFailure()


def request_json(url, *, body=None, bearer=None):
    if not (url.startswith(ISSUER) or any(url.startswith(v[0] + "/") for v in TARGETS.values())):
        raise LoginFailure()
    # Cloudflare answers the default Python-urllib agent with Error 1010, so name this client.
    headers = {"Accept": "application/json", "Cache-Control": "no-cache, no-store",
               "User-Agent": "leaf-studio-agent-login/1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if bearer is not None:
        if not isinstance(bearer, str) or not bearer or any(c.isspace() for c in bearer):
            raise LoginFailure()
        headers["Authorization"] = "Bearer " + bearer
    req = Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
    # TLS uses the stdlib's default certificate/hostname validation. Do not use
    # ambient proxy settings, cookies, netrc, provider SDKs or credential env vars.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=20) as res:
            if res.status != 200 or res.geturl() != url:
                raise LoginFailure()
            raw = res.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise LoginFailure()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise LoginFailure()
            return result
    except HTTPError:
        raise LoginFailure() from None


def credential_package(environment):
    if os.name != "nt" or environment not in TARGETS:
        raise LoginFailure()
    local = os.environ.get("LOCALAPPDATA")
    system = os.environ.get("SystemRoot")
    if not local or not system:
        raise LoginFailure()
    path = Path(local) / "LeafAutomation" / "StudioAgentAdmin" / (environment + ".dpapi")
    powershell = Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # Only a fixed program is an argv value. Ciphertext crosses stdin; the
    # decrypted JSON crosses captured stdout, never a console or temp file.
    program = r"""$ErrorActionPreference='Stop'; try {
Add-Type -AssemblyName System.Security;
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);
$inputBytes=[Convert]::FromBase64String([Console]::In.ReadToEnd());
$plain=[System.Security.Cryptography.ProtectedData]::Unprotect($inputBytes,$null,[System.Security.Cryptography.DataProtectionScope]::CurrentUser);
[Console]::Out.Write([Text.Encoding]::UTF8.GetString($plain));
[Array]::Clear($plain,0,$plain.Length); exit 0
} catch { exit 1 }"""
    import base64
    with path.open("rb") as credential_file:
        encrypted = credential_file.read(MAX_RESPONSE + 1)
    if not encrypted or len(encrypted) > MAX_RESPONSE:
        raise LoginFailure()
    # Isolated child environment: no inherited Auth0/Leaf credentials or profile.
    completed = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", program],
        input=base64.b64encode(encrypted), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={"SystemRoot": system}, timeout=20, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode != 0 or len(completed.stdout) > MAX_RESPONSE:
        raise LoginFailure()
    return validate_package(json.loads(completed.stdout), environment)


def validate_package(package, environment):
    if environment not in TARGETS or not isinstance(package, dict):
        raise LoginFailure()
    # This purpose-named sealed package is the operator's client identity pin.
    # Client IDs are not guessed or accepted from argv/ambient environment.
    if (package.get("schema") != "leaf.studio-agent-admin.credential.v1"
            or package.get("environment") != environment
            or package.get("domain") != "leafautomation.us.auth0.com"
            or package.get("audience") != TARGETS[environment][1]
            or not isinstance(package.get("client_id"), str)
            or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", package["client_id"]) is None
            or not isinstance(package.get("client_secret"), str)
            or not package["client_secret"] or len(package["client_secret"]) > 8192):
        raise LoginFailure()
    return package


def verify_token(token, package):
    import jwt
    if not isinstance(token, str) or len(token) > 32768:
        raise LoginFailure()
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise LoginFailure()
    keys = request_json(ISSUER + ".well-known/jwks.json").get("keys")
    if not isinstance(keys, list):
        raise LoginFailure()
    matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]
               and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"
               and key.get("alg", "RS256") == "RS256"]
    if len(matches) != 1:
        raise LoginFailure()
    payload = jwt.decode(token, jwt.PyJWK.from_dict(matches[0]).key, algorithms=["RS256"],
                         issuer=ISSUER, audience=package["audience"],
                         options={"require": ["iss", "aud", "exp", "iat", "sub", "azp"]})
    if (payload.get("sub") != package["client_id"] + "@clients"
            or payload.get("azp") != package["client_id"]
            or payload.get(NS + "principal_type") != "service_admin"
            or payload.get(NS + "tier") != "admin"
            or type(payload.get("exp")) not in (int, float)
            or payload["exp"] <= time.time()):
        raise LoginFailure()
    return payload


def check_source(origin, source):
    health = request_json(origin + "/api/health")
    # Fresh request, fixed HTTPS host, exact immutable release source receipt.
    if (health.get("ok") is not True or health.get("release_source_sha") != source
            or health.get("source_identity") not in {"built", "adopted"}):
        raise LoginFailure()


def approve(environment, challenge, source):
    if environment not in TARGETS or re.fullmatch(r"[0-9a-f]{40}", source) is None:
        raise LoginFailure()
    if str(UUID(challenge)) != challenge:
        raise LoginFailure()
    origin = TARGETS[environment][0]
    check_source(origin, source)
    metadata = request_json(origin + "/api/auth/agent/challenge/" + challenge + "/status")
    if (metadata.get("exists") is not True or metadata.get("pending") is not True
            or metadata.get("expired") is not False
            or metadata.get("environment") != environment or metadata.get("origin") != origin):
        raise LoginFailure()
    package = credential_package(environment)
    provider = request_json(ISSUER + "oauth/token", body={
        "grant_type": "client_credentials", "client_id": package["client_id"],
        "client_secret": package["client_secret"], "audience": package["audience"]})
    token = provider.get("access_token")
    if provider.get("token_type", "").lower() != "bearer":
        raise LoginFailure()
    verify_token(token, package)
    # Recheck after the mint, immediately before sending the privileged bearer.
    check_source(origin, source)
    result = request_json(origin + "/api/auth/agent/approve",
                          body={"challenge_id": challenge}, bearer=token)
    if (result.get("approved") is not True or result.get("challenge_id") != challenge
            or result.get("environment") != environment or result.get("origin") != origin):
        raise LoginFailure()


def main(argv=None):
    parser = ReceiptParser(description="Approve a public Studio service login challenge")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("approve")
    command.add_argument("--environment", choices=sorted(TARGETS), required=True)
    command.add_argument("--challenge-id", required=True)
    command.add_argument("--expect-source", required=True)
    environment = None
    try:
        args = parser.parse_args(argv)
        environment = args.environment
        approve(args.environment, args.challenge_id, args.expect_source)
    except Exception:
        print(json.dumps({"approved": False, "environment": environment,
                          "error": "service login approval failed"}))
        return 1
    print(json.dumps({"approved": True, "environment": args.environment,
                      "challenge_id": args.challenge_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
