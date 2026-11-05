"""Exercise staged solver snapshots and the unchanged AWS fallback."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
STAGED_ZIP = "/work/autofill-solver-snapshot/snapshot.zip"


def test_staged_solver_skips_aws_and_missing_snapshot_uses_aws(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is absent; solver snapshot execution needs bash")

    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(source), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q")
    git("-c", "user.name=Solver Fixture", "-c", "user.email=solver@example.test",
        "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "fixture")
    revision = git("rev-parse", "HEAD")
    bundle = tmp_path / "snapshot.bundle"
    git("bundle", "create", str(bundle), "HEAD")
    staged = tmp_path / "staged" / "snapshot.zip"
    staged.parent.mkdir()
    with zipfile.ZipFile(staged, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "head_sha": revision,
            "repository": "LEAF-Solar-Design/autofill-solver",
        }))
        archive.write(bundle, "snapshot.bundle")

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "autofill-solver-sources.json").write_text(
        json.dumps({revision: {}}), encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$AWS_LOG"\nexit 99\n',
                   encoding="utf-8")
    aws.chmod(0o755)
    aws_log = tmp_path / "aws.log"
    checkout = tmp_path / "checkout"
    block = (ROOT / ".codebuild/ci.sh").read_text(encoding="utf-8").split(
        "# LEAF_AUTOFILL_SOLVER_BEGIN\n", 1,
    )[1].split("# LEAF_AUTOFILL_SOLVER_END", 1)[0]
    block = block.replace(STAGED_ZIP, staged.as_posix()).replace(
        "/tmp/autofill-solver", checkout.as_posix(),
    )
    env = os.environ.copy()
    for key in ("AUTOFILL_SOLVER_ROOT", "AUTOFILL_SOLVER_REVISION",
                "LEAF_AUTOFILL_SOLVER_ABSENT_OK"):
        env.pop(key, None)
    env.update(PATH=str(bin_dir) + os.pathsep + env["PATH"],
               AWS_LOG=str(aws_log), TMPDIR=str(tmp_path))
    command = 'set -euo pipefail\n' + block + '''
printf 'ROOT=%s\\nREVISION=%s\\nABSENT=%s\\n' "${AUTOFILL_SOLVER_ROOT:-}" "${AUTOFILL_SOLVER_REVISION:-}" "${LEAF_AUTOFILL_SOLVER_ABSENT_OK:-}"
'''

    result = subprocess.run([bash, "-c", command], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert f"ROOT={checkout}\nREVISION={revision}\nABSENT=\n" in result.stdout
    assert f"from relay snapshot staged:{staged}" in result.stdout
    assert not aws_log.exists()
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    assert head == revision

    staged.unlink()
    result = subprocess.run([bash, "-c", command], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "ROOT=\nREVISION=\nABSENT=1\n" in result.stdout
    assert "snapshot list failed" in result.stderr
    assert aws_log.read_text(encoding="utf-8").startswith("s3api list-objects-v2 ")
