"""Tests for scripts/solar_pile_block_mapping_evidence.py: the s6 document from a synthetic intake validates under the
comparator, carries the plugin adapter's parameters and row shapes, and refuses missing or malformed intakes."""
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent / "solar_pile_block_mapping_evidence.py"
_SPEC = importlib.util.spec_from_file_location("solar_pile_block_mapping_evidence", _PATH)
prod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prod)

REV = "c" * 40


def intake():
    eng = prod.engine
    source = {"template_name": "source-1", "local_width_m": 20.0, "local_height_m": 4.0}
    saved = eng.read_back(eng.default_template(source), source)
    other = {k: v for k, v in eng.new_template("x").items() if k != "Name"}
    return {"format": "pile-block-map-intake-v1", "units": "m", "mapping_record": None,
            "pile_store": {"active_template": "template-1", "templates": [
                {"template": "source-1", "fields": {k: v for k, v in eng.serialised(saved).items() if k != "Name"}},
                {"template": "template-1", "fields": other}]},
            "sources": [{"source": "source-1", "source_kind": "Block", "count": 3, "has_pvcase_piling": False,
                         "status": "unmapped", "local_width": {"kind": "length", "unit": "m", "value": 20.0},
                         "local_height": {"kind": "length", "unit": "m", "value": 4.0}}]}


def write_intake(tmp_path, value=None):
    (tmp_path / "s6-intake.json").write_text(json.dumps(value if value is not None else intake()), encoding="utf-8")
    return tmp_path


def test_the_document_validates_and_carries_the_adapter_shape(tmp_path):
    doc = prod.run_steps(write_intake(tmp_path), REV)["s6"]
    prod.compare.validate_evidence(doc, "exports")
    assert doc["parameters"] == {"answers": [], "form_values": {"source_row": 0, "save_mapping": 1,
                                                                "confirm_ok": 1, "close": 1}}
    assert doc["units"] == "m" and doc["after"]["source_revision"] == "s6"
    kinds = [row["type"] for row in doc["after"]["rows"]]
    assert kinds == sorted(kinds) and set(kinds) == {"pile-block-mapping", "pile-source", "report"}
    assert doc["fixture_sha256"] == prod.compare.semantic_hash(intake())


def test_main_writes_s6(tmp_path, capsys):
    out = tmp_path / "out"
    assert prod.main(["--intakes", str(write_intake(tmp_path)), "--out", str(out)]) == 0
    assert [p.name for p in out.iterdir()] == ["s6.json"]
    assert "pile-block-mapping" in capsys.readouterr().out


def test_missing_or_malformed_intakes_refuse(tmp_path):
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(tmp_path)
    bad = intake()
    bad["units"] = "ft"
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(write_intake(tmp_path, bad))
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(write_intake(tmp_path), "not-a-sha")
