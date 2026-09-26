"""The vendored mushy-code surfaces must match their pin manifests.

PR #474 review (sol-critic, P2): scripts/sync-mushy-code.py --verify existed
but nothing invoked it, so a hand edit or partial sync of a vendored file
would pass CI until someone remembered the manual command. This gate makes the
verifier's READY/NOT-READY verdict a CI fact. Hermetic: it hashes committed
files against committed manifests — no network, no upstream checkout.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import re
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
        "ports/converse.ts",
        "ports/fakes/fakeSessionStore.ts",
        "ports/impl/converseSdkRunner.ts",
        "ports/impl/harnessSchema.ts",
        "ports/impl/pgSessionStore.ts",
        "ports/impl/sessionStore.ts",
        "ports/impl/tenantChangeRepo.ts",
        "ports/impl/tenantRepoProvider.ts",
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


def test_both_pins_share_one_kit_commit():
    harness = json.loads((REPO / "harness/src/vendor/VENDOR-PIN.json").read_text())
    server = json.loads((REPO / "server/_vendor/VENDOR-PIN.json").read_text())
    assert re.fullmatch(r"[0-9a-f]{40}", harness["upstream_commit"])
    assert harness["upstream_commit"] == server["upstream_commit"]
    assert "file_upstream_commits" not in server


def test_held_schema_overlay_names_its_retirement_slice():
    pin = json.loads((REPO / "harness/src/vendor/VENDOR-PIN.json").read_text())
    assert "AD4b" in pin["downstream_overlays"]["ports/impl/harnessSchema.ts"]["reason"]


def _scratch_sync(tmp_path):
    sync = load_sync_module()
    sync.REPO = tmp_path / "repo"
    sync.SURFACES = [
        (name, sync.REPO / name, sync.REPO / f"{name}-pin.json")
        for name in ("harness", "server")
    ]
    sync.SINGLE_FILES = []
    for _, vendored, pin in sync.SURFACES:
        vendored.mkdir(parents=True)
        pin.write_text(json.dumps({"files": {}, "upstream_commit": "a" * 40}))
    return sync


def test_sync_carries_disposition_and_contract_files(tmp_path):
    sync = _scratch_sync(tmp_path)
    upstream = tmp_path / "upstream"
    for name, _, _ in sync.SURFACES:
        (upstream / name).mkdir(parents=True)
        (upstream / name / "source.txt").write_bytes(b"source\n")
    blob = b'{"contract": true}\r\n'
    (upstream / "schema.json").write_bytes(blob)
    pin = sync.SURFACES[1][2]
    disposition = "wired-core: fixture disposition"
    manifest = json.loads(pin.read_text())
    manifest.update({
        "disposition": disposition,
        "file_upstream_commits": {"source.txt": "b" * 40},
        "contract_files": {"schema.json": {
            "path": "contract/schema.json", "upstream_path": "schema.json",
            "sha256": "0" * 64, "upstream_commit": "b" * 40,
        }},
    })
    pin.write_text(json.dumps(manifest))
    subprocess.run(["git", "init"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"],
                   cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "add", "."],
                   cwd=upstream, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Vendor Test", "-c", "user.email=vendor@example.com",
         "commit", "-m", "fixture"],
        cwd=upstream, check=True, capture_output=True,
    )
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=upstream,
                          check=True, capture_output=True, text=True).stdout.strip()
    assert sync.do_sync(upstream) == 0
    result = json.loads(pin.read_text())
    assert result["disposition"] == disposition
    assert "file_upstream_commits" not in result
    assert (sync.REPO / "contract/schema.json").read_bytes() == blob
    assert result["contract_files"]["schema.json"] == {
        "path": "contract/schema.json", "upstream_path": "schema.json",
        "sha256": hashlib.sha256(blob).hexdigest(), "upstream_commit": head,
    }
    assert sync.do_verify() == 0


def test_verify_flags_contract_file_drift(tmp_path, capsys):
    sync = _scratch_sync(tmp_path)
    pin = sync.SURFACES[1][2]
    manifest = json.loads(pin.read_text())
    manifest["contract_files"] = {"schema.json": {
        "path": "schema.json", "sha256": hashlib.sha256(b"original").hexdigest(),
    }}
    pin.write_text(json.dumps(manifest))
    schema = sync.REPO / "schema.json"
    schema.write_bytes(b"changed")
    assert sync.do_verify() == 1
    assert "DRIFT schema.json" in capsys.readouterr().out
    schema.unlink()
    assert sync.do_verify() == 1
    assert "DRIFT schema.json" in capsys.readouterr().out


def test_verify_flags_split_pins(tmp_path, capsys):
    sync = _scratch_sync(tmp_path)
    pin = sync.SURFACES[1][2]
    manifest = json.loads(pin.read_text())
    manifest["upstream_commit"] = "b" * 40
    pin.write_text(json.dumps(manifest))
    assert sync.do_verify() == 1
    assert "SPLIT PIN harness=aaaaaaaaaaaa server=bbbbbbbbbbbb" in capsys.readouterr().out
