"""Studio auto-fill-then-solve (AutoFillSolve): the chained producer replays its committed responses and
reproduces the plugin's committed strings; auto-fill regrids the groups it changes the way the plugin
rebuilds them; the evidence scope for a REMOVEPANEL cut is grouped panels only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RECEIPT = ROOT / "docs" / "parity" / "receipts" / "auto-fill-then-solve" / "rooftop-demo.json"
REPLAY = ROOT / "docs" / "parity" / "evidence" / "auto-fill-then-solve" / "rooftop-demo" / "studio-responses.json"
REMOVED = [f"81{value:02X}" for value in range(0xC5, 0xE1)]      # the 28 panels REMOVEPANEL cut: 81C5..81E0


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


chain = _load("solar_w1_studio_autofill_solve")
evidence = _load("solar_studio_evidence")
compare = evidence.compare   # the module instance the evidence builder raises from


def arguments(tmp_path):
    argv = ["--fixture", str(ROOT / "data" / "rooftop_demo.dwg"),
            "--intake", str(ROOT / "data" / "rooftop_demo.v2.intake.json"),
            "--sizing-response", str(ROOT / "server" / "tests" / "fixtures" / "w1_rooftop_unsplit_sizing_response.json"),
            "--branch-max-offset", "120.0", "--alignment-tolerance", "12.0", "--layer-contains", "Panels",
            "--installation-design", "Roof", "--max-string-length", "14", "--dwgname", "rooftop_demo.dwg",
            "--replay", str(REPLAY), "--out-graph", str(tmp_path / "graph.json"),
            "--out-metadata", str(tmp_path / "metadata.json")]
    for handle in REMOVED:
        argv += ["--remove", handle]
    return argv


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("autofill-solve")
    assert chain.main(arguments(tmp_path)) == 0
    graph = json.loads((tmp_path / "graph.json").read_text(encoding="utf-8"))
    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    stage = json.loads((tmp_path / "graph.autofill.json").read_text(encoding="utf-8"))
    return graph, metadata, stage


def test_replay_reproduces_the_plugins_committed_strings(produced):
    graph, metadata, _ = produced
    document = evidence.build_evidence(graph, "strings", metadata)
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    result = compare.compare(receipt["comparison"]["plugin"], document, "strings", capability="auto-fill-then-solve")
    assert result["verdict"] == "pass", result["diffs"][:10]
    assert len(document["after"]["strings"]) == 170 and document["after"]["unassigned_panels"] == []


def test_autofill_moves_one_panel_and_records_the_chain(produced):
    _, metadata, _ = produced
    autofill = metadata["provenance"]["autofill"]
    assert autofill["panels_moved"] == 1 and autofill["panels_removed"] == 28
    assert [c["panels"] for c in autofill["corrections"]] == [["8201"]]
    assert metadata["provenance"]["unassigned_scope"] == "grouped"
    assert len(metadata["provenance"]["response_sha256s"]) == 23
    assert metadata["versions"]["producer"] == "solar_w1_studio_autofill_solve.v1"


def test_the_receiving_group_is_regridded_with_the_far_panel_alone(produced):
    _, _, stage = produced
    moved = next(p for p in stage["panels"] if p["provenance"]["source_handle"].upper() == "8201")
    frame = next(f for f in stage["frames"] if f["id"] == moved["frame_ref"])
    cell = moved["matrix_cell"]
    row = frame["matrix"][cell["row"]]
    column = [r[cell["col"]] for r in frame["matrix"]]
    # WriteMatrixForPanelGroup put the far panel (d=1027) in a row and a column of its own.
    assert [c["panel_ref"] for c in row if c["code"] == "panel"] == [moved["id"]]
    assert [c["panel_ref"] for c in column if c["code"] == "panel"] == [moved["id"]]
    assert frame["module_rows"] == len(frame["matrix"]) and frame["module_slots"] == \
        frame["module_rows"] * frame["module_columns"]
    for panel in stage["panels"]:
        if panel["frame_ref"] == frame["id"]:
            placed = frame["matrix"][panel["matrix_cell"]["row"]][panel["matrix_cell"]["col"]]
            assert placed["panel_ref"] == panel["id"]


def test_evidence_scope_is_opt_in_and_bounded(produced):
    graph, metadata, _ = produced
    everything = dict(metadata, provenance={k: v for k, v in metadata["provenance"].items() if k != "unassigned_scope"})
    with pytest.raises((KeyError, compare.InputError)):
        # Without the grouped scope the 28 cut panels are unassigned and absent from the entity mapping.
        evidence.build_evidence(graph, "strings", everything)
    with pytest.raises(compare.InputError, match="unassigned_scope"):
        evidence.build_evidence(graph, "strings", dict(metadata, provenance={**metadata["provenance"],
                                                                             "unassigned_scope": "all"}))


def test_live_and_replay_modes_are_exclusive(tmp_path, capsys):
    argv = arguments(tmp_path) + ["--tenant", "w1-live"]
    with pytest.raises(SystemExit):
        chain.main(argv)
    assert "--tenant" in capsys.readouterr().err
