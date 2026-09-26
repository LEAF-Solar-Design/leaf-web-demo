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


pytestmark = pytest.mark.skipif(sys.platform != "linux" or shutil.which("strace") is None,
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
    code = supervisor.run(context, output, commands[case], descendant_grace=2)
    receipt = json.loads((output / "reports" / "trace-receipt-smoke.json").read_text())
    certificate_path = output / "reports" / "process-tree-smoke.json"
    certificate = json.loads(certificate_path.read_text()) if certificate_path.exists() else {}
    diagnostics = json.dumps({"case": case, "code": code, "receipt": receipt,
                              "loss_counters": certificate.get("loss_counters"),
                              "unknown_fd_samples": certificate.get("loss_counters", {}).get("unknown_fd_samples"),
                              "reasons": certificate.get("reasons"),
                              "capture_errors": receipt.get("capture_errors"),
                              "tasks": certificate.get("tasks")}, sort_keys=True)
    assert code == 0, diagnostics
    assert receipt["capture_complete"] is True, diagnostics
    assert certificate.get("complete") is True, diagnostics
    assert certificate["loss_counters"]["unknown_descriptors"] == 0, diagnostics
    assert certificate["reasons"] == [], diagnostics
    assert certificate["seed_fds"] == receipt["seed_fds"], diagnostics
    if case == "pytest":
        assert {"path": "test_smoke.py", "kind": "bytecode"} in certificate["reads"], diagnostics
