"""Read-only comparison of the declared Leaf SPA client with Auth0.

Credentials come only from the environment. No response body or credential is
logged; receipts contain comparison states and list differences only.
Exit codes: 0 match, 1 drift, 2 unavailable, 3 receipt write failed.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.auth0\.com$")
ID_RE = re.compile(r"[A-Za-z0-9_-]+")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DECLARED_PATH = Path(__file__).resolve().parents[1] / "auth0/leaf-web-platform-spa-client.json"
RECEIPT_SCHEMA = "leaf.auth0-spa-client-drift-check.v1"
ERROR_PREFIX = "LEAF_AUTH0_SPA_DRIFT_ERROR="
EXIT_CODES = {"match": 0, "drift": 1, "unavailable": 2}
_MISSING = object()


class CheckError(Exception):
    """A fixed error code; never an HTTP body or exception message."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CheckError("arguments_invalid")


def build_parser():
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--domain", required=True, metavar="HOST")
    parser.add_argument("--receipt-dir", required=True, type=Path)
    parser.add_argument("--declared", type=Path, default=DECLARED_PATH)
    return parser


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpsTransport:
    """Fixed tenant and client, two allowed operations, bounded HTTPS reads."""

    def __init__(self, domain, client_id):
        if not isinstance(domain, str) or DOMAIN_RE.fullmatch(domain) is None:
            raise CheckError("domain_invalid")
        if not isinstance(client_id, str) or ID_RE.fullmatch(client_id) is None:
            raise CheckError("declared_invalid")
        self.domain = domain
        self.client_path = "/api/v2/clients/" + client_id
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method, path, *, body=None, query=None, token=None):
        token_request = method == "POST" and path == "/oauth/token"
        read = method == "GET" and path == self.client_path
        if not (token_request or read):
            raise CheckError("operation_not_allowed")
        if (token_request and (token is not None or query is not None)) or (read and body is not None):
            raise CheckError("operation_not_allowed")
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
                    raise CheckError("request_failed")
                raw = response.read(MAX_RESPONSE_BYTES)
                if len(raw) >= MAX_RESPONSE_BYTES:
                    raise CheckError("request_failed")
                value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise CheckError("request_failed")
            return value
        except CheckError:
            raise
        except urllib.error.HTTPError as exc:
            raise CheckError("management_forbidden" if exc.code == 403 else "request_failed") from None
        except Exception:
            raise CheckError("request_failed") from None


def compare(declared, live):
    """Compare only declared keys; never include scalar values in a receipt."""
    if isinstance(declared, list):
        # JSON encodings also support lists of structured values without hashing
        # mutable objects. Include types so true and 1 are distinct.
        expected = {json.dumps(item, sort_keys=True) for item in declared}
        actual = ({json.dumps(item, sort_keys=True) for item in live}
                  if isinstance(live, list) else set())
        return {"state": "match" if isinstance(live, list) and actual == expected else "drift",
                "added": [json.loads(item) for item in sorted(actual - expected)],
                "removed": [json.loads(item) for item in sorted(expected - actual)]}
    if isinstance(declared, dict):
        fields = {key: compare(value, live.get(key, _MISSING) if isinstance(live, dict) else _MISSING)
                  for key, value in declared.items()}
        return {"state": "match" if isinstance(live, dict) and all(
            field["state"] == "match" for field in fields.values()) else "drift", "fields": fields}
    return {"state": "match" if type(declared) is type(live) and declared == live else "drift"}


def main(argv=None, *, transport=None, now=None):
    try:
        args = build_parser().parse_args(argv)
    except CheckError:
        print(ERROR_PREFIX + "arguments_invalid", file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)
    domain = args.domain if DOMAIN_RE.fullmatch(args.domain) else None
    client_id, fields, error = None, {}, None
    state = "unavailable"
    token = None
    secret = os.environ.get("LEAF_AUTH0_ACTIONS_CLIENT_SECRET")
    try:
        if domain is None:
            raise CheckError("domain_invalid")
        try:
            record = json.loads(args.declared.read_text(encoding="utf-8"))
            candidate = record["client_id"]
            declared = record["declared"]
            if (record["schema"] != "leaf.auth0-spa-client.v1"
                    or not isinstance(candidate, str) or ID_RE.fullmatch(candidate) is None
                    or not isinstance(declared, dict) or not declared):
                raise ValueError
            client_id = candidate
        except (OSError, ValueError, KeyError, TypeError):
            raise CheckError("declared_invalid") from None
        machine_id = os.environ.get("LEAF_AUTH0_ACTIONS_CLIENT_ID")
        if not machine_id or not secret:
            raise CheckError("credentials_missing")
        active = transport if transport is not None else HttpsTransport(domain, client_id)
        response = active.request("POST", "/oauth/token", body={
            "grant_type": "client_credentials", "client_id": machine_id,
            "client_secret": secret, "audience": f"https://{domain}/api/v2/", "scope": "read:clients"})
        token = response.get("access_token")
        if (not isinstance(token, str) or not token
                or any(not 33 <= ord(char) <= 126 for char in token)):
            raise CheckError("request_failed")
        live = active.request("GET", "/api/v2/clients/" + client_id, token=token,
                              query={"fields": ",".join(declared), "include_fields": "true"})
        if not isinstance(live, dict):
            raise CheckError("request_failed")
        fields = compare(declared, live)["fields"]
        state = "match" if all(field["state"] == "match" for field in fields.values()) else "drift"
    except CheckError as exc:
        error = str(exc) if str(exc) in {
            "domain_invalid", "declared_invalid", "credentials_missing", "management_forbidden",
            "request_failed", "operation_not_allowed"} else "request_failed"
    except Exception:
        error = "request_failed"
    receipt = {"schema": RECEIPT_SCHEMA, "domain": domain, "client_id": client_id,
               "state": state, "error": error, "fields": fields}
    text = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    # Defensive redaction even if an unexpected list value echoes a credential.
    for sensitive in (secret, token):
        if isinstance(sensitive, str) and sensitive:
            text = text.replace(json.dumps(sensitive)[1:-1], "[redacted]")
    checked = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        args.receipt_dir.mkdir(parents=True, exist_ok=True)
        path = args.receipt_dir / ("auth0-spa-client-drift-" + checked.strftime("%Y%m%dT%H%M%SZ") + ".json")
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError:
        print(ERROR_PREFIX + "receipt_write_failed", file=sys.stderr)
        return 3
    if error == "management_forbidden":
        print("the machine client needs read:clients", file=sys.stderr)
    print("LEAF_AUTH0_SPA_DRIFT=" + state)
    return EXIT_CODES[state]


if __name__ == "__main__":
    raise SystemExit(main())
