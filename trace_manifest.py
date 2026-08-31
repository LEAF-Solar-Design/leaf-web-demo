#!/usr/bin/env python3
"""One traced run of the extracted manifest script. Scratch debug helper."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "scripts"))
import test_build_platform_images_workflow as t  # noqa: E402

relay = t._strict_yaml(
    (t.WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(encoding="utf-8"))
man = next(s for s in relay["jobs"]["dispatch"]["steps"] if s.get("id") == "manifest")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="trace-"))
zd = tmp / "z"
zd.mkdir()
bindir = tmp / "bin"
bindir.mkdir()
(bindir / "gh").write_text(t._FAKE_GH_MANIFEST, encoding="utf-8")
(bindir / "gh").chmod(0o755)

arts = {
    "900": [{"name": "docs-noop-tipdocs-attempt-1", "id": 0}],
    "800": [{"name": "staging-supply-set-a111111-attempt-1", "id": 1}],
    "850": [{"name": "staging-supply-set-b222222-attempt-1", "id": 2}],
}
for aid, tag, src in ((1, "prod-a111111", "a111111"), (2, "prod-b222222", "b222222")):
    with zipfile.ZipFile(zd / f"{aid}.zip", "w") as z:
        z.writestr("staging-supply-set.json", json.dumps(
            {"schema": "leaf.staging-supply-set.v1",
             "build_tag": tag, "source_revision": src}))

script = tmp / "m.sh"
script.write_text(man["run"], encoding="utf-8")
gh_out = tmp / "o"
gh_out.write_text("", encoding="utf-8")
wd = tmp / "w"
wd.mkdir()

env = dict(os.environ)
env.update(
    PATH=f"{bindir}{os.pathsep}{env['PATH']}",
    GITHUB_REPOSITORY="o/r", GITHUB_OUTPUT=str(gh_out),
    DEPLOY_WORKFLOW="d.yml", BUILD_RUN_ID="900", BUILD_HEAD_SHA="tipdocs",
    BUILD_RUN_ATTEMPT="1", BUILD_WORKFLOW_ID="42", GH_TOKEN="x",
    FAKE_ARTIFACTS=json.dumps(arts),
    FAKE_RUNS=json.dumps([
        {"id": 800, "head_sha": "a111111", "run_attempt": 1},
        {"id": 850, "head_sha": "b222222", "run_attempt": 1}]),
    FAKE_RELATIONS=json.dumps({"a111111": "ahead", "b222222": "ahead"}),
    FAKE_ZIP_DIR=str(zd),
)

bash = shutil.which("bash")
proc = subprocess.run([bash, "-x", str(script)], env=env, cwd=str(wd),
                      text=True, capture_output=True)
print("bash:", bash)
print("rc:", proc.returncode)
print("GITHUB_OUTPUT:", gh_out.read_text(encoding="utf-8").strip())
print("--- STDOUT ---")
print(proc.stdout)
print("--- STDERR (trace) ---")
print(proc.stderr)
