"""Exercise the seeding CLI against real isolated filesystem stores."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import seed_proof_drawings as seeder


REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "scripts/seed_proof_drawings.py"


def seed(root, cwd, tenant="demo-tenant", *extra_tenants):
    env = dict(os.environ, LEAF_BLOB_STORE="filesystem", LEAF_DRAWING_STORE="legacy",
               APS_LIVE="0", PYTHONDONTWRITEBYTECODE="1")
    env.pop("LEAF_STORE_DIR", None)
    if root is not None:
        env["LEAF_STORE_DIR"] = str(root)
    tenant_args = [arg for value in (tenant, *extra_tenants) for arg in ("--tenant", value)]
    return subprocess.run(
        [sys.executable, "-B", str(SEED), *tenant_args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("kind", ["unset", "empty", "missing", "file"])
def test_missing_store_fails_closed(tmp_path, kind):
    root = {"unset": None, "empty": "", "missing": tmp_path / "missing",
            "file": tmp_path / "file"}[kind]
    if kind == "file":
        root.write_text("not a directory", encoding="utf-8")
    result = seed(root, tmp_path)
    assert result.returncode != 0
    assert "LEAF_STORE_DIR must name an existing proof store directory" in result.stderr
    assert not (tmp_path / "missing").exists()
    assert not (tmp_path / "tenants").exists()


def test_seeds_both_drawings_idempotently_for_each_tenant(tmp_path):
    root = tmp_path / "drawings"
    root.mkdir()
    intake = (REPO / "data/rooftop_demo.intake.json").read_bytes()
    for tenant in ("demo-tenant", "proof-tenant"):
        result = seed(root, tmp_path, tenant)
        assert result.returncode == 0, result.stderr
        drawings = root / "tenants" / tenant / "drawings"
        assert {p.name for p in drawings.iterdir()} == {"cat", "cat-panels"}
        for drawing in ("cat", "cat-panels"):
            path = drawings / drawing
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            assert [v["v"] for v in manifest["versions"]] == [1]
            assert (path / "v/00000001.dwg").read_bytes() == intake
        before = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in root.rglob("*") if p.is_file()}
        result = seed(root, tmp_path, tenant)
        assert result.returncode == 0, result.stderr
        after = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in root.rglob("*") if p.is_file()}
        assert after == before


def test_repeated_tenant_flags_seed_both_tenants(tmp_path):
    root = tmp_path / "drawings"
    root.mkdir()
    result = seed(root, tmp_path, "demo-tenant", "proof-tenant")
    assert result.returncode == 0, result.stderr
    assert {p.name for p in (root / "tenants").iterdir()} == {"demo-tenant", "proof-tenant"}
    intake = (REPO / "data/rooftop_demo.intake.json").read_bytes()
    for tenant in ("demo-tenant", "proof-tenant"):
        drawings = root / "tenants" / tenant / "drawings"
        assert {p.name for p in drawings.iterdir()} == {"cat", "cat-panels"}
        for drawing in ("cat", "cat-panels"):
            path = drawings / drawing
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            assert [v["v"] for v in manifest["versions"]] == [1]
            assert (path / "v/00000001.dwg").read_bytes() == intake


def test_duplicate_tenants_are_seeded_once_in_first_seen_order(monkeypatch):
    calls = []
    monkeypatch.setattr(seeder, "seed_proof_drawings", calls.append)
    monkeypatch.setattr(sys, "argv", [str(SEED), "--tenant", "proof-tenant",
                                    "--tenant", "demo-tenant", "--tenant", "proof-tenant",
                                    "--tenant", "demo-tenant"])
    seeder.main()
    assert calls == ["proof-tenant", "demo-tenant"]


@pytest.mark.parametrize("runner", ["run_unified_local_proof.ps1", "run_console_checkout_proof.ps1"])
def test_managed_runners_seed_default_and_selected_tenants(runner):
    text = (REPO / "web/scripts" / runner).read_text(encoding="utf-8")
    # Pin the unconditional default, conditional extra argument, single splatted
    # invocation, and immediate exit-code guard without executing PowerShell.
    pattern = (
        r"(?m)^  \$proofTenantArgs = @\('--tenant', 'demo-tenant'\)\s*\n"
        r"  if \(\$env:VITE_TENANT_ID -and \$env:VITE_TENANT_ID -cne 'demo-tenant'\) \{\s*\n"
        r"    \$proofTenantArgs \+= @\('--tenant', \$env:VITE_TENANT_ID\)\s*\n"
        r"  \}\s*\n"
        r"  & \$launcherFile -B \(Join-Path \$repoRoot 'scripts/seed_proof_drawings.py'\) @proofTenantArgs\s*\n"
        r"  if \(\$LASTEXITCODE -ne 0\) \{ throw 'Could not seed the proof drawings' \}"
    )
    assert re.search(pattern, text), runner
    assert text.count("'scripts/seed_proof_drawings.py'") == 1
