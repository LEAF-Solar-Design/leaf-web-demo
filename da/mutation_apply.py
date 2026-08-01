"""Protected provisioning for the fixed LeafApplyMutations APS Activity.

No network call occurs at import.  This module does not expose a WorkItem
submission function, so provisioning and readiness checks cannot incur engine
cost.
"""
from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path
from typing import Any

import requests

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import client  # noqa: E402
from apply_lisp import PLAN_LOCALNAME, OUT_LOCALNAME, build_apply_scr  # noqa: E402

ACTIVITY_ID = "LeafApplyMutations"
ALIAS = "prod"
ENGINE = "Autodesk.AutoCAD+26_0"
HOST_DWG_LOCALNAME = "host.dwg"
COMMAND_LINE = (
    r'$(engine.path)\accoreconsole.exe /i "$(args[HostDwg].path)" '
    r'/s "$(settings[script].path)"'
)
_TIMEOUT = 60


def activity_spec() -> dict[str, Any]:
    """Return the complete fixed Activity definition."""
    return {
        "id": ACTIVITY_ID,
        "engine": ENGINE,
        "commandLine": [COMMAND_LINE],
        "parameters": {
            "HostDwg": {
                "verb": "get", "required": True,
                "localName": HOST_DWG_LOCALNAME,
            },
            "Plan": {
                "verb": "get", "required": True,
                "localName": PLAN_LOCALNAME,
            },
            "Result": {
                "verb": "put", "required": True,
                "localName": OUT_LOCALNAME,
            },
        },
        "settings": {"script": {"value": build_apply_scr()}},
        "description": "Leaf fixed closed-format drawing mutation interpreter.",
    }


def _headers() -> dict[str, str]:
    return {**client._auth_headers(), "Content-Type": "application/json"}


def _post(path: str, body: dict[str, Any]):
    return requests.post(
        f"{client.DA}{path}", headers=_headers(), data=json.dumps(body),
        timeout=_TIMEOUT,
    )


def _patch(path: str, body: dict[str, Any]):
    return requests.patch(
        f"{client.DA}{path}", headers=_headers(), data=json.dumps(body),
        timeout=_TIMEOUT,
    )


def _get(path: str):
    return requests.get(
        f"{client.DA}{path}", headers=client._auth_headers(), timeout=_TIMEOUT,
    )


def _require_status(response, allowed: tuple[int, ...], operation: str) -> None:
    if response.status_code not in allowed:
        # APS bodies can echo request data. Do not leak them through errors.
        raise RuntimeError(f"{operation} failed with HTTP {response.status_code}")


def _version(value: dict[str, Any], operation: str) -> int:
    try:
        version = value["version"]
        if isinstance(version, bool):
            raise ValueError
        return int(version)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{operation} returned no valid version") from exc


def provision_activity() -> dict[str, Any]:
    """Create/advance the Activity and point its prod alias at the new version."""
    spec = activity_spec()
    response = _post("/activities", spec)
    advanced = response.status_code == 409
    if advanced:
        response = _post(
            f"/activities/{ACTIVITY_ID}/versions",
            {key: value for key, value in spec.items() if key != "id"},
        )
    _require_status(response, (200, 201), "activity version create")
    version = _version(response.json(), "activity version create")

    alias = _post(
        f"/activities/{ACTIVITY_ID}/aliases",
        {"id": ALIAS, "version": version},
    )
    if alias.status_code == 409:
        alias = _patch(
            f"/activities/{ACTIVITY_ID}/aliases/{ALIAS}", {"version": version},
        )
    _require_status(alias, (200, 201), "activity alias update")
    return {
        "id": ACTIVITY_ID, "alias": ALIAS, "version": version,
        "advanced": advanced,
    }


def _read_json(path: str, operation: str) -> dict[str, Any]:
    response = _get(path)
    _require_status(response, (200,), operation)
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{operation} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{operation} returned invalid JSON")
    return value


def readiness() -> dict[str, Any]:
    """Resolve prod and compare its immutable Activity version to the fixed spec."""
    try:
        alias = _read_json(
            f"/activities/{ACTIVITY_ID}/aliases/{ALIAS}", "activity alias read",
        )
        version = _version(alias, "activity alias")
        deployed = _read_json(
            f"/activities/{ACTIVITY_ID}/versions/{version}",
            "activity version read",
        )
    except RuntimeError as exc:
        return {"ready": False, "mismatches": [str(exc)]}

    expected = activity_spec()
    mismatches = []
    for key in ("engine", "commandLine", "parameters", "settings"):
        if deployed.get(key) != expected[key]:
            mismatches.append(f"activity {key} mismatch")
    if deployed.get("appbundles") not in (None, []):
        mismatches.append("activity appbundles mismatch")
    return {
        "ready": not mismatches,
        "mismatches": mismatches,
        "activity": {"alias": ALIAS, "version": version},
    }


def main(argv: list[str] | None = None) -> int:
    """Protected operator CLI with stable, nonsecret JSON receipts."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("provision", "readiness"):
        child = subparsers.add_parser(command)
        child.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    if args.command == "provision":
        try:
            result = {"ok": True, "operation": "provision", **provision_activity()}
        except Exception:
            # Auth and HTTP exceptions can contain credential paths, request
            # headers, or signed fields. The protected runner gets only this
            # stable fail-closed receipt.
            result = {"ok": False, "operation": "provision", "error": "provision failed"}
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 1
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0

    try:
        result = {"operation": "readiness", **readiness()}
    except Exception:
        result = {
            "operation": "readiness", "ready": False,
            "mismatches": ["readiness failed"],
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
