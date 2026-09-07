"""Protected provisioning for the fixed LeafApplyMutations APS Activity.

No network call occurs at import.  This module does not expose a WorkItem
submission function, so provisioning and readiness checks cannot incur engine
cost.
"""
from __future__ import annotations

import json
import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import client  # noqa: E402
from lisp import MUTATION_INSPECT_BLOCKS, build_scr  # noqa: E402
from apply_lisp import (  # noqa: E402
    INTAKE_LOCALNAME,
    OUT_LOCALNAME,
    PLAN_LOCALNAME,
    build_apply_scr,
    build_apply_scr_v3 as _build_apply_scr_v3,
)

ACTIVITY_ID = "LeafApplyMutations"
ALIAS = "prod"
ENGINE = "Autodesk.AutoCAD+26_0"
HOST_DWG_LOCALNAME = "host.dwg"
COMMAND_LINE = (
    r'$(engine.path)\accoreconsole.exe /i "$(args[HostDwg].path)" '
    r'/s "$(settings[script].path)"'
)
INSPECTION_COMMAND_LINE = (
    r'$(engine.path)\accoreconsole.exe /i "$(args[Result].path)" '
    r'/s "$(settings[inspectScript].path)"'
)
_TIMEOUT = 60


def build_apply_scr_v3() -> str:
    """Accept INSERT operations through the separate v3 interpreter."""
    return _build_apply_scr_v3()


MUTATION_INSPECT_BLOCKS_V3 = MUTATION_INSPECT_BLOCKS


@dataclass(frozen=True)
class Contract:
    activity_id: str
    build_apply: Callable[[], str]
    inspect_blocks: tuple[str, ...]

    @property
    def alias(self) -> str:
        # Read at call time, never cached: this must always name the SAME
        # alias the client actually submits WorkItems to (client.ALIAS, which
        # tracks APS_ALIAS), so readiness and submission never drift apart.
        return client.ALIAS


CONTRACTS = {
    2: Contract(activity_id=ACTIVITY_ID,
                build_apply=build_apply_scr, inspect_blocks=MUTATION_INSPECT_BLOCKS),
    3: Contract(activity_id="LeafApplyMutationsV3",
                build_apply=build_apply_scr_v3, inspect_blocks=MUTATION_INSPECT_BLOCKS_V3),
}


def activity_spec(contract: int = 2) -> dict[str, Any]:
    """Return the complete fixed Activity definition."""
    target = CONTRACTS[contract]
    inspect_script = build_scr(INTAKE_LOCALNAME, extra_blocks=target.inspect_blocks)
    return {
        "id": target.activity_id,
        "engine": ENGINE,
        # The second command reopens the exact saved Result bytes before it
        # emits verification intake. This preserves the prior closed-file proof
        # while paying one APS queue and one WorkItem.
        "commandLine": [COMMAND_LINE, INSPECTION_COMMAND_LINE],
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
            # Optional keeps the alias backward-compatible while the app rolls:
            # an older caller can omit it, while the new caller fails closed if
            # its requested inspection output is absent or malformed.
            "Intake": {
                "verb": "put", "required": False,
                "localName": INTAKE_LOCALNAME,
            },
        },
        "settings": {
            "script": {"value": target.build_apply()},
            # W4g-3: the inspection also reports LINE / CIRCLE / ARC with
            # handles (the kinds the browser engine writes), so a v2 plan's
            # effects verify; the LeafExtract script itself is untouched.
            "inspectScript": {"value": inspect_script},
        },
        "description": (
            "Leaf fixed closed-format drawing mutation interpreter with "
            "same-WorkItem output inspection."
        ),
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


def _delete(path: str):
    return requests.delete(
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


def provision_activity(contract: int = 2) -> dict[str, Any]:
    """Create/advance the Activity and point its prod alias at the new version."""
    target = CONTRACTS[contract]
    spec = activity_spec(contract)
    response = _post("/activities", spec)
    advanced = response.status_code == 409
    if advanced:
        response = _post(
            f"/activities/{target.activity_id}/versions",
            {key: value for key, value in spec.items() if key != "id"},
        )
    _require_status(response, (200, 201), "activity version create")
    version = _version(response.json(), "activity version create")

    alias = _post(
        f"/activities/{target.activity_id}/aliases",
        {"id": target.alias, "version": version},
    )
    if alias.status_code == 409:
        alias = _patch(
            f"/activities/{target.activity_id}/aliases/{target.alias}", {"version": version},
        )
    _require_status(alias, (200, 201), "activity alias update")
    return {
        "id": target.activity_id, "alias": target.alias, "version": version,
        "advanced": advanced,
    }


def alias_state(contract: int = 2) -> dict[str, Any]:
    """Capture the exact rollback target without changing APS state."""
    target = CONTRACTS[contract]
    response = _get(f"/activities/{target.activity_id}/aliases/{target.alias}")
    if response.status_code == 404:
        return {"id": target.activity_id, "alias": target.alias, "exists": False, "version": None}
    _require_status(response, (200,), "activity alias read")
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError("activity alias read returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("activity alias read returned invalid JSON")
    return {
        "id": target.activity_id, "alias": target.alias, "exists": True,
        "version": _version(value, "activity alias"),
    }


def restore_alias(version: int | None, contract: int = 2) -> dict[str, Any]:
    """Restore the captured prod alias target, including prior absence."""
    target = CONTRACTS[contract]
    path = f"/activities/{target.activity_id}/aliases/{target.alias}"
    if version is None:
        response = _delete(path)
        _require_status(response, (200, 204, 404), "activity alias delete")
    else:
        if isinstance(version, bool) or version < 1:
            raise ValueError("restore version must be a positive integer")
        response = _patch(path, {"version": version})
        if response.status_code == 404:
            response = _post(
                f"/activities/{target.activity_id}/aliases",
                {"id": target.alias, "version": version},
            )
        _require_status(response, (200, 201), "activity alias restore")
    observed = alias_state(contract)
    if observed["exists"] is not (version is not None) or observed["version"] != version:
        raise RuntimeError("activity alias restore readback mismatch")
    return observed


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


def _canonical_parameters(value: Any) -> Any:
    """Restore APS defaults before comparing an Activity parameter map."""
    if not isinstance(value, dict):
        return value
    canonical = {}
    for name, parameter in value.items():
        if not isinstance(parameter, dict):
            canonical[name] = parameter
            continue
        normalized = dict(parameter)
        normalized.setdefault("required", False)
        canonical[name] = normalized
    return canonical


def readiness(contract: int = 2) -> dict[str, Any]:
    """Resolve prod and compare its immutable Activity version to the fixed spec."""
    target = CONTRACTS[contract]
    try:
        alias = _read_json(
            f"/activities/{target.activity_id}/aliases/{target.alias}", "activity alias read",
        )
        version = _version(alias, "activity alias")
        deployed = _read_json(
            f"/activities/{target.activity_id}/versions/{version}",
            "activity version read",
        )
    except RuntimeError as exc:
        return {"ready": False, "mismatches": [str(exc)], "contract": contract}

    expected = activity_spec(contract)
    mismatches = []
    for key in ("engine", "commandLine", "settings"):
        if deployed.get(key) != expected[key]:
            mismatches.append(f"activity {key} mismatch")
    if _canonical_parameters(deployed.get("parameters")) != _canonical_parameters(
        expected["parameters"]
    ):
        mismatches.append("activity parameters mismatch")
    if deployed.get("appbundles") not in (None, []):
        mismatches.append("activity appbundles mismatch")
    return {
        "ready": not mismatches,
        "mismatches": mismatches,
        "activity": {"alias": target.alias, "version": version},
        "contract": contract,
    }


def main(argv: list[str] | None = None) -> int:
    """Protected operator CLI with stable, nonsecret JSON receipts."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("provision", "readiness", "alias-state"):
        child = subparsers.add_parser(command)
        child.add_argument("--json", action="store_true", dest="as_json")
        child.add_argument("--contract", type=int, choices=(2, 3), default=2)
    restore = subparsers.add_parser("restore-alias")
    restore.add_argument("--version", required=True)
    restore.add_argument("--json", action="store_true", dest="as_json")
    restore.add_argument("--contract", type=int, choices=(2, 3), default=2)
    args = parser.parse_args(argv)

    if args.command == "provision":
        try:
            result = {
                "ok": True, "operation": "provision",
                **provision_activity(contract=args.contract), "contract": args.contract,
            }
        except Exception:
            # Auth and HTTP exceptions can contain credential paths, request
            # headers, or signed fields. The protected runner gets only this
            # stable fail-closed receipt.
            result = {
                "ok": False, "operation": "provision", "error": "provision failed",
                "contract": args.contract,
            }
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 1
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0

    if args.command == "alias-state":
        try:
            result = {
                "ok": True, "operation": "alias-state",
                **alias_state(contract=args.contract), "contract": args.contract,
            }
        except Exception:
            result = {
                "ok": False, "operation": "alias-state",
                "error": "alias snapshot failed",
                "contract": args.contract,
            }
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 1
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0

    if args.command == "restore-alias":
        try:
            version = None if args.version == "absent" else int(args.version)
            result = {
                "ok": True, "operation": "restore-alias",
                **restore_alias(version, contract=args.contract), "contract": args.contract,
            }
        except Exception:
            result = {
                "ok": False, "operation": "restore-alias",
                "error": "alias restore failed",
                "contract": args.contract,
            }
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 1
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0

    try:
        result = {
            "operation": "readiness", **readiness(contract=args.contract),
            "contract": args.contract,
        }
    except Exception:
        result = {
            "operation": "readiness", "ready": False,
            "mismatches": ["readiness failed"],
            "contract": args.contract,
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
