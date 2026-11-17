"""Literal synthetic Roof/Ground evidence for the independent saved metrics."""

import base64
from copy import deepcopy
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import struct
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benches.x05_saved_metrics import measure_saved_graph


def ident(kind, number):
    return f"leaf:{kind}:00000000-0000-4000-8000-{number:012x}"


P = [ident("panel", n) for n in range(1, 5)]


def string(number, refs):
    return {"id": ident("string", number), "module_count": len(refs),
            "ordered_panel_refs": list(refs)}


def roof():
    return {
        "panels": [{"id": p} for p in P[:3]],
        "frames": [{"id": ident("frame", 1), "panel_refs": P[:2],
                    "module_power_watts": 400.125},
                   {"id": ident("frame", 2), "panel_refs": P[2:3],
                    "module_power_watts": 455}],
        "strings": [string(1, P[:2]), string(2, P[2:3])],
        "inverters": [{"id": ident("inverter", n), "equipment_type": equipment}
                      for n, equipment in enumerate(
                          ("string_inverter", "central_inverter", "combiner_box"), 1)],
    }


def block(refs):
    raw = b"".join(uuid.UUID(p.split(":")[-1]).bytes for p in refs)
    return {"codec": "leaf.solar-ground-slots.v1", "count": len(refs),
            "panel_ids": base64.b64encode(raw).decode("ascii"),
            "centres": base64.b64encode(struct.pack("<dd", 1.25, -2.5) * len(refs)).decode("ascii"),
            "angle": 0, "panel": {"rev": 0, "provenance": {}, "validity": {}, "extra": {}}}


def ground():
    graph = roof()
    graph["panels"] = []
    for frame in graph["frames"]:
        frame["ground_slots"] = block(frame.pop("panel_refs"))
    return graph


def test_literal_roof_counts_and_exact_dc():
    measured = measure_saved_graph(roof())
    assert measured["values"] == {"panels": "3", "optimizers": None,
                                  "inverters": "2", "strings": "2", "kw_dc": "1.25525"}
    assert Fraction(measured["values"]["kw_dc"]) == Fraction(5021, 4000)
    assert measured["reasons"] == {"panels": "", "optimizers": "OPTIMIZER_MEASUREMENT_ABSENT",
                                    "inverters": "", "strings": "", "kw_dc": ""}


def test_compact_equivalence_mixed_union_and_nonmutation():
    explicit, compact = roof(), ground()
    mixed = ground()
    mixed["panels"] = [{"id": P[3]}]
    mixed["frames"].append({"id": ident("frame", 3), "panel_refs": [P[3]],
                            "module_power_watts": 100})
    mixed["strings"].append(string(3, [P[3]]))
    for graph in (explicit, compact, mixed):
        before = deepcopy(graph)
        measured = measure_saved_graph(graph)
        assert graph == before
        assert measured["values"]["panels"] == ("4" if graph is mixed else "3")
        assert measured["values"]["kw_dc"] == ("1.35525" if graph is mixed else "1.25525")
    assert measure_saved_graph(explicit) == measure_saved_graph(compact)


def test_dc_does_not_use_ambient_decimal_context():
    graph = roof()
    graph["frames"][0]["module_power_watts"] = 0.123456789012345
    graph["frames"][1]["module_power_watts"] = 0.000000000000001
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.Emin = -2
        result = measure_saved_graph(graph)
    assert result["values"]["kw_dc"] == "0.000246913578024691"
    assert Decimal(result["values"]["kw_dc"]) == Decimal("0.000246913578024691")


@pytest.mark.parametrize("power", [None, True, "450", 0, -1, float("nan"), float("inf"), 1e-16, 1e18])
def test_missing_or_unsupported_power_affects_only_dc(power):
    graph = roof()
    graph["frames"][0]["module_power_watts"] = power
    graph["frames"][0]["module_model"] = "Super450"
    result = measure_saved_graph(graph)
    assert result["values"] == {"panels": "3", "optimizers": None, "inverters": "2",
                                "strings": "2", "kw_dc": None}
    assert result["reasons"]["kw_dc"] == "DC_COVERAGE_OR_POWER_INVALID"


@pytest.mark.parametrize("case", ["duplicate", "wrong_kind", "uppercase", "non_v4", "hole"])
def test_invalid_panel_identity_blocks_dependent_metrics(case):
    graph = roof()
    if case == "duplicate":
        graph["panels"].append(deepcopy(graph["panels"][0]))
    elif case == "hole":
        graph["panels"].append(None)
    else:
        graph["panels"][0]["id"] = {
            "wrong_kind": ident("frame", 1),
            "uppercase": "leaf:panel:AAAAAAAA-0000-4000-8000-000000000001",
            "non_v4": "leaf:panel:00000000-0000-1000-8000-000000000001",
        }[case]
    result = measure_saved_graph(graph)
    assert all(result["values"][m] is None for m in ("panels", "strings", "kw_dc"))
    assert result["values"]["inverters"] == "2"


@pytest.mark.parametrize("case", ["foreign", "overlap", "duplicate_ref", "duplicate_id", "wrong_kind", "count", "bool"])
def test_bad_string_evidence_does_not_hide_other_metrics(case):
    graph = roof()
    item = graph["strings"][1]
    if case in ("foreign", "overlap", "duplicate_ref"):
        item["ordered_panel_refs"] = {"foreign": [P[3]], "overlap": [P[0]],
                                      "duplicate_ref": [P[2], P[2]]}[case]
        item["module_count"] = len(item["ordered_panel_refs"])
    elif case in ("duplicate_id", "wrong_kind"):
        item["id"] = ident("string", 1) if case == "duplicate_id" else ident("panel", 1)
    else:
        item["module_count"] = True if case == "bool" else 2
    result = measure_saved_graph(graph)
    assert result["values"]["strings"] is None
    assert result["values"]["panels"] == "3"
    assert result["values"]["kw_dc"] == "1.25525"
    assert result["strings_sha256"] is not None


@pytest.mark.parametrize("case", ["collision", "cross_frame", "explicit_collision", "base64", "length", "coordinates", "codec", "template", "angle"])
def test_compact_invalid_evidence_is_never_zero(case):
    graph = ground()
    slot = graph["frames"][0]["ground_slots"]
    if case == "collision":
        graph["frames"][0]["ground_slots"] = block([P[0], P[0]])
    elif case == "cross_frame":
        graph["frames"][1]["ground_slots"] = block([P[0]])
    elif case == "explicit_collision":
        graph["panels"] = [{"id": P[0]}]
    elif case == "base64":
        slot["panel_ids"] = "!" * len(slot["panel_ids"])
    elif case == "length":
        slot["count"] += 1
    elif case == "coordinates":
        slot["centres"] = base64.b64encode(struct.pack("<dddd", 0, 0, float("inf"), 0)).decode("ascii")
    elif case == "codec":
        slot["codec"] = "future"
    elif case == "template":
        slot["panel"]["rev"] = True
    else:
        slot["angle"] = True
    result = measure_saved_graph(graph)
    assert all(result["values"][m] is None for m in ("panels", "strings", "kw_dc"))
    assert result["values"]["inverters"] == "2"


def test_hash_is_independent_stable_and_assignment_sensitive():
    graph = roof()
    payload = json.dumps(graph["strings"], sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert measure_saved_graph(graph)["strings_sha256"] == expected
    reread = json.loads(json.dumps(graph))
    reread["strings"][0] = dict(reversed(list(reread["strings"][0].items())))
    reread["stored_digest"] = "untrusted"
    assert measure_saved_graph(reread)["strings_sha256"] == expected
    reread["strings"][0]["ordered_panel_refs"].reverse()
    assert measure_saved_graph(reread)["strings_sha256"] != expected
    assert measure_saved_graph(reread)["values"] == measure_saved_graph(graph)["values"]


def test_frame_identity_coverage_and_dc_total_limits():
    graph = roof()
    graph["frames"][1]["id"] = ident("frame", 1)
    assert measure_saved_graph(graph)["values"]["kw_dc"] is None
    graph["frames"][1]["id"] = ident("panel", 1)
    assert measure_saved_graph(graph)["values"]["kw_dc"] is None
    graph = roof()
    graph["frames"][1]["panel_refs"] = []
    assert measure_saved_graph(graph)["values"]["kw_dc"] is None
    graph["frames"][1]["panel_refs"] = [P[3]]
    assert measure_saved_graph(graph)["values"]["kw_dc"] is None
    graph = roof()
    graph["panels"] = [{"id": ident("panel", n)} for n in range(1, 2001)]
    graph["frames"] = [{"id": ident("frame", 1),
                        "panel_refs": [p["id"] for p in graph["panels"]],
                        "module_power_watts": 999_999_999_999_999_999}]
    assert measure_saved_graph(graph)["values"]["kw_dc"] is None
    assert measure_saved_graph(graph)["values"]["panels"] == "2000"


def test_empty_string_digest_has_literal_expectation():
    result = measure_saved_graph({"strings": []})
    assert result["strings_sha256"] == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


@pytest.mark.parametrize("case", ["nan", "object", "cycle", "depth", "key"])
def test_non_json_string_payload_has_no_digest(case):
    graph = roof()
    bad = {"nan": float("nan"), "object": object(), "key": {1: "value"}}.get(case)
    if case == "cycle":
        bad = []
        bad.append(bad)
    elif case == "depth":
        bad = "end"
        for _ in range(40):
            bad = [bad]
    graph["strings"][0]["extra"] = bad
    result = measure_saved_graph(graph)
    assert result["strings_sha256"] is None
    assert result["values"]["strings"] == "2"


def test_coverage_devices_bounds_empty_and_malformed_siblings():
    graph = roof()
    graph["frames"][1]["panel_refs"] = [P[0]]
    graph["inverters"][0]["equipment_type"] = "optimizer"
    result = measure_saved_graph(graph)
    assert result["values"]["kw_dc"] is None
    assert result["values"]["inverters"] is None
    assert result["values"]["strings"] == "2"
    graph = roof()
    graph["inverters"].append(deepcopy(graph["inverters"][0]))
    assert measure_saved_graph(graph)["values"]["inverters"] is None
    graph["strings"] = None
    assert measure_saved_graph(graph)["values"]["panels"] == "3"
    graph["panels"] = [None] * 100_001
    assert measure_saved_graph(graph)["values"]["panels"] is None
    empty = measure_saved_graph({key: [] for key in ("panels", "frames", "strings", "inverters")})
    assert empty["values"] == {"panels": "0", "strings": "0", "inverters": "0", "optimizers": None, "kw_dc": None}
    for bad in (None, [], "graph", {"panels": []}):
        result = measure_saved_graph(bad)
        assert all(v is None for v in result["values"].values())
        assert result["reasons"]["optimizers"] == "OPTIMIZER_MEASUREMENT_ABSENT"
        json.dumps(result, allow_nan=False)
