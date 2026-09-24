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


@pytest.fixture
def active_candidate(candidate):
    candidate[0]["hardening"].update({
        "authored_execution_staging": 1,
        "staging_authored_activation": {
            "tool_sandbox_provider": "e2b",
            "customization_r5_mode": "all",
            "customization_r6_mode": "all",
            "customization_store": "postgres",
            "e2b_api_key_secret": True,
            "tenant_cap_usd": 10,
        },
    })
    return candidate


@pytest.mark.parametrize("cap", [10, 2.5])
def test_row12_active_authored_posture(active_candidate, cap):
    active_candidate[0]["hardening"]["staging_authored_activation"]["tenant_cap_usd"] = cap
    result = cli(active_candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = line(result.stdout, 12)
    assert evidence.startswith("12 PASS ")
    assert f"authored staging=active (e2b, r5 all, r6 all, postgres, cap {cap}) production=0" in evidence


@pytest.mark.parametrize("activation", [None, [], "active"])
def test_row12_missing_or_invalid_activation(active_candidate, activation):
    hardening = active_candidate[0]["hardening"]
    if activation is None:
        del hardening["staging_authored_activation"]
    else:
        hardening["staging_authored_activation"] = activation
    result = cli(active_candidate)
    assert result.returncode == 1
    assert line(result.stdout, 12).startswith("12 FAIL ")


@pytest.mark.parametrize("field, value", [
    ("tool_sandbox_provider", "local"),
    ("customization_r5_mode", "off"),
    ("customization_r6_mode", "off"),
    ("customization_store", "memory"),
    ("e2b_api_key_secret", False),
    ("e2b_api_key_secret", 1),
    ("tenant_cap_usd", 0),
    ("tenant_cap_usd", -1),
    ("tenant_cap_usd", "10"),
    ("tenant_cap_usd", True),
])
def test_row12_broken_activation_member(active_candidate, field, value):
    active_candidate[0]["hardening"]["staging_authored_activation"][field] = value
    result = cli(active_candidate)
    assert result.returncode == 1
    assert line(result.stdout, 12).startswith("12 FAIL ")


@pytest.mark.parametrize("field", [
    "tool_sandbox_provider", "customization_r5_mode", "customization_r6_mode",
    "customization_store", "e2b_api_key_secret", "tenant_cap_usd",
])
def test_row12_missing_activation_member(active_candidate, field):
    del active_candidate[0]["hardening"]["staging_authored_activation"][field]
    result = cli(active_candidate)
    assert result.returncode == 1
    assert line(result.stdout, 12).startswith("12 FAIL ")


def test_row12_idle_authored_posture(candidate):
    candidate[0]["hardening"]["authored_execution_staging"] = 0
    candidate[0]["hardening"].pop("staging_authored_activation", None)
    result = cli(candidate)
    assert result.returncode == 0
    evidence = line(result.stdout, 12)
    assert evidence.startswith("12 PASS ")
    assert "authored staging=idle production=0" in evidence


@pytest.mark.parametrize("staging", ["0", None, 2, False, True])
def test_row12_invalid_staging_value(active_candidate, staging):
    active_candidate[0]["hardening"]["authored_execution_staging"] = staging
    result = cli(active_candidate)
    assert result.returncode == 1
    assert line(result.stdout, 12).startswith("12 FAIL ")


def test_row2_identity_probe_path(monkeypatch):
    urls = []
    sha = "a" * 40

    def get_json(url):
        urls.append(url)
        if url == verifier.STAGING + "/api/health":
            return {"source_sha": sha}
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(verifier, "get_json", get_json)
    assert verifier.probe_health_sha(verifier.STAGING) == {
        "source_sha": sha, "identity_status": 401}
    assert urls == [verifier.STAGING + "/api/health",
                    verifier.STAGING + "/api/deployment-identity"]


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


def test_row1_live_count_skips_drafts(monkeypatch):
    candidate = "a" * 40

    def command(*args):
        if args[0:2] == ("gh", "pr"):
            assert args == ("gh", "pr", "list", "--state", "open", "--json",
                            "number,createdAt,isDraft", "--limit", "100")
            return json.dumps([
                {"number": 1, "createdAt": "2026-09-16T19:00:00Z", "isDraft": True},
                {"number": 2, "createdAt": "2026-09-16T20:00:00Z", "isDraft": False},
                {"number": 3, "createdAt": "2026-09-16T22:00:00Z", "isDraft": False},
            ])
        if args[0:2] == ("git", "show"):
            assert args == ("git", "show", "-s", "--format=%cI", candidate)
            return "2026-09-16T21:00:00Z"
        raise AssertionError(args)

    monkeypatch.setattr(verifier, "command", command)
    monkeypatch.setattr(verifier.shutil, "which", lambda name: "gh")
    assert verifier.probe_gh_open_prs(candidate) == 1


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
        if request.full_url.endswith("/api/deployment-identity"):
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
    ("door_pr", "deploy_receipt", 13),
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
    candidate[0]["door_pr"]["deploy_receipt"] = receipt_path
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


def test_e07_completed_door_with_target_and_receipt_passes(candidate):
    data, path = candidate
    result = cli(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = line(result.stdout, 13)
    assert evidence.startswith("13 PASS ")
    assert "state=completed" in evidence and "merged_sha=" + "d" * 40 in evidence
    receipt = path.parent / "door-receipt.json"
    receipt.write_text(json.dumps({"target": data["door_pr"]["target"]}), encoding="utf-8")
    data["door_pr"]["deploy_receipt"] = str(receipt)
    result = cli(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    assert line(result.stdout, 13).startswith("13 PASS ")


@pytest.mark.parametrize("deploy_receipt", [None, "", "absent-door-receipt.json"])
def test_e07_completed_door_without_receipt_fails(candidate, deploy_receipt):
    if deploy_receipt is None:
        del candidate[0]["door_pr"]["deploy_receipt"]
    else:
        candidate[0]["door_pr"]["deploy_receipt"] = deploy_receipt
    result = cli(candidate)
    assert result.returncode == 1
    assert line(result.stdout, 13).startswith("13 FAIL ")


@pytest.mark.parametrize("merged_sha", ["D" * 40, "d" * 39, "g" * 40, 7, None])
def test_e07_completed_door_bad_merged_sha_fails(candidate, merged_sha):
    candidate[0]["door_pr"]["merged_sha"] = merged_sha
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 13)
    assert evidence.startswith("13 FAIL ") and "state=completed" in evidence


@pytest.mark.parametrize("body", [{"target": "platform-staging.leafdesign.ai"}, ["not", "an", "object"], "not json"])
def test_e07_completed_door_receipt_target_mismatch_fails(candidate, body):
    data, path = candidate
    receipt = path.parent / "door-receipt.json"
    receipt.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    data["door_pr"]["deploy_receipt"] = str(receipt)
    result = cli(candidate)
    assert result.returncode == 1
    assert line(result.stdout, 13).startswith("13 FAIL ")


def test_e07_draft_door_still_passes(candidate):
    candidate[0]["door_pr"] = {"number": 103, "state": "draft", "rollback": "restore prior production pin"}
    result = cli(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = line(result.stdout, 13)
    assert evidence.startswith("13 PASS ") and "state=draft" in evidence


def proof_row(candidate, side, name, status):
    for row in candidate[0][side]["rows"]:
        if row["title"].endswith(name):
            row["status"] = status


def test_e07_proof_new_skip_fails(candidate):
    proof_row(candidate, "candidate_proof", "fixture green", "skipped")
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 FAIL ")
    assert "new_skipped=1" in evidence and "fixture green" in evidence


def test_e07_proof_missing_row_fails(candidate):
    rows = candidate[0]["candidate_proof"]["rows"]
    rows[:] = [row for row in rows if not row["title"].endswith("fixture green")]
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 FAIL ")
    assert "missing=1" in evidence and "fixture green" in evidence


def test_e07_proof_replacement_red_fails(candidate):
    # Rows only, so the row comparison alone must catch the equal-count swap.
    for side in ("baseline_proof", "candidate_proof"):
        del candidate[0][side]["reds"]
    proof_row(candidate, "candidate_proof", "existing red", "passed")
    candidate[0]["candidate_proof"]["rows"].append(
        {"title": "spec.mjs:700:3 › replacement red", "status": "failed"})
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 FAIL ")
    assert "new_reds=0" in evidence and "new_failed=1" in evidence
    assert "replacement red" in evidence


def test_e07_proof_stale_sha_fails(candidate):
    candidate[0]["candidate_proof"]["source_sha"] = "b" * 40
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 FAIL ") and "stale proof: " + "b" * 40 in evidence
    del candidate[0]["baseline_proof"]["rows"]
    result = cli(candidate)
    assert line(result.stdout, 3).startswith("03 FAIL ")


def test_e07_proof_identical_rows_pass(candidate):
    result = cli(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 PASS ")
    assert "new_reds=0; new_failed=0; new_skipped=0; missing=0" in evidence
    assert "legacy reds-only" not in evidence
    del candidate[0]["baseline_proof"]["rows"]
    result = cli(candidate)
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 PASS ") and "legacy reds-only" in evidence


def test_e07_proof_flaky_counts_as_passed(candidate):
    proof_row(candidate, "candidate_proof", "fixture green", "flaky")
    result = cli(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    assert line(result.stdout, 3).startswith("03 PASS ")
    proof_row(candidate, "baseline_proof", "fixture green", "flaky")
    proof_row(candidate, "candidate_proof", "fixture green", "skipped")
    result = cli(candidate)
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 FAIL ") and "new_skipped=1" in evidence


def playwright_title(key, directory="e2e\\local\\", position="3203:3", separator="›"):
    """A row key as Playwright's reporter writes it: a path, :line:col and U+203A separators."""
    spec_file, rest = key.split(" > ", 1)
    return f"{directory}{spec_file}:{position} {separator} " + rest.replace(" > ", f" {separator} ")


def retire(candidate, retired, replacement=None, status="passed"):
    """The retired row passed in the baseline and is gone from the candidate proof."""
    candidate[0]["baseline_proof"]["rows"].append({"title": playwright_title(retired), "status": "passed"})
    if replacement is not None:
        candidate[0]["candidate_proof"]["rows"].append(
            {"title": playwright_title(replacement, directory="e2e/local/", position="3150:3"),
             "status": status})


@pytest.mark.parametrize("retired", sorted(verifier.W7_RETIRED_PROOF_ROWS))
def test_w7_retired_row_missing_with_its_replacement_passed_passes(candidate, retired):
    retire(candidate, retired, verifier.W7_RETIRED_PROOF_ROWS[retired])
    result = cli(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 PASS ") and "missing=0" in evidence


def test_w7_retired_row_missing_without_its_replacement_fails(candidate):
    retired = sorted(verifier.W7_RETIRED_PROOF_ROWS)[0]
    retire(candidate, retired)
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 FAIL ") and "missing=1" in evidence
    assert retired.rsplit(" > ", 1)[1] in evidence


def test_w7_retired_row_missing_with_its_replacement_failed_fails(candidate):
    retired = sorted(verifier.W7_RETIRED_PROOF_ROWS)[0]
    retire(candidate, retired, verifier.W7_RETIRED_PROOF_ROWS[retired], status="failed")
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 FAIL ") and "missing=1" in evidence and "new_failed=1" in evidence
    assert retired.rsplit(" > ", 1)[1] in evidence


def test_w7_unrelated_missing_row_still_fails_beside_an_excused_one(candidate):
    retired = sorted(verifier.W7_RETIRED_PROOF_ROWS)[0]
    retire(candidate, retired, verifier.W7_RETIRED_PROOF_ROWS[retired])
    rows = candidate[0]["candidate_proof"]["rows"]
    rows[:] = [row for row in rows if not row["title"].endswith("fixture green")]
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 3)
    assert evidence.startswith("03 FAIL ") and "missing=1" in evidence
    assert "fixture green" in evidence
    assert retired.rsplit(" > ", 1)[1] not in evidence


@pytest.mark.parametrize("raw", [
    "e2e\\local\\one-shell-mount.spec.mjs:3289:3 › route matrix, rail ON › a row",
    "e2e/local/one-shell-mount.spec.mjs ΓÇ║ route matrix, rail ON ΓÇ║ a row",
    "e2e/local/one-shell-mount.spec.mjs:3289:3 ΓÇ║ route matrix, rail ON ΓÇ║ a row",
    "\x1b[31mweb\\e2e/local\\one-shell-mount.spec.mjs:12:7 › route matrix, rail ON › a row\x1b[0m",
    "one-shell-mount.spec.mjs > route matrix, rail ON > a row",
])
def test_w7_proof_row_key_normalizes_paths_positions_and_separators(raw):
    assert verifier.proof_row_key(raw) == "one-shell-mount.spec.mjs > route matrix, rail ON > a row"


def test_w7_proof_row_key_keeps_arrows_inside_a_title():
    raw = "e2e/local/continuity-cross-scene.spec.mjs:65:3 › /try -> /app -> /try keeps ONE"
    assert verifier.proof_row_key(raw) == "continuity-cross-scene.spec.mjs > /try -> /app -> /try keeps ONE"


def test_w7_retired_table_shape():
    table = verifier.W7_RETIRED_PROOF_ROWS
    assert len(table) == 5
    for key, replacement in table.items():
        for value in (key, replacement):
            assert value.isascii(), value
            assert re.match(r"[a-z0-9-]+\.spec\.mjs > \S", value), value
            assert verifier.proof_row_key(value) == value
        assert replacement not in table, replacement
        assert key.split(" > ", 1)[0] == replacement.split(" > ", 1)[0]
    assert verifier.W7_LEGACY_FLAG_ROW in table.values()


def test_e07_smoke_all_required_rows_pass(candidate):
    assert [row["name"] for row in candidate[0]["rows"]] == list(verifier.PROD_SMOKE_ROWS)
    result = cli(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = line(result.stdout, 10)
    assert evidence.startswith("10 PASS ") and "required_rows=8/8 passed" in evidence


@pytest.mark.parametrize("name", ["production app renders the studio shell",
                                  "production deployment identity answers without a bearer"])
def test_e07_smoke_missing_required_row_fails(candidate, name):
    candidate[0]["rows"] = [row for row in candidate[0]["rows"] if row["name"] != name]
    candidate[0]["rows"].append({"name": "an unrelated extra row", "status": "passed"})
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 10)
    assert evidence.startswith("10 FAIL ") and f"{name}=missing" in evidence


LEGACY_FLAG_ROW = "production runtime flags turn oneShell on"
STUDIO_ROW = "production app renders the studio shell"


def test_e07_smoke_legacy_flag_row_is_not_required():
    assert LEGACY_FLAG_ROW not in verifier.PROD_SMOKE_ROWS
    assert STUDIO_ROW in verifier.PROD_SMOKE_ROWS


def test_e07_smoke_legacy_flag_row_does_not_satisfy_the_studio_row(candidate):
    candidate[0]["rows"] = [
        {"name": LEGACY_FLAG_ROW, "status": "passed"} if row["name"] == STUDIO_ROW else row
        for row in candidate[0]["rows"]
    ]
    assert STUDIO_ROW not in [row["name"] for row in candidate[0]["rows"]]
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 10)
    assert evidence.startswith("10 FAIL ") and f"{STUDIO_ROW}=missing" in evidence


@pytest.mark.parametrize("statuses", [["skipped"], ["passed", "skipped"]])
def test_e07_smoke_skipped_required_row_fails(candidate, statuses):
    name = verifier.PROD_SMOKE_ROWS[1]
    candidate[0]["rows"] = [row for row in candidate[0]["rows"] if row["name"] != name]
    candidate[0]["rows"].extend({"name": name, "status": status} for status in statuses)
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 10)
    assert evidence.startswith("10 FAIL ") and f"{name}={'/'.join(statuses)}" in evidence


def test_e07_smoke_baseline_sha_is_pending(candidate):
    candidate[0]["served_source_sha"] = "b" * 40
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 10)
    assert evidence.startswith("10 PENDING ") and "baseline smoke of " + "b" * 40 in evidence


def test_e07_smoke_legacy_count_receipt_is_pending(candidate):
    del candidate[0]["rows"]
    candidate[0]["result"] = "7 passed in fixture"
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 10)
    assert evidence.startswith("10 PENDING ") and "legacy count receipt; rows required" in evidence


def frozen(candidate, monkeypatch, mode, ancestor, code):
    data, path = candidate
    drift = "e" * 40
    data["main"].update({"sha": drift, "frozen": True, "candidate_is_ancestor": ancestor,
                         "drift_commits": 3})
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert args == ["git", "merge-base", "--is-ancestor", data["candidate"], drift]
        return type("Completed", (), {"returncode": code})()

    monkeypatch.setattr(verifier, "probe_main_sha", lambda: drift)
    monkeypatch.setattr(verifier.subprocess, "run", run)
    path = write(candidate)
    exit_code = verifier.main(["--manifest", str(path), "--live" if mode == "live" else "--no-live"])
    return exit_code, calls


@pytest.mark.parametrize("mode", ["offline", "live"])
def test_e07_frozen_candidate_ancestor_passes(candidate, monkeypatch, capsys, mode):
    # Live ignores the manifest's ancestry claim and asks git.
    exit_code, calls = frozen(candidate, monkeypatch, mode, mode == "offline", 0)
    out = capsys.readouterr().out
    assert exit_code == 0, out
    evidence = line(out, 1)
    assert evidence.startswith("01 PASS ")
    assert "frozen candidate; main moved" in evidence and "drift_commits=3" in evidence
    assert len(calls) == (1 if mode == "live" else 0)


@pytest.mark.parametrize("mode, code, status", [
    ("offline", 0, "FAIL"), ("live", 1, "FAIL"), ("live", 128, "PENDING"),
])
def test_e07_frozen_candidate_not_ancestor_fails(candidate, monkeypatch, capsys, mode, code, status):
    exit_code, _ = frozen(candidate, monkeypatch, mode, mode == "live", code)
    out = capsys.readouterr().out
    assert exit_code == 1
    evidence = line(out, 1)
    assert evidence.startswith(f"01 {status} ") and "frozen candidate; main moved" in evidence


@pytest.mark.parametrize("flag", ["absent", False, 1, "true"])
def test_e07_unfrozen_drift_still_fails(candidate, flag):
    candidate[0]["main"].update({"sha": "e" * 40, "candidate_is_ancestor": True})
    if flag != "absent":
        candidate[0]["main"]["frozen"] = flag
    result = cli(candidate)
    assert result.returncode == 1
    evidence = line(result.stdout, 1)
    assert evidence.startswith("01 FAIL ") and "frozen candidate" not in evidence
