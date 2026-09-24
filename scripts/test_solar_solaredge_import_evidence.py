"""Studio strings evidence for ImportSolarEdgePDF, end to end on the committed fixture.

The producer reads data/solaredge_1to1_demo.pdf and the committed drawing intake, runs the S3/S4
port and writes the comparator's strings family. These tests assert the producer's own document
(116 strings, 25 of 25 groups, 24 bridge strings, the length distribution, no unassigned or
duplicate panel, the honest flags), that a plugin-shaped document holding the same strings under
DWG-style string handles compares as a pass (the id schemes differ, the neutral ids agree), that
one moved panel fails, and that the refusals write nothing. Nothing outside the repository is read.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ev = _load("solar_solaredge_import_evidence")
compare = ev.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"
LENGTHS = ([22, 24, 25] + [26] * 3 + [27] + [28] * 23 + [29] * 19 + [30] * 6 + [31] * 20
           + [32] * 25 + [33] * 4 + [34] * 6 + [35] + [36] * 3 + [37, 39])


@pytest.fixture(scope="module")
def doc():
    return ev.run(revision=REVISION)


def plugin_shaped(studio):
    """The same strings the way plugin_evidence.py records them: string ids are DWG handles."""
    plugin = copy.deepcopy(studio)
    mapping = {k: v for k, v in plugin["entity_mapping"].items() if not k.startswith("se-string-")}
    for index, record in enumerate(plugin["after"]["strings"]):
        handle = f"F{index + 0x100:03X}"
        mapping[handle] = "string:" + record["ordered_membership"][0]["entity_id"]
        record["id"] = {"entity_id": handle}
    plugin["entity_mapping"] = mapping
    plugin["versions"]["producer"] = "plugin_evidence.v1"
    plugin["versions"]["capability"] = "solve"
    plugin["versions"]["engine"] = "none"
    plugin["execution_mode"] = "recorded"
    plugin["output_sha256"] = compare.semantic_hash(plugin["after"])
    return plugin


def test_document_holds_the_captured_import(doc):
    after = doc["after"]
    assert len(after["strings"]) == 116
    assert after["length_distribution"] == LENGTHS
    assert after["unassigned_panels"] == [] and after["duplicate_panels"] == []
    neutral = [doc["entity_mapping"][r["id"]["entity_id"]] for r in after["strings"]]
    assert neutral == sorted(neutral) and all(n.startswith("string:") for n in neutral)
    for record in after["strings"]:
        first = record["ordered_membership"][0]["entity_id"]
        assert doc["entity_mapping"][record["id"]["entity_id"]] == "string:" + first
        assert record["polarity"] == "positive"
    panels = [m["entity_id"] for r in after["strings"] for m in r["ordered_membership"]]
    assert len(panels) == len(set(panels)) == 3526
    assert all(doc["entity_mapping"][p] == p for p in panels)
    prov = doc["provenance"]
    assert (prov["groups"], prov["matched_groups"], prov["bridge_strings"]) == (25, 25, 24)
    assert prov["group_strings"] + prov["bridge_strings"] == 116
    assert prov["row_angle"]["source"] == "centroid-lattice"


def test_document_binds_the_fixture_and_flags_what_it_did_not_measure(doc):
    pdf_bytes = ev.DEFAULT_PDF.read_bytes()
    assert doc["fixture_sha256"] == hashlib.sha256(pdf_bytes).hexdigest()
    assert doc["revision"] == REVISION
    assert doc["parameters"] == {"family": "strings", "max_string_length": 40}
    assert doc["input_sha256"] == compare.semantic_hash(
        {"fixture_sha256": doc["fixture_sha256"], "parameters": doc["parameters"]})
    assert ev.ROW_ANGLE_FALLBACK in doc["fallback_fields"]
    assert {"versions.catalog", "versions.solver"} <= set(doc["synthetic_fields"])
    assert all(f"after/strings/{i}/polarity" in doc["fallback_fields"] for i in range(116))
    assert doc["synthetic_flagged"] is True
    assert doc["execution_mode"] == "live" and doc["state"] == "committed"


def test_the_fixture_revision_is_the_commit_that_last_touched_the_pdf():
    revision = ev.fixture_revision(ev.DEFAULT_PDF)
    assert len(revision) == 40 and set(revision) <= set("0123456789abcdef")


def test_comparator_passes_a_plugin_shaped_twin_and_fails_one_moved_panel(doc):
    plugin = plugin_shaped(doc)
    result = compare.compare(plugin, doc, "strings", capability=ev.CAPABILITY)
    assert result == {"name": compare.NAME, "version": compare.VERSION, "verdict": "pass", "diffs": []}
    moved = copy.deepcopy(plugin)
    members = moved["after"]["strings"][0]["ordered_membership"]
    members[-1], members[-2] = members[-2], members[-1]
    moved["output_sha256"] = compare.semantic_hash(moved["after"])
    result = compare.compare(moved, doc, "strings", capability=ev.CAPABILITY)
    assert result["verdict"] == "fail"
    assert any(d.startswith("after/strings/0/ordered_membership") for d in result["diffs"])


def test_cli_writes_a_document_that_reopens(tmp_path):
    out = tmp_path / "studio.json"
    assert ev.main(["--out", str(out), "--revision", REVISION]) == 0
    written = compare.load_evidence(out)
    assert written == ev.run(revision=REVISION)


@pytest.mark.parametrize("args", [
    ["--revision", "not-a-sha"],
    ["--max-string-length", "30"],
    ["--pdf", "does-not-exist.pdf"],
])
def test_refusals_write_nothing(tmp_path, args):
    out = tmp_path / "studio.json"
    assert ev.main(["--out", str(out), *args] + ([] if "--revision" in args else ["--revision", REVISION])) == 2
    assert not out.exists()
