"""Card F-7 tripwire: the mushy_fold vendor pin's recorded disposition must
match reality.

The vendored package (server/_vendor/mushy_fold, pinned to
LEAF-Solar-Design/mushy-code) is WIRED-CORE: deps.load_tenant_repo_tools
delegates the call-time registry read to the vendored
mushy_fold.registry.load_repo_registry_tools (deps.py, PR #474), while tenant
resolution stays in-repo. If deps.py stops importing the vendored core (or
the pin loses its recorded disposition), this test fails until
VENDOR-PIN.json's `disposition` field is re-recorded to match reality. The
first version of this very test asserted the opposite ("reference-only",
from an audit agent's claim) and failed immediately against deps.py:180 —
that is the tripwire doing its job. Fails closed on a missing or malformed
pin.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
VENDOR = SERVER / "_vendor"
PIN = VENDOR / "VENDOR-PIN.json"


def _pin() -> dict:
    data = json.loads(PIN.read_text(encoding="utf-8"))
    assert data.get("contract") == "leaf.vendor-pin.v1"
    return data


def test_pin_hashes_match_the_vendored_files_exactly():
    pin = _pin()
    files = pin["files"]
    assert files, "empty pin manifest"
    for name, expected in files.items():
        blob = (VENDOR / "mushy_fold" / name).read_bytes()
        actual = hashlib.sha256(blob).hexdigest()
        assert actual == expected, f"vendored {name} drifted from its pin"
    # And nothing unpinned rides along in the vendored package.
    on_disk = {p.name for p in (VENDOR / "mushy_fold").glob("*.py")}
    assert on_disk == set(files), f"unpinned vendored files: {on_disk ^ set(files)}"


def test_recorded_disposition_is_wired_core_and_deps_matches_it():
    pin = _pin()
    disposition = pin.get("disposition", "")
    assert disposition.startswith("wired-core"), (
        "VENDOR-PIN.json must record the mushy_fold disposition explicitly"
    )
    deps_src = (SERVER / "deps.py").read_text(encoding="utf-8")
    imports_vendor = re.findall(
        r"^\s*from\s+_vendor\.mushy_fold\.(\w+)\s+import\s+([\w, ]+)", deps_src, re.M
    )
    assert imports_vendor, (
        "deps.py no longer imports the vendored mushy_fold core: the "
        "wired-core disposition in VENDOR-PIN.json is stale — re-record it "
        "(card F-7)"
    )
    # The wired surface is exactly the registry fold; widening what deps
    # consumes from the vendor is a disposition change, not a drive-by.
    assert imports_vendor == [("registry", "load_repo_registry_tools")], (
        f"deps.py's vendored-import surface changed: {imports_vendor} — "
        "re-record the disposition in VENDOR-PIN.json (card F-7)"
    )
