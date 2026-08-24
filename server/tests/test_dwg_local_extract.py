"""
The APS-free DWG read lane (dwg2dxf -> dxf_intake) and the engine toggle's
server half: caged conversion, structured fail-closed rejections, engine
routing (per-upload field + LEAF_GUEST_DWG_EXTRACT default), the upfront
availability gate, policy advertisement, and the byte-identical APS path.

The subprocess plumbing is exercised through a STUB converter that honors the
real dwg2dxf argv contract ([bin, -y, -o, OUT, SRC]) so every mode runs on any
host; the one test that needs the REAL binary (converting the repo's real
data/rooftop_demo.dwg) skips with an allowlisted reason where dwg2dxf is not
installed and runs wherever it is (the app container ships it).

Run:  cd server && python -m pytest tests/test_dwg_local_extract.py -q
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import dwg_convert
import dxf_intake
import guest_uploads
import write_loop

# Passes the route's cheap AC1 magic sniff; hostile to any real converter.
MALFORMED_DWG = b"AC1032" + b"\x00" * 64 + b"this is not a drawing"

# What the stub "converts" every source to — coordinates nothing else in the
# repo shares (rooftop demo lives around 14000-15500).
STUB_DXF = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLWPOLYLINE\n5\nCAFE\n8\nConverted\n70\n1\n"
    "10\n771.5\n20\n882.25\n10\n993.0\n20\n104.75\n10\n555.125\n20\n616.5\n"
    "0\nENDSEC\n0\nEOF\n"
)
STUB_COORD = 771.5
ROOFTOP_COORD = 14323.816  # first polyline x of data/rooftop_demo.intake.json

ROOFTOP_DWG = Path(__file__).resolve().parents[2] / "data" / "rooftop_demo.dwg"
_REAL_DWG2DXF = shutil.which("dwg2dxf")


def _stub_converter(tmp_path: Path, mode: str = "ok") -> Path:
    """A platform-appropriate fake dwg2dxf honoring the real argv contract,
    so what's under test is the actual subprocess plumbing."""
    stub_py = tmp_path / f"stub_{mode}.py"
    stub_py.write_text(textwrap.dedent(f"""
        import sys, time
        mode = {mode!r}
        out = sys.argv[sys.argv.index("-o") + 1]
        src = sys.argv[-1]
        if mode == "fail":
            print("ERROR: Invalid DWG")
            sys.exit(1)
        if mode == "empty":
            sys.exit(0)
        if mode == "sleep":
            time.sleep(30)
            sys.exit(0)
        open(src, "rb").read()  # the staged source must exist and be readable
        with open(out, "w") as fh:
            fh.write({STUB_DXF!r})
        sys.exit(0)
    """), encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / f"stub_{mode}.bat"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{stub_py}" %*\r\n',
                           encoding="utf-8")
    else:
        wrapper = tmp_path / f"stub_{mode}.sh"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub_py}" "$@"\n',
                           encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_GUEST_DWG_EXTRACT", raising=False)
    monkeypatch.delenv("LEAF_DWG2DXF_BIN", raising=False)
    guest_uploads._reset_rate_state()
    # Deterministic tests: run extraction inline instead of a thread.
    monkeypatch.setattr(
        guest_uploads, "start_extraction_thread",
        lambda tenant_id, drawing_id, ext: guest_uploads.run_extraction(
            tenant_id, drawing_id, ext))
    return TestClient(app_module.app)


def _upload(client, data=MALFORMED_DWG, name="site.dwg", engine=None,
            headers=None):
    form = {"engine": engine} if engine else None
    return client.post("/api/drawings/upload",
                       files={"file": (name, io.BytesIO(data))},
                       data=form, headers=headers or {})


def _status(client, receipt):
    return client.get(
        f"/api/drawings/{receipt['drawing_id']}/upload-status",
        headers={"X-Tenant-Id": receipt["tenant_id"]}).json()


# --------------------------------------------------------------------------- #
# dwg_convert unit coverage (stub converter, real subprocess)
# --------------------------------------------------------------------------- #
def test_convert_success_parses_and_cleans_scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with dwg_convert.converted_dxf(src) as dxf_path:
        scratch = dxf_path.parent
        intake = dxf_intake.parse_dxf_file(dxf_path, source_name="u.dwg")
    assert intake["polylines"][0]["pts"][0][0] == STUB_COORD
    assert not scratch.exists(), "the conversion scratch dir must be deleted"


def test_convert_failure_is_a_structured_rejection(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN",
                       str(_stub_converter(tmp_path, "fail")))
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            raise AssertionError("a failed conversion must never yield")
    assert err.value.error_code == "BAD_PARAMS"
    assert err.value.retryable is False


def test_convert_empty_output_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN",
                       str(_stub_converter(tmp_path, "empty")))
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "BAD_PARAMS"


def test_convert_timeout_kills_the_child_and_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN",
                       str(_stub_converter(tmp_path, "sleep")))
    monkeypatch.setenv("LEAF_DWG_CONVERT_TIMEOUT_S", "1")
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "TIMEOUT"
    assert err.value.retryable is False


def test_convert_output_size_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    monkeypatch.setenv("LEAF_DWG_CONVERT_MAX_OUTPUT_BYTES", "16")
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "BAD_PARAMS"
    assert "output cap" in err.value.message


def test_convert_unavailable_raises_internal(monkeypatch, tmp_path):
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "INTERNAL"


@pytest.mark.skipif(_REAL_DWG2DXF is None,
                    reason="dwg2dxf binary not installed on this host")
def test_real_dwg2dxf_converts_the_repo_fixture(monkeypatch):
    """REAL producer topology: GNU dwg2dxf converting the repo's real DWG.

    Runs wherever the binary exists (the app container ships it; see
    deploy/Dockerfile.app); allowlisted skip elsewhere."""
    monkeypatch.delenv("LEAF_DWG2DXF_BIN", raising=False)
    with dwg_convert.converted_dxf(ROOFTOP_DWG) as dxf_path:
        intake = dxf_intake.parse_dxf_file(dxf_path,
                                           source_name="rooftop_demo.dwg")
    assert intake["layers"], "the real drawing must yield real layer names"
    assert intake["polylines"], "the real drawing must yield real polylines"


# --------------------------------------------------------------------------- #
# the cage: hard bounds on the native parser child
# --------------------------------------------------------------------------- #
# These bytes are attacker-controlled and unauthenticated by design, so the
# rlimit/no-new-privs cage around dwg2dxf is a SECURITY control, not a tuning
# knob. What is pinned here is the argv the child is actually launched with —
# the limits' live enforcement is a kernel property proven against the real
# binary in the container, not something a unit test can observe.
_CAGE_TOOLS_PRESENT = all(shutil.which(t) for t in ("prlimit", "setpriv"))


def _fake_cage_tools(monkeypatch, present: bool) -> None:
    """Decide cage-tool availability regardless of the host, so BOTH branches
    are covered on every platform (Linux CI has util-linux; dev macOS/Windows
    do not, and neither may silently skip its half of this contract)."""
    real_which = shutil.which
    monkeypatch.setattr(
        dwg_convert.shutil, "which",
        lambda name: (f"/usr/bin/{name}" if present else None)
        if name in ("prlimit", "setpriv") else real_which(name))


def _captured_argv(monkeypatch, tmp_path, **env):
    """The exact argv converted_dxf() hands subprocess.run, without running it."""
    seen = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        raise AssertionError("stop before exec")

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    monkeypatch.setattr(dwg_convert.subprocess, "run", _fake_run)
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(AssertionError, match="stop before exec"):
        with dwg_convert.converted_dxf(src):
            pass
    return seen["argv"]


def test_cage_wraps_the_parser_in_prlimit_and_setpriv(monkeypatch, tmp_path):
    """The child must be launched THROUGH the cage, and the cage must be spelled
    as an exec chain: prlimit and setpriv each exec their argument, so the whole
    thing stays ONE process that the wall-clock timeout can still kill."""
    _fake_cage_tools(monkeypatch, True)
    argv = _captured_argv(monkeypatch, tmp_path)

    assert argv[0] == "prlimit", f"the parser ran uncaged: {argv}"
    setpriv = argv.index("setpriv")
    assert argv[setpriv - 1] == "--", "prlimit needs its -- before the command"
    assert "--no-new-privs" in argv, (
        "a corrupted child must not be able to gain privilege via a setuid exec")
    assert "--inh-caps=-all" in argv
    # The real binary and its real argv contract survive the wrapping.
    assert argv[-4:-2] == ["-y", "-o"]
    assert argv[-1].endswith("u.dwg")


def test_cage_limits_track_their_knobs(monkeypatch, tmp_path):
    _fake_cage_tools(monkeypatch, True)
    argv = _captured_argv(
        monkeypatch, tmp_path,
        LEAF_DWG_CONVERT_MEM_BYTES="123456789",
        LEAF_DWG_CONVERT_NOFILE="7",
        LEAF_DWG_CONVERT_NPROC="3",
        LEAF_DWG_CONVERT_TIMEOUT_S="30",
        LEAF_DWG_CONVERT_MAX_OUTPUT_BYTES="1000")

    assert "--as=123456789" in argv
    assert "--nofile=7" in argv
    assert "--nproc=3" in argv
    assert "--core=0" in argv
    # CPU bound sits just above the wall-clock budget so the wall timeout, which
    # produces the honest TIMEOUT rejection, is what normally fires.
    assert "--cpu=31" in argv
    # FSIZE keeps HEADROOM over the output cap so the post-run check — and its
    # precise "output cap" message — stays reachable instead of being pre-empted
    # by a SIGXFSZ kill that reports nothing useful.
    assert "--fsize=5096" in argv


def test_cage_mem_limit_zero_disables_only_the_address_space_cap(
        monkeypatch, tmp_path):
    _fake_cage_tools(monkeypatch, True)
    argv = _captured_argv(monkeypatch, tmp_path,
                          LEAF_DWG_CONVERT_MEM_BYTES="0")
    assert not [a for a in argv if a.startswith("--as=")]
    assert "--core=0" in argv and "--nofile=64" in argv


def test_malformed_cage_knob_falls_back_to_the_safe_default(
        monkeypatch, tmp_path):
    """An operator typo must not take the upload lane down, and must not
    silently widen a bound either — every default here is the safe value."""
    _fake_cage_tools(monkeypatch, True)
    argv = _captured_argv(monkeypatch, tmp_path,
                          LEAF_DWG_CONVERT_MEM_BYTES="not-a-number",
                          LEAF_DWG_CONVERT_NOFILE="-5")
    assert f"--as={2 * 1024 * 1024 * 1024}" in argv
    assert "--nofile=64" in argv


def test_without_the_tools_the_parser_still_runs_bare(monkeypatch, tmp_path):
    """Dev hosts with no util-linux keep working — but only because they have
    NOT declared themselves caged (see the fail-closed test below)."""
    _fake_cage_tools(monkeypatch, False)
    monkeypatch.delenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", raising=False)
    argv = _captured_argv(monkeypatch, tmp_path)
    assert "prlimit" not in argv and "setpriv" not in argv
    assert argv[1:3] == ["-y", "-o"]


def test_a_deployment_that_requires_the_cage_fails_closed_without_it(
        monkeypatch, tmp_path):
    """THE load-bearing one. deploy/Dockerfile.app sets
    LEAF_DWG_CONVERT_REQUIRE_CAGE=1, so an image that lost util-linux must
    REFUSE to parse hostile bytes rather than quietly parse them bare — a
    downgrade that no health check would ever show."""
    _fake_cage_tools(monkeypatch, False)
    monkeypatch.setenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", "1")
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))

    def _never(*args, **kwargs):
        raise AssertionError("the parser must NOT run without its cage")

    monkeypatch.setattr(dwg_convert.subprocess, "run", _never)
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "INTERNAL"
    assert err.value.retryable is False, (
        "retrying hits the same image with the same missing tools")


@pytest.mark.skipif(_REAL_DWG2DXF is None or not _CAGE_TOOLS_PRESENT,
                    reason="the real dwg2dxf plus util-linux prlimit/setpriv are not installed on this host")
def test_real_caged_conversion_still_converts_the_repo_fixture(monkeypatch):
    """The cage must not cost the honest path anything. Runs the REAL parser
    under the REAL limits against the repo's real DWG."""
    monkeypatch.delenv("LEAF_DWG2DXF_BIN", raising=False)
    monkeypatch.setenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", "1")
    with dwg_convert.converted_dxf(ROOFTOP_DWG) as dxf_path:
        intake = dxf_intake.parse_dxf_file(dxf_path,
                                           source_name="rooftop_demo.dwg")
    assert intake["layers"] and intake["polylines"]


@pytest.mark.skipif(_REAL_DWG2DXF is None or not _CAGE_TOOLS_PRESENT,
                    reason="the real dwg2dxf plus util-linux prlimit/setpriv are not installed on this host")
def test_real_caged_conversion_refuses_hostile_bytes_cleanly(
        monkeypatch, tmp_path):
    """Hostile bytes that clear the 3-byte magic gate must produce a structured
    rejection — never a hang, and never a crash that takes the worker with it."""
    monkeypatch.delenv("LEAF_DWG2DXF_BIN", raising=False)
    monkeypatch.setenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", "1")
    src = tmp_path / "hostile.dwg"
    src.write_bytes(b"AC1032" + os.urandom(64 * 1024))
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            raise AssertionError("hostile bytes must never yield a DXF")
    assert err.value.error_code == "BAD_PARAMS"


# --------------------------------------------------------------------------- #
# the seccomp layer: syscall denylist on top of the rlimit/no-new-privs cage
# --------------------------------------------------------------------------- #
# deploy/gen_seccomp_filter.c is compiled and run inside deploy/Dockerfile.app,
# so the compiled .bpf only exists in the real app image — never on a plain
# dev host, even one with prlimit/setpriv installed. That gate is exactly what
# _SECCOMP_FILTER_PRESENT checks, kept separate from _CAGE_TOOLS_PRESENT.
_SECCOMP_FILTER_PRESENT = dwg_convert.seccomp_filter_path() is not None


def test_seccomp_filter_is_wired_into_the_setpriv_argv_when_present(
        monkeypatch, tmp_path):
    _fake_cage_tools(monkeypatch, True)
    monkeypatch.delenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", raising=False)
    bpf = tmp_path / "fake.bpf"
    bpf.write_bytes(b"\x00" * 8)  # one dummy sock_filter entry, contents unused
    argv = _captured_argv(monkeypatch, tmp_path,
                          LEAF_DWG_CONVERT_SECCOMP_FILE=str(bpf))
    assert f"--seccomp-filter={bpf}" in argv
    # It must land BEFORE the final "--" so setpriv treats it as its own flag,
    # not part of the wrapped command.
    setpriv = argv.index("setpriv")
    tail = argv.index("--", setpriv)
    assert f"--seccomp-filter={bpf}" in argv[setpriv:tail]


def test_seccomp_filter_is_omitted_when_absent(monkeypatch, tmp_path):
    """The base rlimit/no-new-privs cage must not depend on the filter being
    compiled in — a dev host with util-linux but no built image still runs
    caged, just without this one additive layer."""
    _fake_cage_tools(monkeypatch, True)
    monkeypatch.delenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", raising=False)
    monkeypatch.delenv("LEAF_DWG_CONVERT_SECCOMP_FILE", raising=False)
    monkeypatch.setattr(dwg_convert, "_DEFAULT_SECCOMP_FILTER_PATH",
                        str(tmp_path / "does-not-exist.bpf"))
    argv = _captured_argv(monkeypatch, tmp_path)
    assert not [a for a in argv if a.startswith("--seccomp-filter=")]
    assert "--no-new-privs" in argv, "the rest of the cage is unaffected"


def test_a_deployment_that_requires_the_cage_fails_closed_without_its_seccomp_filter(
        monkeypatch, tmp_path):
    """THE seccomp half of the load-bearing fail-closed test above: prlimit
    and setpriv being present is not enough once the deployment declared
    itself caged (LEAF_DWG_CONVERT_REQUIRE_CAGE=1) — a build that lost JUST
    the compiled filter must refuse rather than parse hostile bytes one
    defense layer thinner with no health check able to see the downgrade."""
    _fake_cage_tools(monkeypatch, True)
    monkeypatch.delenv("LEAF_DWG_CONVERT_SECCOMP_FILE", raising=False)
    monkeypatch.setattr(dwg_convert, "_DEFAULT_SECCOMP_FILTER_PATH",
                        str(tmp_path / "does-not-exist.bpf"))
    monkeypatch.setenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", "1")
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))

    def _never(*args, **kwargs):
        raise AssertionError("the parser must NOT run without its seccomp filter")

    monkeypatch.setattr(dwg_convert.subprocess, "run", _never)
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "INTERNAL"
    assert err.value.retryable is False


@pytest.mark.skipif(
    _REAL_DWG2DXF is None or not _CAGE_TOOLS_PRESENT or not _SECCOMP_FILTER_PRESENT,
    reason="needs the real app image: dwg2dxf, prlimit/setpriv, AND the "
           "compiled seccomp filter deploy/Dockerfile.app bakes in")
def test_real_seccomp_caged_conversion_matches_the_uncaged_parse(monkeypatch):
    """The filter must not cost the honest path a single byte of intake.
    Converts the repo's real DWG twice — once through the full seccomp+rlimit
    cage, once through the base cage with the seccomp layer explicitly
    disabled — and asserts identical layers, polyline count, and total point
    count, so the comparison is self-verifying rather than pinned to numbers
    this test cannot independently confirm."""
    monkeypatch.delenv("LEAF_DWG2DXF_BIN", raising=False)
    monkeypatch.setenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", "1")
    with dwg_convert.converted_dxf(ROOFTOP_DWG) as dxf_path:
        seccomp_intake = dxf_intake.parse_dxf_file(
            dxf_path, source_name="rooftop_demo.dwg")

    # REQUIRE_CAGE off for this leg: with the filter pointed at a path that
    # does not exist, REQUIRE_CAGE=1 would correctly fail closed (see the
    # test above) rather than run degraded — that refusal is the point of
    # this whole change, so proving the seccomp layer is a no-op for the
    # honest path has to ask for the base cage explicitly, not lean on a
    # combination the deployment itself would refuse to run.
    monkeypatch.delenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", raising=False)
    monkeypatch.setenv("LEAF_DWG_CONVERT_SECCOMP_FILE", "/nonexistent")
    with dwg_convert.converted_dxf(ROOFTOP_DWG) as dxf_path:
        base_intake = dxf_intake.parse_dxf_file(
            dxf_path, source_name="rooftop_demo.dwg")

    assert seccomp_intake["layers"] == base_intake["layers"]
    assert len(seccomp_intake["polylines"]) == len(base_intake["polylines"])
    seccomp_pts = sum(len(p["pts"]) for p in seccomp_intake["polylines"])
    base_pts = sum(len(p["pts"]) for p in base_intake["polylines"])
    assert seccomp_pts == base_pts
    assert seccomp_intake["layers"], "the real drawing must yield real layer names"
    assert seccomp_intake["polylines"], "the real drawing must yield real polylines"


@pytest.mark.skipif(
    _REAL_DWG2DXF is None or not _CAGE_TOOLS_PRESENT or not _SECCOMP_FILTER_PRESENT,
    reason="needs the real app image: dwg2dxf, prlimit/setpriv, AND the "
           "compiled seccomp filter deploy/Dockerfile.app bakes in")
def test_real_seccomp_caged_conversion_refuses_hostile_bytes_cleanly(
        monkeypatch, tmp_path):
    """Same guarantee as the base-cage version above, under the seccomp
    filter too: a structured rejection, never a hang, never a container
    death (SIGSYS would kill dwg2dxf's process, not the app — this proves
    the caller still sees a clean ConvertError either way)."""
    monkeypatch.delenv("LEAF_DWG2DXF_BIN", raising=False)
    monkeypatch.setenv("LEAF_DWG_CONVERT_REQUIRE_CAGE", "1")
    src = tmp_path / "hostile.dwg"
    src.write_bytes(b"AC1032" + os.urandom(64 * 1024))
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            raise AssertionError("hostile bytes must never yield a DXF")
    assert err.value.error_code == "BAD_PARAMS"


# --------------------------------------------------------------------------- #
# engine resolution
# --------------------------------------------------------------------------- #
def test_engine_default_auto_tracks_converter_availability(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAF_GUEST_DWG_EXTRACT", raising=False)
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    assert guest_uploads.dwg_extract_mode() == "local"
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    assert guest_uploads.dwg_extract_mode() == "aps"
    # An explicit value wins regardless of availability; a typo resolves auto.
    monkeypatch.setenv("LEAF_GUEST_DWG_EXTRACT", "aps")
    assert guest_uploads.dwg_extract_mode() == "aps"
    monkeypatch.setenv("LEAF_GUEST_DWG_EXTRACT", "lokal")
    assert guest_uploads.dwg_extract_mode() == "aps"  # auto: no converter


# --------------------------------------------------------------------------- #
# end-to-end through the route (stub converter)
# --------------------------------------------------------------------------- #
def test_dwg_local_upload_serves_converted_geometry(client, monkeypatch, tmp_path):
    """Acceptance 1's shape: a .dwg upload on the Local engine lands the SAME
    intake surface a native DXF upload lands — status ready, geometry from the
    conversion of their bytes, never the cached rooftop demo."""
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    r = _upload(client, engine="local")
    assert r.status_code == 202, r.text
    receipt = r.json()
    assert receipt["status"] == "extracting"
    assert _status(client, receipt)["status"] == "ready"

    tenant, did = receipt["tenant_id"], receipt["drawing_id"]
    marker = guest_uploads.read_marker(
        write_loop.upload_backend_for_tenant(tenant), tenant, did)
    assert marker["extract_engine"] == "local"

    i = client.get(f"/api/drawings/{did}/intake",
                   headers={"X-Tenant-Id": tenant})
    assert i.status_code == 200
    intake = i.json()["intake"]
    coords = [c for p in intake["polylines"] for pt in p["pts"] for c in pt]
    assert STUB_COORD in coords, "the converted geometry must be served"
    assert ROOFTOP_COORD not in coords, "the demo intake must NEVER leak in"

    # The v1 version blob still holds the RAW DWG bytes (what a later live
    # write signs and sends to APS as HostDwg) — conversion feeds the intake
    # cache only, exactly like the broker path.
    import store
    backend = write_loop.upload_backend_for_tenant(tenant)
    manifest = json.loads(
        backend.get(store.manifest_key(tenant, did)).decode("utf-8"))
    assert manifest["head"] == 1
    assert backend.get(
        store.drawing_version_key(tenant, did, 1)) == MALFORMED_DWG


def test_dwg_local_malformed_fails_closed_process_healthy(
        client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN",
                       str(_stub_converter(tmp_path, "fail")))
    r = _upload(client, engine="local")
    assert r.status_code == 202
    view = _status(client, r.json())
    assert view["status"] == "failed"
    assert view["error"]["error_code"] == "BAD_PARAMS"
    assert view["error"]["retryable"] is False

    # Fail CLOSED means the drawing never became readable...
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    i = client.get(f"/api/drawings/{did}/intake",
                   headers={"X-Tenant-Id": tenant})
    assert i.status_code == 404

    # ...and the process stays healthy: the next upload works end to end.
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    r2 = _upload(client, data=MALFORMED_DWG + b"2", engine="local")
    assert r2.status_code == 202
    assert _status(client, r2.json())["status"] == "ready"


def test_dwg_aps_engine_is_byte_identical_at_aps_live_0(client, monkeypatch, tmp_path):
    """Acceptance 2: the APS side of the toggle behaves exactly like today —
    even when the local converter is present and would have worked."""
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    r = _upload(client, engine="aps")
    assert r.status_code == 202
    view = _status(client, r.json())
    assert view["status"] == "failed"
    assert view["error"] == {
        "error_code": "APS_UNAVAILABLE",
        "message": "DWG extraction requires the live APS path; "
                   "upload a DXF to try the local demo",
        "retryable": False,
    }


def test_dwg_default_engine_used_when_field_absent(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    r = _upload(client)  # no engine field: auto resolves local (stub present)
    assert r.status_code == 202
    assert _status(client, r.json())["status"] == "ready"
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    marker = guest_uploads.read_marker(
        write_loop.upload_backend_for_tenant(tenant), tenant, did)
    assert marker["extract_engine"] == "local"


def test_same_bytes_other_engine_is_a_new_drawing_not_a_dedupe_hit(
        client, monkeypatch, tmp_path):
    """sol-critic #552 round-1 RED: a content-dedupe hit must never silently
    override the visible toggle. Same bytes + same engine recover the same
    receipt; same bytes on the OTHER engine are a DIFFERENT drawing whose own
    engine really runs."""
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    first = _upload(client, engine="local").json()
    tenant = first["tenant_id"]
    assert _status(client, first)["status"] == "ready"

    # Same bytes + same engine: the SAME drawing (idempotent recovery intact).
    again = _upload(client, engine="local",
                    headers={"X-Tenant-Id": tenant}).json()
    assert again["drawing_id"] == first["drawing_id"]

    # Same bytes + APS engine: a NEW drawing that really runs the APS branch
    # (honest APS_UNAVAILABLE at APS_LIVE=0) while the local drawing stays
    # ready and untouched.
    other = _upload(client, engine="aps",
                    headers={"X-Tenant-Id": tenant}).json()
    assert other["drawing_id"] != first["drawing_id"]
    aps_view = _status(client, other)
    assert aps_view["status"] == "failed"
    assert aps_view["error"]["error_code"] == "APS_UNAVAILABLE"
    assert _status(client, first)["status"] == "ready"

    marker = guest_uploads.read_marker(
        write_loop.upload_backend_for_tenant(tenant), tenant,
        other["drawing_id"])
    assert marker["extract_engine"] == "aps"


def test_engine_field_garbage_is_400(client):
    r = _upload(client, engine="cloud")
    assert r.status_code == 400
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert "engine" in r.json()["error"]["message"]


def test_local_engine_unavailable_is_upfront_503_and_burns_no_quota(
        client, monkeypatch):
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    r = _upload(client, engine="local")
    assert r.status_code == 503
    assert "not available" in r.json()["error"]["message"]
    with guest_uploads._RATE_LOCK:
        assert guest_uploads._RATE_STATE["total"] == 0


def test_dxf_uploads_ignore_the_dwg_engine_field(client, monkeypatch):
    """The toggle governs DWG only; a .dxf upload with the field set still
    parses locally and records no engine."""
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    dxf = ("0\nSECTION\n2\nENTITIES\n"
           "0\nLWPOLYLINE\n5\nAB\n8\nL1\n70\n0\n"
           "10\n1.5\n20\n2.5\n10\n3.5\n20\n4.5\n"
           "0\nENDSEC\n0\nEOF\n").encode("utf-8")
    r = _upload(client, data=dxf, name="mine.dxf", engine="local")
    assert r.status_code == 202
    receipt = r.json()
    assert _status(client, receipt)["status"] == "ready"
    marker = guest_uploads.read_marker(
        write_loop.upload_backend_for_tenant(receipt["tenant_id"]),
        receipt["tenant_id"], receipt["drawing_id"])
    assert marker["extract_engine"] is None


def test_policy_advertises_the_engine_toggle(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    body = client.get("/api/site/guest-upload-policy").json()
    assert body["dwg_engines"] == ["local", "aps"]
    assert body["dwg_engine_default"] == "local"
    assert body["dwg_local_ok"] is True
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    body = client.get("/api/site/guest-upload-policy").json()
    assert body["dwg_engine_default"] == "aps"
    assert body["dwg_local_ok"] is False
