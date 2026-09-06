"""Pin the leaf-web-demo native CodeBuild CI rail without parsing YAML."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".codebuild/ci.sh"
PROJECT_PATH = ROOT / "scripts/ci/pr-codebuild-project.sh"
BASH = shutil.which("bash")


class TestCodebuildCiScript(unittest.TestCase):
    def test_workflow_commands(self):
        script = CI_PATH.read_text(encoding="utf-8")
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))
        commands = [line.strip() for line in script.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(commands[0], "set -euo pipefail")
        self.assertLess(script.index("CODEBUILD_WEBHOOK_BASE_REF:-"),
                        script.index("##refs/heads/"))
        self.assertNotIn("git fetch", script)
        invocations = re.findall(
            r"^python scripts/run-all-gates\.py\b[^\n]*(?:\\\n[^\n]*)*",
            script, re.MULTILINE)
        self.assertEqual(len(invocations), 1)
        for flag in ("--retry 1", "--result-json", "--log-dir"):
            self.assertIn(flag, invocations[0])
        for flag in ("--shard-count", "--shard-index", "--verify-shard-results"):
            self.assertNotIn(flag, script)

        workflow = (ROOT / ".github/workflows/test-gate.yml").read_text(encoding="utf-8-sig")
        requirements = re.findall(r"^\s*-r\s+(\S+)", workflow, re.MULTILINE)
        self.assertTrue(requirements, "No workflow requirement lines found")
        installed = re.findall(r"^\s*-r\s+(\S+)", script, re.MULTILINE)
        self.assertEqual(installed, requirements)
        self.assertRegex(script, r"playwright install[^\n]*chromium")
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

    def test_steps_reset_directory(self):
        lines = CI_PATH.read_text(encoding="utf-8").splitlines()
        steps = [i for i, line in enumerate(lines) if line.startswith('echo "--- ')]
        self.assertTrue(steps, "No CI step markers found")
        for i in steps:
            self.assertTrue(any(line.startswith("cd ") for line in lines[max(0, i - 3):i]),
                            f"Step must reset cwd: {lines[i]}")

    @unittest.skipUnless(BASH, "bash is not on PATH; shell syntax check requires bash")
    def test_bash_syntax(self):
        for path in (".codebuild/ci.sh", "scripts/ci/pr-codebuild-project.sh"):
            result = subprocess.run([BASH, "-n", path], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


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
        self.assertEqual(project["environment"]["image"], "aws/codebuild/standard:7.0")
        self.assertEqual(project["environment"]["computeType"], "BUILD_GENERAL1_LARGE")
        self.assertEqual(project["logsConfig"]["cloudWatchLogs"]["groupName"], "/codebuild/leaf-ci-leaf-web-demo")
        self.assertEqual(project["source"]["gitCloneDepth"], 0)
        self.assertIs(project["source"]["reportBuildStatus"], True)
        loader = project["source"]["buildspec"]
        for text in (".codebuild/ci.sh", "origin/main", "nodejs: 20", "python: 3.12",
                     "shell: bash", 'git show "$REF:.codebuild/ci.sh" > /tmp/ci.sh', "bash /tmp/ci.sh"):
            self.assertIn(text, loader)
        self.assertNotIn("git fetch", loader)
        self.assertEqual(document["webhook"], {
            "projectName": "leaf-ci-leaf-web-demo",
            "filterGroups": [
                [{"type": "EVENT", "pattern": "PUSH"},
                 {"type": "HEAD_REF", "pattern": "^refs/heads/main$"}],
                [{"type": "EVENT", "pattern": "PULL_REQUEST_CREATED,PULL_REQUEST_UPDATED,PULL_REQUEST_REOPENED"},
                 {"type": "BASE_REF", "pattern": "^refs/heads/main$"}],
            ],
        })
