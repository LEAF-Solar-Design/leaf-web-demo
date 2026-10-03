"""Fake-runner tests for scripts/proof_dwg_cage_image.py. No Docker: the host
half runs against a recording fake runner, and the in-image proof program runs
in-process against fake dwg_convert and dxf_intake modules."""
import ast
import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import proof_dwg_cage_image as proof

IMAGE = "leaf-app:proof"
CASE_KEYS = {"case", "expected", "observed", "exit_status"}
GOOD = {"honest_geometry": "geometry_match", "hostile_bad_params": "BAD_PARAMS"}


def line(case, observed):
    return proof.MARK + json.dumps({"case": case, "observed": observed})


def stdout_for(results):
    return "\n".join(line(case, obs) for case, obs in results.items()) + "\n"


class FakeRunner:
    def __init__(self, code=0, stdout=None, stderr="", raises=None):
        self.code = code
        self.stdout = stdout_for(GOOD) if stdout is None else stdout
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, argv, stdin_text, timeout_s):
        self.calls.append((list(argv), stdin_text, timeout_s))
        if self.raises is not None:
            raise self.raises
        return self.code, self.stdout, self.stderr


def run(tmp_path, runner, image=IMAGE):
    receipt_path = tmp_path / "receipt.json"
    receipt = proof.run_proofs(image, receipt_path, runner=runner)
    on_disk = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert on_disk == receipt
    assert sorted(p.name for p in tmp_path.iterdir()) == ["receipt.json"]
    assert set(receipt) == {"image", "passed", "cases"}
    assert receipt["image"] == image
    assert [c["case"] for c in receipt["cases"]] == list(GOOD)
    for case in receipt["cases"]:
        assert set(case) == CASE_KEYS
        assert case["expected"] == GOOD[case["case"]]
    return receipt


def observed(receipt):
    return {c["case"]: c["observed"] for c in receipt["cases"]}


# --------------------------------------------------------------------------- #
# host half
# --------------------------------------------------------------------------- #
def test_argv_is_network_isolated_python_on_stdin_with_a_300s_bound(tmp_path):
    runner = FakeRunner()
    run(tmp_path, runner)
    assert len(runner.calls) == 1
    argv, stdin_text, timeout_s = runner.calls[0]
    assert argv == ["docker", "run", "--rm", "-i", "--network", "none",
                    "--entrypoint", "python", IMAGE, "-B", "-"]
    assert stdin_text == proof.PROOF_PROGRAM
    assert timeout_s == 300


def test_default_runner_uses_argv_without_a_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="out", stderr="err")

    monkeypatch.setattr(proof.subprocess, "run", fake_run)
    argv = proof.docker_argv(IMAGE)
    assert proof.default_runner(argv, "prog", 300) == (0, "out", "err")
    assert seen["argv"] == argv
    assert seen["shell"] is False
    assert seen["timeout"] == 300
    assert seen["input"] == "prog"


@pytest.mark.parametrize("image", ["", "--privileged", "-v", "img --network host"])
def test_an_image_docker_would_read_as_a_flag_fails_without_launching(tmp_path, image):
    runner = FakeRunner()
    receipt = run(tmp_path, runner, image=image)
    assert runner.calls == []
    assert receipt["passed"] is False
    assert all(o.startswith("invalid_image") for o in observed(receipt).values())


def test_both_cases_pass_and_the_cli_exits_zero(tmp_path, capsys):
    receipt = run(tmp_path, FakeRunner())
    assert receipt["passed"] is True
    assert observed(receipt) == GOOD
    assert all(c["exit_status"] == 0 for c in receipt["cases"])
    code = proof.main(["--image", IMAGE, "--receipt", str(tmp_path / "receipt.json")],
                      runner=FakeRunner())
    assert code == 0
    assert capsys.readouterr().out.count("PASS ") == 2


@pytest.mark.parametrize("raises, prefix", [
    (FileNotFoundError("docker"), "launch_error"),
    (subprocess.TimeoutExpired(["docker"], 300), "timeout"),
])
def test_launch_error_and_timeout_fail_both_cases(tmp_path, raises, prefix):
    receipt = run(tmp_path, FakeRunner(raises=raises))
    assert receipt["passed"] is False
    for case in receipt["cases"]:
        assert case["observed"].startswith(prefix)
        assert case["exit_status"] is None
    code = proof.main(["--image", IMAGE, "--receipt", str(tmp_path / "receipt.json")],
                      runner=FakeRunner(raises=raises))
    assert code == 1


def test_nonzero_exit_fails_even_with_matching_output(tmp_path):
    receipt = run(tmp_path, FakeRunner(code=1))
    assert observed(receipt) == GOOD
    assert all(c["exit_status"] == 1 for c in receipt["cases"])
    assert receipt["passed"] is False


@pytest.mark.parametrize("present", ["honest_geometry", "hostile_bad_params"])
def test_each_case_is_required(tmp_path, present):
    runner = FakeRunner(stdout=stdout_for({present: GOOD[present]}), stderr="Traceback boom")
    receipt = run(tmp_path, runner)
    assert receipt["passed"] is False
    for case, obs in observed(receipt).items():
        if case == present:
            assert obs == GOOD[case]
        else:
            assert obs.startswith("missing_output")
            assert "Traceback boom" in obs


def test_no_output_at_all_fails(tmp_path):
    receipt = run(tmp_path, FakeRunner(stdout=""))
    assert receipt["passed"] is False
    assert all(o == "missing_output" for o in observed(receipt).values())


@pytest.mark.parametrize("bad", [
    proof.MARK + "{not json",
    proof.MARK + json.dumps(["honest_geometry"]),
    proof.MARK + json.dumps({"case": "other", "observed": "geometry_match"}),
    proof.MARK + json.dumps({"case": "hostile_bad_params", "observed": 7}),
])
def test_malformed_case_output_fails(tmp_path, bad):
    out = line("honest_geometry", "geometry_match") + "\n" + bad + "\n"
    receipt = run(tmp_path, FakeRunner(stdout=out))
    assert receipt["passed"] is False
    assert observed(receipt)["hostile_bad_params"] == "malformed_output"


def test_duplicate_case_output_fails(tmp_path):
    out = stdout_for(GOOD) + line("hostile_bad_params", "BAD_PARAMS") + "\n"
    receipt = run(tmp_path, FakeRunner(stdout=out))
    assert receipt["passed"] is False
    assert observed(receipt)["hostile_bad_params"] == "duplicate_output"


@pytest.mark.parametrize("results", [
    {"honest_geometry": "geometry_mismatch: {}", "hostile_bad_params": "BAD_PARAMS"},
    {"honest_geometry": "geometry_match", "hostile_bad_params": "INTERNAL"},
    {"honest_geometry": "geometry_match", "hostile_bad_params": "yielded_dxf"},
    {"honest_geometry": "prerequisites_missing: seccomp_filter",
     "hostile_bad_params": "prerequisites_missing: seccomp_filter"},
])
def test_a_mismatch_fails_the_receipt(tmp_path, results):
    receipt = run(tmp_path, FakeRunner(stdout=stdout_for(results)))
    assert receipt["passed"] is False
    assert observed(receipt) == results


# --------------------------------------------------------------------------- #
# the in-image proof program, run in-process against fakes
# --------------------------------------------------------------------------- #
class ConvertError(Exception):
    def __init__(self, error_code):
        super().__init__(error_code)
        self.error_code = error_code


def fake_modules(tmp_path, *, mismatch=False, hostile_code="BAD_PARAMS",
                 has_bin=True, has_filter=True):
    calls = []
    dxf = tmp_path / "out.dxf"
    dxf.write_text("0\nEOF\n")

    @contextlib.contextmanager
    def converted_dxf(source):
        calls.append({
            "source": Path(source).name,
            "require": os.environ.get("LEAF_DWG_CONVERT_REQUIRE_CAGE"),
            "seccomp": os.environ.get("LEAF_DWG_CONVERT_SECCOMP_FILE"),
        })
        if Path(source).name == "hostile.dwg":
            if hostile_code is None:
                yield dxf
                return
            raise ConvertError(hostile_code)
        yield dxf

    def parse_dxf_file(path, *, source_name=""):
        base = os.environ.get("LEAF_DWG_CONVERT_SECCOMP_FILE") == "/nonexistent"
        points = 3 if (mismatch and base) else 4
        return {"layers": ["Roof"], "polylines": [{"pts": [[0, 0]] * points}]}

    dwg_convert = SimpleNamespace(
        ConvertError=ConvertError,
        converted_dxf=converted_dxf,
        dwg2dxf_bin=lambda: "/usr/local/bin/dwg2dxf" if has_bin else None,
        seccomp_filter_path=lambda: "/usr/local/etc/leaf/seccomp-dwg2dxf.bpf" if has_filter else None,
    )
    dxf_intake = SimpleNamespace(parse_dxf_file=parse_dxf_file)
    return dwg_convert, dxf_intake, calls


def exec_program(monkeypatch, capsys, tmp_path, modules, which=lambda t: "/usr/bin/" + t,
                 fixture_exists=True):
    fixture = tmp_path / "rooftop_demo.dwg"
    if fixture_exists:
        fixture.write_bytes(b"AC1032")
    old = 'FIXTURE = "/app/data/rooftop_demo.dwg"'
    assert old in proof.PROOF_PROGRAM
    src = proof.PROOF_PROGRAM.replace(old, f"FIXTURE = {str(fixture)!r}")
    dwg_convert, dxf_intake, _ = modules
    monkeypatch.setitem(sys.modules, "dwg_convert", dwg_convert)
    monkeypatch.setitem(sys.modules, "dxf_intake", dxf_intake)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(shutil, "which", which)
    for name in ("LEAF_DWG_CONVERT_REQUIRE_CAGE", "LEAF_DWG2DXF_BIN",
                 "LEAF_DWG_CONVERT_SECCOMP_FILE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as done:
        exec(compile(src, "<proof-program>", "exec"), {"__name__": "__main__"})
    out = capsys.readouterr().out
    return done.value.code, proof.parse_cases(out)


def test_program_is_stdlib_python_aimed_at_the_image_paths():
    tree = ast.parse(proof.PROOF_PROGRAM)
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in (node.names if isinstance(node, ast.Import) else [SimpleNamespace(name=node.module)])}
    assert imported <= {"json", "os", "shutil", "sys", "tempfile", "pathlib",
                        "dwg_convert", "dxf_intake"}
    assert 'SERVER_DIR = "/app/server"' in proof.PROOF_PROGRAM
    assert 'FIXTURE = "/app/data/rooftop_demo.dwg"' in proof.PROOF_PROGRAM
    assert "skip" not in proof.PROOF_PROGRAM.lower()


def test_program_proves_both_cases_under_the_required_cage(monkeypatch, capsys, tmp_path):
    modules = fake_modules(tmp_path)
    code, results = exec_program(monkeypatch, capsys, tmp_path, modules)
    assert code == 0
    assert results == GOOD
    calls = modules[2]
    assert [c["source"] for c in calls] == ["rooftop_demo.dwg", "rooftop_demo.dwg", "hostile.dwg"]
    assert calls[0] == {"source": "rooftop_demo.dwg", "require": "1", "seccomp": None}
    assert calls[1] == {"source": "rooftop_demo.dwg", "require": None, "seccomp": "/nonexistent"}
    assert calls[2] == {"source": "hostile.dwg", "require": "1", "seccomp": None}


@pytest.mark.parametrize("kwargs, which, fixture_exists, missing", [
    ({"has_bin": False}, lambda t: "/usr/bin/" + t, True, "dwg2dxf"),
    ({}, lambda t: None if t == "prlimit" else "/usr/bin/" + t, True, "prlimit"),
    ({}, lambda t: None if t == "setpriv" else "/usr/bin/" + t, True, "setpriv"),
    ({"has_filter": False}, lambda t: "/usr/bin/" + t, True, "seccomp_filter"),
    ({}, lambda t: "/usr/bin/" + t, False, "fixture"),
])
def test_program_fails_rather_than_skips_on_a_missing_prerequisite(
        monkeypatch, capsys, tmp_path, kwargs, which, fixture_exists, missing):
    modules = fake_modules(tmp_path, **kwargs)
    code, results = exec_program(monkeypatch, capsys, tmp_path, modules,
                                 which=which, fixture_exists=fixture_exists)
    assert code == 3
    assert modules[2] == [], "nothing is proven before the prerequisites hold"
    for obs in results.values():
        assert obs == "prerequisites_missing: " + missing


def test_program_reports_a_geometry_mismatch(monkeypatch, capsys, tmp_path):
    code, results = exec_program(monkeypatch, capsys, tmp_path,
                                 fake_modules(tmp_path, mismatch=True))
    assert code == 1
    assert results["honest_geometry"].startswith("geometry_mismatch: ")
    assert results["hostile_bad_params"] == "BAD_PARAMS"


@pytest.mark.parametrize("hostile_code, expected", [
    ("INTERNAL", "INTERNAL"),
    (None, "yielded_dxf"),
])
def test_program_reports_a_hostile_case_that_is_not_bad_params(
        monkeypatch, capsys, tmp_path, hostile_code, expected):
    code, results = exec_program(monkeypatch, capsys, tmp_path,
                                 fake_modules(tmp_path, hostile_code=hostile_code))
    assert code == 1
    assert results == {"honest_geometry": "geometry_match", "hostile_bad_params": expected}
