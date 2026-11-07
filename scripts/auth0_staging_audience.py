"""Plan staging Auth0 audience changes; apply only a reviewed operation digest."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, urlsplit


DECLARED_PATH = Path(__file__).resolve().parents[1] / "auth0/staging-api-audience.json"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CLIENT_ID_RE = re.compile(r"[A-Za-z0-9]{32}")
REFUSALS = {"resource_server_conflict", "grant_conflict", "metadata_unexpected",
            "staging_api_missing", "confirm_mismatch", "operation_not_allowed"}


class SyncError(Exception):
    """Only fixed codes may leave this tool."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SyncError("arguments_invalid")


def build_parser():
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--phase", choices=("provision", "cutover", "rollback"), required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--declared", type=Path, default=DECLARED_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    return parser


def validate_declared(value):
    def require(condition):
        if not condition:
            raise SyncError("declared_invalid")

    try:
        require(value["schema"] == "leaf.auth0-staging-audience.v1")
        server = value["resource_server"]
        production = value["production_identifier"]
        for identifier in (production, server["identifier"]):
            parsed = urlsplit(identifier)
            require(parsed.scheme == "https" and parsed.hostname and not parsed.username)
            require(not parsed.password and not parsed.query and not parsed.fragment)
        require(production != server["identifier"])
        require(isinstance(server, dict) and isinstance(server["scopes"], list))
        for scope in server["scopes"]:
            require(isinstance(scope["value"], str) and isinstance(scope["description"], str))
        require(isinstance(value["grants"], list) and value["grants"])
        ids = []
        for grant in value["grants"]:
            require(CLIENT_ID_RE.fullmatch(grant["client_id"]))
            require(isinstance(grant["scope"], list))
            require(all(isinstance(scope, str) for scope in grant["scope"]))
            ids.append(grant["client_id"])
        require(len(ids) == len(set(ids)))
        require(isinstance(value["tenant_audience_clients"], list))
        require(len(value["tenant_audience_clients"]) == len(set(value["tenant_audience_clients"])))
        for client_id in value["tenant_audience_clients"]:
            require(CLIENT_ID_RE.fullmatch(client_id) and client_id in ids)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SyncError("declared_invalid") from None
    return value


def load_declared(path=DECLARED_PATH):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SyncError("declared_invalid") from None
    return validate_declared(value)


def resource_path(declared):
    return "resource-servers?identifiers=" + quote(declared["resource_server"]["identifier"], safe="")


def grant_path(declared, client_id):
    return ("client-grants?audience=" + quote(declared["resource_server"]["identifier"], safe="")
            + "&client_id=" + client_id)


def client_path(client_id):
    return "clients/" + client_id + "?fields=client_id,name,client_metadata&include_fields=true"


class CliTransport:
    """Fixed CLI operations; the operator's CLI session holds credentials."""

    def __init__(self, declared, *, phase, allow_writes=False, runner=subprocess.run):
        self.declared = deepcopy(validate_declared(declared))
        self.phase = phase
        self.allow_writes = allow_writes
        self.runner = runner

    def request(self, method, path, *, body=None):
        declared = self.declared
        reads = {resource_path(declared)}
        reads.update(grant_path(declared, grant["client_id"]) for grant in declared["grants"])
        reads.update(client_path(grant["client_id"]) for grant in declared["grants"])
        allowed = method == "GET" and path in reads and body is None
        if self.allow_writes and self.phase == "provision" and method == "POST":
            allowed = (path == "resource-servers" and body == declared["resource_server"]) or (
                path == "client-grants" and any(body == {
                    "client_id": grant["client_id"], "audience": declared["resource_server"]["identifier"],
                    "scope": grant["scope"]} for grant in declared["grants"]))
        if self.allow_writes and self.phase in ("cutover", "rollback") and method == "PATCH":
            target = declared["resource_server"]["identifier"] if self.phase == "cutover" else declared["production_identifier"]
            allowed = (path in {"clients/" + cid for cid in declared["tenant_audience_clients"]}
                       and isinstance(body, dict) and set(body) == {"client_metadata"}
                       and isinstance(body["client_metadata"], dict)
                       and body["client_metadata"].get("leaf_tenant_audience") == target)
        if not allowed:
            raise SyncError("operation_not_allowed")
        argv = ["auth0", "api", method.lower(), path]
        if body is not None:
            argv += ["--data", json.dumps(body, separators=(",", ":"))]
        try:
            response = self.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=60, shell=False)
            raw = response.stdout
            if response.returncode != 0 or len(raw) > MAX_RESPONSE_BYTES:
                raise SyncError("request_failed")
            return json.loads(raw)
        except Exception:
            raise SyncError("request_failed") from None


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SyncError("request_failed")
    return value


def _scopes(value):
    try:
        return {(scope["value"], scope["description"]) for scope in value}
    except (TypeError, KeyError):
        raise SyncError("request_failed") from None


def build_plan(declared, transport, phase):
    operations = []
    staging = declared["resource_server"]["identifier"]
    if phase in ("provision", "cutover"):
        servers = _rows(transport.request("GET", resource_path(declared)))
        if len(servers) > 1:
            raise SyncError("resource_server_conflict")
        if not servers:
            if phase == "cutover":
                raise SyncError("staging_api_missing")
            operations.append({"op": "create_resource_server", "client_id": None,
                               "body": deepcopy(declared["resource_server"])})
        else:
            for key, expected in declared["resource_server"].items():
                actual = servers[0].get(key)
                try:
                    different = _scopes(actual) != _scopes(expected) if key == "scopes" else actual != expected
                except SyncError:
                    raise SyncError("resource_server_conflict") from None
                if different:
                    raise SyncError("resource_server_conflict")
        for grant in declared["grants"]:
            rows = _rows(transport.request("GET", grant_path(declared, grant["client_id"])))
            if len(rows) > 1:
                raise SyncError("grant_conflict")
            if rows:
                row = rows[0]
                if (row.get("client_id") != grant["client_id"] or row.get("audience") != staging
                        or not isinstance(row.get("scope"), list)
                        or row["scope"] and not all(isinstance(item, str) for item in row["scope"])
                        or set(row["scope"]) != set(grant["scope"])):
                    raise SyncError("grant_conflict")
            elif phase == "cutover":
                raise SyncError("staging_api_missing")
            else:
                operations.append({"op": "create_client_grant", "client_id": grant["client_id"],
                                   "body": {"client_id": grant["client_id"], "audience": staging,
                                            "scope": deepcopy(grant["scope"])}})
    if phase in ("cutover", "rollback"):
        source, target = declared["production_identifier"], staging
        if phase == "rollback":
            source, target = target, source
        for client_id in declared["tenant_audience_clients"]:
            client = transport.request("GET", client_path(client_id))
            if not isinstance(client, dict) or client.get("client_id") != client_id:
                raise SyncError("request_failed")
            metadata = client.get("client_metadata")
            if not isinstance(metadata, dict) or metadata.get("leaf_tenant_audience") not in (source, target):
                raise SyncError("metadata_unexpected")
            if metadata["leaf_tenant_audience"] != target:
                updated = deepcopy(metadata)
                updated["leaf_tenant_audience"] = target
                operations.append({"op": "set_tenant_audience", "client_id": client_id,
                                   "body": {"client_metadata": updated}})
    return operations


def plan_digest(operations):
    if not operations:
        return None
    return hashlib.sha256(json.dumps(operations, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def apply_plan(operations, transport):
    for operation in operations:
        name = operation["op"]
        path = {"create_resource_server": "resource-servers", "create_client_grant": "client-grants"}.get(name)
        transport.request("POST" if path else "PATCH", path or "clients/" + operation["client_id"],
                          body=operation["body"])


def check_readback(declared, transport, phase, operations):
    try:
        if build_plan(declared, transport, phase):
            raise SyncError("readback_mismatch")
        for operation in operations:
            if operation["op"] == "set_tenant_audience":
                client = transport.request("GET", client_path(operation["client_id"]))
                if not isinstance(client, dict) or client.get("client_metadata") != operation["body"]["client_metadata"]:
                    raise SyncError("readback_mismatch")
    except SyncError as exc:
        if str(exc) == "request_failed":
            raise
        raise SyncError("readback_mismatch") from None


def main(argv=None, *, transport=None, now=None):
    report = {"schema": "leaf.auth0-staging-audience-run.v1", "phase": None, "mode": "check",
              "staging_identifier": None, "operations": [], "plan_digest": None,
              "result": "error", "error": None}
    args = None
    code = 2
    try:
        args = build_parser().parse_args(argv)
        report.update(phase=args.phase, mode="apply" if args.apply else "check")
        if ((args.apply and (args.confirm is None or re.fullmatch(r"[0-9a-f]{64}", args.confirm) is None))
                or (not args.apply and args.confirm is not None)):
            raise SyncError("arguments_invalid")
        declared = load_declared(args.declared)
        report["staging_identifier"] = declared["resource_server"]["identifier"]
        active = transport if transport is not None else CliTransport(declared, phase=args.phase, allow_writes=args.apply)
        operations = build_plan(declared, active, args.phase)
        report["operations"] = [{"op": op["op"], "client_id": op["client_id"]} for op in operations]
        report["plan_digest"] = plan_digest(operations)
        report["result"] = "drift" if operations else "match"
        if args.apply and operations:
            if args.confirm != report["plan_digest"]:
                raise SyncError("confirm_mismatch")
            apply_plan(operations, active)
            check_readback(declared, active, args.phase, operations)
            report["result"] = "applied"
        code = 0 if report["result"] in ("match", "applied") else 1
    except SyncError as exc:
        error = str(exc)
        report.update(result="readback_mismatch" if error == "readback_mismatch" else "error", error=error)
        code = 4 if error in REFUSALS else 1 if error == "readback_mismatch" else 2
        print("LEAF_AUTH0_STAGING_AUDIENCE_ERROR=" + error, file=sys.stderr)
    except SystemExit as exc:
        return int(exc.code)
    if args is not None:
        try:
            stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            args.receipt_dir.mkdir(parents=True, exist_ok=True)
            path = args.receipt_dir / ("auth0-staging-audience-" + args.phase + "-" + stamp + ".json")
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError:
            report.update(result="error", error="receipt_write_failed")
            print("LEAF_AUTH0_STAGING_AUDIENCE_ERROR=receipt_write_failed", file=sys.stderr)
            code = 3
    print(json.dumps(report, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
