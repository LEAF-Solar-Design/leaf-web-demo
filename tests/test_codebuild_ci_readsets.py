"""Exercise advisory read-set publication after the native CodeBuild gate."""

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from test_codebuild_ci_script import BASH, CI_PATH, ROOT


BUILD_ID = "leaf-ci-leaf-web-demo:12345678-1234-1234-1234-123456789abc"
ATTEMPTS_REF = "readsets/sample/1/attempts-main-123.jsonl"


def fixture_catalog():
    catalog = {"schema": "leaf.ci.test-catalog.v1", "kind": "web",
               "runner_catalog_sha256": "e" * 64,
               "suites": [{"id": "sample", "test_ids": ["sample::test_sample.py::test_ok"]}]}
    catalog["catalog_sha256"] = hashlib.sha256(json.dumps(
        catalog, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return catalog


@unittest.skipUnless(BASH, "bash is not on PATH; read-set publication requires bash")
class TestCodebuildCiReadsets(unittest.TestCase):
    def run_publication(self, *, tracing=True, aws_status=0, gate_status=0,
                        missing_decision=False, malformed_manifest=False):
        script = CI_PATH.read_text(encoding="utf-8")
        export_start = script.index('  export LEAF_READSET_CATALOG_SHA256=')
        export_end = script.index('LEAF_CAPTURE_ID\n)"', export_start) + len('LEAF_CAPTURE_ID\n)"')
        catalog_export = script[export_start:export_end]
        # Run the real post-gate publication and finalizer with isolated log paths.
        script = script[script.index("# Read-set publication is advisory"):]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = root / "selection"
            logs = root / "gate-logs"
            results = root / "gate-results"
            binaries = root / "bin"
            for directory in (selection, logs / "readsets" / "sample" / "1",
                              logs / "attempts", results, binaries):
                directory.mkdir(parents=True, exist_ok=True)
            catalog = fixture_catalog()
            shard = {"schema": "leaf.ci.readset.v1", "suite_id": "sample", "worker": "main",
                     "run_id": BUILD_ID, "source_sha": "b" * 40, "source_tree": "c" * 40,
                     "capture_sha": "d" * 40, "catalog_sha256": catalog["catalog_sha256"],
                     "outcomes_ref": ATTEMPTS_REF}
            (logs / "readsets" / "sample" / "1" / "main-123.json").write_text(json.dumps(shard))
            (logs / ATTEMPTS_REF).write_text(json.dumps({
                "schema": "leaf.ci.test-attempt.v1", "nodeid": "test_sample.py::test_ok",
                "attempt": 1, "phase": "call", "outcome": "passed"}) + "\n")
            (selection / "catalog.json").write_text(json.dumps(catalog))
            for name in ("select_tests.py", "full_run_manifest.py"):
                shutil.copyfile(ROOT / "scripts" / "ci" / name, selection / name)
            (selection / "decision.json").write_bytes(b'{"execution_mode": "full"}\n')
            if missing_decision:
                (selection / "decision.json").unlink()
            build_id = BUILD_ID
            (logs / "attempts" / "sample.jsonl").write_text(json.dumps({
                "suite_id": "sample", "run_id": build_id, "attempt": 1,
                "test_ids": ["sample::test_sample.py::test_ok"], "failed_test_ids": [],
                "test_report_complete": True, "status": "PASS"}) + "\n")
            (selection / "detail.json").write_text(json.dumps({
                "schema": "leaf.ci.selection.v1", "phase": "shadow",
                "build_id": build_id, "execution_mode": "full",
                "executed_suite_ids": ["sample"], "readsets_ref": str(logs / "readsets"),
            }), encoding="utf-8")
            aws = binaries / "aws"
            aws.write_bytes(b'#!/usr/bin/env bash\n'
                            b'printf "%s\\n" "$@" >> "$READSETS_AWS_CALLS"\n'
                            b'exit "$READSETS_AWS_STATUS"\n')
            aws.chmod(0o755)
            calls_path = root / "aws-calls"
            tar_calls_path = root / "tar-calls"
            env = dict(os.environ, selection_dir="selection",
                       tracing_ready="1" if tracing else "0", reporters_ready="1",
                       trusted_sha_override="1", loader_check="override",
                       gate_status=str(gate_status), CODEBUILD_BUILD_ID=build_id,
                       HEAD_SHA="b" * 40, TRUSTED_SHA="b" * 40,
                       LEAF_READSET_SOURCE_TREE="c" * 40,
                       LEAF_READSET_CAPTURE_SHA="bad" if malformed_manifest else "d" * 40,
                       CODEBUILD_BUILD_IMAGE="fixture-image",
                       READSETS_AWS_CALLS=calls_path.as_posix(),
                       READSETS_AWS_STATUS=str(aws_status),
                       READSETS_TAR_CALLS=tar_calls_path.as_posix())
            script = script.replace("/tmp/gate-logs", logs.as_posix()).replace(
                "/tmp/gate-results", results.as_posix())
            command = ('set -euo pipefail\n'
                       f'cd {shlex.quote(root.as_posix())}\n'
                       'selection_dir="$PWD/selection"\n'
                       'export PATH="$PWD/bin:$PATH"\n'
                       f'python() {{ {shlex.quote(Path(sys.executable).as_posix())} "$@"; }}\n'
                       'tar() { printf "%s\\n" tar "$@" >> "$READSETS_TAR_CALLS"; command tar "$@"; }\n'
                       + catalog_export + '\nprintf "%s" "$LEAF_READSET_CATALOG_SHA256" > selection/exported-catalog\n'
                       + script)
            result = subprocess.run([BASH, "-s"], input=command, cwd=ROOT, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, gate_status, result.stdout + result.stderr)
            final = next(json.loads(line.removeprefix("LEAF_SELECTION_FINAL "))
                         for line in result.stdout.splitlines()
                         if line.startswith("LEAF_SELECTION_FINAL "))
            shadow = next(json.loads(line.removeprefix("LEAF_SHADOW "))
                          for line in result.stdout.splitlines()
                          if line.startswith("LEAF_SHADOW "))
            detail = json.loads((selection / "detail.json").read_text(encoding="utf-8"))
            self.assertEqual((selection / "exported-catalog").read_text(), catalog["catalog_sha256"])
            if tracing and not malformed_manifest:
                manifest = json.loads((selection / "full-run.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["schema"], "leaf.ci.full-run.v1")
                self.assertEqual(manifest["repo"], "leaf-web-demo")
                self.assertEqual(manifest["provider_binding"], {key: shard[key] for key in
                    ("run_id", "source_sha", "source_tree", "capture_sha", "catalog_sha256")})
                self.assertTrue(manifest["provider_bound"])
                self.assertEqual(manifest["catalog_sha256"], catalog["catalog_sha256"])
                self.assertEqual(manifest["execution_mode"], "full")
                self.assertTrue(manifest["full_run_complete"])
                self.assertTrue(manifest["test_id_reporting_complete"])
                self.assertEqual(manifest["suite_ids"], ["sample"])
                self.assertEqual(manifest["workers_by_suite"], {"sample": ["main"]})
                self.assertRegex(manifest["toolchain_fingerprint"], r"^[0-9a-f]{64}$")
            else:
                self.assertFalse((selection / "full-run.json").exists())
            for field in ("readsets_object", "readsets_sha256", "readsets_bytes", "readsets_status",
                          "readsets_archive_members"):
                self.assertEqual(final[field], shadow[field])
                self.assertEqual(final[field], detail[field])
            self.assertEqual(final["test_exit_code"], gate_status)
            self.assertEqual(final["build_exit_code"], gate_status)
            self.assertEqual(final["readsets_ref"], str(logs / "readsets"))
            calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
            archive = selection / "readsets.tar.gz"
            return (result, final, calls, archive.read_bytes() if archive.exists() else None,
                    tar_calls_path.read_text(encoding="utf-8") if tar_calls_path.exists() else "")

    def test_tracing_upload_is_immutable_and_receipted(self):
        result, final, calls, archive, tar_calls = self.run_publication()
        self.assertEqual(final["readsets_status"], "uploaded")
        self.assertRegex(final["readsets_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(final["readsets_sha256"], hashlib.sha256(archive).hexdigest())
        self.assertEqual(final["readsets_bytes"], len(archive))
        members = ["readsets", "catalog.json", "decision.json", "full-run.json", ATTEMPTS_REF]
        self.assertEqual(final["readsets_archive_members"], members)
        self.assertEqual(tar_calls.splitlines().count("tar"), 1)
        self.assertEqual([arg for arg in tar_calls.splitlines() if arg in members], members)
        self.assertEqual(calls[:2], ["s3api", "put-object"])
        self.assertEqual(calls.count("put-object"), 1)
        self.assertEqual(calls[calls.index("--if-none-match") + 1], "*")
        self.assertEqual(calls[calls.index("--checksum-algorithm") + 1], "SHA256")
        bucket = calls[calls.index("--bucket") + 1]
        key = calls[calls.index("--key") + 1]
        self.assertEqual(bucket, "leaf-mq-transport-807034087062-us-east-1")
        self.assertEqual(key, "mq/leaf-web-demo/selection/"
                         "12345678-1234-1234-1234-123456789abc.readsets.tar.gz")
        self.assertEqual(final["readsets_object"], f"s3://{bucket}/{key}")
        self.assertEqual(dict(item.split("=", 1) for item in
                              calls[calls.index("--metadata") + 1].split(",")), {
            "build_id": "leaf-ci-leaf-web-demo:12345678-1234-1234-1234-123456789abc",
            "head_sha": "b" * 40, "trusted_sha": "b" * 40, "trusted_sha_override": "1",
        })
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
            shard = json.loads(packed.extractfile("readsets/sample/1/main-123.json").read())
            self.assertEqual(json.loads(packed.extractfile(shard["outcomes_ref"]).read())["schema"],
                             "leaf.ci.test-attempt.v1")
            self.assertEqual(json.loads(packed.extractfile("catalog.json").read()), fixture_catalog())
            manifest = json.loads(packed.extractfile("full-run.json").read())
            self.assertTrue(manifest["full_run_complete"])
            self.assertEqual(json.loads(packed.extractfile("decision.json").read()), {"execution_mode": "full"})
        self.assertNotIn("WARNING: readsets upload failed", result.stderr)

    def test_missing_decision_still_uploads_available_members(self):
        result, final, calls, archive, tar_calls = self.run_publication(missing_decision=True)
        self.assertEqual(final["readsets_status"], "uploaded")
        members = ["readsets", "catalog.json", "full-run.json", ATTEMPTS_REF]
        self.assertEqual(final["readsets_archive_members"], members)
        self.assertEqual([arg for arg in tar_calls.splitlines() if arg in members], members)
        self.assertNotIn("decision.json", tar_calls.splitlines())
        self.assertEqual(calls.count("put-object"), 1)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
            self.assertEqual(sorted({name.split("/")[0] for name in packed.getnames()}),
                             sorted({name.split("/")[0] for name in members}))
        self.assertNotIn("WARNING: readsets upload failed", result.stderr)

    def test_malformed_manifest_still_uploads_readsets(self):
        result, final, calls, archive, tar_calls = self.run_publication(malformed_manifest=True)
        self.assertEqual(final["readsets_status"], "manifest_failed")
        self.assertIsNotNone(final["readsets_object"])
        self.assertEqual(calls.count("put-object"), 1)
        self.assertNotIn("full-run.json", tar_calls.splitlines())
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
            self.assertIn(ATTEMPTS_REF, packed.getnames())
        self.assertIn("invalid_capture_sha", result.stderr)

    def test_failed_upload_preserves_gate_result(self):
        for gate_status in (0, 7):
            with self.subTest(gate_status=gate_status):
                result, final, calls, archive, _ = self.run_publication(
                    aws_status=255, gate_status=gate_status)
                self.assertEqual(final["readsets_status"], "upload_failed")
                self.assertIsNone(final["readsets_object"])
                self.assertEqual(final["readsets_sha256"], hashlib.sha256(archive).hexdigest())
                self.assertEqual(final["readsets_bytes"], len(archive))
                self.assertEqual(calls.count("put-object"), 1)
                self.assertEqual(result.stderr,
                                 "WARNING: readsets upload failed (put-object exit 255)\n")

    def test_non_tracing_skips_archive_and_upload(self):
        result, final, calls, archive, tar_calls = self.run_publication(tracing=False)
        self.assertEqual(final["readsets_status"], "not_traced")
        self.assertEqual(final["readsets_archive_members"], [])
        for field in ("readsets_object", "readsets_sha256", "readsets_bytes"):
            self.assertIsNone(final[field])
        self.assertEqual(calls, [])
        self.assertEqual(tar_calls, "")
        self.assertIsNone(archive)
        self.assertNotIn("WARNING: readsets upload failed", result.stderr)
