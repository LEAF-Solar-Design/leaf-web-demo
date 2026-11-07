"""Pin the leaf-web-demo native CodeBuild CI rail without parsing YAML."""

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".codebuild/ci.sh"
PROJECT_PATH = ROOT / "scripts/ci/pr-codebuild-project.sh"
BASH = shutil.which("bash")


@lru_cache(maxsize=None)
def _bash_runs_python() -> bool:
    if not BASH:
        return False
    try:
        return subprocess.run(
            [BASH, "-c", "python3 -c 'import sys'"],  # pr-codebuild-project.sh runs python3
            capture_output=True, text=True, timeout=30).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


class TestCodebuildCiScript(unittest.TestCase):
    def _run_diagnostic(self, overrides, ids=None, runner_status=0, postgres_status=0,
                        catalog_status=0, raw_catalog=None):
        script = CI_PATH.read_text(encoding="utf-8")
        # Execute the real PostgreSQL setup, EXIT trap, admission, catalog validator,
        # runner invocation and early exit. Runner/PG are inert doubles; the real
        # retained-log publisher reads and writes only fixture-private artifacts.
        start = script.index('echo "LEAF_T start postgres ')
        block = script[start:script.index('# LEAF_DIAGNOSTIC_END', start)]
        env = {key: value for key, value in os.environ.items()
               if key not in ("LEAF_DIAGNOSTIC_PROFILE", "LEAF_GATE_ONLY", "BASH_ENV")}
        env.update(overrides)
        catalog = {"schema": "leaf.ci.catalog.v1",
                   "suites": [{"id": suite_id} for suite_id in (ids or [])]}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            raw_log = ("padding\n" * 40000 + "INERT_BOUNDED_RUNNER_EVIDENCE\n"
                       'context={"private":"inert-private-context"}\n'
                       "password=inert-private-password\n").encode()
            (directory / "input.log").write_bytes(raw_log)
            (directory / "logs").mkdir()
            runner = directory / "fake_runner.py"
            runner.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['TEST_CALLS'], 'a', encoding='utf-8') as out:\n"
                "    out.write(json.dumps(args) + '\\n')\n"
                "if '--list' in args:\n"
                "    print(os.environ['TEST_CATALOG'])\n"
                "    sys.exit(int(os.environ['TEST_CATALOG_STATUS']))\n"
                "suite = args[args.index('--only') + 1]\n"
                "status = int(os.environ['TEST_RUNNER_STATUS'])\n"
                "directory = Path(os.environ['TEST_DIRECTORY'])\n"
                "(directory / 'result.json').write_text(json.dumps({'results': [\n"
                "    {'id': suite, 'status': 'FAIL' if status else 'PASS', 'attempts': 1}\n"
                "]}), encoding='utf-8')\n"
                "(directory / 'logs' / (suite + '.log')).write_bytes(\n"
                "    (directory / 'input.log').read_bytes())\n"
                "sys.exit(int(os.environ['TEST_RUNNER_STATUS']))\n", encoding="utf-8")
            # Forward slashes also work when this test is run by Windows Python/Git Bash.
            env.update(MSYS_NO_PATHCONV="1", MSYS2_ARG_CONV_EXCL="*",
                       TEST_PYTHON=sys.executable.replace("\\", "/"),
                       TEST_RUNNER=runner.as_posix(), TEST_CALLS=(directory / "calls").as_posix(),
                       TEST_DIRECTORY=directory.as_posix(),
                       HEAD_SHA="a" * 40, CODEBUILD_BUILD_ID="device:dv-" + "b" * 16,
                       TEST_DIAGNOSTIC=(directory / "diagnostic.txt").as_posix(),
                       TEST_CATALOG=raw_catalog if raw_catalog is not None else json.dumps(catalog),
                       TEST_CATALOG_STATUS=str(catalog_status),
                       TEST_RUNNER_STATUS=str(runner_status), TEST_POSTGRES_STATUS=str(postgres_status))
            command = '''set -euo pipefail
selection_dir="$1"
CODEBUILD_SRC_DIR="$2"
python() {
  if [[ "$1" == scripts/run-all-gates.py ]]; then
    command "$TEST_PYTHON" "$TEST_RUNNER" "$@"
  elif [[ "$1" == -I && "$2" == -B && "$3" == scripts/ci/retain_failed_gate_logs.py ]]; then
    [[ "$#" == 14 ]] || return 98
    [[ "$*" == "-I -B scripts/ci/retain_failed_gate_logs.py --result /tmp/gate-results/gate-result.json --logs /tmp/gate-logs --output /tmp/gate-results/diagnostic-failed-gate-logs --emit-stdout --source $HEAD_SHA --build-id $CODEBUILD_BUILD_ID" ]] || return 98
    shift 9
    command "$TEST_PYTHON" -I -B "$CODEBUILD_SRC_DIR/scripts/ci/retain_failed_gate_logs.py" \
      --result "$TEST_DIRECTORY/result.json" --logs "$TEST_DIRECTORY/logs" \
      --output "$TEST_DIRECTORY/retained" "$@"
  else
    command "$TEST_PYTHON" "$@"
  fi
}
bash() {
  [[ "$1" == "$CODEBUILD_SRC_DIR/scripts/ci/start_ephemeral_postgres.sh" ]] || return 98
  if [[ "$2" == start ]]; then
    printf 'postgresql://inert-private-database\n'
    return "$TEST_POSTGRES_STATUS"
  fi
  [[ "$2" == stop ]] || return 98
  printf 'POSTGRES_CLEANUP\n'
}
tee() {
  # Keep the production block byte-for-byte intact while redirecting its fixed
  # output destination into this test's temporary directory.
  if [[ "$1" == -a ]]; then
    [[ "$2" == /tmp/gate-results/diagnostic.txt && "$#" == 2 ]] || return 98
    command tee -a "$TEST_DIAGNOSTIC"
  else
    [[ "$1" == /tmp/gate-results/diagnostic.txt && "$#" == 1 ]] || return 98
    command tee "$TEST_DIAGNOSTIC"
  fi
}
''' + block + '''
printf 'ORDINARY_REQUIRED_CONTROLS\nFULL_SHARD_GREEN_PROOF\n'
'''
            result = subprocess.run(
                [BASH, "-c", command, "diagnostic", directory.as_posix(), ROOT.as_posix()],
                env=env, capture_output=True, text=True, timeout=30)
            calls_path = directory / "calls"
            calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()] \
                if calls_path.exists() else []
            diagnostic_path = directory / "diagnostic.txt"
            diagnostic = diagnostic_path.read_text(encoding="utf-8") if diagnostic_path.exists() else None
            manifest_path = directory / "retained" / "manifest.json"
            result.retained_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) \
                if manifest_path.exists() else None
            result.fixture_log_sha256 = hashlib.sha256(raw_log).hexdigest()
            return result, calls, diagnostic

    @unittest.skipUnless(BASH, "bash is not on PATH; diagnostic execution requires bash")
    def test_diagnostic_exact_suite_status_and_no_full_proof(self):
        for suite_id in ("a", "studio-suite", "a" + "-" * 63):
            for status in (0, 7, 137):
                with self.subTest(suite_id=suite_id, status=status):
                    result, calls, diagnostic = self._run_diagnostic(
                        {"LEAF_DIAGNOSTIC_PROFILE": "web-demo-suite", "LEAF_GATE_ONLY": suite_id},
                        ["other", suite_id], runner_status=status)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                    self.assertEqual(calls, [
                        ["scripts/run-all-gates.py", "--list", "--catalog-root", ROOT.as_posix()],
                        ["scripts/run-all-gates.py", "--jobs", "1", "--retry", "0", "--fail-fast",
                         "--result-json", "/tmp/gate-results/gate-result.json", "--log-dir",
                         "/tmp/gate-logs", "--only", suite_id]])
                    self.assertIn("diagnostic-only/no-full-verdict", result.stdout)
                    self.assertNotIn("ORDINARY_REQUIRED_CONTROLS", result.stdout)
                    self.assertNotIn("FULL_SHARD_GREEN_PROOF", result.stdout)
                    self.assertEqual(result.stdout.count("POSTGRES_CLEANUP"), 1)
                    self.assertIn(f"suite={suite_id} status={status} postgres_status=0", diagnostic)
                    self.assertEqual(len(diagnostic.splitlines()), 1)
                    self.assertNotIn("INERT_BOUNDED_RUNNER_EVIDENCE", diagnostic)
                    frames = [line for line in result.stdout.splitlines()
                              if line.startswith("LEAF_FAILED_LOG_")]
                    self.assertLessEqual(len(("\n".join(frames) + "\n").encode()), 2 * 1024 * 1024)
                    retained = result.retained_manifest
                    self.assertIsNotNone(retained, result.stdout + result.stderr)
                    self.assertEqual(retained["failed_suites"], int(status != 0))
                    self.assertNotIn("WARNING:", result.stderr)
                    for private in ("inert-private-context", "inert-private-password",
                                    "inert-private-database"):
                        self.assertNotIn(private, result.stdout + result.stderr + diagnostic)
                    if status == 0:
                        self.assertEqual(frames, [])
                        self.assertEqual(retained["files"], [])
                    else:
                        manifests = [json.loads(line.split(" ", 1)[1]) for line in frames
                                     if line.startswith("LEAF_FAILED_LOG_MANIFEST ")]
                        chunks = [json.loads(line.split(" ", 1)[1]) for line in frames
                                  if line.startswith("LEAF_FAILED_LOG_CHUNK ")]
                        self.assertEqual(len(manifests), 1)
                        self.assertTrue(chunks)
                        manifest = manifests[0]
                        self.assertEqual(manifest["schema"], "leaf.ci.failed-logs.stdout.v1")
                        self.assertEqual(manifest["failed_suites"], 1)
                        for frame in [manifest, *chunks]:
                            self.assertEqual(frame["source_sha"], "a" * 40)
                            self.assertEqual(frame["build_id"], "device:dv-" + "b" * 16)
                        self.assertEqual([chunk["chunk_index"] for chunk in chunks], list(range(len(chunks))))
                        for chunk in chunks:
                            self.assertEqual(chunk["suite_id"], suite_id)
                            self.assertEqual(chunk["log_name"], suite_id + ".log")
                        published = "".join(chunk["text"] for chunk in chunks)
                        self.assertEqual(published.count("INERT_BOUNDED_RUNNER_EVIDENCE"), 1)
                        self.assertIn("[REDACTED]", published)
                        self.assertEqual(len(manifest["files"]), 1)
                        evidence = manifest["files"][0]
                        self.assertEqual(evidence["source"], suite_id + ".log")
                        self.assertEqual(evidence["source_sha256"], result.fixture_log_sha256)
                        self.assertEqual(evidence["status"], "copied")
                        self.assertTrue(evidence["truncated"])
                        self.assertTrue(evidence["partial_first_line_dropped"])
                        self.assertEqual(evidence["publication_status"], "published")
                        self.assertFalse(evidence["publication_truncated"])
                        self.assertEqual(evidence["copy_sha256"], retained["files"][0]["copy_sha256"])
                        self.assertLessEqual(evidence["copied_size"], 256 * 1024)
                        self.assertEqual(evidence["published_sha256"], hashlib.sha256(published.encode()).hexdigest())
                        self.assertEqual(evidence["published_bytes"], len(published.encode()))

    @unittest.skipUnless(BASH, "bash is not on PATH; diagnostic execution requires bash")
    def test_diagnostic_unknown_colliding_or_invalid_catalog_runs_no_suite(self):
        cases = [([], 0, None), (["other"], 0, None), (["suite-extra"], 0, None),
                 (["suite", "suite-extra"], 0, None), (["prefix-suite", "suite"], 0, None),
                 (["suite", "suite"], 0, None), (["suite"], 9, None),
                 ([], 0, "not json"), ([], 0, "[]"),
                 ([], 0, '{"schema":"wrong","suites":[{"id":"suite"}]}'),
                 ([], 0, '{"schema":"leaf.ci.catalog.v1","suites":[{}]}'),
                 ([None], 0, None)]
        for ids, catalog_status, raw_catalog in cases:
            with self.subTest(ids=ids, catalog_status=catalog_status, raw_catalog=raw_catalog):
                result, calls, diagnostic = self._run_diagnostic(
                    {"LEAF_DIAGNOSTIC_PROFILE": "web-demo-suite", "LEAF_GATE_ONLY": "suite"},
                    ids, catalog_status=catalog_status, raw_catalog=raw_catalog)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(calls, [["scripts/run-all-gates.py", "--list", "--catalog-root",
                                          ROOT.as_posix()]])
                self.assertIn("LEAF_DIAGNOSTIC refused", result.stderr)
                self.assertIn("status=2", diagnostic)
                self.assertNotIn("FULL_SHARD_GREEN_PROOF", result.stdout)
                self.assertNotIn("ORDINARY_REQUIRED_CONTROLS", result.stdout)
                self.assertNotIn("LEAF_FAILED_LOG_", result.stdout)
                self.assertIsNone(result.retained_manifest)
                self.assertIn("failed_log_retention error=input_or_output_refused", result.stdout)
                self.assertEqual(result.stdout.count("POSTGRES_CLEANUP"), 1)

    @unittest.skipUnless(BASH, "bash is not on PATH; diagnostic execution requires bash")
    def test_diagnostic_unpaired_wrong_or_malformed_env_preserves_normal_path(self):
        cases = [{}, {"LEAF_DIAGNOSTIC_PROFILE": "web-demo-suite"}, {"LEAF_GATE_ONLY": "suite"},
                 {"LEAF_DIAGNOSTIC_PROFILE": "other", "LEAF_GATE_ONLY": "suite"},
                 {"LEAF_DIAGNOSTIC_PROFILE": "", "LEAF_GATE_ONLY": "suite"},
                 {"LEAF_DIAGNOSTIC_PROFILE": "web-demo-suite\n", "LEAF_GATE_ONLY": "suite"}]
        cases.extend({"LEAF_DIAGNOSTIC_PROFILE": "web-demo-suite", "LEAF_GATE_ONLY": value}
                     for value in ("", "Suite", "-suite", "suite_name", "suite/path", "suite other",
                                   "suite;false", "suite\n", "a" * 65))
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result, calls, diagnostic = self._run_diagnostic(overrides, ["suite"])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, [])
                self.assertIsNone(diagnostic)
                self.assertNotIn("LEAF_DIAGNOSTIC", result.stdout)
                self.assertIn("ORDINARY_REQUIRED_CONTROLS\nFULL_SHARD_GREEN_PROOF", result.stdout)
                self.assertEqual(result.stdout.count("POSTGRES_CLEANUP"), 1)

    @unittest.skipUnless(BASH, "bash is not on PATH; diagnostic execution requires bash")
    def test_diagnostic_postgres_failure_never_becomes_pass(self):
        for runner_status, expected in ((0, 1), (7, 7)):
            with self.subTest(runner_status=runner_status):
                result, calls, diagnostic = self._run_diagnostic(
                    {"LEAF_DIAGNOSTIC_PROFILE": "web-demo-suite", "LEAF_GATE_ONLY": "suite"},
                    ["suite"], runner_status=runner_status, postgres_status=1)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1], [
                    "scripts/run-all-gates.py", "--jobs", "1", "--retry", "0", "--fail-fast",
                    "--result-json", "/tmp/gate-results/gate-result.json", "--log-dir",
                    "/tmp/gate-logs", "--only", "suite"])
                self.assertIn("FATAL: ephemeral PostgreSQL did not start", result.stderr)
                self.assertIn(f"status={expected} postgres_status=1", diagnostic)
                self.assertNotIn("FULL_SHARD_GREEN_PROOF", result.stdout)
                self.assertNotIn("ORDINARY_REQUIRED_CONTROLS", result.stdout)
                self.assertIsNotNone(result.retained_manifest, result.stdout + result.stderr)
                self.assertEqual(result.retained_manifest["failed_suites"], int(runner_status != 0))
                self.assertNotIn("WARNING:", result.stderr)
                self.assertEqual(result.stdout.count("POSTGRES_CLEANUP"), 1)

    def test_diagnostic_exit_precedes_normal_publication_and_proof(self):
        script = CI_PATH.read_text(encoding="utf-8")
        before, rest = script.split("# LEAF_DIAGNOSTIC_BEGIN\n", 1)
        block, after = rest.split("# LEAF_DIAGNOSTIC_END\n", 1)
        self.assertIn('export LEAF_GATE_REQUIRE_DATABASE=1', before)
        self.assertIn('exit "$diagnostic_status"', block)
        self.assertNotIn("--early-feedback", block)
        self.assertNotIn("--emit-proof", block)
        self.assertNotIn("aws ", block)
        self.assertTrue(after.startswith('gate_status=0\ngate_fail_fast_args=(--fail-fast)\n'))
        self.assertIn('# LEAF_GATE_PROOF_BEGIN', after)
        self.assertIn('--key "mq/leaf-web-demo/selection/main/', after)

    @unittest.skipUnless(BASH, "bash is not on PATH; diagnostic syntax requires bash")
    def test_diagnostic_bash_syntax(self):
        result = subprocess.run([BASH, "-n", CI_PATH.as_posix()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(BASH, "bash is not on PATH; capture summary requires bash")
    def test_capture_summary_exception_names_type_and_preserves_verdict(self):
        source = CI_PATH.read_text(encoding="utf-8")
        summary = source.split("<<'LEAF_CAPTURE_SUMMARY'", 1)[1].split("\n", 1)[1].split(
            "\nLEAF_CAPTURE_SUMMARY\n", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            for gate_status in (0, 7):
                command = (f'{shlex.quote(sys.executable)} -I -B - "$1" invalid "$1" '
                           "<<'SUMMARY'\n" + summary + "\nSUMMARY\nexit " + str(gate_status))
                result = subprocess.run([BASH, "-c", command, "summary", temporary],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, gate_status, result.stderr)
                self.assertEqual(result.stdout, "LEAF_CAPTURE package_error summary_failed:ValueError\n")

    def test_tracer_helpers_are_not_run_as_isolated_scripts(self):
        # python -I implies -P: a helper run as a script cannot import its siblings, so capture 1
        # (build 0779b89d, 2026-11-01) refused with epoch_unavailable. Helpers that import
        # trace_supervisor must be loaded from the helper dir through -c.
        script = CI_PATH.read_text(encoding="utf-8")
        for helper in ("epoch_probe", "trace_supervisor", "trace_process_tree", "external_inventory"):
            self.assertNotRegex(script, r'python3? -I[^\n]*"\$selection_dir/' + helper + r'\.py"', helper)
        self.assertIn("from epoch_probe import main; sys.exit(main())", script)

    @unittest.skipUnless(BASH, "bash is not on PATH; capture guard requires bash")
    def test_process_capture_guard(self):
        script = CI_PATH.read_text(encoding="utf-8")
        block = script.split("# LEAF_PROCESS_CAPTURE_BEGIN\n", 1)[1].split(
            "# LEAF_PROCESS_CAPTURE_END", 1)[0]
        cases = [
            ({}, None),
            ({"LEAF_PROCESS_CAPTURE": "0"}, None),
            ({"LEAF_PROCESS_CAPTURE": "1", "CODEBUILD_WEBHOOK_EVENT": "PUSH"}, "non_manual_event"),
            ({"LEAF_PROCESS_CAPTURE": "1", "CODEBUILD_WEBHOOK_EVENT": "PULL_REQUEST_UPDATED"},
             "non_manual_event"),
            ({"LEAF_PROCESS_CAPTURE": "1", "CODEBUILD_INITIATOR": "states/leaf-forge-ci-relay"},
             "non_manual_event"),
            ({"LEAF_PROCESS_CAPTURE": "1", "FORGE_CI_OPERATION": "push"}, "non_manual_event"),
            ({"LEAF_PROCESS_CAPTURE": "1", "CODEBUILD_WEBHOOK_HEAD_REF": "refs/pull/7/head"},
             "non_manual_event"),
            ({"LEAF_PROCESS_CAPTURE": "1", "CODEBUILD_SOURCE_VERSION": "pr/7"}, "non_manual_event"),
            ({"LEAF_PROCESS_CAPTURE": "1", "CI_SHARD_TOTAL": "3"}, "sharded_build"),
            ({"LEAF_PROCESS_CAPTURE": "1"}, "strace_unavailable"),
            ({"LEAF_PROCESS_CAPTURE": "1", "CI_SHARD_TOTAL": "1"}, "strace_unavailable"),
        ]
        for overrides, reason in cases:
            with self.subTest(overrides=overrides):
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith(("LEAF_", "FORGE_", "CODEBUILD_", "CI_SHARD_"))}
                env.update(overrides)
                env.update(LEAF_PROCESS_CAPTURE_CONTEXT="ambient-context",
                           LEAF_PROCESS_CAPTURE_OUT="ambient-output", LEAF_PROOF_TRACING="1")
                # No apt or tracer is ever launched by these snippet tests.
                command = ('set -euo pipefail\n'
                           'command() { if [[ "$*" == "-v strace" ]]; then return 1; '
                           'else builtin command "$@"; fi; }\n'
                           'timeout() { printf "APT %s\\n" "$*" >&2; return 1; }\n'
                           + block + '\n'
                           'printf "ENV %s %s %s\\n" "${LEAF_PROCESS_CAPTURE_CONTEXT:-unset}" '
                           '"${LEAF_PROCESS_CAPTURE_OUT:-unset}" "$LEAF_PROOF_TRACING"\n')
                result = subprocess.run([BASH, "-c", command], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if reason is None:
                    self.assertEqual(result.stdout, "ENV ambient-context ambient-output 1\n")
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertEqual(result.stdout, f"LEAF_CAPTURE refused {reason}\nENV unset unset 1\n")
                    self.assertEqual("APT" in result.stderr, reason == "strace_unavailable")
                    if reason == "strace_unavailable":
                        self.assertIn("120 apt-get update", result.stderr)

    def test_device_capture_archive_contract(self):
        script = CI_PATH.read_text(encoding="utf-8")
        packaging = script.split('  publish_readsets() {\n', 1)[1]
        guard = ('if [[ "${process_capture_active:-0}" == 1 && '
                 '"${CODEBUILD_BUILD_ID:-}" =~ ^device:dv-[0-9a-f]{16}$ ]]; then')
        start = packaging.index(guard)
        end = packaging.index('    local archive_cap=', start)
        block = packaging[start:end]
        self.assertLess(packaging.index('readsets_sha256="${digest%% *}"'), start)
        self.assertLess(end, packaging.index('aws s3api put-object'))
        self.assertLess(packaging.index('key="mq/leaf-web-demo/selection/'),
                        packaging.index('aws s3api put-object'))
        self.assertIn('cp "$archive" /tmp/process-capture/readsets.tar.gz', block)
        self.assertIn('python -I -B - /tmp/process-capture/readsets.json "$CODEBUILD_BUILD_ID" '
                      '"$HEAD_SHA"', block)
        self.assertIn('"$readsets_bytes" "$readsets_sha256" "$readsets_archive_members"', block)
        for field in ('"schema": "leaf.ci.device-capture-archive.v1"',
                      '"build_id": sys.argv[2]', '"head_sha": sys.argv[3]',
                      '"bytes": int(sys.argv[4])', '"sha256": sys.argv[5]',
                      '"members": sys.argv[6].split()'):
            self.assertIn(field, block)
        self.assertIn('LEAF_CAPTURE device_archive $readsets_bytes $readsets_sha256', block)
        self.assertIn('LEAF_CAPTURE device_archive_copy_failed', block)
        self.assertNotRegex(block, r'\b(?:exit|return)\s')
        self.assertNotRegex(block, r'\bgate_status=')

    @unittest.skipUnless(BASH, "bash is not on PATH; device archive requires bash")
    def test_device_capture_archive_before_s3_and_fail_open(self):
        script = CI_PATH.read_text(encoding="utf-8")
        start = script.index('    readsets_error="archive measurement failed"\n')
        stop = script.index('    if [[ "${process_capture_active:-0}" == 1 && '
                            '${#diagnostic_members[@]} -gt 0 ]]; then', start)
        block = script[start:stop]
        archive_bytes = b"fake packed readsets archive\n"
        digest = hashlib.sha256(archive_bytes).hexdigest()
        cases = [("device:dv-0123456789abcdef", "1", "", True),
                 ("leaf-ci:0123456789abcdef", "1", "", False),
                 ("device:dv-0123456789abcdef", "0", "", False),
                 ("device:dv-0123456789abcdeF", "1", "", False),
                 ("device:dv-0123456789abcde", "1", "", False),
                 ("device:dv-0123456789abcdef0", "1", "", False),
                 ("device:dv-0123456789abcdef", "1", "mkdir", False),
                 ("device:dv-0123456789abcdef", "1", "copy", False),
                 ("device:dv-0123456789abcdef", "1", "receipt", False)]
        for build_id, active, failure, copied in cases:
            with self.subTest(build_id=build_id, active=active, failure=failure):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    archive = directory / "readsets.tar.gz"
                    archive.write_bytes(archive_bytes)
                    capture = directory / "process-capture"
                    env = {key: value for key, value in os.environ.items() if key != "BASH_ENV"}
                    env.update(MSYS_NO_PATHCONV="1", MSYS2_ARG_CONV_EXCL="*",
                               TEST_PYTHON=sys.executable.replace("\\", "/"),
                               TEST_ARCHIVE=archive.as_posix(), TEST_CAPTURE_DIR=capture.as_posix(),
                               TEST_FAILURE=failure, TEST_EXPECT_COPY=str(int(copied)),
                               CODEBUILD_BUILD_ID=build_id, process_capture_active=active,
                               HEAD_SHA="a" * 40, TRUSTED_SHA="b" * 40, trusted_sha_override="0")
                    # Redirect only the fixed destination; execute the real measurement,
                    # device guard, copy, receipt, cap and S3 publication sequence.
                    redirected = block.replace("/tmp/process-capture", shlex.quote(capture.as_posix()))
                    command = '''set -euo pipefail
python() {
  [[ "$TEST_FAILURE" != receipt ]] || return 1
  command "$TEST_PYTHON" "$@"
}
mkdir() {
  [[ "$TEST_FAILURE" != mkdir ]] || return 1
  command mkdir "$@"
}
cp() {
  [[ "$TEST_FAILURE" != copy ]] || return 1
  command cp "$@"
}
aws() {
  [[ "$1 $2" == 's3api put-object' ]] || return 98
  if [[ "$TEST_EXPECT_COPY" == 1 ]]; then
    [[ -f "$TEST_CAPTURE_DIR/readsets.tar.gz" && -f "$TEST_CAPTURE_DIR/readsets.json" ]] || return 99
  else
    [[ ! -f "$TEST_CAPTURE_DIR/readsets.json" ]] || return 99
  fi
}
publish_readsets() {
  local archive="$TEST_ARCHIVE" digest build_uuid key
  readsets_archive_members=$'readsets  full-run.json\\tcapture/context.json '
''' + redirected + '''
}
publish_readsets
printf 'PUBLISHED %s\\n' "$readsets_status"
'''
                    result = subprocess.run([BASH, "-c", command], env=env,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("PUBLISHED uploaded\n", result.stdout)
                    self.assertEqual("LEAF_CAPTURE device_archive_copy_failed" in result.stdout,
                                     bool(failure))
                    if copied:
                        self.assertEqual((capture / "readsets.tar.gz").read_bytes(), archive_bytes)
                        self.assertEqual(json.loads((capture / "readsets.json").read_text(encoding="utf-8")),
                                         {"schema": "leaf.ci.device-capture-archive.v1",
                                          "build_id": build_id, "head_sha": "a" * 40,
                                          "bytes": len(archive_bytes), "sha256": digest,
                                          "members": ["readsets", "full-run.json", "capture/context.json"]})
                        self.assertIn(f"LEAF_CAPTURE device_archive {len(archive_bytes)} {digest}\n",
                                      result.stdout)
                    elif not failure:
                        self.assertFalse(capture.exists())
                        self.assertNotIn("LEAF_CAPTURE device_archive", result.stdout)

    def test_forge_relay_event_manifest(self):
        script = CI_PATH.read_text(encoding="utf-8")
        block = script.split("<<'LEAF_SELECTION_INPUTS'\n", 1)[1].split(
            "\nLEAF_SELECTION_INPUTS", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            env = {"CODEBUILD_INITIATOR": "states/leaf-forge-ci-relay",
                   "CODEBUILD_WEBHOOK_EVENT": "PULL_REQUEST_UPDATED",
                   "CODEBUILD_WEBHOOK_HEAD_REF": "refs/pull/7/head",
                   "CODEBUILD_WEBHOOK_BASE_REF": "refs/heads/main",
                   "FORGE_CI_OPERATION": "ci-fixture", "HEAD_SHA": "a" * 40,
                   "TRUSTED_SHA": "b" * 40, "CODEBUILD_BUILD_ID": "fixture-build",
                   "LEAF_RELAY_MANIFEST": str(manifest_path)}
            manifest = {"schema": "leaf.forge-ci-relay.snapshot.v1",
                        "repository": "LEAF-Solar-Design/leaf-web-demo",
                        "head_sha": env["HEAD_SHA"], "base_sha": env["TRUSTED_SHA"],
                        "base_ref": env["CODEBUILD_WEBHOOK_BASE_REF"],
                        "operation": env["FORGE_CI_OPERATION"], "bundle_sha256": "c" * 64}
            for status, head in (("verified", "a" * 40), ("mismatch:head_sha", "d" * 40)):
                with self.subTest(status=status):
                    manifest["head_sha"] = head
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    (root / "catalog.json").write_text(json.dumps({
                        "schema": "leaf.ci.catalog.v1", "suites": [{"id": "sample"}],
                        "catalog_sha256": "e" * 64}), encoding="utf-8")
                    with patch.dict(os.environ, env, clear=True), \
                         patch.object(sys, "argv", ["inputs", str(root), "f" * 40]):
                        exec(compile(block, "selection-inputs", "exec"), {})
                    event = json.loads((root / "event.json").read_text(encoding="utf-8"))
                    self.assertEqual(event["provider"], "forge")
                    self.assertEqual(event["pr_number"], 7)
                    self.assertEqual(event["source_version"], "")
                    self.assertEqual(event["head_sha"], env["HEAD_SHA"])
                    self.assertEqual(event["target_sha"], env["TRUSTED_SHA"])
                    self.assertEqual(event["relay_manifest"], status)
                    self.assertIs(event["provider_bound"], status == "verified")
                    self.assertEqual(event["evidence_ref"]["manifest_sha256"],
                                     hashlib.sha256(manifest_path.read_bytes()).hexdigest())

    def test_forge_device_event(self):
        script = CI_PATH.read_text(encoding="utf-8")
        block = script.split("<<'LEAF_SELECTION_INPUTS'\n", 1)[1].split(
            "\nLEAF_SELECTION_INPUTS", 1)[0]
        operation = lambda kind, number, head: "ci-" + hashlib.sha256(
            ("LEAF-Solar-Design/leaf-web-demo|" + kind + "|" + str(number) + "|" + head)
            .encode("utf-8")).hexdigest()[:40]
        env = {"CODEBUILD_BUILD_ID": "device:dv-0123456789abcdef",
               "CODEBUILD_RESOLVED_SOURCE_VERSION": "a" * 40,
               "CODEBUILD_WEBHOOK_EVENT": "PULL_REQUEST_UPDATED",
               "CODEBUILD_WEBHOOK_HEAD_REF": "refs/pull/7/head",
               "CODEBUILD_WEBHOOK_BASE_REF": "refs/heads/main",
               "HEAD_SHA": "a" * 40, "TRUSTED_SHA": "b" * 40,
               "LEAF_CI_BASE_SHA": "b" * 40, "LEAF_CI_BASE_REF": "main",
               "FORGE_CI_HEAD_BRANCH": "feature/device",
               "FORGE_CI_OPERATION": operation("pull_request", 7, "a" * 40)}
        cases = [({}, "forge-device"),
                 ({"CODEBUILD_WEBHOOK_EVENT": "PULL_REQUEST_CREATED"}, "forge-device"),
                 ({"CODEBUILD_WEBHOOK_EVENT": "PULL_REQUEST_REOPENED"}, "forge-device"),
                 ({"FORGE_CI_OPERATION": operation("pull_request", 8, "a" * 40)}, "github"),
                 ({"FORGE_CI_OPERATION": operation("pull_request", 7, "c" * 40)}, "github"),
                 ({"CODEBUILD_BUILD_ID": "device:dv-0123456789abcde"}, "github"),
                 ({"CODEBUILD_BUILD_ID": "device:dv-0123456789abcdeg"}, "github"),
                 ({"CODEBUILD_WEBHOOK_HEAD_REF": "refs/pull/0/head"}, "github"),
                 ({"CODEBUILD_WEBHOOK_HEAD_REF": "refs/heads/feature"}, "github"),
                 ({"CODEBUILD_WEBHOOK_BASE_REF": "refs/heads/../main"}, "github"),
                 ({"CODEBUILD_WEBHOOK_BASE_REF": "main"}, "github"),
                 ({"HEAD_SHA": "bad"}, "github"),
                 ({"TRUSTED_SHA": "bad"}, "github"),
                 ({"CODEBUILD_RESOLVED_SOURCE_VERSION": "c" * 40}, "github"),
                 ({"CODEBUILD_WEBHOOK_EVENT": "PUSH",
                   "CODEBUILD_WEBHOOK_HEAD_REF": "refs/heads/main",
                   "FORGE_CI_OPERATION": operation("push", 0, "a" * 40)}, "github")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for changes, provider in cases:
                with self.subTest(changes=changes):
                    (root / "catalog.json").write_text(json.dumps({
                        "schema": "leaf.ci.catalog.v1", "suites": [{"id": "sample"}],
                        "catalog_sha256": "e" * 64}), encoding="utf-8")
                    current = dict(env, **changes)
                    with patch.dict(os.environ, current, clear=True), \
                         patch.object(sys, "argv", ["inputs", str(root), "f" * 40]):
                        exec(compile(block, "selection-inputs", "exec"), {})
                    event = json.loads((root / "event.json").read_text(encoding="utf-8"))
                    self.assertEqual(event["provider"], provider)
                    self.assertEqual(event["event"], current["CODEBUILD_WEBHOOK_EVENT"])
                    if provider == "forge-device":
                        self.assertEqual(event["initiator"], "leaf-ci-device")
                        self.assertIs(event["provider_bound"], True)
                        self.assertEqual(event["build_id"], env["CODEBUILD_BUILD_ID"])
                        self.assertEqual(event["pr_number"], 7)
                        self.assertEqual(event["source_version"], "")
                        self.assertEqual(event["head_sha"], env["HEAD_SHA"])
                        self.assertEqual(event["target_sha"], env["TRUSTED_SHA"])
                        self.assertEqual(event["head_ref"], env["CODEBUILD_WEBHOOK_HEAD_REF"])
                        self.assertEqual(event["target_ref"], env["CODEBUILD_WEBHOOK_BASE_REF"])
                        self.assertEqual(event["evidence_ref"], {
                            "build": env["CODEBUILD_BUILD_ID"], "operation": env["FORGE_CI_OPERATION"]})
                    else:
                        self.assertEqual(event["initiator"], "")

    def test_relay_receipt_event_classifier(self):
        script = CI_PATH.read_text(encoding="utf-8")
        block = script.split("<<'LEAF_SELECTION_RECEIPTS'\n", 1)[1].split(
            "\nLEAF_SELECTION_RECEIPTS", 1)[0]
        classifier = block[block.index('source = e.get("CODEBUILD_SOURCE_VERSION"'):block.index('\nids =')]
        for raw, ref, expected in (("PULL_REQUEST_UPDATED", "refs/pull/7/head", "pr"),
                                   ("PUSH", "refs/heads/main", "main"),
                                   ("PULL_REQUEST_UPDATED", "refs/pull/0/head", "unknown")):
            with self.subTest(raw=raw, ref=ref):
                namespace = {"e": {"CODEBUILD_INITIATOR": "states/leaf-forge-ci-relay",
                                    "CODEBUILD_WEBHOOK_EVENT": raw,
                                    "CODEBUILD_WEBHOOK_HEAD_REF": ref}, "re": re}
                exec(compile(classifier, "receipt-classifier", "exec"), namespace)
                self.assertEqual(namespace["event"], expected)

        for raw, ref, expected in (("PULL_REQUEST_UPDATED", "refs/pull/7/head", "pr"),
                                   ("PUSH", "refs/heads/main", "main"),
                                   ("PULL_REQUEST_UPDATED", "refs/pull/0/head", "unknown")):
            with self.subTest(device=True, raw=raw, ref=ref):
                namespace = {"e": {"CODEBUILD_BUILD_ID": "device:dv-0123456789abcdef",
                                    "CODEBUILD_WEBHOOK_EVENT": raw,
                                    "CODEBUILD_WEBHOOK_HEAD_REF": ref}, "re": re}
                exec(compile(classifier, "receipt-classifier", "exec"), namespace)
                self.assertEqual(namespace["event"], expected)

    def test_gate_fails_fast_with_unbuffered_output(self):
        script = CI_PATH.read_text(encoding="utf-8")
        call = next(line for line in script.splitlines()
                    if line.startswith("python scripts/run-all-gates.py "))
        self.assertIn('"${gate_fail_fast_args[@]}"', call)
        self.assertIn('"${gate_tier_args[@]}"', call)
        self.assertNotIn("--fail-fast", call)
        self.assertIn('export PYTHONUNBUFFERED=1\n' + call, script)
        self.assertLess(script.index("gate_fail_fast_args=(--fail-fast)"),
                        script.index('\nunset PYTHONSAFEPATH\n'))

    @unittest.skipUnless(BASH, "bash is not on PATH; capture fail-fast execution requires bash")
    def test_capture_runs_omit_fail_fast_and_keep_failure_status(self):
        script = CI_PATH.read_text(encoding="utf-8")
        start = script.index("gate_fail_fast_args=(--fail-fast)")
        call = next(line for line in script.splitlines()
                    if line.startswith("python scripts/run-all-gates.py "))
        block = script[start:script.index(call, start) + len(call)]
        self.assertIn('\nunset PYTHONSAFEPATH\n', block)
        self.assertIn('export PYTHONUNBUFFERED=1\n' + call, block)
        command = '''set -euo pipefail
gate_status=0
only_args=()
python() {
  printf '%s\\n' "$@" > "$TEST_ARGS"
  return "$TEST_RUNNER_STATUS"
}
''' + block + '''
printf '%s\\n' "$gate_status"
'''
        for active, event in ((None, None), ("0", "PUSH"), ("1", "PUSH"),
                              (None, "PULL_REQUEST_CREATED"), ("0", "PULL_REQUEST_UPDATED"),
                              ("1", "PULL_REQUEST_REOPENED"), (None, "PULL_REQUEST_MERGED")):
            for status in (0, 1):
                with self.subTest(process_capture_active=active, event=event, status=status):
                    with tempfile.TemporaryDirectory() as temporary:
                        args_path = Path(temporary) / "args"
                        env = {key: value for key, value in os.environ.items()
                               if key not in ("BASH_ENV", "process_capture_active", "LEAF_GATE_JOBS",
                                              "CODEBUILD_WEBHOOK_EVENT")}
                        env.update(TEST_ARGS=args_path.as_posix(), TEST_RUNNER_STATUS=str(status),
                                   LEAF_CI_SCRIPT_PATH="/trusted/ci.sh",
                                   pr_tier_active="1" if event in (
                                       "PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED") else "0")
                        if active is not None:
                            env["process_capture_active"] = active
                        if event is not None:
                            env["CODEBUILD_WEBHOOK_EVENT"] = event
                        result = subprocess.run([BASH, "-c", command], env=env,
                                                capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        args = args_path.read_text(encoding="utf-8").splitlines()
                        expected = ["scripts/run-all-gates.py", "--jobs", "auto", "--retry", "0",
                                    "--early-feedback"]
                        if active != "1":
                            expected.append("--fail-fast")
                        expected.extend(["--result-json", "/tmp/gate-results/gate-result.json",
                                         "--log-dir", "/tmp/gate-logs"])
                        if event in ("PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED"):
                            expected.extend(["--tier-file", "scripts/ci/pr-tier.json"])
                        expected.extend(["--before-heavy-command", "bash", "/trusted/ci.sh",
                                         "--heavy-prerequisites"])
                        self.assertEqual(args, expected)
                        self.assertEqual(result.stdout.strip(), str(status))

    def test_workflow_commands(self):
        """Pin CodeBuild's deliberate --with-deps deviation from the workflow."""
        script = CI_PATH.read_text(encoding="utf-8")
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))
        commands = [line.strip() for line in script.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(commands[0], "set -euo pipefail")
        self.assertLess(script.index("CODEBUILD_WEBHOOK_BASE_REF:-"),
                        script.index("##refs/heads/"))
        self.assertNotIn("git fetch", script)
        # The diagnostic branch has its own executable contract; keep the
        # original required-control invocation pinned independently.
        ordinary_script = re.sub(r"# LEAF_DIAGNOSTIC_BEGIN\n.*?# LEAF_DIAGNOSTIC_END\n",
                                 "", script, flags=re.DOTALL)
        invocations = re.findall(
            r"\bpython(?: -u)? scripts/run-all-gates\.py\b(?:(?!\bpython(?: -u)? scripts/run-all-gates\.py\b)[^\n])*",
            re.sub(r"\\\n\s*", " ", ordinary_script))
        gate_runs = [call for call in invocations
                     if "--verify-shard-results" not in call and "--verify-gate-proof" not in call]
        self.assertEqual(gate_runs, [invocations[0]])
        for flag in ("--retry 0", "--early-feedback", '"${gate_fail_fast_args[@]}"', "--result-json", "--log-dir"):
            self.assertIn(flag, invocations[0])
        self.assertIn("gate_fail_fast_args=(--fail-fast)", script)
        self.assertNotIn("--retry 1", gate_runs[0])
        self.assertIn('LEAF_CI_SCRIPT_PATH="$(realpath "${BASH_SOURCE[0]}")"', script)
        self.assertIn('--before-heavy-command bash "$LEAF_CI_SCRIPT_PATH" --heavy-prerequisites',
                      gate_runs[0])
        shard_start = script.index(
            'if [[ "${CI_SHARD_MODE:-all}" == shard && ${#only_args[@]} == 0 ]]; then\n')
        shard_end = script.index('\nfi\n', shard_start)
        for flag in ("--shard-count", "--shard-index"):
            self.assertEqual(script.count(flag), 1)
            self.assertLess(shard_start, script.index(flag))
            self.assertLess(script.index(flag), shard_end)
        proof = script.split("# LEAF_GATE_PROOF_BEGIN\n", 1)[1].split(
            "# LEAF_GATE_PROOF_END", 1)[0]
        self.assertEqual(script.count("--verify-shard-results"), 1)
        result_path = re.search(r"--result-json (\S+)", gate_runs[0]).group(1)
        results_dir = re.search(r"--verify-shard-results (\S+)", proof).group(1)
        self.assertEqual(result_path.rsplit("/", 1)[0], results_dir)

        workflow = (ROOT / ".github/workflows/test-gate.yml").read_text(encoding="utf-8-sig")
        requirements = re.findall(r"^\s*-r\s+(\S+)", workflow, re.MULTILINE)
        self.assertTrue(requirements, "No workflow requirement lines found")
        installed = re.findall(r"^\s*-r\s+(\S+)", script, re.MULTILINE)
        self.assertEqual(installed, requirements)
        self.assertEqual(script.splitlines().count("install_ci_browser npx playwright"), 1)
        self.assertEqual(script.splitlines().count("install_ci_browser python -m playwright"), 1)
        self.assertRegex(workflow, r"(?m)^[ \t]*(?:run:[ \t]*)?npx playwright install chromium\b[ \t]*$")
        self.assertIn("check_license_fence.py --self-test", script)
        self.assertIn("check_license_fence.py .", script)

        contract = (ROOT / ".github/workflows/contract.yml").read_text(encoding="utf-8-sig")
        paths = re.findall(r"^\s*(tests/[^\s]+\.py)\s*$", contract, re.MULTILINE)
        self.assertTrue(paths, "No contract test paths found")
        for path in paths:
            self.assertIn(path, script)
        self.assertIn("PYTHONSAFEPATH=1 python -m pytest -q", script)
        self.assertIn("export LEAF_AUTOFILL_SOLVER_ABSENT_OK=1", script)
        self.assertLess(script.index("=== job contract ==="), script.index("=== job license-fence ==="))
        self.assertLess(script.index("=== job license-fence ==="), script.index("=== job test-gate ==="))

    def test_shard_result_reuse_evidence_is_immutable_and_advisory(self):
        script = CI_PATH.read_text(encoding="utf-8")
        key = '--key "mq/leaf-web-demo/selection/${CODEBUILD_BUILD_ID}.result.json"'
        self.assertIn(key, script)
        gate_end = script.index('echo "LEAF_T end gate $(date +%s%3N) rc=$gate_status"')
        missing_result_inspection = script.index(
            'if [[ ! -f /tmp/gate-results/gate-result.json ]]; then', gate_end)
        block_start = script.index(
            'if [[ -n "${CODEBUILD_BUILD_ID:-}" && -f /tmp/gate-results/gate-result.json ]]; then')
        block_end = script.index('\nfi\n', block_start)
        self.assertLess(gate_end, block_start)
        self.assertLess(block_end, missing_result_inspection)
        block = script[block_start:block_end]
        put_object = block[block.index('aws s3api put-object'):].strip()
        self.assertIn(key, put_object)
        self.assertIn("--if-none-match '*'", put_object)
        self.assertTrue(put_object.endswith(
            "|| echo 'WARNING: result_write_failed; reuse evidence absent for this build' >&2"))
        self.assertNotRegex(block, r"\bexit\b")
        self.assertNotRegex(block, r"\bgate_status\s*=")

    def test_main_push_donor_pointer_is_gated_immutable_and_advisory(self):
        script = CI_PATH.read_text(encoding="utf-8")
        key = ('--key "mq/leaf-web-demo/selection/main/${HEAD_SHA}/'
               'shard-${CI_SHARD_INDEX_N}-of-${CI_SHARD_TOTAL_N}.json"')
        self.assertIn(key, script)
        gate_end = script.index('echo "LEAF_T end gate $(date +%s%3N) rc=$gate_status"')
        missing_result_inspection = script.index(
            'if [[ ! -f /tmp/gate-results/gate-result.json ]]; then', gate_end)
        block_start = script.index('# Reuse donor pointer (plan ci-test-selection-20260925 S1b)')
        block_end = script.index('\nfi\n', block_start)
        self.assertLess(script.index(
            '--key "mq/leaf-web-demo/selection/${CODEBUILD_BUILD_ID}.result.json"'),
            block_start)
        self.assertLess(gate_end, block_start)
        self.assertLess(block_end, missing_result_inspection)
        block = script[block_start:block_end]
        condition = block[block.index('if [['):block.index(']]; then')]
        for guard in ('refs/heads/main', '== PUSH', '"$selection_mode" == full',
                      '"$trusted_sha_override" != 1', '"$gate_status" == 0',
                      '"$postgres_status" == 0', '=~ ^[0-9a-f]{40}$'):
            self.assertIn(guard, condition)
        put_object = block[block.index('aws s3api put-object'):].strip()
        self.assertIn(key, put_object)
        self.assertIn("--if-none-match '*'", put_object)
        self.assertTrue(put_object.endswith(
            "|| echo 'WARNING: pointer_write_failed; this shard is not a reuse donor' >&2"))
        self.assertIn('"schema":"leaf.ci.main-shard-pointer.v1"',
                      block[block.index('printf '):block.index('aws s3api put-object')])
        self.assertNotRegex(block, r"\bexit\b")
        self.assertNotRegex(block, r"\bgate_status\s*=")

    def test_autofill_solver_fetch_is_pinned_and_fail_open(self):
        script = CI_PATH.read_text(encoding="utf-8")
        block = script.split("# LEAF_AUTOFILL_SOLVER_BEGIN\n", 1)[1].split(
            "# LEAF_AUTOFILL_SOLVER_END", 1)[0]
        self.assertLess(script.index("# LEAF_AUTOFILL_SOLVER_END"),
                        script.index("python scripts/run-all-gates.py"))
        self.assertIn("deploy/autofill-solver-sources.json", block)
        self.assertIn('prefix="forge-ci-relay/LEAF-Solar-Design/autofill-solver/'
                      '${autofill_solver_revision}/"', block)
        self.assertIn('aws s3api list-objects-v2 --bucket "$bucket" --prefix "$prefix"', block)
        self.assertIn('manifest.get("head_sha") != revision', block)
        self.assertIn('"LEAF-Solar-Design/autofill-solver"', block)
        self.assertIn('git -C /tmp/autofill-solver checkout -q --detach "$autofill_solver_revision"',
                      block)
        self.assertIn('[[ "$(timeout 5 git -C /tmp/autofill-solver rev-parse HEAD)" == '
                      '"$autofill_solver_revision" ]]', block)
        # Every network, git and python call is bounded by timeout.
        for line in block.splitlines():
            if re.search(r"\b(?:aws|git|python3)\s", line) and "timeout" not in line \
                    and not line.lstrip().startswith(("#", "echo")):
                self.fail(f"unbounded command in solver fetch: {line.strip()}")
        self.assertNotIn("unzip", block)
        self.assertNotIn("set -e", block)
        self.assertNotRegex(block, r"\bexit\s")
        # AUTOFILL_SOLVER_ROOT is exported only on the success path; the
        # fallback keeps the previous ABSENT_OK behavior.
        branch = block.split("if fetch_autofill_solver; then\n", 1)[1]
        success, fallback = branch.split("\nelse\n", 1)
        self.assertEqual(script.count("AUTOFILL_SOLVER_ROOT="), 1)
        self.assertIn("export AUTOFILL_SOLVER_ROOT=/tmp/autofill-solver "
                      'AUTOFILL_SOLVER_REVISION="$autofill_solver_revision"', success)
        self.assertIn("autofill-solver: pinned", success)
        self.assertNotIn("LEAF_AUTOFILL_SOLVER_ABSENT_OK", success)
        self.assertIn("export LEAF_AUTOFILL_SOLVER_ABSENT_OK=1", fallback)
        self.assertNotIn("AUTOFILL_SOLVER_ROOT", fallback)
        self.assertEqual(script.count("export LEAF_AUTOFILL_SOLVER_ABSENT_OK=1"), 1)

    def test_gate_proof_receipt_guard(self):
        script = CI_PATH.read_text(encoding="utf-8")
        guard = script.split("<<'LEAF_GATE_PROOF_ELIGIBLE'\n", 1)[1].split(
            "\nLEAF_GATE_PROOF_ELIGIBLE", 1)[0]
        self.assertIn('python -I -B - "$selection_dir/detail.json"', script)
        self.assertIn('(out / "detail.json").write_text(canonical(detail)', script)
        self.assertLess(script.rindex('(out / "detail.json").write_text'),
                        script.index('# LEAF_GATE_PROOF_BEGIN'))
        base = dict(schema="leaf.ci.selection.v1", execution_mode="full",
                    selection_mode="full", phase="enforce", trusted_sha_override=False,
                    apply_filter=False)
        cases = [
            ("full", {}, 0),
            ("sel", dict(execution_mode="selected", apply_filter=True), 1),
            ("tier", dict(execution_mode="selected", selection_mode="selected", selection_source="pr-tier"), 1),
            ("shadow", dict(selection_mode="shadow"), 0),
            ("phase", dict(phase="shadow"), 0),
            ("filter", dict(apply_filter=True), 1),
            ("nofilter", dict(apply_filter=None), 1),
            ("override", dict(trusted_sha_override=True), 1),
            ("missing", dict(trusted_sha_override=None), 1),
            ("mode", dict(execution_mode=None), 1),
            ("schema", dict(schema="other"), 1),
        ]
        for name, changes, expected in cases:
            with self.subTest(name=name):
                # Environment claims must not override receipt eligibility.
                with patch.dict(os.environ, LEAF_SELECTION_EXECUTION_MODE="full",
                                trusted_sha_override="0", LEAF_PROOF_TRUSTED_SHA=""), \
                     patch("sys.argv", ["guard", "receipt.json"]), \
                     patch("builtins.open", mock_open(read_data=json.dumps(dict(base, **changes)))), \
                     self.assertRaises(SystemExit) as stopped:
                    exec(compile(guard, "receipt-guard", "exec"), {})
                self.assertEqual(stopped.exception.code, expected)
        self.assertNotIn("environ", guard)

    def test_gate_proof_publication_contract(self):
        script = CI_PATH.read_text(encoding="utf-8")
        proof = script.split("# LEAF_GATE_PROOF_BEGIN\n", 1)[1].split(
            "# LEAF_GATE_PROOF_END", 1)[0]
        self.assertIn('if [[ "$gate_status" == 0 ]] && python', proof)
        self.assertIn("git rev-parse 'HEAD^{tree}'", proof)
        self.assertIn('--emit-proof "$gate_proof"', proof)
        self.assertIn('--verify-gate-proof "$gate_proof" --expect-tree "$gate_tree"; then', proof)
        self.assertLess(proof.index("--emit-proof"), proof.index("--verify-gate-proof"))
        self.assertLess(proof.index("--verify-gate-proof"), proof.index("aws s3api put-object"))
        self.assertIn('--bucket leaf-mq-transport-807034087062-us-east-1', proof)
        self.assertIn('--key "mq/leaf-web-demo/selection/gate-proof/${gate_tree}.json"', proof)
        self.assertIn('--body "$gate_proof"', proof)
        self.assertIn("--if-none-match '*' --checksum-algorithm SHA256", proof)
        self.assertIn('--metadata "producer-build-id=${CODEBUILD_BUILD_ID},'
                      'producer-project=leaf-ci-leaf-web-demo,tree=${gate_tree}"', proof)
        self.assertIn('if gate_tree=', proof)
        self.assertIn('&& python scripts/run-all-gates.py --verify-shard-results', proof)
        self.assertIn('&& python scripts/run-all-gates.py --verify-gate-proof', proof)
        self.assertIn('if aws s3api put-object', proof)
        self.assertIn("'412|PreconditionFailed'", proof)
        self.assertIn('gate proof mint or verification failed; build verdict unchanged', proof)
        self.assertIn('gate proof put failed; build verdict unchanged', proof)
        self.assertNotRegex(proof, r"\bgate_status=")
        self.assertNotRegex(proof, r"\b(?:exit|return)\s")
        self.assertTrue(script.endswith('# LEAF_GATE_PROOF_END\nexit "$gate_status"\n'))

    def test_steps_reset_directory(self):
        lines = CI_PATH.read_text(encoding="utf-8").splitlines()
        steps = [i for i, line in enumerate(lines) if line.startswith('echo "--- ')]
        self.assertTrue(steps, "No CI step markers found")
        for i in steps:
            self.assertTrue(any(line.startswith("cd ") for line in lines[max(0, i - 3):i]),
                            f"Step must reset cwd: {lines[i]}")

    def test_capture_context_has_realpath_aliases(self):
        script = CI_PATH.read_text(encoding="utf-8")
        context = script.split("<<'LEAF_CAPTURE_BASE'", 1)[1].split("\n", 1)[1].split(
            "\nLEAF_CAPTURE_BASE\n", 1)[0]
        self.assertIn('roots[str(path)] = os_image\n'
                      '        if os.path.realpath(path) != str(path):\n'
                      '            roots[os.path.realpath(path)] = os_image', context)
        self.assertIn('roots[str(browsers)] = os_image\n'
                      '    if os.path.realpath(browsers) != str(browsers):\n'
                      '        roots[os.path.realpath(browsers)] = os_image', context)

    def test_capture_environment_scope(self):
        script = CI_PATH.read_text(encoding="utf-8")
        start = script.index('if [[ "$tracing_ready" == 1 ]]; then')
        stop = script.index('\nelse\n', start)
        tracing = script[start:stop]
        exports = re.findall(r"(?m)^\s*export LEAF_READSET_\w+=.*$", script)
        self.assertTrue(exports)
        for export in exports:
            self.assertIn(export, tracing)
        self.assertIn('\nfi\nexport PYTHONPATH="$selection_dir"\n', script[:start])
        self.assertEqual(script.count('export PYTHONPATH="$selection_dir"'), 1)
        self.assertNotIn('export PYTHONPATH=', tracing)
        self.assertIn('export LEAF_READSET_DIR=/tmp/gate-logs/readsets', tracing)
        self.assertIn('if [[ "$reporters_ready" == 1 ]]; then\n'
                      '  export LEAF_TRUSTED_CI_DIR="$selection_dir"\n'
                      'else\n  unset LEAF_TRUSTED_CI_DIR\nfi', script[:start])
        skipped = script[stop:script.index('\ngate_status=0', stop)]
        self.assertNotIn('unset PYTHONPATH', script)
        self.assertIn('unset "${!LEAF_READSET_@}"', skipped)
        self.assertIn('INFO: read-set tracing skipped (not a tracing build)', skipped)
        self.assertIn('WARNING: trusted capture unavailable;', skipped)
        self.assertRegex(script, r'unset PYTHONSAFEPATH\n(?:#[^\n]*\n)*'
                                 r'export PYTHONUNBUFFERED=1\npython(?: -u)? scripts/run-all-gates\.py ')
        self.assertIn('tracing_active=sys.argv[2] == "1", reporters_active=sys.argv[3] == "1"', script)
        self.assertIn('"tracing_active": detail["tracing_active"]', script)
        self.assertIn('"reporters_active": detail["reporters_active"]', script)

    @unittest.skipUnless(BASH, "bash is not on PATH; tracing event check requires bash")
    def test_tracing_build_classes(self):
        script = CI_PATH.read_text(encoding="utf-8")
        start = script.index('tracing_ready=0\n')
        eligibility = script[start:script.index('\nexport TRUSTED_SHA HEAD_SHA', start)]
        self.assertNotIn('CODEBUILD_WEBHOOK_EVENT', eligibility)
        self.assertNotIn('CODEBUILD_WEBHOOK_HEAD_REF', eligibility)
        self.assertIn('${LEAF_PROOF_TRACING:-}', eligibility)
        cases = [
            ("PUSH", "refs/heads/main", "", "1", "0"),
            ("PUSH", "refs/heads/gh-readonly-queue/main/pr-1", "", "1", "0"),
            ("PUSH", "refs/heads/main-extra", "", "1", "0"),
            ("PULL_REQUEST_UPDATED", "refs/heads/main", "", "1", "0"),
            ("", "", "", "1", "0"),
            ("", "", "1", "1", "1"),
            ("", "", "0", "1", "0"),
            ("", "", "2", "1", "0"),
            ("PUSH", "refs/heads/main", "", "0", "0"),
            ("", "", "1", "0", "0"),
        ]
        for event, ref, proof, helpers, expected in cases:
            with self.subTest(event=event, ref=ref, proof=proof, helpers=helpers):
                command = ('CODEBUILD_WEBHOOK_EVENT="$1"\nCODEBUILD_WEBHOOK_HEAD_REF="$2"\n'
                           'LEAF_PROOF_TRACING="$3"\ntracing_helpers_ready="$4"\n'
                           + eligibility + '\nprintf "%s" "$tracing_ready"\n')
                result = subprocess.run([BASH, "-c", command, "tracing-scope", event, ref, proof, helpers],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    @unittest.skipUnless(BASH, "bash is not on PATH; shell syntax check requires bash")
    def test_bash_syntax(self):
        for path in (".codebuild/ci.sh", "scripts/ci/pr-codebuild-project.sh"):
            result = subprocess.run([BASH, "-n", path], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_proof_trusted_sha_receipt_contract(self):
        script = CI_PATH.read_text(encoding="utf-8")
        self.assertIn('trusted_sha_override=0', script)
        self.assertLess(script.index('TRUSTED_SHA="$HEAD_SHA"'),
                        script.index('show "$TRUSTED_SHA:scripts/ci/select_tests.py"'))
        self.assertIn('export TRUSTED_SHA HEAD_SHA trusted_sha_override loader_check', script)
        receipt = script.split("<<'LEAF_SELECTION_RECEIPTS'\n", 1)[1].split(
            '\nLEAF_SELECTION_RECEIPTS', 1)[0]
        self.assertIn('"trusted_sha_override": e.get("trusted_sha_override") == "1"', receipt)
        self.assertIn('"loader_check": e.get("loader_check")', receipt)
        self.assertIn('trusted_sha_override=receipt["trusted_sha_override"]', receipt)
        self.assertIn('loader_check=receipt["loader_check"]', receipt)
        self.assertIn('print("LEAF_SELECTION " + canonical(detail))', receipt)
        finalize = script.split("<<'LEAF_SELECTION_FINALIZE'", 1)[1]
        self.assertIn('"$reporters_ready" "$trusted_sha_override" "$loader_check" \\\n  "${CODEBUILD_BUILD_ID:-}"', script)
        self.assertIn("<<'LEAF_SELECTION_FINALIZE'", script)
        self.assertIn('trusted_sha_override=sys.argv[5] == "1", loader_check=sys.argv[6]', finalize)
        self.assertIn('print("LEAF_SELECTION_FINAL " + canonical(detail))', finalize)
        self.assertIn('"trusted_sha_override": detail["trusted_sha_override"]', finalize)
        self.assertIn('print("LEAF_SHADOW " + canonical(shadow))', finalize)

    @unittest.skipUnless(BASH, "bash is not on PATH; proof trust check requires bash")
    def test_proof_trusted_sha_build_classes(self):
        script = CI_PATH.read_text(encoding="utf-8")
        start = script.index('  if [[ "${LEAF_PROOF_TRUSTED_SHA+x}" == x ]]; then')
        # Exercise the real override and loader guard without loading helpers or installing dependencies.
        guard = script[start:script.index('  elif git ', start)] + '\n  fi\n'
        warning_start = script.index('if [[ "${LEAF_PROOF_TRUSTED_SHA+x}" == x &&')
        warning = script[warning_start:script.index('\n\n# The emitter', warning_start)]
        main_sha, head_sha = "a" * 40, "b" * 40
        webhook_names = ("CODEBUILD_WEBHOOK_EVENT", "CODEBUILD_WEBHOOK_HEAD_REF",
                         "CODEBUILD_WEBHOOK_TRIGGER")
        cases = [
            ({}, head_sha, head_sha, "1", "override", ""),
            ({}, main_sha, main_sha, "0", "loader_sha_mismatch", "head_sha_mismatch"),
            ({}, "not-a-sha", main_sha, "0", "loader_sha_mismatch", "malformed_sha"),
            ({}, "", main_sha, "0", "loader_sha_mismatch", "malformed_sha"),
            ({}, head_sha.upper(), main_sha, "0", "loader_sha_mismatch", "head_sha_mismatch"),
            ({}, None, main_sha, "0", "loader_sha_mismatch", ""),
        ]
        for name in webhook_names:
            for value in ("present", ""):
                cases.append(({name: value}, head_sha, main_sha, "0",
                              "loader_sha_mismatch", "webhook_build"))
        env = {key: value for key, value in os.environ.items()
               if key not in (*webhook_names, "LEAF_PROOF_TRUSTED_SHA", "LEAF_LOADER_TRUSTED_SHA")}
        for webhook, proof, expected_sha, expected_flag, expected_loader, reason in cases:
            with self.subTest(webhook=webhook, proof=proof):
                case_env = dict(env, **webhook)
                if proof is not None:
                    case_env["LEAF_PROOF_TRUSTED_SHA"] = proof
                # Deliberately disagree with main to prove the loader check still runs unless overridden.
                case_env["LEAF_LOADER_TRUSTED_SHA"] = "c" * 40
                command = ('set -eu\nTRUSTED_SHA="$1"\nHEAD_SHA="$2"\n'
                           'trusted_sha_override=0\nloader_check=not_supplied\n'
                           'proof_override_reason=trusted_load_failed\n'
                           'selection_bootstrap_reason=trusted_load_failed\n'
                           + guard + warning
                           + '\nprintf "%s\\n" "$TRUSTED_SHA" "$trusted_sha_override" "$loader_check"\n')
                result = subprocess.run([BASH, "-c", command, "proof-trust", main_sha, head_sha],
                                        cwd=ROOT, env=case_env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [expected_sha, expected_flag, expected_loader])
                self.assertEqual(result.stderr, f"WARNING: LEAF_PROOF_TRUSTED_SHA ignored ({reason})\n"
                                 if reason else "")


class TestPrCodebuildProject(unittest.TestCase):
    def test_identity_and_no_secret(self):
        script = PROJECT_PATH.read_text(encoding="utf-8")
        self.assertIn("${AWS_PROFILE:?", script)
        self.assertIn('aws --profile "$AWS_PROFILE"', script)
        self.assertIn("sts get-caller-identity", script)
        self.assertIn("arn:aws:sts::${account}:assumed-role/*|arn:aws:iam::${account}:user/*)", script)
        self.assertIn("*) echo 'Refusing unexpected account or root identity' >&2; exit 1 ;;", script)
        self.assertNotIn("secretsmanager", script)
        self.assertNotIn("GH_TOKEN", script)
        self.assertNotIn("aws_cli iam", script)

    @unittest.skipUnless(BASH, "bash is not on PATH; project dry-run requires bash")
    def test_dry_run(self):
        if not _bash_runs_python():
            self.skipTest("bash cannot run python3 (for example only the Windows Store python alias); the project dry-run shells out to python3")
        result = subprocess.run(
            [BASH, "scripts/ci/pr-codebuild-project.sh", "--dry-run"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        document = json.loads(result.stdout)
        project = document["project"]
        self.assertEqual(project["name"], "leaf-ci-leaf-web-demo")
        self.assertEqual(project["serviceRole"], "arn:aws:iam::807034087062:role/leaf-gha-runner-codebuild")
        self.assertEqual(project["timeoutInMinutes"], 60)
        self.assertEqual(project["artifacts"], {"type": "NO_ARTIFACTS"})
        self.assertFalse(project["environment"].get("environmentVariables"))
        self.assertEqual(project["environment"]["image"],
                         "807034087062.dkr.ecr.us-east-1.amazonaws.com/leaf-ci-base:"
                         "web-ad9b9375382ab004bf640594c7a6a22f0258f918")
        self.assertEqual(project["environment"]["computeType"], "BUILD_GENERAL1_XLARGE")
        self.assertEqual(project["environment"]["imagePullCredentialsType"], "SERVICE_ROLE")
        self.assertEqual(project["environment"]["fleet"], {
            "fleetArn": "arn:aws:codebuild:us-east-1:807034087062:fleet/leaf-ci-heavy-pilot:"
                        "f625198b-4219-4066-9edb-f68655cf7b0a"})
        self.assertEqual(project["cache"], {"type": "LOCAL", "modes": ["LOCAL_SOURCE_CACHE", "LOCAL_CUSTOM_CACHE"]})
        self.assertEqual(project["logsConfig"]["cloudWatchLogs"]["groupName"], "/codebuild/leaf-ci-leaf-web-demo")
        self.assertEqual(project["source"]["gitCloneDepth"], 0)
        self.assertIs(project["source"]["reportBuildStatus"], True)
        loader = project["source"]["buildspec"]
        for text in (".codebuild/ci.sh", "origin/main",
                     "shell: bash", 'git show "$REF:.codebuild/ci.sh" > /tmp/ci.sh', "bash /tmp/ci.sh",
                     "/root/.cache/ms-playwright/**/*", "/root/.npm/**/*", "/root/.cache/pip/**/*"):
            self.assertIn(text, loader)
        self.assertNotIn("runtime-versions", loader)
        self.assertNotIn("git fetch", loader)
        self.assertEqual(document["webhook"], {
            "projectName": "leaf-ci-leaf-web-demo",
            "filterGroups": [
                [{"type": "EVENT", "pattern": "PUSH"},
                 {"type": "HEAD_REF", "pattern": "^refs/heads/main$"}],
                [{"type": "EVENT", "pattern": "PULL_REQUEST_CREATED,PULL_REQUEST_UPDATED,PULL_REQUEST_REOPENED"},
                 {"type": "BASE_REF", "pattern": "^refs/heads/main$"}],
                [{"type": "EVENT", "pattern": "PUSH"},
                 {"type": "HEAD_REF", "pattern": "^refs/heads/gh-readonly-queue/"}],
            ],
        })

    @unittest.skipUnless(BASH, "bash is not on PATH; project usage check requires bash")
    def test_usage_mentions_no_webhook(self):
        result = subprocess.run(
            [BASH, "scripts/ci/pr-codebuild-project.sh", "--bogus"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--no-webhook", result.stderr)
