"""The vendored mushy-code surfaces must match their pin manifests.

PR #474 review (sol-critic, P2): scripts/sync-mushy-code.py --verify existed
but nothing invoked it, so a hand edit or partial sync of a vendored file
would pass CI until someone remembered the manual command. This gate makes the
verifier's READY/NOT-READY verdict a CI fact. Hermetic: it hashes committed
files against committed manifests — no network, no upstream checkout.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SYNC = REPO / "scripts" / "sync-mushy-code.py"


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
