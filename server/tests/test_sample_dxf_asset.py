"""W4g-1c: the committed web/public/sample.dxf IS the synthesis of the committed
web/public/sample.intake.json (the bytes the DXF route would serve for that
intake as a head), bound by the manifest beside it. A drift in either file
without regenerating the other fails here, so the public demo can never open
a drawing that is not the one it draws.

Run:  cd server && python -m pytest tests/test_sample_dxf_asset.py -q
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "web" / "public"
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import dxf_intake  # noqa: E402
import intake_dxf  # noqa: E402


def test_sample_dxf_is_the_synthesis_of_the_sample_intake_and_the_manifest_binds_them():
    raw = (PUBLIC / "sample.intake.json").read_bytes()
    intake = json.loads(raw)
    data = (PUBLIC / "sample.dxf").read_bytes()
    manifest = json.loads((PUBLIC / "sample.dxf.json").read_text(encoding="utf-8"))
    assert data == intake_dxf.intake_to_dxf(intake), "sample.dxf is not the synth of sample.intake.json; run web/scripts/build_sample_dxf.py"
    assert manifest["intake_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["dxf_sha256"] == hashlib.sha256(data).hexdigest()
    assert manifest["bytes"] == len(data)
    assert manifest["entities"] == len(intake["polylines"])
    # The engine's own ceiling (web/src/api.js MAX_DRAWING_DXF_BYTES, 16 MB).
    assert len(data) <= 16 * 1024 * 1024


def test_sample_dxf_round_trips_every_handle_the_demo_intake_names():
    intake = json.loads((PUBLIC / "sample.intake.json").read_text(encoding="utf-8"))
    back = dxf_intake.parse_dxf_bytes((PUBLIC / "sample.dxf").read_bytes(), source_name="sample.dxf")
    assert [p["handle"].upper() for p in intake["polylines"]] == [p["handle"] for p in back["polylines"]]
    assert back["layers"] == intake["layers"]
