"""Real supervisor smoke gate: includes python -c pass, not a canned tracer.

Amendment 7: wrapper inheritance and runtime syscalls must produce complete trees.
"""

import os
import json
from pathlib import Path
import py_compile
import shutil
import sys

import pytest


SELECTOR = Path(os.environ.get("LEAF_TRUSTED_CI_DIR") or Path(__file__).resolve().parents[1] / "scripts" / "ci")
sys.path.insert(0, str(SELECTOR))
import trace_supervisor as supervisor


def print_reason_context(certificate, transcript_path):
    """Print at most three nine-line transcript windows for each sampled reason."""
    try:
        transcript = transcript_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        print("reason_samples: retained transcript unavailable")
        return
    counts = {}
    for sample in certificate.get("reason_samples", []):
        reason, sequence = sample["reason"], sample["sequence"]
        if counts.get(reason, 0) >= 3 or (reason not in counts and len(counts) >= 32):
            continue
        if type(sequence) is not int or not 0 <= sequence < len(transcript):
            continue
        counts[reason] = counts.get(reason, 0) + 1
        print("reason sample: " + json.dumps(sample, sort_keys=True))
        for index in range(max(0, sequence - 6), min(len(transcript), sequence + 3)):
            print(f"{index}: {transcript[index]}")


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("strace") is None,
                    reason="strace or Linux unavailable")
@pytest.mark.parametrize("case", ["true", "python -c pass", "pipeline", "subprocess", "pytest"])
def test_real_supervisor_complete(tmp_path, monkeypatch, case):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "capture"
    source = repo / "test_smoke.py"
    source.write_text("def test_one():\n    assert 1 + 1 == 2\n\n"
                      "def test_two(tmp_path):\n    assert tmp_path.is_dir()\n", encoding="utf-8")
    # --assert=plain makes pytest load this pre-existing interpreter bytecode.
    bytecode = repo / "__pycache__" / ("test_smoke." + sys.implementation.cache_tag + ".pyc")
    py_compile.compile(str(source), cfile=str(bytecode), doraise=True, optimize=0)
    context = {
        "source_root": "/work",
        "initial_cwd": "/work",
        "seed_fds": {
            "0": {
                "kind": "devnull"
            },
            "1": {
                "kind": "supervisor"
            },
            "2": {
                "kind": "supervisor"
            }
        },
        "inventory": {
            "files": [
                "src/a.py",
                "src/real.py",
                "data/fixture.json",
                "modules/m/main.tf"
            ],
            "symlinks": {
                "link.py": "src/real.py"
            }
        },
        "external_roots": {
            "/usr/bin": "os-image",
            "/usr/lib/python3.11": "python-installation",
            "/tmp": "generated",
            "/dev": "device"
        },
        "run_id": "s15a-fixture-complete",
        "source_sha": "1111111111111111111111111111111111111111",
        "source_tree": "2222222222222222222222222222222222222222",
        "capture_sha": "3333333333333333333333333333333333333333",
        "catalog_sha256": "4444444444444444444444444444444444444444444444444444444444444444",
        "capture_group": "fixture-complete",
        "suites": [
            {
                "suite_id": "unit.py",
                "attempt": 1,
                "worker": "gw0",
                "test_ids": [
                    "unit.py::tests/test_a.py::test_one"
                ],
                "outcomes_ref": "reports/outcomes.json"
            }
        ],
        "helper_blobs": {
            "trace_reads_sha256": "5555555555555555555555555555555555555555555555555555555555555555",
            "reporting_helper_sha256s": {
                "report": "6666666666666666666666666666666666666666666666666666666666666666"
            }
        },
        "strace_package": "strace=5.16",
        "toolchain_fingerprint": {
            "python": "3.11"
        },
        "image_manifest_digest": "sha256:diagnostic-only",
        "distribution_list_digest": "diagnostic-only",
        "interpreter_identity": "python3.11"
    }
    context.update(source_root=repo.as_posix(), initial_cwd=repo.as_posix(), capture_group="smoke",
                   inventory={"files": ["test_smoke.py"], "symlinks": {}},
                   external_roots={"/": "device", SELECTOR.as_posix(): "supervisor",
                                   output.as_posix(): "supervisor"})
    commands = {
        "true": ["true"],
        "python -c pass": [sys.executable, "-c", "pass"],
        "pipeline": ["bash", "-c", "echo x | cat"],
        "subprocess": [sys.executable, "-c", "import subprocess; subprocess.run(['true'], check=True)"],
        "pytest": [sys.executable, "-m", "pytest", "-q", "--assert=plain", "-p", "no:cacheprovider", str(source)],
    }
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    monkeypatch.delenv("PYTHONOPTIMIZE", raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    transcript_dir = tmp_path / "transcript"
    code = supervisor.run(context, output, commands[case], descendant_grace=2, keep_transcript=transcript_dir)
    receipt = json.loads((output / "reports" / "trace-receipt-smoke.json").read_text())
    certificate_path = output / "reports" / "process-tree-smoke.json"
    certificate = json.loads(certificate_path.read_text()) if certificate_path.exists() else {}
    diagnostics = json.dumps({"case": case, "code": code, "receipt": receipt,
                              "loss_counters": certificate.get("loss_counters"),
                              "unknown_fd_samples": certificate.get("loss_counters", {}).get("unknown_fd_samples"),
                              "reasons": certificate.get("reasons"),
                              "reason_samples": certificate.get("reason_samples"),
                              "capture_errors": receipt.get("capture_errors"),
                              "tasks": certificate.get("tasks")}, sort_keys=True)
    try:
        assert code == 0, diagnostics
        assert receipt["capture_complete"] is True, diagnostics
        assert certificate.get("complete") is True, diagnostics
        assert certificate["loss_counters"]["unknown_descriptors"] == 0, diagnostics
        assert certificate["reasons"] == [], diagnostics
        assert certificate["seed_fds"] == receipt["seed_fds"], diagnostics
        if case == "pytest":
            assert {"path": "test_smoke.py", "kind": "bytecode"} in certificate["reads"], diagnostics
    except AssertionError:
        print_reason_context(certificate, transcript_dir / "transcript.strace")
        raise


def test_smoke_failure_prints_reason_context_offline(tmp_path, monkeypatch, capsys):
    certificate = {"complete": False, "reasons": ["unknown_path_base"], "reason_samples": [
        {"reason": "unknown_path_base", "sequence": sequence, "pid": 100, "syscall": "openat",
         "fd": 7, "fd_kind": "endpoint", "shape": "openat"} for sequence in (6, 10, 14, 18, 22)]}
    monkeypatch.setattr(supervisor.tree, "decode_stream", lambda *args: {"certificate": certificate})

    def capture(context, output, command, *, descendant_grace, keep_transcript):
        report = supervisor.tree.decode_stream((), context)["certificate"]
        reports = output / "reports"
        reports.mkdir(parents=True)
        (reports / "process-tree-smoke.json").write_text(json.dumps(report), encoding="utf-8")
        (reports / "trace-receipt-smoke.json").write_text(json.dumps({"capture_complete": False}), encoding="utf-8")
        keep_transcript.mkdir()
        (keep_transcript / "transcript.strace").write_text(
            "".join(f"context line {index}\n" for index in range(30)), encoding="utf-8")
        return 0

    monkeypatch.setattr(supervisor, "run", capture)
    with pytest.raises(AssertionError, match="unknown_path_base"):
        test_real_supervisor_complete(tmp_path, monkeypatch, "pipeline")
    output = capsys.readouterr().out
    assert output.count("reason sample: ") == 3
    assert output.count("context line ") == 27
    assert "0: context line 0\n" in output
    assert "16: context line 16\n" in output
    assert "17: context line 17\n" not in output
