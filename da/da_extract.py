"""da/da_extract.py — CLI for the extract WorkItem.

    python da/da_extract.py --dry-run <dwg>     # mint token + print WorkItem body (no live APS)
    python da/da_extract.py <dwg> [out.json]    # LIVE: upload, run, download, parse -> Intake JSON

--dry-run performs NO bucket/upload/workitem calls. It DOES mint a 2-legged token
(proving auth) and prints the exact WorkItem submission body plus the extract
Activity spec (which names the real engine Autodesk.AutoCAD+26_0).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402


def main():
    args = [a for a in sys.argv[1:]]
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if not args:
        print(__doc__)
        sys.exit(2)
    dwg = args[0]
    out_json = args[1] if len(args) > 1 else None

    if dry:
        # Prove auth first (acceptance: auth_token() returns a token).
        tok = client.auth_token()
        print(f"[auth] token OK (len={len(tok)}), nickname={client.nickname()}")
        print(f"[engine] {client.ENGINE}")
        print("\n=== EXTRACT ACTIVITY SPEC (ROOT posts once to /activities) ===")
        print(json.dumps(client.extract_activity_spec(), indent=2)[:4000])
        body = client.extract(dwg, dry_run=True)
        print("\n=== WORKITEM SUBMISSION BODY (POST /da/us-east/v3/workitems) ===")
        print(json.dumps(body, indent=2))
        return

    intake = client.extract(dwg, dry_run=False)
    if out_json:
        json.dump(intake, open(out_json, "w"))
        print(f"[extract] wrote {out_json}: "
              f"{len(intake['polylines'])} polylines, {len(intake['layers'])} layers")
    else:
        print(json.dumps(intake))


if __name__ == "__main__":
    main()
