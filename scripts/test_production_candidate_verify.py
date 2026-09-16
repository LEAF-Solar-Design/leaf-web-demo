"""Ten frozen V-01 rows. No test invokes a live network probe."""

import importlib.util
import builtins
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error

import pytest


SCRIPT = Path(__file__).with_name("production_candidate_verify.py")
FIXTURE = SCRIPT.parent / "fixtures" / "production_candidate_manifest.json"
spec = importlib.util.spec_from_file_location("production_candidate_verify", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
REAL_HEALTH_PROBE = verifier.probe_health_sha


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    monkeypatch.setattr(verifier, "probe_main_sha", lambda: data["candidate"])
    monkeypatch.setattr(verifier, "probe_gh_open_prs", lambda candidate: 0)
    monkeypatch.setattr(verifier, "probe_health_sha", lambda origin: (
        data["staging"] if origin == verifier.STAGING else
        {"source_sha": data["served_source_sha"]}))
    return data, tmp_path / FIXTURE.name


def write(candidate):
    data, path = candidate
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def cli(candidate):
    path = write(candidate)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(path), "--no-live"],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
             "PYTHONDONTWRITEBYTECODE": "1"})


def line(output, number):
    return next(value for value in output.splitlines() if value.startswith(f"{number:02d} "))


def test_row1_full_fixture(candidate, capsys):
    result = cli(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    lines = [value for value in result.stdout.splitlines() if re.match(r"^\d\d ", value)]
    assert len(lines) == 14
    for number, value in enumerate(lines, 1):
        status = "PASS" if number < 14 else "OPERATOR"
        assert value.startswith(f"{number:02d} {status} ")
        assert " :: " in value
    for item in candidate[0]["operator_items"]:
        assert f"   - {item['id']} ({item['owner']}): {item['text']}" in result.stdout
    assert verifier.main(["--manifest", str(candidate[1]), "--no-live"]) == 0
    assert capsys.readouterr().out == result.stdout
    for section, field, value, number in (
        ("ssd2", "served", candidate[0]["ssd2"]["total"] - 1, 6),
        ("hardening", "authored_execution_production", 1, 12),
        ("ssd1", "proven", 0, 7),
    ):
        original = candidate[0][section][field]
        candidate[0][section][field] = value
        result = cli(candidate)
        assert result.returncode == 1
        assert line(result.stdout, number).startswith(f"{number:02d} FAIL ")
        candidate[0][section][field] = original


def test_row2_new_red(candidate):
    candidate[0]["candidate_proof"]["reds"].append("new.mjs:10:2 › newly broken row")
    result = cli(candidate)
    assert result.returncode == 1
    assert "03 FAIL " in line(result.stdout, 3)
    assert "newly broken row" in line(result.stdout, 3)
    assert "new_reds=1" in line(result.stdout, 3)


def test_row1_older_open_pr(candidate):
    candidate[0]["main"]["older_open_prs"] = 1
    result = cli(candidate)
    assert result.returncode == 1
    assert line(result.stdout, 1).startswith("01 FAIL ")
    assert "older_open_prs=1" in line(result.stdout, 1)


def test_row2_equal_count_replacement_red(candidate):
    data = candidate[0]
    data["candidate_proof"]["reds"] = list(data["baseline_proof"]["reds"])
    data["candidate_proof"]["reds"][0] = "replacement.mjs:10:2 › replacement broken row"
    assert len(data["candidate_proof"]["reds"]) == len(data["baseline_proof"]["reds"])
    result = cli(candidate)
    assert result.returncode == 1
    assert line(result.stdout, 3).startswith("03 FAIL ")
    assert "replacement broken row" in line(result.stdout, 3)
    assert "new_reds=1" in line(result.stdout, 3)


def test_row3_line_number_and_ansi_normalization(candidate):
    candidate[0]["candidate_proof"]["reds"] = ["\x1b[31mspec.mjs:574:3 › existing red\x1b[0m"]
    result = cli(candidate)
    assert result.returncode == 0
    assert "03 PASS " in line(result.stdout, 3)
    assert "new_reds=0" in line(result.stdout, 3)


def test_row4_plan_source_mismatch(candidate):
    candidate[0]["source_revision"] = "c" * 40
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 9)
    assert evidence.startswith("09 FAIL ")
    assert "c" * 40 in evidence and candidate[0]["candidate"] in evidence


def test_row4_plan_exit_false_is_not_zero(candidate):
    candidate[0]["exit"] = False
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 9)
    assert evidence.startswith("09 FAIL ") and "exit=False" in evidence


def test_row4_failing_start_board_row(candidate):
    candidate[0]["start_board"]["rows"] = [{"name": "fixture start", "pass": True}, {"name": "broken row", "pass": False}]
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 4)
    assert evidence.startswith("04 FAIL ") and "broken row" in evidence


def test_row5_missing_auth_ladder(candidate):
    del candidate[0]["auth_ladder"]
    result = cli(candidate)
    assert result.returncode == 1
    assert line(result.stdout, 11).startswith("11 PENDING ")
    assert result.stderr == ""
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize("state, status, code", [
    ("merged", "FAIL", 1), ("closed", "FAIL", 1), ("open", "FAIL", 1), ("draft", "PASS", 0),
])
def test_row6_merged_door(candidate, state, status, code):
    candidate[0]["door_pr"]["state"] = state
    result = cli(candidate)
    assert result.returncode == code
    assert line(result.stdout, 13).startswith(f"13 {status} ")


@pytest.mark.parametrize("failure", ["URLError", "HTTP 503", "HTTP 200", "JSONDecodeError", "401"])
def test_row7_live_probe_error(candidate, monkeypatch, capsys, failure):
    def response(request, timeout):
        assert timeout == 15
        assert request.get_header("User-agent") == verifier.USER_AGENT
        if failure == "URLError":
            raise urllib.error.URLError("Bearer abc eyJhbGciOi... should never print")
        if request.full_url.endswith("/api/identity"):
            if failure in ("HTTP 503", "401"):
                code = 503 if failure == "HTTP 503" else 401
                raise urllib.error.HTTPError(request.full_url, code, "private error", {}, None)
            body = b"not json" if failure == "JSONDecodeError" else b'{}'
        else:
            sha = (candidate[0]["candidate"] if request.full_url.startswith(verifier.STAGING)
                   else candidate[0]["served_source_sha"])
            body = json.dumps({"source_sha": sha}).encode()
        stream = io.BytesIO(body)
        stream.status = 200
        return stream

    monkeypatch.setattr(verifier, "probe_health_sha", REAL_HEALTH_PROBE)
    monkeypatch.setattr(verifier.urllib.request, "urlopen", response)
    path = write(candidate)
    assert verifier.main(["--manifest", str(path), "--live"]) == (0 if failure == "401" else 1)
    captured = capsys.readouterr()
    status = "PASS" if failure == "401" else "PENDING"
    assert line(captured.out, 2).startswith(f"02 {status} ")
    assert ("identity=401" if failure == "401" else failure) in line(captured.out, 2)
    assert "Traceback" not in captured.out + captured.err
    assert "Bearer " not in captured.out + captured.err
    assert captured.err == ""


def test_row7_live_production_sha_mismatch(candidate, monkeypatch, capsys):
    data, _ = candidate
    different = "c" * 40
    monkeypatch.setattr(verifier, "probe_health_sha", lambda origin: (
        data["staging"] if origin == verifier.STAGING else {"source_sha": different}))
    path = write(candidate)
    assert verifier.main(["--manifest", str(path), "--live"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    for number in range(1, 14):
        status = "FAIL" if number == 10 else "PASS"
        assert line(captured.out, number).startswith(f"{number:02d} {status} ")
    assert data["served_source_sha"] in line(captured.out, 10)
    assert different in line(captured.out, 10)


@pytest.mark.parametrize("invalid", ["not a sha", "z" * 40])
def test_row8_refused_candidate(candidate, invalid):
    candidate[0]["candidate"] = invalid
    result = cli(candidate)
    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert result.stderr.startswith("Manifest refused (")


@pytest.mark.parametrize("disposition", ["opaque-private-741", "HTTPS://alice:p4ss@example.invalid/receipt",
                                        "custom+TLS://alice:p4ss@example.invalid/receipt"])
def test_row9_secret_fields_never_print(candidate, disposition):
    candidate[0]["auth_ladder"].update({"tenant_jwt": "eyJhbGciOi...", "token": "Bearer abc"})
    candidate[0]["auth_ladder"].update({"tenant_bearer": "opaque-private-741", "token": "alpha-private"})
    candidate[0]["ssd5"]["disposition"] = disposition
    candidate[0]["surfaces"]["browser"]["evidence"] = "alpha-\x1b[31mprivate\x1b[0m"
    result = cli(candidate)
    assert result.returncode == 0
    output = result.stdout + result.stderr
    for secret in ("eyJhbGciOi...", "Bearer abc", "eyJ", "Bearer ",
                   "opaque-private-741", "alpha-private", "p4ss"):
        assert secret not in output
    assert line(result.stdout, 5).startswith("05 PASS ")
    assert line(result.stdout, 8).startswith("08 PASS ")


def test_row9_receipt_values_redact_earlier_evidence(candidate):
    data, path = candidate
    receipt = path.parent / "auth.json"
    receipt.write_text(json.dumps({
        "pass": True, "ladder": [401, 200, 403], "staging": {"ladder": [401, 200, 403]},
        "nested": [{"session_COOKIE": {"values": ["receipt-private-741"]}}],
    }), encoding="utf-8")
    data["auth_ladder"]["receipt"] = str(receipt)
    data["surfaces"]["browser"]["evidence"] = "receipt-private-741"
    data["ssd5"]["disposition"] = "YWJj.ZGVm.Z2hp bEaReR abc123"
    result = cli(candidate)
    assert result.returncode == 0
    assert "receipt-private-741" not in result.stdout + result.stderr
    assert "YWJj.ZGVm.Z2hp" not in result.stdout + result.stderr
    assert "abc123" not in result.stdout + result.stderr
    assert "[redacted]" in line(result.stdout, 5)


def test_row9_sensitive_key_values(candidate):
    values = {key: f"private-{key}-741" for key in ("jwt", "secret", "password", "authorization")}
    candidate[0]["auth_ladder"].update(values)
    candidate[0]["ssd5"]["disposition"] = " ".join(values.values())
    result = cli(candidate)
    assert result.returncode == 0
    for value in values.values():
        assert value not in result.stdout + result.stderr
    assert line(result.stdout, 8).endswith("SSD5=" + " ".join(["[redacted]"] * 4))


@pytest.mark.parametrize("token, disposition, replacement", [
    ("@example.invalid", "HTTPS://alice:p4ss@example.invalid/receipt", "[redacted-url]"),
    ("Bearer", "Bearer uncatalogued-private-741", "[redacted]"),
])
def test_row9_overlapping_redaction_rules(candidate, token, disposition, replacement):
    candidate[0]["auth_ladder"]["token"] = token
    candidate[0]["ssd5"]["disposition"] = disposition
    result = cli(candidate)
    assert result.returncode == 0
    output = result.stdout + result.stderr
    for value in (token, disposition, "p4ss", "uncatalogued-private-741"):
        assert value not in output
    assert line(result.stdout, 8).startswith("08 PASS ")
    assert line(result.stdout, 8).endswith("SSD5=" + replacement)


@pytest.mark.parametrize("receipt_path", [r"\\server\share\receipt.json", "//server/share/receipt.json",
                                          "HTTPS://alice:p4ss@example.invalid/receipt"])
@pytest.mark.parametrize("section, field, number", [
    ("production_plan", "receipt", 9), ("prod_smoke", "receipt", 10),
    ("auth_ladder", "receipt", 11), ("hardening", "aps_window_receipt", 12),
])
def test_row10_refused_receipt_path(candidate, monkeypatch, capsys, receipt_path, section, field, number):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("network and filesystem opens must not be used")

    monkeypatch.setattr(verifier.urllib.request, "urlopen", forbidden)
    # Exercise the in-memory report so reading the manifest and unrelated local
    # receipts cannot mask an attempted open of the refused path.
    for receipt_section in ("production_plan", "prod_smoke", "auth_ladder"):
        candidate[0].pop(receipt_section, None)
    candidate[0]["hardening"]["aps_window_receipt"] = receipt_path
    candidate[0].setdefault(section, {})
    candidate[0][section][field] = receipt_path
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert verifier.report(candidate[0], candidate[1].parent, live=False) == 1
    captured = capsys.readouterr()
    assert line(captured.out, number).startswith(f"{number:02d} PENDING ")
    assert "path refused" in line(captured.out, number)
    assert captured.err == ""
    assert calls == []
