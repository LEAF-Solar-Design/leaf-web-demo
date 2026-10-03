#!/usr/bin/env python3
"""Run the real DWG cage proofs inside a built app image and write one receipt.

server/tests/test_dwg_local_extract.py skips its two real-cage proofs on any
host without dwg2dxf, prlimit/setpriv and the compiled seccomp filter. The app
image (deploy/Dockerfile.app) ships all three plus data/rooftop_demo.dwg and
sets LEAF_DWG_CONVERT_REQUIRE_CAGE=1, so this runs the same two proofs there:

  honest_geometry     the real drawing converts identically under the full
                      seccomp cage and the base cage (layers, polyline count,
                      total point count), and yields real geometry.
  hostile_bad_params  hostile bytes under REQUIRE_CAGE=1 end in a structured
                      ConvertError with error_code BAD_PARAMS.

Missing prerequisites fail, never skip. The container runs with no network, a
300 s bound, argv only (no shell), and a stdlib proof program fed on stdin. The
only host artifact is the receipt: {image, passed, cases: [{case, expected,
observed, exit_status}]}. Exit 0 only when both cases pass.

    python scripts/proof_dwg_cage_image.py --image leaf-app:local --receipt C:/tmp/dwg-cage.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DOCKER = "docker"
TIMEOUT_S = 300
MARK = "LEAF_DWG_CAGE_CASE "
EXPECTED = {
    "honest_geometry": "geometry_match",
    "hostile_bad_params": "BAD_PARAMS",
}
MAX_OBSERVED_CHARS = 500
MAX_PARSED_LINES = 2000

# Runs inside the image as `python -B -` from WORKDIR /app/server. Stdlib only on
# the host side of the pipe; the image's own modules do the conversion. It must
# stay a single program with no triple single quotes inside.
PROOF_PROGRAM = r'''
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SERVER_DIR = "/app/server"
FIXTURE = "/app/data/rooftop_demo.dwg"
MARK = "LEAF_DWG_CAGE_CASE "
EXPECTED = {
    "honest_geometry": "geometry_match",
    "hostile_bad_params": "BAD_PARAMS",
}


def emit(case, observed):
    record = {"case": case, "observed": str(observed)[:500]}
    print(MARK + json.dumps(record, sort_keys=True), flush=True)


def caged_env():
    os.environ["LEAF_DWG_CONVERT_REQUIRE_CAGE"] = "1"
    os.environ.pop("LEAF_DWG2DXF_BIN", None)
    os.environ.pop("LEAF_DWG_CONVERT_SECCOMP_FILE", None)


def geometry(intake):
    polylines = intake.get("polylines") or []
    return {
        "layers": intake.get("layers") or [],
        "polylines": len(polylines),
        "points": sum(len(p["pts"]) for p in polylines),
    }


def honest(dwg_convert, dxf_intake):
    caged_env()
    with dwg_convert.converted_dxf(Path(FIXTURE)) as dxf_path:
        seccomp = geometry(dxf_intake.parse_dxf_file(
            dxf_path, source_name="rooftop_demo.dwg"))
    # The base-cage leg: REQUIRE_CAGE=1 with no filter correctly refuses, so
    # the comparison asks for the base cage explicitly, as the server test does.
    os.environ.pop("LEAF_DWG_CONVERT_REQUIRE_CAGE", None)
    os.environ["LEAF_DWG_CONVERT_SECCOMP_FILE"] = "/nonexistent"
    try:
        with dwg_convert.converted_dxf(Path(FIXTURE)) as dxf_path:
            base = geometry(dxf_intake.parse_dxf_file(
                dxf_path, source_name="rooftop_demo.dwg"))
    finally:
        caged_env()
    if not seccomp["layers"] or not seccomp["polylines"]:
        return "empty_geometry: " + json.dumps(seccomp, sort_keys=True)
    if seccomp != base:
        return "geometry_mismatch: " + json.dumps(
            {"seccomp": seccomp, "base": base}, sort_keys=True)
    return "geometry_match"


def hostile(dwg_convert):
    caged_env()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "hostile.dwg"
        src.write_bytes(b"AC1032" + os.urandom(64 * 1024))
        try:
            with dwg_convert.converted_dxf(src):
                return "yielded_dxf"
        except dwg_convert.ConvertError as err:
            return str(err.error_code)


def describe(err):
    code = getattr(err, "error_code", None)
    detail = code if code else err
    return "error: %s: %s" % (type(err).__name__, detail)


def main():
    sys.path.insert(0, SERVER_DIR)
    try:
        import dwg_convert
        import dxf_intake
    except Exception as err:
        for case in EXPECTED:
            emit(case, "import_error: " + describe(err))
        return 3
    caged_env()
    missing = []
    if dwg_convert.dwg2dxf_bin() is None:
        missing.append("dwg2dxf")
    for tool in ("prlimit", "setpriv"):
        if shutil.which(tool) is None:
            missing.append(tool)
    if dwg_convert.seccomp_filter_path() is None:
        missing.append("seccomp_filter")
    if not os.path.isfile(FIXTURE):
        missing.append("fixture")
    if missing:
        for case in EXPECTED:
            emit(case, "prerequisites_missing: " + ",".join(missing))
        return 3
    status = 0
    proofs = (
        ("honest_geometry", lambda: honest(dwg_convert, dxf_intake)),
        ("hostile_bad_params", lambda: hostile(dwg_convert)),
    )
    for case, proof in proofs:
        try:
            observed = proof()
        except Exception as err:
            observed = describe(err)
        emit(case, observed)
        if observed != EXPECTED[case]:
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
'''


def docker_argv(image: str) -> list[str]:
    """The exact container argv. Refuses an image that docker would read as a
    flag, so --image can never smuggle --privileged or a network back in."""
    if not isinstance(image, str) or not image.strip() or image.startswith("-") \
            or any(ch.isspace() for ch in image):
        raise ValueError(f"invalid image reference: {image!r}")
    return [DOCKER, "run", "--rm", "-i", "--network", "none",
            "--entrypoint", "python", image, "-B", "-"]


def default_runner(argv: list[str], stdin_text: str, timeout_s: int):
    """argv only, never a shell; raises TimeoutExpired past the bound."""
    done = subprocess.run(argv, input=stdin_text, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout_s, check=False, shell=False)
    return done.returncode, done.stdout, done.stderr


def _clip(text: str) -> str:
    return text[:MAX_OBSERVED_CHARS]


def parse_cases(stdout: str, stderr: str = "") -> dict[str, str]:
    """Map each expected case to what the container reported. A case with no
    line, a duplicate line, or an unattributable malformed line is a failure
    observation, never a pass."""
    seen: dict[str, str] = {}
    duplicate: set[str] = set()
    malformed = False
    for line in (stdout or "").splitlines()[:MAX_PARSED_LINES]:
        if not line.startswith(MARK):
            continue
        try:
            record = json.loads(line[len(MARK):])
        except ValueError:
            malformed = True
            continue
        case = record.get("case") if isinstance(record, dict) else None
        observed = record.get("observed") if isinstance(record, dict) else None
        if case not in EXPECTED or not isinstance(observed, str):
            malformed = True
            continue
        if case in seen:
            duplicate.add(case)
        seen[case] = observed
    tail = (stderr or "").strip()[-300:]
    results: dict[str, str] = {}
    for case in EXPECTED:
        if case in duplicate:
            results[case] = "duplicate_output"
        elif case in seen:
            results[case] = _clip(seen[case])
        else:
            reason = "malformed_output" if malformed else "missing_output"
            results[case] = _clip(f"{reason}; stderr: {tail}" if tail else reason)
    return results


def run_proofs(image: str, receipt_path, *, runner=default_runner) -> dict:
    """Run both proofs in one container and write exactly one receipt."""
    exit_status = None
    failure = None
    observed: dict[str, str] = {}
    try:
        argv = docker_argv(image)
    except ValueError as err:
        failure = f"invalid_image: {err}"
    else:
        try:
            result = runner(argv, PROOF_PROGRAM, TIMEOUT_S)
        except subprocess.TimeoutExpired:
            failure = f"timeout: container exceeded {TIMEOUT_S}s"
        except (OSError, subprocess.SubprocessError, ValueError) as err:
            failure = _clip(f"launch_error: {type(err).__name__}: {err}")
        else:
            try:
                code, stdout, stderr = result
            except (TypeError, ValueError):
                failure = "launch_error: runner returned no result"
            else:
                if isinstance(code, int) and not isinstance(code, bool):
                    exit_status = code
                    observed = parse_cases(stdout, stderr)
                else:
                    failure = "launch_error: runner returned no exit status"
    cases = []
    for case, expected in EXPECTED.items():
        cases.append({
            "case": case,
            "expected": expected,
            "observed": failure if failure else observed[case],
            "exit_status": exit_status,
        })
    passed = all(c["observed"] == c["expected"] and c["exit_status"] == 0
                 for c in cases)
    receipt = {"image": image, "passed": passed, "cases": cases}
    Path(receipt_path).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")
    return receipt


def main(argv=None, *, runner=default_runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True, help="built app image reference")
    parser.add_argument("--receipt", required=True, help="path of the JSON receipt to write")
    args = parser.parse_args(argv)
    receipt = run_proofs(args.image, args.receipt, runner=runner)
    for case in receipt["cases"]:
        verdict = "PASS" if case["observed"] == case["expected"] and case["exit_status"] == 0 else "FAIL"
        print(f"{verdict} {case['case']}: expected {case['expected']}, observed "
              f"{case['observed']}, exit {case['exit_status']}")
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
