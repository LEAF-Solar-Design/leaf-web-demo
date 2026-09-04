"""W4g-1c: the public demo's drawing as a DXF the browser engine can open.

The anonymous demo (`?demo=1` signed out) draws `web/public/sample.intake.json`
with no server session at all, so the head opener (W4g-1b) has no
`GET /api/drawings/{id}/dxf` to call there. This script writes the SAME bytes
that route would serve for an intake-backed head, `server/intake_dxf`'s
synthesis of that intake, to `web/public/sample.dxf`, beside a manifest that
binds the artifact to its source (`sample.dxf.json`: both digests, the byte
count, the entity count), so a committed DXF can never drift from the intake
it was made from (server/tests/test_sample_dxf_asset.py recomputes and
compares; `--check` here does the same for a pre-commit hand).

Run:  python web/scripts/build_sample_dxf.py [--check]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
PUBLIC = ROOT / "web" / "public"
INTAKE = PUBLIC / "sample.intake.json"
DXF = PUBLIC / "sample.dxf"
MANIFEST = PUBLIC / "sample.dxf.json"


def synthesize() -> tuple[bytes, dict]:
    if str(SERVER) not in sys.path:
        sys.path.insert(0, str(SERVER))
    import intake_dxf  # noqa: PLC0415

    raw = INTAKE.read_bytes()
    intake = json.loads(raw)
    data = intake_dxf.intake_to_dxf(intake)
    manifest = {
        "schema": 1,
        "source": INTAKE.name,
        "intake_sha256": hashlib.sha256(raw).hexdigest(),
        "dxf_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "entities": len(intake.get("polylines") or []) + len(intake.get("texts") or [])
        + len(intake.get("circles") or []) + len(intake.get("arcs") or []),
    }
    return data, manifest


def main(argv: list[str]) -> int:
    data, manifest = synthesize()
    if "--check" in argv:
        current = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else None
        fresh = DXF.exists() and hashlib.sha256(DXF.read_bytes()).hexdigest() == manifest["dxf_sha256"] and current == manifest
        print("sample.dxf", "FRESH" if fresh else "STALE", json.dumps(manifest))
        return 0 if fresh else 1
    DXF.write_bytes(data)
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("wrote", DXF, manifest["bytes"], "bytes,", manifest["entities"], "entities,", manifest["dxf_sha256"][:12])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
