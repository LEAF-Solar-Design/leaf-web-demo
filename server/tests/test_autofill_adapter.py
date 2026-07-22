from solver_adapters import autofill


SMOKE_INPUT = {
    "groups": [
        {"handle": "A", "name": "A", "count": 25, "centroidX": 0.0,
         "centroidY": 0.0, "electricalZone": "Z", "elevationZone": ""},
        {"handle": "B", "name": "B", "count": 15, "centroidX": 10.0,
         "centroidY": 0.0, "electricalZone": "Z", "elevationZone": ""},
    ],
    "panelsPerString": 10,
    "options": {"drainThreshold": 23, "drainDiscount": 0.0,
                "activeGroupPenalty": 10, "concentrationBias": 0.15,
                "clusterMarginPitches": 2.0},
}


def test_real_autofill_solver_smoke_is_deterministic():
    first = autofill.run(SMOKE_INPUT)
    second = autofill.run(SMOKE_INPUT)
    assert first == second
    assert first["solver_result"] == {
        "clusters": [["A", "B"]], "diagnostics": [], "feasible": True,
        "groupTargets": {"A": 40, "B": 0}, "totalDisruption": 30,
    }
    assert first["result_sha256"] == "525e2d417d916ab896ab25525352783302c98f6f436631777731a4c08bb1ed59"
    assert first["request_sha256"]
    assert len(first["source_sha256"]) == 64
    assert first["solver_input"]["panelsPerString"] == 10
    assert first["solver_revision"]
    assert first["runtime"].startswith("python-")


def test_adapter_rejects_non_json_and_invalid_panel_count():
    for invalid in ({}, {"groups": [{}], "panelsPerString": True},
                    {"groups": [{}], "panelsPerString": 2},
                    {"groups": [{}], "panelsPerString": 10, "options": []}):
        try:
            autofill.run(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid adapter input was accepted: {invalid!r}")
