"""Pin the leaf-web-demo native CodeBuild CI rail without parsing YAML."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".codebuild/ci.sh"
PROJECT_PATH = ROOT / "scripts/ci/pr-codebuild-project.sh"
BASH = shutil.which("bash")


class TestCodebuildCiScript(unittest.TestCase):
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
        invocations = re.findall(
            r"\bpython scripts/run-all-gates\.py\b(?:(?!\bpython scripts/run-all-gates\.py\b)[^\n])*",
            re.sub(r"\\\n\s*", " ", script))
        gate_runs = [call for call in invocations
                     if "--verify-shard-results" not in call and "--verify-gate-proof" not in call]
        self.assertEqual(gate_runs, [invocations[0]])
        for flag in ("--retry 1", "--result-json", "--log-dir"):
            self.assertIn(flag, invocations[0])
        for flag in ("--shard-count", "--shard-index"):
            self.assertNotIn(flag, script)
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

    def test_gate_proof_receipt_guard(self):
        script = CI_PATH.read_text(encoding="utf-8")
        guard = script.split("<<'LEAF_GATE_PROOF_ELIGIBLE'\n", 1)[1].split(
            "\nLEAF_GATE_PROOF_ELIGIBLE", 1)[0]
        self.assertIn('python -I -B - "$selection_dir/final.json"', script)
        self.assertIn('(out / "final.json").write_text(canonical(detail)', script)
        self.assertLess(script.index('(out / "final.json").write_text'),
                        script.index('# LEAF_GATE_PROOF_BEGIN'))
        base = dict(schema="leaf.ci.selection.v1", execution_mode="full",
                    selection_mode="full", phase="enforce", trusted_sha_override=False)
        cases = [
            ("full", {}, 0),
            ("sel", dict(execution_mode="selected"), 1),
            ("shadow", dict(selection_mode="shadow"), 1),
            ("phase", dict(phase="shadow"), 1),
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
        self.assertIn('unset PYTHONSAFEPATH\npython scripts/run-all-gates.py', script)
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
        self.assertIn('"$reporters_ready" "$trusted_sha_override" "$loader_check" '
                      "<<'LEAF_SELECTION_FINALIZE'", script)
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
