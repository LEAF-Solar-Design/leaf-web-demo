"""Paired evidence shards (contract G19) from synthetic step documents.

Every document is authored in this file (no capture, no network, no git), so the collected
count is the same on every runner and nothing skips.

Covered: the row budgets (frames 500, pile sets 250 per shard), terrain-grid bands of 30 grid
rows and terrain-mesh bands of 30 cell rows with band_start and band_rows, every other row
in shard 1, the mapping per shard (G8, including a pile set's frame reference), the empty
shard when one side has more rows, recombination, refusal of tampered shards and of bad
inputs, the CLI, and the frozen comparator passing every pair, including at the terrain
fixture's real t1 and t4 shapes where the unsharded document is refused.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


shard = _load("solar_evidence_shard")
compare = shard.compare
FRAME = {"coordinate_system": "world", "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": "none"}


def ref(x):
    return {"entity_id": x}


def pt(*values):
    return {"kind": "coordinate", "value": list(values), "unit": "m"}


def length(v):
    return {"kind": "length", "value": v, "unit": "m"}


def frame(n):
    return {"id": ref(f"frame-{n}"), "type": "frame", "quantity": 1, "unit": "each",
            "vertices": [pt(n, 0.0), pt(n + 1.0, 0.0), pt(n + 1.0, 4.0), pt(n, 4.0)],
            "row_index": n, "col_index": 0, "color_index": 5}


def pile_set(n, owner, piles=2):
    return {"id": ref(f"pile-set-{n}"), "type": "pile-set", "quantity": 1, "unit": "each",
            "frame": ref(f"frame-{owner}") if owner else None, "diameter": length(0.15),
            "length": length(2.0), "count": piles,
            "piles": [pt(float(n), float(i), -1.25) for i in range(piles)]}


def marker(n):
    return {"id": ref(f"marker-{n}"), "type": "marker", "quantity": 1, "unit": "each",
            "role": "collision", "bbox": {"min": pt(0.0, 0.0), "max": pt(1.0, 1.0)}}


def removed(n):
    return {"id": ref(f"removed-{n}"), "type": "removed", "quantity": 1, "unit": "each", "of": "frame", "count": 3}


def extent(rows, cols):
    return {"min": pt(0.0, 0.0), "max": pt(float(cols), float(rows))}


def grid(rows, cols, offset=0.0):
    return {"id": ref("terrain-grid-1"), "type": "terrain-grid", "quantity": 1, "unit": "each",
            "rows": rows, "cols": cols, "extent": extent(rows, cols),
            "elevations": [length(round(i * 0.01 + offset, 6)) for i in range(rows * cols)]}


def mesh(rows, cols):
    return {"id": ref("terrain-mesh-1"), "type": "terrain-mesh", "quantity": 1, "unit": "each",
            "rows": rows, "cols": cols, "extent": extent(rows, cols),
            "cell_colors": [51200 + i % 7 for i in range((rows - 1) * (cols - 1))]}


def doc(rows, side="plugin", mapping=None):
    after = {"rows": rows, "source_revision": "t9", "format": "ground-v1"}
    refs = sorted(shard._references(rows, set()))
    return {
        "fixture_sha256": "a" * 64, "input_sha256": "b" * 64,
        "output_sha256": compare.scan_input(after),  # the same canonical digest as semantic_hash
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "versions": {"schema": compare.SCHEMA, "producer": side, "capability": "0", "engine": side,
                     "catalog": "none", "solver": "none"},
        "parameters": {"units_keyword": "Meters"}, "units": "m", "frame": deepcopy(FRAME),
        "entity_mapping": mapping if mapping is not None else {x: x for x in refs},
        "before": {"recorded": False}, "after": after,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [], "rejected_inputs": [], "provenance": {"side": side},
        "elapsed_ms": 0, "execution_mode": "live", "state": "committed", "survived_reopen": True,
        "synthetic_fields": [], "fallback_fields": [], "synthetic_flagged": False,
    }


def pair(rows):
    return doc(deepcopy(rows), "plugin"), doc(deepcopy(rows), "studio")


def ids(shard_doc):
    return [row["id"]["entity_id"] for row in shard_doc["after"]["rows"]]


def compare_pair(left, right, capability="piling-generate"):
    return compare.compare_document({"schema": compare.SCHEMA, "capability": capability, "family": "exports",
                                     "plugin": left, "studio": right})


def test_frame_rows_are_500_per_shard_and_map_only_their_own_ids():
    p, s = pair([frame(n) for n in range(1, 1202)])
    pairs = shard.split(p, s)
    assert shard.plan(p, s)["shards"] == 3
    assert [len(ids(left)) for left, _ in pairs] == [500, 500, 201]
    for left, right in pairs:
        assert sorted(left["entity_mapping"]) == sorted(ids(left)) == sorted(right["entity_mapping"])
    assert ids(pairs[2][0])[0] == "frame-1001"


def test_pile_sets_are_250_per_shard_with_their_frames_in_the_mapping():
    rows = [pile_set(n, n) for n in range(1, 601)] + [pile_set(601, None)]
    p, s = pair(rows)
    pairs = shard.split(p, s)
    assert [len(ids(left)) for left, _ in pairs] == [250, 250, 101]
    last = pairs[2][0]
    expected = {f"pile-set-{n}" for n in range(501, 602)} | {f"frame-{n}" for n in range(501, 601)}
    assert set(last["entity_mapping"]) == expected
    assert last["after"]["rows"][-1]["frame"] is None
    first = pairs[0][0]
    assert set(first["entity_mapping"]) == ({f"pile-set-{n}" for n in range(1, 251)} | {f"frame-{n}" for n in range(1, 251)})


def test_every_other_row_rides_in_shard_one():
    rows = [frame(n) for n in range(1, 502)] + [marker(1), marker(2), removed(1)]
    p, s = pair(rows)
    pairs = shard.split(p, s)
    assert len(pairs) == 2
    assert ids(pairs[0][0])[-3:] == ["marker-1", "marker-2", "removed-1"]
    assert ids(pairs[1][0]) == ["frame-501"]
    assert set(pairs[1][0]["entity_mapping"]) == {"frame-501"}


def test_terrain_grid_bands_of_30_grid_rows_keep_the_whole_fields():
    p, s = pair([grid(65, 4)])
    pairs = shard.split(p, s)
    bands = [left["after"]["rows"][0] for left, _ in pairs]
    assert [(b["band_start"], b["band_rows"], len(b["elevations"])) for b in bands] == [(0, 30, 120), (30, 30, 120), (60, 5, 20)]
    for b in bands:
        assert b["id"] == ref("terrain-grid-1") and b["rows"] == 65 and b["cols"] == 4 and b["extent"] == extent(65, 4)
    assert bands[1]["elevations"][0] == length(1.2)


def test_terrain_mesh_bands_of_30_cell_rows():
    p, s = pair([mesh(62, 5)])
    pairs = shard.split(p, s)
    bands = [left["after"]["rows"][0] for left, _ in pairs]
    assert [(b["band_start"], b["band_rows"], len(b["cell_colors"])) for b in bands] == [(0, 30, 120), (30, 30, 120), (60, 1, 4)]
    assert all(b["rows"] == 62 and b["cols"] == 5 for b in bands)


def test_grid_and_mesh_bands_share_shard_indices():
    p, s = pair([grid(61, 3), mesh(61, 3)])
    pairs = shard.split(p, s)
    assert len(pairs) == 3  # grid: 61 rows, 3 bands; mesh: 60 cell rows, 2 bands
    assert [[r["type"] for r in left["after"]["rows"]] for left, _ in pairs] == [
        ["terrain-grid", "terrain-mesh"], ["terrain-grid", "terrain-mesh"], ["terrain-grid"]]
    assert all(set(left["entity_mapping"]) == set(ids(left)) for left, _ in pairs)


def test_the_side_with_fewer_rows_gets_an_empty_shard_and_the_difference_is_a_diff():
    p = doc([frame(n) for n in range(1, 502)], "plugin")
    s = doc([frame(n) for n in range(1, 501)], "studio")
    layout = shard.plan(p, s)
    assert layout["shards"] == 2 and layout["studio"][1] == []
    pairs = shard.split(p, s)
    assert pairs[1][1]["after"]["rows"] == [] and pairs[1][1]["entity_mapping"] == {}
    assert compare_pair(*pairs[0], "frame-generate")["verdict"] == "pass"
    result = compare_pair(*pairs[1], "frame-generate")
    assert result["verdict"] == "fail"
    assert any(d.startswith("after/rows: length differs") for d in result["diffs"])


def test_plan_reads_ids_and_dimensions_never_values():
    a = doc([grid(40, 3, 0.0)] + [frame(n) for n in range(1, 4)])
    b = doc([grid(40, 3, 7.5)] + [frame(n) for n in range(1, 4)])
    for row in b["after"]["rows"][1:]:
        row["color_index"] = 1
    b["output_sha256"] = compare.semantic_hash(b["after"])
    assert shard.plan(a, a) == shard.plan(b, b)


def test_shards_recombine_to_the_step_after_and_carry_their_own_hash():
    rows = [frame(n) for n in range(1, 503)] + [marker(1), pile_set(1, 1), removed(1), grid(33, 2), mesh(33, 2)]
    p, s = pair(rows)
    pairs = shard.split(p, s)
    for side, original in ((0, p), (1, s)):
        shards = [pr[side] for pr in pairs]
        rebuilt = shard.recombine([x["after"] for x in shards])
        assert json.dumps(rebuilt, sort_keys=True) == json.dumps(original["after"], sort_keys=True)
        for x in shards:
            assert x["output_sha256"] == compare.semantic_hash(x["after"])
            envelope = {k: v for k, v in x.items() if k not in ("after", "entity_mapping", "output_sha256")}
            assert envelope == {k: v for k, v in original.items() if k not in ("after", "entity_mapping", "output_sha256")}
        shard.prove(original, shards)


def _drop_elevation(shards):
    shards[1]["after"]["rows"][0]["elevations"].pop()


def _drop_row(shards):
    shards[0]["after"]["rows"].pop()


def _duplicate_row(shards):
    shards[1]["after"]["rows"].append(deepcopy(shards[0]["after"]["rows"][-1]))


def _band_gap(shards):
    shards[1]["after"]["rows"][0]["band_start"] = 31


def _whole_field_changed(shards):
    shards[1]["after"]["rows"][0]["cols"] = 9


@pytest.mark.parametrize("tamper", [_drop_elevation, _drop_row, _duplicate_row, _band_gap, _whole_field_changed])
def test_a_tampered_shard_is_refused(tamper):
    p, _ = pair([grid(61, 2), marker(1)])
    shards = [left for left, _ in shard.split(p, deepcopy(p))]
    shard.prove(p, shards)
    shards = deepcopy(shards)
    tamper(shards)
    for x in shards:
        x["output_sha256"] = compare.semantic_hash(x["after"])
    with pytest.raises(shard.ShardError):
        shard.prove(p, shards)


def test_a_shard_with_a_stale_hash_is_refused():
    p, s = pair([frame(n) for n in range(1, 3)])
    shards = [left for left, _ in shard.split(p, s)]
    shards[0]["after"] = deepcopy(shards[0]["after"])
    shards[0]["after"]["rows"][0]["color_index"] = 2
    with pytest.raises(shard.ShardError, match="output_sha256"):
        shard.prove(p, shards)


def test_rows_out_of_type_order_fail_the_recombination_proof():
    p, s = pair([frame(n) for n in range(1, 502)] + [marker(1), frame(9999)])
    with pytest.raises(shard.ShardError, match="recombine"):
        shard.split(p, s)


def _bad_output_hash(d):
    d["output_sha256"] = "0" * 64


def _unmapped_frame(d):
    d["entity_mapping"].pop("frame-1")


def _already_banded(d):
    d["after"]["rows"][0]["band_start"] = 0


def _grid_dims_not_int(d):
    d["after"]["rows"][-1]["rows"] = 3.0


def _rows_not_list(d):
    d["after"]["rows"] = {}


@pytest.mark.parametrize("breaks", [_bad_output_hash, _unmapped_frame, _already_banded, _grid_dims_not_int, _rows_not_list])
def test_malformed_input_is_refused(breaks):
    p, s = pair([pile_set(1, 1), grid(3, 2)])
    breaks(p)
    if breaks is not _bad_output_hash:  # each case fails for its own reason, not a stale hash
        p["output_sha256"] = compare.scan_input(p["after"])
    with pytest.raises(shard.ShardError):
        shard.split(p, s)


def test_input_over_the_comparator_input_bound_is_refused(monkeypatch):
    p, s = pair([grid(40, 3)])
    monkeypatch.setattr(compare, "MAX_INPUT_NODES", 400)
    with pytest.raises(shard.ShardError, match="input bounds"):
        shard.split(p, s)


def test_tiny_documents_split_and_every_pair_passes_the_comparator():
    rows = [frame(n) for n in range(1, 503)] + [marker(1), pile_set(1, 1), pile_set(2, None), grid(35, 3), mesh(35, 3)]
    p, s = pair(rows)
    pairs = shard.split(p, s)
    assert len(pairs) == 2
    for left, right in pairs:
        result = compare_pair(left, right, "terrain-import")
        assert result["verdict"] == "pass", result["diffs"]


@pytest.mark.parametrize("shape", ["t1", "t4"])
def test_real_step_shapes_are_refused_whole_and_pass_in_shards(shape):
    if shape == "t1":
        rows, expected = [grid(90, 150), mesh(90, 150)], 3
    else:
        rows, expected = [pile_set(n, n, piles=8) for n in range(1, 1198)], 5
    p, s = pair(rows)
    with pytest.raises(compare.InputError, match="structural limit"):
        compare_pair(p, s)
    pairs = shard.split(p, s)
    assert len(pairs) == expected
    for left, right in pairs:
        assert compare_pair(left, right)["verdict"] == "pass"


def test_cli_writes_paired_part_files(tmp_path):
    p, s = pair([frame(n) for n in range(1, 502)])
    (tmp_path / "p.json").write_text(json.dumps(p), encoding="utf-8")
    (tmp_path / "s.json").write_text(json.dumps(s), encoding="utf-8")
    out = tmp_path / "out"
    assert shard.main(["--plugin", str(tmp_path / "p.json"), "--studio", str(tmp_path / "s.json"), "--out-dir", str(out)]) == 0
    assert sorted(x.name for x in out.iterdir()) == [
        "plugin-part1of2.json", "plugin-part2of2.json", "studio-part1of2.json", "studio-part2of2.json"]
    for k in (1, 2):
        left = compare.load_evidence(out / f"plugin-part{k}of2.json")
        right = compare.load_evidence(out / f"studio-part{k}of2.json")
        assert compare_pair(left, right, "frame-generate")["verdict"] == "pass"


def test_cli_refuses_without_writing(tmp_path):
    p, s = pair([frame(1)])
    p["output_sha256"] = "0" * 64
    (tmp_path / "p.json").write_text(json.dumps(p), encoding="utf-8")
    (tmp_path / "s.json").write_text(json.dumps(s), encoding="utf-8")
    out = tmp_path / "out"
    assert shard.main(["--plugin", str(tmp_path / "p.json"), "--studio", str(tmp_path / "s.json"), "--out-dir", str(out)]) == 2
    assert not out.exists()
