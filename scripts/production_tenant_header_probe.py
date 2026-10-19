#!/usr/bin/env python3
"""Read-only production acceptance and environment truth probe (stdlib only)."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(base_url, path, headers, timeout_s):
    """Single HTTP boundary: return (status, parsed JSON or None).

    HTTP refusals are observations; transport exceptions propagate to main.
    Never follow a redirect with the acceptance bearer attached.
    """
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(base_url + path, headers=headers, method="GET")
    try:
        response = opener.open(request, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status = response.code
        if status != 200:
            return status, None
        try:
            return status, json.loads(response.read())
        except (ValueError, UnicodeError):
            return status, None


def _base_url(value):
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid base URL")
    parsed = urlsplit(value)
    port = parsed.port
    if (not parsed.hostname or port == 0 or "\\" in parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or "?" in value or "#" in value):
        raise ValueError("invalid base URL")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    ):
        raise ValueError("HTTPS required except on loopback")
    return value.rstrip("/")


def _org(value):
    return str(uuid.UUID(value))


def _catalog(values, key):
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise ValueError("invalid catalog")
    values = sorted(values)
    canonical = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return {"count": len(values), key: values,
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _redact(value, token):
    # Even a server that echoes credentials into an allowlisted field cannot
    # cause this probe to persist the bearer.
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    if isinstance(value, dict):
        return {_redact(k, token): _redact(v, token) for k, v in value.items()}
    return value


def _write_snapshot(path, snapshot, token):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix="." + path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(_redact(snapshot, token), stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


class _CheckFailed(Exception):
    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--acceptance-org-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--env", default="production")
    parser.add_argument("--timeout-s", default=20, type=float)
    args = parser.parse_args(argv)
    token = os.environ.get("LEAF_PROBE_BEARER", "")
    try:
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("missing or invalid bearer")
        base_url = _base_url(args.base_url)
        acceptance_org = _org(args.acceptance_org_id)
        if not math.isfinite(args.timeout_s) or args.timeout_s <= 0:
            raise ValueError("invalid timeout")
    except (ValueError, AttributeError):
        print("probe: invalid configuration", file=sys.stderr)
        return 2

    snapshot = {
        "schema": "leaf.studio-environment-truth.v1", "env": args.env,
        "base_url": base_url,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": False, "failed_check": None,
        "session": {"tenant_id": None, "org_id": None, "acceptance_org_match": False},
        "spoof": {"status": None, "honored": False},
        "deployment_identity": {"status": None, "services": {}},
        "capabilities": None, "tools": None,
    }
    headers = {"Authorization": "Bearer " + token,
               "User-Agent": "leaf-production-acceptance-probe/1.0"}
    check = "session"
    code = 1
    try:
        status, body = http_get(base_url, "/api/session", headers, args.timeout_s)
        if status != 200 or not isinstance(body, dict):
            raise _CheckFailed("acceptance_org_mismatch")
        tenant, org = body.get("tenant_id"), body.get("org_id")
        match = False
        try:
            match = isinstance(tenant, str) and bool(tenant) and _org(org) == acceptance_org
        except (ValueError, TypeError, AttributeError):
            pass
        snapshot["session"] = {
            "tenant_id": tenant if isinstance(tenant, str) else None,
            "org_id": org if isinstance(org, str) else None,
            "acceptance_org_match": match,
        }
        if not match:
            raise _CheckFailed("acceptance_org_mismatch")

        check = "spoof"
        foreign = str(uuid.uuid4())
        while foreign == tenant or foreign == org:
            foreign = str(uuid.uuid4())
        status, body = http_get(base_url, "/api/session",
                                {**headers, "X-Tenant-Id": foreign}, args.timeout_s)
        honored = False
        same = False
        if status == 200:
            try:
                same = (isinstance(body, dict) and body.get("tenant_id") == tenant
                        and _org(body.get("org_id")) == _org(org))
            except (ValueError, TypeError, AttributeError):
                pass
            honored = not same
        snapshot["spoof"] = {"status": status, "honored": honored}
        if not (same or 400 <= status < 500):
            raise _CheckFailed("spoofed_tenant_honored")

        check = "deployment_identity"
        status, body = http_get(base_url, "/api/deployment-identity", headers, args.timeout_s)
        if status != 200 or not isinstance(body, dict):
            raise _CheckFailed("deployment_identity_not_verified")
        services = body.get("services")
        snapshot["deployment_identity"]["status"] = (
            body.get("status") if isinstance(body.get("status"), str) else None)
        if not isinstance(services, dict):
            raise _CheckFailed("deployment_identity_not_verified")
        snapshot["deployment_identity"]["services"] = {
            name: {"digest": entry.get("image_digest") if isinstance(entry.get("image_digest"), str) else None,
                   "commit": entry.get("source_revision") if isinstance(entry.get("source_revision"), str) else None}
            for name, entry in services.items() if isinstance(entry, dict)
        }
        if body.get("status") != "verified":
            raise _CheckFailed("deployment_identity_not_verified")

        for check in ("capabilities", "tools"):
            status, body = http_get(base_url, "/api/" + check, headers, args.timeout_s)
            try:
                if status != 200 or not isinstance(body, dict):
                    raise ValueError("invalid response")
                if check == "capabilities":
                    # server.catalog._capability_entry identifies each capability by name.
                    families = body["families"]
                    if not isinstance(families, list) or any(
                        not isinstance(f, dict) or not isinstance(f.get("capabilities"), list)
                        for f in families
                    ):
                        raise ValueError("invalid families")
                    values = [entry["name"] for family in families
                              for entry in family["capabilities"]]
                    key = "ids"
                else:
                    if not isinstance(body.get("tools"), list):
                        raise ValueError("invalid tools")
                    values = [entry["name"] for entry in body["tools"]]
                    key = "names"
                snapshot[check] = _catalog(values, key)
            except (KeyError, TypeError, ValueError):
                raise _CheckFailed("invalid_" + check) from None
        snapshot["ok"] = True
        code = 0
    except _CheckFailed as exc:
        snapshot["failed_check"] = str(exc)
    except (OSError, urllib.error.URLError):
        snapshot["failed_check"] = "unreadable_" + check
        code = 3
    try:
        _write_snapshot(args.out, snapshot, token)
    except OSError:
        print("probe: snapshot could not be written", file=sys.stderr)
        return 3
    return code


if __name__ == "__main__":
    raise SystemExit(main())
