"""Exercise the inline native CI failure diagnostics without running gates."""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


CI_SCRIPT = Path(__file__).resolve().parents[1] / ".codebuild" / "ci.sh"
BEGIN = "# BEGIN GATE FAIL NAMES"
END = "# END GATE FAIL NAMES"


def block_text():
    return CI_SCRIPT.read_text(encoding="utf-8").split(BEGIN, 1)[1].split(END, 1)[0]


@pytest.fixture
def run_program(tmp_path):
    result_path = tmp_path / "gate-result.json"
    log_dir = tmp_path / "gate-logs"
    log_dir.mkdir()
    block = block_text()
    program = re.search(r"python3 - <<'PY'\n(.*?)\nPY", block, re.DOTALL).group(1)
    program = program.replace("/tmp/gate-results/gate-result.json", result_path.as_posix())
    program = program.replace("/tmp/gate-logs", log_dir.as_posix())
    script = tmp_path / "fail_names.py"
    script.write_text(program, encoding="utf-8")

    def run(results=None, logs=None, raw=None):
        if raw is not None:
            result_path.write_text(raw, encoding="utf-8")
        else:
            result_path.write_text(json.dumps({"schema": 1, "results": results}), encoding="utf-8")
        for name, content in (logs or {}).items():
            (log_dir / name).write_text(content, encoding="utf-8")
        return subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                              encoding="utf-8", timeout=10)

    return run


def fail_suite(suite_id="web-vitest", failed_ids=None, note="failed"):
    return {"id": suite_id, "status": "FAIL", "attempts": 2, "note": note,
            "test_report": {"failed_test_ids": failed_ids or []}}


def test_block_present_and_fail_open():
    text = CI_SCRIPT.read_text(encoding="utf-8")
    assert text.count(BEGIN) == text.count(END) == 1
    guard = text.index("if [[ -f /tmp/gate-results/gate-result.json ]]; then")
    tail = text.index("tail -n 200 /tmp/gate-results/gate-result.json || true", guard)
    begin = text.index(BEGIN)
    end = text.index(END)
    assert guard < tail < begin < end < text.index("\nfi", guard)
    block = block_text()
    assert block.rstrip().endswith("|| true")
    assert not re.search(r"\bgate_status\s*=", block)


def test_names_failed_ids_and_log_lines(run_program):
    passed = {"id": "server-tool-publication-policy", "status": "PASS", "got": "13",
              "executed": 13, "expected": 13, "attempts": 1, "seconds": 1.4,
              "note": "", "test_report": {"failed_test_ids": [], "test_ids": [],
              "collection_ids_sha256": None, "test_report_complete": False,
              "test_report_refs": [], "test_report_renamed_ids": 0,
              "test_id_granularity": "test"}}
    test_id = ("web-vitest::src/cadedit/drawingObjectList.test.jsx > "
               "keeps all 2,345 rooftop rows and their native controls reachable")
    note = "FAIL after 2 attempts (suite FAILED: 1 failed 27 skipped)"
    proc = run_program([passed, fail_suite(failed_ids=[test_id], note=note)], {
        "web-vitest.log": "first attempt FAIL x\n",
        "web-vitest.retry1.log": (" FAIL  src/cadedit/drawingObjectList.test.jsx > "
                                  "keeps all 2,345 rooftop rows\n"
                                  "Error: Test timed out in 5000ms.\n"),
    })
    assert proc.returncode == 0
    assert f"GATE FAIL web-vitest: {note}" in proc.stdout
    assert f"  failed test: {test_id}" in proc.stdout
    assert "  log:  FAIL  src/cadedit/drawingObjectList.test.jsx" in proc.stdout
    assert "  log: Error: Test timed out in 5000ms." in proc.stdout
    assert "first attempt" not in proc.stdout
    assert passed["id"] not in proc.stdout


def test_truncates_ids_and_total(run_program):
    proc = run_program([fail_suite(failed_ids=[f"test-{n}" for n in range(25)])])
    assert proc.returncode == 0
    assert proc.stdout.count("  failed test: ") == 20
    assert "  failed test: test-19\n" in proc.stdout
    assert "  failed test: test-20\n" not in proc.stdout
    assert "  ... and 5 more failed tests\n" in proc.stdout
    suites = [fail_suite(f"suite-{n}") for n in range(30)]
    logs = {f"suite-{n}.log": "FAIL detail\n" * 30 for n in range(30)}
    proc = run_program(suites, logs)
    assert proc.returncode == 0
    assert "gate fail names: output capped at 400 lines\n" in proc.stdout
    assert len(proc.stdout.splitlines()) <= 401


def test_no_fail_prints_nothing(run_program):
    for results in ([], [{"id": "ok", "status": "PASS"}]):
        proc = run_program(results)
        assert proc.returncode == 0
        assert proc.stdout == ""


def test_malformed_json_one_line(run_program):
    proc = run_program(raw="{invalid")
    assert proc.returncode == 0
    assert proc.stdout == "gate fail names: result JSON unreadable (JSONDecodeError)\n"


def test_missing_log_and_no_marker_tail(run_program, tmp_path):
    proc = run_program([fail_suite("missing")])
    assert proc.returncode == 0
    expected_path = tmp_path / "gate-logs" / "missing.log"
    assert f"  (no suite log at {expected_path})\n" in proc.stdout
    proc = run_program([fail_suite("plain")], {
        "plain.log": "\n".join(f"plain line {n}" for n in range(25)) + "\n",
    })
    assert proc.returncode == 0
    log_lines = [line for line in proc.stdout.splitlines() if line.startswith("  log: ")]
    assert log_lines == [f"  log: plain line {n}" for n in range(5, 25)]
