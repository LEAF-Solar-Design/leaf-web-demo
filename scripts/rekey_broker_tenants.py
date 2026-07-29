"""Re-key broker tenant records from the JWT claim to the platform tenant id.

Issue #304. Moving /api/run to the active platform binding changes the tenant
string the broker is handed. The broker's own records are keyed by that string:

    _provisioned_tier   -> an unknown key returns entitlements.DEFAULT_TIER,
                           which is "demo" and grants every capability except
                           platform_customize. A record that TIGHTENS a tenant
                           below demo therefore stops applying.
    _cap_preflight      -> the hard spend cap is OFF unless a positive cap is
                           configured for that exact tenant, so a cap left under
                           the claim stops applying.
    disabled set        -> a disabled tenant stops being disabled.

None of that is acceptable as a silent side effect, so the records move with the
identity. This script does that move and REFUSES to guess: every key it cannot
map through the platform's own identity_bindings is reported, never dropped.

Usage (dry run is the default; nothing is written without --apply):

    python scripts/rekey_broker_tenants.py --file /data/state/broker_tenants.json
    python scripts/rekey_broker_tenants.py --file /data/state/broker_tenants.json --apply

Run it BEFORE deploying the identity change, and read the report: a non-empty
`unmapped` list means the deploy would silently widen access for those tenants.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def looks_like_platform_id(key: str) -> bool:
    """Platform tenant ids are server-minted UUIDs (platform/store.py)."""
    return bool(_UUID.match(key.strip()))


def resolve_platform_id(claim: str) -> Optional[str]:
    """Map a claim to its active platform tenant, or None if it cannot be.

    Uses the platform's own binding authority rather than any local guess, so a
    claim with no active binding is reported instead of being invented.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
        import platform_link  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"platform authority unavailable: {exc}") from exc
    store = platform_link.platform_store()
    for authority in ("auth0",):
        try:
            binding = store.resolve_active_identity_binding(authority, claim)
        except Exception:  # noqa: BLE001 - an authority error is not a mapping
            binding = None
        if binding is not None:
            return str(binding.platform_tenant_id)
    # The claim may itself be an org alias rather than a subject.
    try:
        org = store.get_org(claim)
    except Exception:  # noqa: BLE001
        org = None
    return str(getattr(org, "org_id", "")) or None


def rekey(records: Dict[str, Any]) -> Dict[str, Any]:
    """Return {mapped, unmapped, already_platform, collisions}."""
    mapped: Dict[str, str] = {}
    unmapped = []
    already = []
    collisions = []
    for key in records:
        if looks_like_platform_id(key):
            already.append(key)
            continue
        target = resolve_platform_id(key)
        if not target:
            unmapped.append(key)
            continue
        if target in records or target in mapped.values():
            collisions.append({"from": key, "to": target})
            continue
        mapped[key] = target
    return {"mapped": mapped, "unmapped": unmapped,
            "already_platform": already, "collisions": collisions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True,
                        help="broker tenants JSON (BROKER_TENANTS)")
    parser.add_argument("--apply", action="store_true",
                        help="write the change; without this it is a dry run")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(json.dumps({"ok": True, "note": "no broker tenants file; nothing "
                                              "is keyed by a claim", "path": str(path)}))
        return 0

    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, dict):
        print(json.dumps({"ok": False, "error": "unexpected shape; expected an "
                                                "object keyed by tenant"}))
        return 2

    report = rekey(records)
    report["ok"] = not report["unmapped"] and not report["collisions"]
    report["path"] = str(path)
    report["applied"] = False

    if args.apply:
        if not report["ok"]:
            print(json.dumps(report, indent=1, sort_keys=True))
            print("refusing to apply: every key must map first", file=sys.stderr)
            return 1
        shutil.copy2(path, path.with_suffix(path.suffix + ".pre-rekey"))
        moved = {report["mapped"].get(k, k): v for k, v in records.items()}
        path.write_text(json.dumps(moved, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        report["applied"] = True

    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
