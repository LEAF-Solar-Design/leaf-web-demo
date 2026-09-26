"""Check and explicitly deploy the two Leaf tenant-claim Auth0 Actions.

Check requires read:actions read:triggers. Deploy also requires update:actions
and create:actions, which Auth0 requires to deploy an existing Action.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request


TARGETS = (
    {"file": "server/auth0-actions/post-login-add-tenant-claim.js",
     "trigger": "post-login", "action_name": "post-login-add-tenant-claim"},
    {"file": "server/auth0-actions/credentials-exchange-add-tenant-claim.js",
     "trigger": "credentials-exchange",
     "action_name": "credentials-exchange-add-tenant-claim"},
)
REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.auth0\.com$")
ID_RE = re.compile(r"[A-Za-z0-9_-]+")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CHECK_SCOPES = "read:actions read:triggers"
DEPLOY_SCOPES = CHECK_SCOPES + " update:actions create:actions"
DRAFT_BUILD_TIMEOUT_SECONDS = 60
REPORT_KEYS = ("trigger", "action_name", "repo_sha256", "deployed_sha256", "state")


class SyncError(Exception):
    """Fixed error codes with optional diagnostics built from safe values only."""

    def __init__(self, code, *, diagnostic=None):
        super().__init__(code)
        self.diagnostic = diagnostic


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes user-supplied argument values.
        raise SyncError("arguments_invalid")


def build_parser():
    parser = _Parser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--deploy", action="store_true")
    parser.add_argument("--domain", required=True, metavar="HOST")
    parser.add_argument("--confirm", metavar="DIGEST")
    return parser


def normalized_sha256(code: str) -> str:
    normalized = code.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def plan_digest(targets) -> Optional[str]:
    changes = [{key: target[key] for key in REPORT_KEYS[:-1]}
               for target in targets if target["state"] in ("drift", "missing")]
    if not changes:
        return None
    payload = json.dumps(sorted(changes, key=lambda target: target["trigger"]),
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpsTransport:
    """HTTPS with a fixed host, an operation allowlist, and bounded responses."""

    def __init__(self, domain, *, allow_writes: bool):
        if not isinstance(domain, str) or DOMAIN_RE.fullmatch(domain) is None:
            raise SyncError("domain_invalid")
        self.domain = domain
        self.allow_writes = allow_writes
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method, path, *, body=None, query=None, token=None):
        token_request = method == "POST" and path == "/oauth/token"
        read = method == "GET" and isinstance(path, str) and re.fullmatch(
            r"/api/v2/actions/(?:triggers/[a-z-]+/bindings|"
            r"actions/[A-Za-z0-9_-]+(?:/versions/[A-Za-z0-9_-]+)?)", path)
        write = self.allow_writes and isinstance(path, str) and (
            (method == "PATCH" and re.fullmatch(
                r"/api/v2/actions/actions/[A-Za-z0-9_-]+", path))
            or (method == "POST" and re.fullmatch(
                r"/api/v2/actions/actions/[A-Za-z0-9_-]+/deploy", path)))
        if not (token_request or read or write):
            raise SyncError("operation_not_allowed")
        if (token_request and (token is not None or query is not None)) or (
                read and body is not None) or (write and query is not None):
            raise SyncError("operation_not_allowed")
        try:
            url = "https://" + self.domain + path
            if query:
                url += "?" + urllib.parse.urlencode(query)
            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            payload = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            with self._opener.open(request, timeout=30) as response:
                if response.geturl() != url or not 200 <= response.status < 300:
                    raise SyncError("response_rejected")
                raw = response.read(MAX_RESPONSE_BYTES)
                # Reject a full buffer rather than reading beyond the cap.
                if len(raw) >= MAX_RESPONSE_BYTES:
                    raise SyncError("response_too_large")
                value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise SyncError("response_invalid")
            return value
        except SyncError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                scopes = DEPLOY_SCOPES if self.allow_writes else CHECK_SCOPES
                raise SyncError(
                    "management_forbidden",
                    diagnostic=f"Auth0 refused access (403); the machine client needs {scopes}.") from None
            raise SyncError("request_failed") from None
        except Exception:
            # HTTP bodies and exception messages can echo credentials or code.
            raise SyncError("request_failed") from None


def _id(value):
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise SyncError("action_identity_invalid")
    return value


def _bindings(transport, token, trigger):
    bindings = []
    expected_total = None
    seen_ids = set()
    for page in range(20):
        response = transport.request(
            "GET", f"/api/v2/actions/triggers/{trigger}/bindings", token=token,
            query={"page": str(page), "per_page": "50"})
        batch = response.get("bindings")
        total = response.get("total")
        if (not isinstance(batch, list) or len(batch) > 50
                or type(total) is not int or not 0 <= total <= 1000
                or response.get("per_page") != 50
                or response.get("page", page) != page):
            raise SyncError("binding_pagination_invalid")
        if expected_total is not None and total != expected_total:
            raise SyncError("binding_inventory_drift")
        expected_total = total
        for binding in batch:
            if not isinstance(binding, dict) or not isinstance(binding.get("action"), dict):
                raise SyncError("binding_invalid")
            binding_id = _id(binding.get("id"))
            if binding_id in seen_ids:
                raise SyncError("binding_pagination_duplicate")
            seen_ids.add(binding_id)
            bindings.append(binding)
        if len(bindings) == total:
            return bindings
        if len(bindings) > total or len(batch) != 50:
            raise SyncError("binding_pagination_incomplete")
    raise SyncError("binding_pagination_incomplete")


def _action_version(transport, token, action_id, action_name):
    action = transport.request("GET", f"/api/v2/actions/actions/{action_id}", token=token)
    if action.get("id") != action_id or action.get("name") != action_name:
        raise SyncError("action_binding_drift")
    deployed = action.get("deployed_version")
    if not isinstance(deployed, dict):
        raise SyncError("deployed_version_invalid")
    return _id(deployed.get("id"))


def _deployed_hash(transport, token, action_id, version_id):
    version = transport.request(
        "GET", f"/api/v2/actions/actions/{action_id}/versions/{version_id}", token=token)
    if (version.get("id") != version_id or version.get("action_id") != action_id
            or version.get("deployed") is not True or version.get("status") != "built"
            or not isinstance(version.get("code"), str)):
        raise SyncError("deployed_version_invalid")
    return normalized_sha256(version["code"])


def _collect(transport, token):
    targets = []
    for target in TARGETS:
        # Read before any writes; retain a single source snapshot for this run.
        code = (REPO_ROOT / target["file"]).read_bytes().decode("utf-8").replace("\r\n", "\n")
        entry = {"trigger": target["trigger"], "action_name": target["action_name"],
                 "repo_sha256": normalized_sha256(code), "deployed_sha256": None,
                 "state": "missing", "code": code}
        matches = [binding["action"] for binding in _bindings(transport, token, target["trigger"])
                   if binding["action"].get("name") == target["action_name"]]
        if len(matches) > 1:
            raise SyncError("action_ambiguous")
        if matches:
            action_id = _id(matches[0].get("id"))
            version_id = _action_version(transport, token, action_id, target["action_name"])
            entry.update(action_id=action_id, version_id=version_id,
                         deployed_sha256=_deployed_hash(transport, token, action_id, version_id))
            entry["state"] = "match" if entry["repo_sha256"] == entry["deployed_sha256"] else "drift"
        targets.append(entry)
    return targets


def _wait_for_draft(transport, token, target, sleep):
    deadline = time.monotonic() + DRAFT_BUILD_TIMEOUT_SECONDS
    last_status = "missing"
    while time.monotonic() < deadline:
        action = transport.request(
            "GET", f"/api/v2/actions/actions/{target['action_id']}", token=token)
        if action.get("id") != target["action_id"] or action.get("name") != target["action_name"]:
            raise SyncError("action_binding_drift")
        status = action.get("status")
        # Never echo arbitrary API data, which can contain code or credentials.
        last_status = status if status in ("pending", "built", "failed") else (
            "missing" if status is None else "unrecognized")
        remaining = deadline - time.monotonic()
        if last_status == "failed" or remaining <= 0:
            break
        if last_status == "built":
            return
        sleep(min(1, remaining))
    raise SyncError(
        "draft_build_failed" if last_status == "failed" else "draft_build_timeout",
        diagnostic=f"Action {target['action_name']} did not build; last status: {last_status}.")


def _deploy(transport, token, targets, sleep):
    for target in targets:
        if target["state"] != "drift":
            continue
        action_id = target["action_id"]
        path = f"/api/v2/actions/actions/{action_id}"
        transport.request("PATCH", path, body={"code": target["code"]}, token=token)
        _wait_for_draft(transport, token, target, sleep)
        transport.request("POST", path + "/deploy", token=token)
        for attempt in range(10):
            if attempt:
                sleep(3)
            version_id = _action_version(transport, token, action_id, target["action_name"])
            if version_id != target["version_id"]:
                break
        else:
            raise SyncError("deploy_not_observed")
        target["deployed_sha256"] = _deployed_hash(transport, token, action_id, version_id)
        target["state"] = "match" if target["repo_sha256"] == target["deployed_sha256"] else "drift"


def main(argv=None, *, transport=None, sleep=time.sleep) -> int:
    try:
        args = build_parser().parse_args(argv)
        if DOMAIN_RE.fullmatch(args.domain) is None:
            raise SyncError("domain_invalid")
        if args.deploy:
            if args.confirm is None or re.fullmatch(r"[0-9a-f]{64}", args.confirm) is None:
                raise SyncError("confirm_required")
        elif args.confirm is not None:
            raise SyncError("confirm_not_allowed")
        client_id = os.environ.get("LEAF_AUTH0_ACTIONS_CLIENT_ID")
        client_secret = os.environ.get("LEAF_AUTH0_ACTIONS_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise SyncError("management_credentials_missing")
        active = transport if transport is not None else HttpsTransport(args.domain, allow_writes=args.deploy)
        scope = DEPLOY_SCOPES if args.deploy else CHECK_SCOPES
        response = active.request("POST", "/oauth/token", body={
            "grant_type": "client_credentials", "client_id": client_id,
            "client_secret": client_secret, "audience": f"https://{args.domain}/api/v2/",
            "scope": scope})
        token = response.get("access_token")
        if (not isinstance(token, str) or not token
                or any(not 33 <= ord(character) <= 126 for character in token)):
            raise SyncError("management_token_invalid")
        targets = _collect(active, token)
        digest = plan_digest(targets)
        result = "match" if digest is None else "drift"
        if args.deploy and digest is not None:
            if any(target["state"] == "missing" for target in targets):
                raise SyncError("action_missing")
            if digest != args.confirm:
                raise SyncError("confirm_mismatch")
            _deploy(active, token, targets, sleep)
            result = "deployed" if all(target["state"] == "match" for target in targets) else "readback_mismatch"
        print(json.dumps({"schema": "leaf.auth0-actions-sync.v1",
                          "mode": "deploy" if args.deploy else "check", "domain": args.domain,
                          "targets": [{key: target[key] for key in REPORT_KEYS} for target in targets],
                          "plan_digest": digest, "result": result}, separators=(",", ":")))
        return 0 if result in ("match", "deployed") else 1
    except SyncError as exc:
        print("LEAF_AUTH0_ACTIONS_ERROR=" + str(exc), file=sys.stderr)
        if exc.diagnostic is not None:
            print(exc.diagnostic, file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code)
    except Exception:
        print("LEAF_AUTH0_ACTIONS_ERROR=unexpected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
