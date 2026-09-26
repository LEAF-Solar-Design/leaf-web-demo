"""Exercise advisory read-set publication after the native CodeBuild gate."""

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest

from test_codebuild_ci_script import BASH, CI_PATH, ROOT


@unittest.skipUnless(BASH, "bash is not on PATH; read-set publication requires bash")
class TestCodebuildCiReadsets(unittest.TestCase):
    def run_publication(self, *, tracing=True, aws_status=0, gate_status=0):
        script = CI_PATH.read_text(encoding="utf-8")
        # Run the real post-gate publication and finalizer with isolated log paths.
        script = script[script.index("# Read-set publication is advisory"):]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = root / "selection"
            logs = root / "gate-logs"
            results = root / "gate-results"
            binaries = root / "bin"
            for directory in (selection, logs / "readsets", results, binaries):
                directory.mkdir(parents=True, exist_ok=True)
            shard = b'{"read":"server/app.py"}\n'
            (logs / "readsets" / "shard.jsonl").write_bytes(shard)
            build_id = "leaf-ci-leaf-web-demo:12345678-1234-1234-1234-123456789abc"
            (selection / "detail.json").write_text(json.dumps({
                "schema": "leaf.ci.selection.v1", "phase": "shadow",
                "build_id": build_id, "execution_mode": "full",
                "executed_suite_ids": [], "readsets_ref": str(logs / "readsets"),
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
                       READSETS_AWS_CALLS=calls_path.as_posix(),
                       READSETS_AWS_STATUS=str(aws_status),
                       READSETS_TAR_CALLS=tar_calls_path.as_posix())
            script = script.replace("/tmp/gate-logs", logs.as_posix()).replace(
                "/tmp/gate-results", results.as_posix())
            command = ('set -euo pipefail\n'
                       f'cd {shlex.quote(root.as_posix())}\n'
                       'export PATH="$PWD/bin:$PATH"\n'
                       f'python() {{ {shlex.quote(Path(sys.executable).as_posix())} "$@"; }}\n'
                       'tar() { printf "tar\\n" >> "$READSETS_TAR_CALLS"; command tar "$@"; }\n'
                       + script)
            # bash -s reads the script from stdin: a long -c argument is mangled by argv quoting under MSYS bash on Windows.
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
            for field in ("readsets_object", "readsets_sha256", "readsets_bytes", "readsets_status"):
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
        self.assertEqual(tar_calls, "tar\n")
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
            self.assertEqual(packed.extractfile("readsets/shard.jsonl").read(),
                             b'{"read":"server/app.py"}\n')
        self.assertNotIn("WARNING: readsets upload failed", result.stderr)

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
        for field in ("readsets_object", "readsets_sha256", "readsets_bytes"):
            self.assertIsNone(final[field])
        self.assertEqual(calls, [])
        self.assertEqual(tar_calls, "")
        self.assertIsNone(archive)
        self.assertNotIn("WARNING: readsets upload failed", result.stderr)
