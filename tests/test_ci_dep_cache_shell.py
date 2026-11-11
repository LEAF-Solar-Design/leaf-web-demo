"""Run the actual seed block and heavy callback with offline tool stubs."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".codebuild/ci.sh"
HELPER = ROOT / "scripts/ci/dep_cache.py"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="bash is not on PATH")
SPEC = importlib.util.spec_from_file_location("shell_dep_cache", HELPER)
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


def shell_path(path):
    value = Path(path).resolve().as_posix()
    return "/" + value[0].lower() + value[2:] if os.name == "nt" else value


def stub(path, body):
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, newline="\n")
    path.chmod(0o755)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home, bins, repo = tmp_path / "home", tmp_path / "bin", tmp_path / "repo"
    bins.mkdir()
    home.mkdir()
    (repo / "web").mkdir(parents=True)
    (repo / "harness").mkdir()
    (repo / "server").mkdir()
    (repo / "web/package-lock.json").write_text(json.dumps({"packages": {
        "node_modules/playwright-core": {"version": "1.60.0", "integrity": "sha512-fixture"}}}))
    (repo / "server/requirements.txt").write_text("playwright==1.60.0\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": shell_path(home),
           "CODEBUILD_SRC_DIR": shell_path(repo), "FIXTURE_BIN": shell_path(bins),
           "FIXTURE_PIP_CACHE": shell_path(tmp_path / "pip"),
           "FIXTURE_NPM_CACHE": shell_path(tmp_path / "npm"),
           "FIXTURE_TRACE": shell_path(tmp_path / "calls"),
           "LEAF_DEP_CACHE_HELPER": shell_path(HELPER),
           "LEAF_CI_CACHE_DIR": shell_path(tmp_path / "cache"),
           "LEAF_CI_CACHE_WRITABLE": "0", "CODEBUILD_BUILD_ID": "device:shell"}
    monkeypatch.setattr(os, "environ", {"PATH": env["PATH"], "HOME": str(home)})
    stub(bins / "python", 'if [[ "${1:-}" == -I && "${2:-}" == -B ]]; then\n'
         + "  exec " + shlex.quote(shell_path(sys.executable)) + ' "$@"\nfi\n'
         + 'if [[ "$*" == "-m pip cache dir" ]]; then echo "$FIXTURE_PIP_CACHE"; exit 0; fi\n'
         + 'printf "python %s\\n" "$*" >> "$FIXTURE_TRACE"\n')
    stub(bins / "node", 'echo v22.1.0\n')
    stub(bins / "date", 'if [[ "$*" == "-u +%Y%m%d" ]]; then echo 20261111; else echo 123456; fi\n')
    tool = ('if [[ "$*" == "config get cache" ]]; then echo "$FIXTURE_NPM_CACHE"; exit 0; fi\n'
            'if [[ "${FIXTURE_INSPECT_BROWSER:-0}" == 1 && "$*" == "playwright install chromium" ]]; then\n'
            '  if [[ -f "$HOME/.cache/ms-playwright/chromium-1/INSTALLATION_COMPLETE" ]]; then\n'
            '    echo seeded-before-install >> "$FIXTURE_TRACE"\n'
            '  else echo cold-before-install >> "$FIXTURE_TRACE"; fi\nfi\n'
            'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$FIXTURE_TRACE"\n')
    stub(bins / "npm", tool)
    stub(bins / "npx", tool)
    return tmp_path, repo, env


def run(setup, command):
    return subprocess.run([BASH, "--noprofile", "--norc", "-c",
                           'set -euo pipefail\nexport PATH="$FIXTURE_BIN:$PATH"\n' + command],
                          env=setup[2], capture_output=True, text=True, timeout=30)


def seed_block(setup):
    text = CI.read_text()
    block = text.split("# LEAF_DEP_CACHE_SEED_BEGIN", 1)[1].split("# LEAF_DEP_CACHE_SEED_END", 1)[0]
    result = run(setup, block + '\nprintf "SEEDED=%s\\n" "$dep_cache_npm_seeded"\n')
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def publish(setup, capsys, kind="dl"):
    root, repo, env = setup
    if kind == "dl":
        for part, payload in (("pip", b"wheel"), ("npm", b"tarball")):
            source = root / ("source-" + part)
            source.mkdir()
            (source / "body").write_bytes(payload)
    else:
        browser = root / "home/.cache/ms-playwright/chromium-1"
        browser.mkdir(parents=True)
        (browser / "INSTALLATION_COMPLETE").write_bytes(b"")
        (browser / "chrome").write_bytes(b"browser")
        (browser / "chrome").chmod(0o755)
    argv = ["publish", "--kind", kind, "--repo", str(repo), "--cache-dir", str(root / "cache"),
            "--writable", "1", "--run", "shell-fixture", "--day", "20261111",
            "--node-version", "v22.1.0", "--pip-cache", str(root / "source-pip"),
            "--npm-cache", str(root / "source-npm")]
    assert cache.main(argv) == 0
    output = capsys.readouterr().out
    assert "result=published" in output
    if kind == "pw":
        shutil.rmtree(root / "home/.cache/ms-playwright")
    return next((root / "cache").glob(kind + "-v1-*"))


def tree(root):
    return {str(p.relative_to(root)): (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in [root, *root.rglob("*")]}


def test_absent_cache_is_inert(setup):
    root, _, env = setup
    env.pop("LEAF_CI_CACHE_DIR")
    output = seed_block(setup)
    assert "LEAF_DEP_CACHE" not in output
    assert "SEEDED=0" in output
    assert not (root / "cache").exists()
    assert not (root / "pip").exists()
    assert not (root / "npm").exists()


def test_exact_seed_is_readonly(setup, capsys):
    entry = publish(setup, capsys)
    before = tree(entry.parent)
    output = seed_block(setup)
    assert "result=exact" in output
    assert "pip=seeded" in output and "npm=seeded" in output
    assert "SEEDED=1" in output
    for part in ("pip", "npm"):
        assert (setup[0] / part / "body").read_bytes() == (entry / part / "body").read_bytes()
    assert tree(entry.parent) == before


def test_corrupt_seed_falls_back(setup, capsys):
    entry = publish(setup, capsys)
    (entry / "npm/body").write_bytes(b"corrupt")
    output = seed_block(setup)
    assert "result=corrupt" in output and "SEEDED=0" in output
    assert not (setup[0] / "pip").exists()
    assert not (setup[0] / "npm").exists()
    assert not list(setup[0].glob("*.leaf-seed-*"))


def test_helper_failure_falls_back(setup):
    helper = setup[0] / "failing.py"
    helper.write_text("raise SystemExit(3)\n")
    setup[2]["LEAF_DEP_CACHE_HELPER"] = shell_path(helper)
    output = seed_block(setup)
    assert "result=error" in output and "SEEDED=0" in output


def test_helper_load_permission_failure_falls_back(setup):
    selection = setup[0] / "selection"
    selection.mkdir()
    stub(setup[0] / "bin/git", "echo '# fixture helper'\n")
    stub(setup[0] / "bin/chmod", "exit 3\n")
    setup[2].update(TRUSTED_SHA="a" * 40, FIXTURE_SELECTION=shell_path(selection))
    block = CI.read_text().split("# LEAF_DEP_CACHE_HELPER_BEGIN", 1)[1].split(
        "# LEAF_DEP_CACHE_HELPER_END", 1)[0]
    result = run(setup, 'selection_dir="$FIXTURE_SELECTION"\n' + block
                 + '\n[[ -z "${LEAF_DEP_CACHE_HELPER:-}" ]]\necho survived\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LEAF_DEP_CACHE kind=all action=load result=off" in result.stdout
    assert "survived" in result.stdout


def test_publish_log_failure_falls_back(setup):
    setup[2]["LEAF_CI_CACHE_WRITABLE"] = "1"
    stub(setup[0] / "bin/mktemp", "exit 3\n")
    block = CI.read_text().split("# LEAF_DEP_CACHE_PUBLISH_BEGIN", 1)[1].split(
        "# LEAF_DEP_CACHE_PUBLISH_END", 1)[0]
    result = run(setup, block + '\n[[ -z "$dep_cache_publish_pid" ]]\necho survived\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LEAF_DEP_CACHE kind=dl action=publish result=error" in result.stdout
    assert "survived" in result.stdout
    assert not (setup[0] / "cache").exists()


def test_heavy_entry_seeds_before_install(setup, capsys):
    publish(setup, capsys, "pw")
    setup[2]["FIXTURE_INSPECT_BROWSER"] = "1"
    command = "exec bash " + shlex.quote(shell_path(CI)) + " --heavy-prerequisites"
    result = run(setup, command)
    assert result.returncode == 0, result.stdout + result.stderr
    trace = (setup[0] / "calls").read_text().splitlines()
    assert trace.index("seeded-before-install") + 1 == trace.index("npx playwright install chromium")
    assert "cold-before-install" not in trace
    (setup[0] / "calls").unlink()
    for key in ("LEAF_CI_CACHE_DIR", "LEAF_CI_CACHE_WRITABLE", "LEAF_DEP_CACHE_HELPER",
                "LEAF_DEP_CACHE_OFF", "PLAYWRIGHT_BROWSERS_PATH", "FIXTURE_INSPECT_BROWSER"):
        setup[2].pop(key, None)
    result = run(setup, command)
    assert result.returncode == 0, result.stdout + result.stderr
    # These independent producers overlap; preserve order within each producer.
    trace = (setup[0] / "calls").read_text().splitlines()
    browser = [line for line in trace if line.startswith("npx ") or "-m playwright" in line]
    normalized = [line.split(" about:blank ")[0] if " pdf about:blank " in line else line for line in browser]
    assert normalized == ["npx playwright install chromium", "npx playwright pdf",
                          "python -m playwright install chromium", "python -m playwright pdf"]
    producers = [line for line in trace if line not in browser]
    assert producers == ["npm run build", "python scripts/check_license_fence.py --self-test",
                         "python scripts/check_license_fence.py ."]


@pytest.mark.parametrize("outcome", ["exact", "miss", "corrupt", "absent", "error"])
def test_autofill_snapshot_cache_preserves_pinned_checkout(setup, capsys, outcome):
    root, repo, env = setup
    revision = "a" * 40
    manifest = {"head_sha": revision, "repository": "LEAF-Solar-Design/autofill-solver"}
    source = root / "snapshot-source"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "snapshot.bundle").write_bytes(b"fixture bundle")
    (repo / "deploy").mkdir()
    (repo / "deploy/autofill-solver-sources.json").write_text(json.dumps({revision: {}}))
    temporary = root / "temporary"
    temporary.mkdir()
    env.update(TMPDIR=shell_path(temporary), FIXTURE_REVISION=revision,
               FIXTURE_RELAY_PREFIX="forge-ci-relay/LEAF-Solar-Design/autofill-solver/" + revision + "/",
               FIXTURE_ARCHIVE=shell_path(root / "relay.zip"))
    with zipfile.ZipFile(root / "relay.zip", "w") as archive:
        for name in ("manifest.json", "snapshot.bundle"):
            archive.write(source / name, name)
    entry = None
    if outcome in ("exact", "corrupt"):
        assert cache.main(["publish", "--kind", "as", "--cache-dir", str(root / "cache"),
                           "--writable", "1", "--run", "fixture", "--revision", revision,
                           "--src", str(source)]) == 0
        assert "result=published" in capsys.readouterr().out
        entry = next((root / "cache").glob("as-v1-*"))
        if outcome == "corrupt":
            (entry / "snapshot/snapshot.bundle").write_bytes(b"corrupt")
    before = tree(root / "cache") if entry else None
    if outcome == "absent":
        env.pop("LEAF_CI_CACHE_DIR")
    elif outcome == "error":
        failing = root / "failing.py"
        failing.write_text("raise SystemExit(3)\n")
        env["LEAF_DEP_CACHE_HELPER"] = shell_path(failing)
    elif outcome == "miss":
        env["LEAF_CI_CACHE_WRITABLE"] = "1"
    stub(root / "bin/python3", "exec " + shlex.quote(shell_path(sys.executable)) + ' "$@"\n')
    stub(root / "bin/git", 'printf "git %s\\n" "$*" >> "$FIXTURE_TRACE"\n'
         'if [[ "$1" == clone ]]; then mkdir -p "${@: -1}"; fi\n'
         'if [[ "$*" == *"rev-parse HEAD" ]]; then echo "$FIXTURE_REVISION"; fi\n')
    stub(root / "bin/aws", 'printf "aws %s\\n" "$*" >> "$FIXTURE_TRACE"\n'
         'if [[ "$2" == list-objects-v2 ]]; then\n'
         '  printf \'{"Contents":[{"Key":"%ssnapshot.zip","LastModified":"2026-11-11"}]}\\n\' "$FIXTURE_RELAY_PREFIX"\n'
         'else cp "$FIXTURE_ARCHIVE" "${@: -1}"; fi\n')
    block = CI.read_text().split("# LEAF_AUTOFILL_SOLVER_BEGIN\n", 1)[1].split(
        "# LEAF_AUTOFILL_SOLVER_END", 1)[0]
    block = block.replace("/tmp/autofill-solver", shell_path(root / "checkout"))
    result = run(setup, 'cd "$CODEBUILD_SRC_DIR"\n' + block)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "autofill-solver: pinned " + revision in result.stdout
    trace = (root / "calls").read_text()
    assert "git clone -q --no-checkout" in trace
    assert "checkout -q --detach " + revision in trace
    assert "rev-parse HEAD" in trace
    if outcome == "exact":
        assert "result=exact" in result.stderr
        assert "from relay snapshot dep-cache" in result.stdout
        assert "aws " not in trace
    else:
        assert "aws s3api list-objects-v2" in trace
        assert "aws s3api get-object" in trace
    if before is not None:
        assert tree(root / "cache") == before
    if outcome == "corrupt":
        assert "result=corrupt" in result.stderr
    if outcome == "error":
        assert "kind=as action=seed result=error" in result.stderr
    if outcome == "miss":
        assert "action=publish result=published" in result.stderr
        published = next((root / "cache").glob("as-v1-*"))
        assert (published / "snapshot/snapshot.bundle").read_bytes() == b"fixture bundle"
    assert not list(temporary.iterdir())
