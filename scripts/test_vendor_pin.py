"""The vendored mushy-code surfaces must match their pin manifests.

PR #474 review (sol-critic, P2): scripts/sync-mushy-code.py --verify existed
but nothing invoked it, so a hand edit or partial sync of a vendored file
would pass CI until someone remembered the manual command. This gate makes the
verifier's READY/NOT-READY verdict a CI fact. Hermetic: it hashes committed
files against committed manifests — no network, no upstream checkout.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SYNC = REPO / "scripts" / "sync-mushy-code.py"


def load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_mushy_code", SYNC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vendored_mushy_code_matches_its_pin():
    proc = subprocess.run(
        [sys.executable, str(SYNC), "--verify"],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )
    assert proc.returncode == 0, (
        f"vendor verify NOT-READY (exit {proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "READY" in proc.stdout


def test_verifier_can_fail():
    """Prove the checker can go red: a corrupted pin hash must flip the verdict.

    Runs against a THROWAWAY copy of one pin manifest in a temp overlay — the
    real tree is never touched. Uses the script's own module logic by editing a
    copied manifest and pointing verification at it via a scratch repo layout.
    """
    import json
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td) / "repo"
        (scratch / "scripts").mkdir(parents=True)
        shutil.copy2(SYNC, scratch / "scripts" / "sync-mushy-code.py")
        for rel in ("harness/src/vendor", "server/_vendor"):
            src = REPO / rel
            dst = scratch / rel
            shutil.copytree(src, dst)
        # also the single files the first manifest pins
        (scratch / "harness" / "scripts").mkdir(parents=True)
        shutil.copy2(REPO / "harness" / "scripts" / "git-worker.cjs",
                     scratch / "harness" / "scripts" / "git-worker.cjs")
        pin = scratch / "server" / "_vendor" / "VENDOR-PIN.json"
        manifest = json.loads(pin.read_text(encoding="utf-8"))
        first = next(iter(manifest["files"]))
        manifest["files"][first] = "0" * 64
        pin.write_text(json.dumps(manifest), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(scratch / "scripts" / "sync-mushy-code.py"), "--verify"],
            capture_output=True, text=True, cwd=str(scratch), timeout=120,
        )
        assert proc.returncode == 1, "corrupted pin hash must make --verify fail"
        assert "DRIFT" in proc.stdout


def test_downstream_overlay_inventory_matches_current_vendor():
    sync = load_sync_module()
    pin = REPO / "harness" / "src" / "vendor" / "VENDOR-PIN.json"
    manifest = json.loads(pin.read_text(encoding="utf-8"))
    overlays = sync.downstream_overlays(manifest)

    assert set(overlays) == {
        "ports/fakes/fakeConverseRunner.ts",
        "ports/impl/agentSdkRunner.ts",
        "ports/impl/converseSdkRunner.ts",
        "ports/impl/tenantChangeRepo.ts",
        "ports/index.ts",
    }
    for rel, declared_hash in overlays.items():
        assert manifest["files"][rel] == declared_hash
        assert sync.sha256(REPO / "harness" / "src" / "vendor" / "mushy-author" / rel) == declared_hash


def test_sync_refuses_overlay_loss_without_writing(tmp_path):
    sync = load_sync_module()
    scratch = tmp_path / "repo"
    vendored = scratch / "vendor"
    pin = scratch / "VENDOR-PIN.json"
    upstream = tmp_path / "upstream"
    incoming = upstream / "surface" / "protected.txt"
    protected = vendored / "protected.txt"

    protected.parent.mkdir(parents=True)
    incoming.parent.mkdir(parents=True)
    protected.write_text("downstream patch\n", encoding="utf-8")
    incoming.write_text("plain upstream\n", encoding="utf-8")
    protected_hash = sync.sha256(protected)
    pin.write_text(json.dumps({
        "contract": "leaf.vendor-pin.v1",
        "files": {"protected.txt": protected_hash},
        "downstream_overlays": {
            "protected.txt": {
                "reason": "fixture downstream patch",
                "sha256": protected_hash,
            }
        },
    }), encoding="utf-8")

    subprocess.run(["git", "init"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=upstream, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Vendor Test", "-c", "user.email=vendor@example.com",
         "commit", "-m", "fixture"],
        cwd=upstream, check=True, capture_output=True,
    )

    sync.REPO = scratch
    sync.SURFACES = [("surface", vendored, pin)]
    sync.SINGLE_FILES = []
    before_file = protected.read_bytes()
    before_pin = pin.read_bytes()

    assert sync.do_sync(upstream) == 1
    assert protected.read_bytes() == before_file
    assert pin.read_bytes() == before_pin
