"""Nine frozen V-01 rows. No test invokes a live network probe."""

import importlib.util
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


def test_row2_new_red(candidate):
    candidate[0]["candidate_proof"]["reds"].append("new.mjs:10:2 › newly broken row")
    result = cli(candidate)
    assert result.returncode == 1
    assert "03 FAIL " in line(result.stdout, 3)
    assert "newly broken row" in line(result.stdout, 3)
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


def test_row5_missing_auth_ladder(candidate):
    del candidate[0]["auth_ladder"]
    result = cli(candidate)
    assert result.returncode == 1
    assert line(result.stdout, 11).startswith("11 PENDING ")
    assert result.stderr == ""
    assert "Traceback" not in result.stdout


def test_row6_merged_door(candidate):
    candidate[0]["door_pr"]["state"] = "merged"
    result = cli(candidate)
    assert result.returncode == 1
    assert line(result.stdout, 13).startswith("13 FAIL ")


def test_row7_live_probe_error(candidate, monkeypatch, capsys):
    def unavailable(origin):
        raise urllib.error.URLError("Bearer abc eyJhbGciOi... should never print")

    monkeypatch.setattr(verifier, "probe_health_sha", unavailable)
    path = write(candidate)
    assert verifier.main(["--manifest", str(path), "--live"]) == 1
    captured = capsys.readouterr()
    assert line(captured.out, 2).startswith("02 PENDING ")
    assert "URLError" in line(captured.out, 2)
    assert "Traceback" not in captured.out + captured.err
    assert "Bearer " not in captured.out + captured.err
    assert captured.err == ""


def test_row8_refused_candidate(candidate):
    candidate[0]["candidate"] = "not a sha"
    result = cli(candidate)
    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert result.stderr.startswith("Manifest refused (")


def test_row9_secret_fields_never_print(candidate):
    candidate[0]["auth_ladder"].update({"tenant_jwt": "eyJhbGciOi...", "token": "Bearer abc"})
    result = cli(candidate)
    assert result.returncode == 0
    output = result.stdout + result.stderr
    for secret in ("eyJhbGciOi...", "Bearer abc", "eyJ", "Bearer "):
        assert secret not in output
