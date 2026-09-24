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

Standardization slice 7b extended this tripwire to a second vendored file,
surface_config.py, and a second deps.py import
(deps.effective_surface_config -> mushy_fold.surface_config.load_repo_surface_config).
Magpie AD4 re-pinned the package, the schema and the harness to one Kit commit.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVER.parent
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
    # The wired surface is exactly the registry fold plus the slice-7b
    # surface-config fold; widening what deps consumes from the vendor beyond
    # these two is a disposition change, not a drive-by. Slice 9c adds one
    # public byte-cap constant so submitted files stay readable by the fold.
    assert imports_vendor == [
        ("registry", "load_repo_registry_tools"),
        ("surface_config", "load_repo_surface_config, MAX_SURFACE_CONFIG_BYTES"),
    ], (
        f"deps.py's vendored-import surface changed: {imports_vendor} — "
        "re-record the disposition in VENDOR-PIN.json (card F-7)"
    )


def test_surface_config_is_at_the_package_wide_pin():
    """The full re-pin removes the former per-file commit override."""
    pin = _pin()
    assert "surface_config.py" in pin["files"], "surface_config.py is not pinned"
    assert "file_upstream_commits" not in pin


def test_surface_config_schema_pinned_byte_identical_to_its_vendored_commit():
    """contract/surface-config.v1.schema.json (repo root) is a second
    artifact vendored from mushy-code, alongside the python fold reader, at
    the SAME package-wide upstream commit. Pinned here the same way the
    python files are pinned: sha256 of
    the bytes on disk, recorded in VENDOR-PIN.json's `contract_files`, so an
    accidental hand-edit that drifts from the merged mushy-code schema fails
    this test instead of silently diverging until a review catches it."""
    pin = _pin()
    entry = pin["contract_files"]["surface-config.v1.schema.json"]
    schema_path = PROJECT_ROOT / entry["path"]
    actual = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    assert actual == entry["sha256"], (
        "contract/surface-config.v1.schema.json drifted from its vendor pin"
    )
    assert entry["upstream_commit"] == pin["upstream_commit"]
    assert entry["upstream_path"] == "contract/surface-config.v1.schema.json"


def test_kit_contract_schema_pinned_byte_identical():
    pin = _pin()
    entry = pin["contract_files"]["assistant.schema.json"]
    assert entry["path"] == "contract/assistant.schema.json"
    assert entry["upstream_path"] == "packages/assistant-contract/schema/assistant.schema.json"
    assert entry["upstream_commit"] == pin["upstream_commit"]
    blob = (PROJECT_ROOT / entry["path"]).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == entry["sha256"]
    assert "ThreadEventV1" in json.loads(blob)["$defs"]
