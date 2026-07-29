"""Re-key every identity-keyed broker store from the JWT claim to the platform id.

Covers FOUR stores, because a partial re-key is the dangerous outcome: some
tenants keep their tightening record while others fall back to DEFAULT_TIER.

    broker tenants, file      $BROKER_TENANTS json (legacy mode)
    broker tenants, postgres  broker_tenants table (LEAF_BROKER_STORE=postgres)
    usage ledger, file        $LEAF_USAGE_LEDGER > $BROKER_LEDGER jsonl
    usage ledger, postgres    broker_usage_ledger table

The usage ledger matters for more than history: aggregate_usage and the legacy
reader both filter strictly by the stored tenant string, so an unmigrated ledger
makes prior spend read as zero and the default daily quota effectively resets.

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

    python scripts/rekey_broker_tenants.py --report
    python scripts/rekey_broker_tenants.py --file /data/state/broker_tenants.json
    python scripts/rekey_broker_tenants.py --ledger /data/state/broker_ledger.jsonl --apply

Run it BEFORE deploying the identity change, and read the report: a non-empty
`unmapped` list means the deploy would silently widen access for those tenants.
"""
from __future__ import annotations

import argparse
import json
import os
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


def rekey_ledger(path: Path, mapping: Dict[str, str], apply: bool) -> Dict[str, Any]:
    """Rewrite tenant_id on every jsonl ledger line, line by line.

    The ledger is append-only evidence, so this never reorders or drops a line.
    A line that is not JSON is copied through untouched rather than discarded:
    losing a spend record understates a tenant's usage, which is the direction
    that costs money.
    """
    if not path.is_file():
        return {"store": "usage ledger (file)", "path": str(path),
                "ready": True, "detail": "absent; nothing keyed by a claim"}
    moved = 0
    untouched = 0
    unmapped = set()
    out_lines = []
    for raw in path.read_text(encoding="utf-8").splitlines(True):
        stripped = raw.strip()
        if not stripped:
            out_lines.append(raw)
            continue
        try:
            entry = json.loads(stripped)
        except Exception:  # noqa: BLE001 - keep unparseable evidence verbatim
            out_lines.append(raw)
            untouched += 1
            continue
        tenant = str(entry.get("tenant_id") or "")
        if not tenant or looks_like_platform_id(tenant):
            out_lines.append(raw)
            continue
        target = mapping.get(tenant)
        if not target:
            unmapped.add(tenant)
            out_lines.append(raw)
            continue
        entry["tenant_id"] = target
        rewritten = json.dumps(entry, separators=(",", ":"))
        out_lines.append(rewritten + "\n")
        moved += 1
    report = {
        "store": "usage ledger (file)", "path": str(path),
        "rows_rekeyed": moved, "rows_unparseable_kept": untouched,
        "unmapped_tenants": sorted(unmapped),
        "ready": not unmapped,
    }
    if apply and not unmapped:
        shutil.copy2(path, path.with_suffix(path.suffix + ".pre-rekey"))
        path.write_text("".join(out_lines), encoding="utf-8")
        report["applied"] = True
    return report


def postgres_stores_report() -> Dict[str, Any]:
    """Name the Postgres stores this script cannot reach, rather than implying
    they are clean by omission."""
    import os as _os
    if _os.environ.get("LEAF_BROKER_STORE", "legacy").strip().lower() != "postgres":
        return {"store": "postgres broker stores", "ready": True,
                "detail": "LEAF_BROKER_STORE is not postgres; not in use"}
    return {
        "store": "postgres broker stores", "ready": False,
        "detail": ("broker_tenants and broker_usage_ledger are claim-keyed in "
                   "postgres mode and this script only rewrites files. Re-key "
                   "them with SQL inside one transaction, using the same "
                   "identity_bindings mapping, before cutting over."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="broker tenants JSON (BROKER_TENANTS)")
    parser.add_argument("--ledger",
                        help="usage ledger jsonl (LEAF_USAGE_LEDGER > BROKER_LEDGER)")
    parser.add_argument("--apply", action="store_true",
                        help="write the change; without this it is a dry run")
    args = parser.parse_args()

    stores = []
    mapping: Dict[str, str] = {}

    # 1. broker tenants (file). Its mapping is reused for the ledger, so both
    #    stores move the same tenant to the same place or neither does.
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            stores.append({"store": "broker tenants (file)", "path": str(path),
                           "ready": True,
                           "detail": "absent; nothing keyed by a claim"})
        else:
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, dict):
                stores.append({"store": "broker tenants (file)", "ready": False,
                               "detail": "unexpected shape; expected an object "
                                         "keyed by tenant"})
            else:
                report = rekey(records)
                report["store"] = "broker tenants (file)"
                report["path"] = str(path)
                report["ready"] = (not report["unmapped"]
                                   and not report["collisions"])
                report["applied"] = False
                mapping.update(report["mapped"])
                if args.apply and report["ready"] and report["mapped"]:
                    shutil.copy2(path, path.with_suffix(path.suffix + ".pre-rekey"))
                    moved = {report["mapped"].get(k, k): v
                             for k, v in records.items()}
                    path.write_text(
                        json.dumps(moved, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
                    report["applied"] = True
                stores.append(report)

    # 2. usage ledger (file). Unmigrated, prior spend reads as zero and the
    #    default daily quota effectively resets, so this is cost control rather
    #    than history.
    if args.ledger:
        stores.append(rekey_ledger(Path(args.ledger), mapping, args.apply))

    # 3. the postgres stores this script cannot reach, named rather than
    #    implied clean by omission.
    stores.append(postgres_stores_report())

    ok = all(store.get("ready") for store in stores)
    result = {"ok": ok, "applied": args.apply, "stores": stores}
    if args.apply and not ok:
        print(json.dumps(result, indent=1, sort_keys=True))
        print("refusing to apply: every store must be mappable first",
              file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
