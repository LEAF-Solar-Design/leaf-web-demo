"""Conformance and provenance for the shared STD-9 lookup contract."""
import copy
import hashlib
import json
from pathlib import Path
import re

import pytest

import lab_honesty

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "contract/fixtures/lab/leaf-lab-honesty.v1.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_frozen_fixture_provenance():
    data = FIXTURE_PATH.read_bytes()
    assert data == (ROOT / "web/src/lab/contract/sources/leaf-lab-honesty.v1.json").read_bytes()
    sources = json.loads((FIXTURE_PATH.parent / "SOURCES.json").read_text(encoding="utf-8"))
    rows = [row for row in sources["sources"] if row["standard"] == "STD-9"]
    assert len(rows) == 1
    row = rows[0]
    assert row["repo"] == "mushy-code"
    assert re.fullmatch(r"[0-9a-f]{40}", row["head"])
    assert row["head"] == "f960f9bdd8fcd7abd19089af685d11d4049b0386"
    assert row["upstream_path"] == "contract/fixtures/leaf-lab-honesty.v1.json"
    assert row["frozen_path"] == str(FIXTURE_PATH.relative_to(ROOT)).replace("\\", "/")
    assert row["bytes"] == len(data) == 20749
    assert row["sha256"] == hashlib.sha256(data).hexdigest() == (
        "209c2558759ca90a9328d3e3400bb16bafb067c6cd6a7229376984c099ac4831")


def test_codes_follow_fixture_order():
    assert lab_honesty.HONESTY_CODES == tuple(row["code"] for row in FIXTURE["codes"])
    assert len(set(lab_honesty.HONESTY_CODES)) == 13


@pytest.mark.parametrize("row", FIXTURE["codes"], ids=lambda row: row["code"])
def test_every_code_matches_fixture(row):
    assert lab_honesty.sentence(row["code"]) == row["sentence"]
    assert lab_honesty.withholds(row["code"]) is row["withholds_result"]
    assert lab_honesty.scope_of(row["code"]) == row["scope"]


def test_required_sentences():
    assert lab_honesty.sentence("runs_on_bot") == "Runs on the bot. Not run here."
    assert lab_honesty.sentence("replay_is_direction") == "Direction only. Never evidence and never a gate."
    assert lab_honesty.sentence("browser_only_block") == (
        "Runs here as you edit. The Lab server's Run does not accept it yet.")


@pytest.mark.parametrize("lookup", [lab_honesty.sentence, lab_honesty.withholds, lab_honesty.scope_of])
def test_unknown_code(lookup):
    for code in ("nope", "__proto__", "constructor", None):
        with pytest.raises(ValueError, match="^unknown_code$"):
            lookup(code)


@pytest.mark.parametrize("invalid", ["null", "schema", "missing_codes", "not_array", "count",
                                    "duplicate", "code", "sentence", "flag"])
def test_invalid_fixture_fails_closed(monkeypatch, invalid):
    fixture = copy.deepcopy(FIXTURE)
    if invalid == "null":
        fixture = None
    elif invalid == "schema":
        fixture["schema"] = "wrong"
    elif invalid == "missing_codes":
        del fixture["codes"]
    elif invalid == "not_array":
        fixture["codes"] = {}
    elif invalid == "count":
        fixture["codes"].pop()
    elif invalid == "duplicate":
        fixture["codes"][-1] = fixture["codes"][0]
    else:
        fixture["codes"][0]["withholds_result" if invalid == "flag" else invalid] = None
    module_path = Path(lab_honesty.__file__)
    source = module_path.read_text(encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: json.dumps(fixture))
    with pytest.raises(ValueError, match="^lab_honesty_invalid$"):
        exec(compile(source, str(module_path), "exec"), {"__file__": str(module_path)})
