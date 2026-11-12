"""Exercise CI setup overlap using marker handshakes instead of timing."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".codebuild/ci.sh"
BASH = shutil.which("bash")

# Each dependency has ten seconds to observe its partner's marker. A serial
# implementation cannot complete the handshake, regardless of host speed.
WAIT_FOR_MARKER = """
wait_for_marker() {
  local deadline=$((SECONDS + 10))
  until [[ -f "$1" ]]; do
    if (( SECONDS >= deadline )); then exit 9; fi
    sleep 0.05
  done
}
"""


def shell_path(path):
    text = path.resolve().as_posix()
    return "/" + text[0].lower() + text[2:] if os.name == "nt" else text


@unittest.skipUnless(BASH, "bash is not on PATH")
class TestCiSetupOverlap(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.bins = self.directory / "bin"
        self.bins.mkdir()
        for name in ("web", "harness", "tmp"):
            (self.directory / name).mkdir()
        self.env = dict(os.environ, CODEBUILD_SRC_DIR=shell_path(self.directory),
                        FIXTURE_BIN=shell_path(self.bins),
                        FIXTURE_ROOT=shell_path(self.directory),
                        TMPDIR=shell_path(self.directory / "tmp"))
        for key in ("CI_SHARD_TOTAL", "CI_SHARD_INDEX", "BASH_ENV", "LEAF_CI_CACHE_DIR",
                    "LEAF_CI_CACHE_WRITABLE", "LEAF_DEP_CACHE_HELPER", "LEAF_DEP_CACHE_OFF",
                    "PLAYWRIGHT_BROWSERS_PATH"):
            self.env.pop(key, None)

    def stub(self, name, body):
        script = self.bins / name
        script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" +
                          WAIT_FOR_MARKER + body, encoding="utf-8", newline="\n")
        script.chmod(0o755)

    def run_shell(self, command):
        return subprocess.run(
            [BASH, "--noprofile", "--norc", "-c",
             'set -euo pipefail\nexport PATH="$FIXTURE_BIN:$PATH"\n' + command],
            env=self.env, capture_output=True, text=True, timeout=60)

    def run_dependencies(self, fail_pip=False, web_rc=0, harness_rc=0):
        self.env["FIXTURE_FAIL_PIP"] = str(int(fail_pip))
        self.env.update(FIXTURE_WEB_RC=str(web_rc),
                        FIXTURE_HARNESS_RC=str(harness_rc))
        self.stub("npm", """
[[ "$*" == ci ]]
if [[ "$PWD" == "$CODEBUILD_SRC_DIR/web" ]]; then
  if [[ "$FIXTURE_FAIL_PIP" == 0 ]]; then
    wait_for_marker "$FIXTURE_ROOT/pip-started"
  fi
  echo web-npm-complete
  exit "$FIXTURE_WEB_RC"
else
  [[ "$PWD" == "$CODEBUILD_SRC_DIR/harness" ]]
  touch "$FIXTURE_ROOT/harness-started"
  echo harness-npm-complete
  touch "$FIXTURE_ROOT/harness-finished"
  exit "$FIXTURE_HARNESS_RC"
fi
""")
        self.stub("python", """
[[ "$PWD" == "$CODEBUILD_SRC_DIR" ]]
if [[ "$FIXTURE_FAIL_PIP" == 1 ]]; then
  if [[ "$FIXTURE_HARNESS_RC" != 0 ]]; then
    wait_for_marker "$FIXTURE_ROOT/harness-finished"
    sleep 0.1
  fi
  exit 7
fi
touch "$FIXTURE_ROOT/pip-started"
wait_for_marker "$FIXTURE_ROOT/harness-started"
echo pip-complete
""")
        entry = CI_PATH.read_text(encoding="utf-8")
        block = entry.split("# LEAF_CI_SETUP_DEPS_BEGIN\n", 1)[1].split(
            "# LEAF_CI_SETUP_DEPS_END", 1)[0]
        return self.run_shell(block)

    def assert_logs_ordered(self, output):
        positions = []
        for step in ("webdeps", "pydeps", "harness-deps"):
            self.assertRegex(output, rf"LEAF_T start {step} \d+")
            positions.append(output.index(f"LEAF_T start {step} "))
        self.assertEqual(positions, sorted(positions))
        self.assertLess(output.index("=== job license-fence ==="),
                        output.index("=== job test-gate ==="))

    def test_dependency_installs_overlap(self):
        result = self.run_dependencies()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_logs_ordered(result.stdout)
        for step in ("webdeps", "pydeps", "harness-deps"):
            self.assertRegex(result.stdout, rf"LEAF_T end {step} \d+ rc=0")
        for first, second in (("webdeps", "pydeps"), ("pydeps", "harness-deps")):
            self.assertLess(result.stdout.index(f"LEAF_T end {first} "),
                            result.stdout.index(f"LEAF_T start {second} "))
        self.assertIn("web-npm-complete", result.stdout)
        self.assertIn("pip-complete", result.stdout)
        self.assertIn("harness-npm-complete", result.stdout)
        self.assertFalse(list((self.directory / "tmp").iterdir()))

    def test_failed_dependency_preserves_other_logs_and_status(self):
        result = self.run_dependencies(fail_pip=True)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assert_logs_ordered(result.stdout)
        self.assertIn("setup: pydeps failed rc=7", result.stdout)
        for step in ("webdeps", "harness-deps"):
            self.assertRegex(result.stdout, rf"LEAF_T end {step} \d+ rc=0")
        self.assertIn("web-npm-complete", result.stdout)
        self.assertIn("harness-npm-complete", result.stdout)
        self.assertGreater(result.stdout.index("setup: pydeps failed rc=7"),
                           result.stdout.index("harness-npm-complete"))
        self.assert_dependency_failures(result, [("pydeps", 7)])

    def assert_dependency_failures(self, result, failures):
        self.assertEqual(result.returncode, failures[0][1],
                         result.stdout + result.stderr)
        self.assert_logs_ordered(result.stdout)
        diagnostics = [f"setup: {step} failed rc={rc}" for step, rc in failures]
        self.assertEqual([line for line in result.stdout.splitlines()
                          if line.startswith("setup:")], diagnostics)
        self.assertTrue(result.stdout.rstrip().endswith("\n".join(diagnostics)),
                        result.stdout)
        # The harness log is last even when it finishes first or fails.
        last_log = result.stdout.index("harness-npm-complete")
        for diagnostic in diagnostics:
            self.assertGreater(result.stdout.index(diagnostic), last_log)
        self.assertIn("web-npm-complete", result.stdout)
        for step in ("webdeps", "pydeps", "harness-deps"):
            if step not in dict(failures):
                self.assertRegex(result.stdout, rf"LEAF_T end {step} \d+ rc=0")
                self.assertLess(result.stdout.index(f"LEAF_T end {step} "),
                                result.stdout.index(diagnostics[0]))

    def test_failed_web_dependency_preserves_all_logs(self):
        result = self.run_dependencies(web_rc=3)
        self.assert_dependency_failures(result, [("webdeps", 3)])

    def test_failed_harness_dependency_preserves_all_logs(self):
        result = self.run_dependencies(harness_rc=4)
        self.assert_dependency_failures(result, [("harness-deps", 4)])

    def test_dependency_failure_status_uses_step_order(self):
        result = self.run_dependencies(fail_pip=True, harness_rc=4)
        self.assert_dependency_failures(result, [("pydeps", 7), ("harness-deps", 4)])

    def run_heavy(self, fail_build=False, fail_browser="", fail_license=""):
        self.env.update(FIXTURE_FAIL_BUILD=str(int(fail_build)),
                        FIXTURE_FAIL_BROWSER=fail_browser,
                        FIXTURE_FAIL_LICENSE=fail_license,
                        FIXTURE_BLOCK_BROWSER=str(int(fail_build or bool(fail_license))),
                        CI_SCRIPT=shell_path(CI_PATH))
        self.stub("npm", """
[[ "$*" == 'run build' ]]
# Waiting for the browser proves it was started before the build, and makes
# the failure case exercise an actual running browser child.
wait_for_marker "$FIXTURE_ROOT/browser.pid"
if [[ "$FIXTURE_FAIL_BUILD" == 1 ]]; then exit 5; fi
touch "$FIXTURE_ROOT/web-built"
""")
        browser_stub = """
browser_install() {
trap 'touch "$FIXTURE_ROOT/term-received"; exit 143' TERM
echo "browser-log-started $1"
echo "$$" > "$FIXTURE_ROOT/browser.pid.tmp"
mv "$FIXTURE_ROOT/browser.pid.tmp" "$FIXTURE_ROOT/browser.pid"
if [[ "$FIXTURE_BLOCK_BROWSER" == 1 ]]; then
  # A foreground failure must terminate this child, rather than wait for a
  # natural exit. This is intentionally longer than the handshake deadlines.
  deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do sleep 0.05; done
  exit 9
fi
wait_for_marker "$FIXTURE_ROOT/web-built"
if [[ "$FIXTURE_FAIL_BROWSER" == "$1" ]]; then
  if [[ "$1" == npx ]]; then exit 11; else exit 12; fi
fi
echo browser-install-complete
}
"""
        self.stub("npx", browser_stub + """
case "$*" in
  'playwright install --with-deps chromium'|'playwright install chromium')
    browser_install npx ;;
  'playwright pdf about:blank '*) ;;
  *) exit 8 ;;
esac
""")
        self.stub("python", browser_stub + """
case "$*" in
  'scripts/check_license_fence.py --self-test')
    if [[ "$FIXTURE_FAIL_LICENSE" == self-test ]]; then exit 13; fi
    echo license-self-test-complete ;;
  'scripts/check_license_fence.py .')
    if [[ "$FIXTURE_FAIL_LICENSE" == scan ]]; then exit 14; fi
    echo license-scan-complete ;;
  '-m playwright install --with-deps chromium'|'-m playwright install chromium')
    browser_install python ;;
  '-m playwright pdf about:blank '*) ;;
  *) exit 8 ;;
esac
""")
        return self.run_shell('exec bash "$CI_SCRIPT" --heavy-prerequisites')

    def test_browser_install_overlaps_web_build(self):
        result = self.run_heavy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("browser-install-complete", result.stdout)
        self.assertRegex(result.stdout, r"LEAF_T end chromium \d+ rc=0")

    def test_failed_build_reaps_browser_child(self):
        result = self.run_heavy(fail_build=True)
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertTrue((self.directory / "term-received").exists(),
                        result.stdout + result.stderr)
        self.assert_browser_log_and_child_stopped(result)

    def assert_browser_log_and_child_stopped(self, result):
        self.assertRegex(result.stdout, r"LEAF_T start chromium \d+")
        self.assertIn("--- 5/6 Install Chromium for browser proofs", result.stdout)
        self.assertIn("browser-log-started npx", result.stdout)
        pid_file = self.directory / "browser.pid"
        self.assertTrue(pid_file.exists(), result.stdout + result.stderr)
        pid = pid_file.read_text(encoding="utf-8").strip()
        self.assertTrue(pid.isdigit(), pid)
        result = self.run_shell(f"if kill -0 {pid} 2>/dev/null; then exit 1; fi")
        self.assertEqual(result.returncode, 0, "browser child is still running")

    def test_failed_npx_browser_preserves_status_after_foreground(self):
        result = self.run_heavy(fail_browser="npx")
        self.assertEqual(result.returncode, 11, result.stdout + result.stderr)
        self.assertIn("license-self-test-complete", result.stdout)
        self.assertIn("license-scan-complete", result.stdout)
        self.assert_browser_log_and_child_stopped(result)

    def test_failed_python_browser_preserves_status_after_foreground(self):
        result = self.run_heavy(fail_browser="python")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertIn("license-self-test-complete", result.stdout)
        self.assertIn("license-scan-complete", result.stdout)
        self.assertIn("browser-log-started python", result.stdout)
        self.assert_browser_log_and_child_stopped(result)

    def test_failed_license_self_test_kills_browser(self):
        result = self.run_heavy(fail_license="self-test")
        self.assertEqual(result.returncode, 13, result.stdout + result.stderr)
        self.assertTrue((self.directory / "term-received").exists(),
                        result.stdout + result.stderr)
        self.assert_browser_log_and_child_stopped(result)

    def test_failed_license_scan_kills_browser(self):
        result = self.run_heavy(fail_license="scan")
        self.assertEqual(result.returncode, 14, result.stdout + result.stderr)
        self.assertIn("license-self-test-complete", result.stdout)
        self.assertTrue((self.directory / "term-received").exists(),
                        result.stdout + result.stderr)
        self.assert_browser_log_and_child_stopped(result)
