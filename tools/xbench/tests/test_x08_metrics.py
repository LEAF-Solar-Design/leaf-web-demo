"""Synthetic evidence only: no browser, network, or customer drawing data."""

from copy import deepcopy

import pytest

from benches.x08_metrics import score
from xbench_core import Step


@pytest.fixture
def samples():
    handles = [f"module-{index}" for index in range(500)]
    return {
        "geometry": {"rendered_module_count": 12, "marker_module_count": 0,
                     "omitted_module_count": 0},
        "single_select_ms": [10] * 20,
        "window_select": [{"latency_ms": 20, "expected_handles": handles.copy(),
                           "selected_handles": list(reversed(handles)),
                           "oracle_independent": True} for _ in range(5)],
        "job_ack": [{"latency_ms": 30, "http_status": 202, "rendered": True}
                    for _ in range(10)],
        "frame_deltas_ms": [16] * 600,
        "cold_run_discarded": True,
        "sequence": {"pan_drags": 4, "zoom_in_steps": 10, "zoom_out_steps": 10,
                     "duration_ms": 10000},
        "reference": {"hostname": "evans-dekstop", "os": "Windows 11 synthetic",
                      "cpu": "synthetic CPU", "gpu": ["synthetic GPU"],
                      "free_ram_bytes": 1024, "chromium_version": "synthetic version",
                      "headed": True, "viewport": {"width": 1920, "height": 1080},
                      "device_scale_factor": 1},
    }


def test_valid_six_steps_and_metadata(samples):
    original = deepcopy(samples)
    steps = score(samples, 12, "synthetic.json")
    assert [step.n for step in steps] == list(range(1, 7))
    assert all(isinstance(step, Step) and step.verdict == "PASS" for step in steps)
    assert all(step.evidence == "synthetic.json" and step.expected for step in steps)
    assert steps[5].observed == samples["reference"]
    assert steps[1].observed["count"] == 20
    assert steps[1].observed["p95_ms"] == 10
    assert samples == original


@pytest.mark.parametrize("payload", [None, {}, [], {"geometry": {}}])
def test_missing_evidence_never_passes(payload):
    steps = score(payload, 12)
    assert len(steps) == 6
    assert all(step.verdict == "UNREADABLE" for step in steps)


@pytest.mark.parametrize("key,index,budget", [
    ("single_select_ms", 1, 100), ("window_select", 2, 100),
    ("job_ack", 3, 200), ("frame_deltas_ms", 4, 33),
])
def test_budget_equality_fails(samples, key, index, budget):
    if key in ("window_select", "job_ack"):
        for row in samples[key]:
            row["latency_ms"] = budget
    else:
        samples[key] = [budget] * len(samples[key])
    assert score(samples, 12)[index].verdict == "FAIL"


def test_p95_is_nearest_rank(samples):
    samples["single_select_ms"] = [1] * 18 + [99, 1000]
    step = score(samples, 12)[1]
    assert step.verdict == "PASS"
    assert step.observed["p95_ms"] == 99


@pytest.mark.parametrize("bad", [None, "10", True, False, 0, -1, float("nan"),
                                 float("inf"), float("-inf")])
@pytest.mark.parametrize("key,index", [
    ("single_select_ms", 1), ("window_select", 2),
    ("job_ack", 3), ("frame_deltas_ms", 4),
])
def test_invalid_timing_is_unreadable(samples, bad, key, index):
    if key in ("window_select", "job_ack"):
        samples[key][0]["latency_ms"] = bad
    else:
        samples[key][0] = bad
    assert score(samples, 12)[index].verdict == "UNREADABLE"


@pytest.mark.parametrize("key,index", [
    ("single_select_ms", 1), ("window_select", 2),
    ("job_ack", 3), ("frame_deltas_ms", 4),
])
@pytest.mark.parametrize("mode", ["missing", "empty", "short", "invalid"])
def test_incomplete_timing_is_unreadable(samples, key, index, mode):
    if mode == "missing":
        del samples[key]
    elif mode == "empty":
        samples[key] = []
    elif mode == "short":
        samples[key].pop()
    else:
        samples[key] = {}
    assert score(samples, 12)[index].verdict == "UNREADABLE"


@pytest.mark.parametrize("cold", [None, False, 1, "true"])
def test_cold_discard_required_for_every_timing_step(samples, cold):
    samples["cold_run_discarded"] = cold
    assert [step.verdict for step in score(samples, 12)[1:5]] == ["UNREADABLE"] * 4


@pytest.mark.parametrize("key", ["rendered_module_count", "marker_module_count",
                                 "omitted_module_count"])
def test_geometry_missing_invalid_or_mismatched(samples, key):
    original = samples["geometry"][key]
    del samples["geometry"][key]
    assert score(samples, 12)[0].verdict == "UNREADABLE"
    samples["geometry"][key] = True
    assert score(samples, 12)[0].verdict == "UNREADABLE"
    samples["geometry"][key] = original + 1
    assert score(samples, 12)[0].verdict == "FAIL"


@pytest.mark.parametrize("mode", ["different", "missing", "extra", "empty"])
def test_exact_window_set_mismatch_fails(samples, mode):
    selected = samples["window_select"][0]["selected_handles"]
    if mode == "different":
        selected[0] = "different-module"
    elif mode == "missing":
        selected.pop()
    elif mode == "extra":
        selected.append("extra-module")
    else:
        selected.clear()
    assert score(samples, 12)[2].verdict == "FAIL"


@pytest.mark.parametrize("key", ["expected_handles", "selected_handles"])
@pytest.mark.parametrize("mode", ["duplicate", "empty_string", "missing", "nonstring"])
def test_invalid_handles_unreadable(samples, key, mode):
    row = samples["window_select"][0]
    if mode == "missing":
        del row[key]
    else:
        row[key][0] = {"duplicate": row[key][1], "empty_string": "", "nonstring": 3}[mode]
    assert score(samples, 12)[2].verdict == "UNREADABLE"


@pytest.mark.parametrize("count", [489, 511])
def test_oracle_cardinality_unreadable(samples, count):
    samples["window_select"][0]["expected_handles"] = [f"module-{i}" for i in range(count)]
    assert score(samples, 12)[2].verdict == "UNREADABLE"


@pytest.mark.parametrize("count", [490, 510])
def test_oracle_cardinality_boundaries_pass(samples, count):
    handles = [f"module-{i}" for i in range(count)]
    for row in samples["window_select"]:
        row["expected_handles"] = handles.copy()
        row["selected_handles"] = handles.copy()
    assert score(samples, 12)[2].verdict == "PASS"


@pytest.mark.parametrize("independent", [None, False, 1])
def test_independent_oracle_required(samples, independent):
    samples["window_select"][0]["oracle_independent"] = independent
    assert score(samples, 12)[2].verdict == "UNREADABLE"


@pytest.mark.parametrize("key,value,verdict", [
    ("http_status", 200, "FAIL"), ("rendered", False, "FAIL"),
    ("http_status", True, "UNREADABLE"), ("rendered", 1, "UNREADABLE"),
    ("http_status", None, "UNREADABLE"), ("rendered", None, "UNREADABLE"),
])
def test_ack_fields(samples, key, value, verdict):
    samples["job_ack"][0][key] = value
    assert score(samples, 12)[3].verdict == verdict


@pytest.mark.parametrize("key,value,verdict", [
    ("pan_drags", 3, "FAIL"), ("zoom_in_steps", 9, "FAIL"),
    ("zoom_out_steps", 11, "FAIL"), ("duration_ms", 9999, "FAIL"),
    ("pan_drags", True, "UNREADABLE"), ("duration_ms", float("nan"), "UNREADABLE"),
])
def test_sequence_fields(samples, key, value, verdict):
    samples["sequence"][key] = value
    assert score(samples, 12)[4].verdict == verdict


def test_missing_sequence_unreadable(samples):
    del samples["sequence"]
    assert score(samples, 12)[4].verdict == "UNREADABLE"


@pytest.mark.parametrize("key,value", [
    ("hostname", "other-host"), ("os", "other OS"), ("headed", False),
    ("viewport", {"width": 1280, "height": 720}), ("device_scale_factor", 2),
])
def test_wrong_reference_fails(samples, key, value):
    samples["reference"][key] = value
    assert score(samples, 12)[5].verdict == "FAIL"


@pytest.mark.parametrize("key", ["hostname", "os", "cpu", "gpu", "free_ram_bytes",
                                 "chromium_version", "headed", "viewport", "device_scale_factor"])
def test_incomplete_reference_unreadable(samples, key):
    del samples["reference"][key]
    assert score(samples, 12)[5].verdict == "UNREADABLE"


@pytest.mark.parametrize("key,value", [
    ("cpu", " "), ("gpu", []), ("gpu", [""]), ("free_ram_bytes", True),
    ("free_ram_bytes", 0), ("device_scale_factor", True),
    ("device_scale_factor", float("inf")), ("headed", 1),
    ("viewport", {"width": True, "height": 1080}),
])
def test_invalid_reference_unreadable(samples, key, value):
    samples["reference"][key] = value
    assert score(samples, 12)[5].verdict == "UNREADABLE"
