"""
Acceptance tests for the NL prompt router (CONTRACT-ADDENDUM §12, MATRIX gap #2).

Two layers, all offline (no APS, no LLM, no subprocess):
  * pure `nl_router.classify` over the LIVE catalog (deps.all_tools());
  * the `POST /api/nl-prompt` endpoint via FastAPI TestClient (§10 envelope).

Run:  cd server && python -m pytest tests/test_nl_router.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
from nl_router import LANE_BUILD, LANE_RUN, LANE_SOLVE, classify  # noqa: E402

# The live catalog the endpoint matches against (registry + write seed + authored).
CATALOG = deps.all_tools()


def _c(text):
    return classify(text, CATALOG)


# --------------------------------------------------------------------------- #
# RUN lane — confident tool matches
# --------------------------------------------------------------------------- #
def test_count_panels_per_layer_routes_run_count_by_layer():
    r = _c("count panels per layer")
    assert r["lane"] == LANE_RUN
    assert r["tool"] == "count-by-layer"          # grouping intent beats count-panels
    assert r["confidence"] >= 0.8
    assert isinstance(r["rationale"], str) and r["rationale"]


def test_measure_total_panel_area_routes_measure_panel_area():
    r = _c("measure the total panel area")
    assert r["lane"] == LANE_RUN
    assert r["tool"] == "measure-panel-area"
    assert r["confidence"] >= 0.8


def test_highlight_near_edge_extracts_distance_200():
    r = _c("highlight panels within 200 of the edge")
    assert r["lane"] == LANE_RUN
    assert r["tool"] == "highlight-panels-near-edge"
    assert r["params"].get("distance") == 200      # numeric param prefilled
    assert r["confidence"] >= 0.8


def test_delete_panel_extracts_handle_as_string():
    r = _c("delete panel 9462")
    assert r["lane"] == LANE_RUN
    assert r["tool"] == "delete-marked-panel"
    assert r["params"].get("handle") == "9462"     # handle stays a STRING
    assert isinstance(r["params"]["handle"], str)


def test_exact_name_phrase_is_high_confidence():
    r = _c("highlight panels near edge")
    assert r["tool"] == "highlight-panels-near-edge"
    assert r["confidence"] >= 0.9                   # near-exact name -> very high


# --------------------------------------------------------------------------- #
# BUILD lane — authoring intent, description passthrough
# --------------------------------------------------------------------------- #
def test_build_a_tool_that_reports_bounding_box():
    text = "build a tool that reports the bounding box of each layer"
    r = _c(text)
    assert r["lane"] == LANE_BUILD
    assert r["tool"] is None
    assert r["params"]["description"] == text       # verbatim passthrough
    assert r["confidence"] >= 0.5


def test_make_me_a_tool_to_flag_small_panels():
    text = "make me a tool to flag panels smaller than 10 sqft"
    r = _c(text)
    assert r["lane"] == LANE_BUILD
    assert r["tool"] is None
    assert r["params"]["description"] == text


# --------------------------------------------------------------------------- #
# SOLVE lane — explicit optimisation intent (future lane)
# --------------------------------------------------------------------------- #
def test_solve_phrase_routes_solve_lane():
    r = _c("optimize the panel layout to maximize energy production")
    assert r["lane"] == LANE_SOLVE
    assert r["tool"] is None
    assert "solve" in r["rationale"].lower()        # honestly says the lane is future


def test_plain_solve_verb_routes_solve():
    r = _c("solve the string sizing for this array")
    assert r["lane"] == LANE_SOLVE
    assert r["tool"] is None


# --------------------------------------------------------------------------- #
# ambiguity / gibberish — honest low confidence, never a fake high score
# --------------------------------------------------------------------------- #
def test_gibberish_is_low_confidence_no_tool():
    r = _c("asdf qwer zxcvbn plorp")
    assert r["tool"] is None
    assert r["confidence"] <= 0.4


def test_confidence_is_bounded():
    for text in ("count panels per layer", "asdf", "delete panel 9462",
                 "optimize layout", "build a tool that does X"):
        r = _c(text)
        assert 0.0 <= r["confidence"] <= 1.0


# --------------------------------------------------------------------------- #
# disambiguation is deterministic regardless of live-store state
# --------------------------------------------------------------------------- #
def test_grouping_beats_panel_name_on_explicit_catalog():
    catalog = [
        {"name": "count-by-layer", "engine_op": "count_by_layer",
         "description": "Counts every model-space entity per layer.",
         "params": {"type": "object", "properties": {}}},
        {"name": "count-panels", "engine_op": "count_by_layer",
         "description": "count panels on layer Panels",
         "params": {"type": "object", "properties": {"layer": {"type": "string"}}}},
    ]
    r = classify("count panels per layer", catalog)
    assert r["tool"] == "count-by-layer"


def test_internal_qa_tools_are_filtered():
    catalog = [
        {"name": "qa-sleep-count", "internal": True, "engine_op": "count_by_layer",
         "description": "QA-only sleeps then counts by layer.",
         "params": {"type": "object", "properties": {}}},
    ]
    r = classify("count by layer", catalog)
    assert r["tool"] is None                        # internal tool never matched


# --------------------------------------------------------------------------- #
# endpoint — §10 envelope + validation
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


def _assert_envelope(body):
    # §10: every response body carries error + degraded_mode.
    assert "error" in body and "degraded_mode" in body
    # contract §12 success shape.
    for k in ("lane", "tool", "params", "confidence", "rationale"):
        assert k in body


def test_endpoint_run_envelope(client):
    resp = client.post("/api/nl-prompt", json={"text": "count panels per layer"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["error"] is None
    assert body["degraded_mode"] is False
    assert body["lane"] == LANE_RUN
    assert body["tool"] == "count-by-layer"


def test_endpoint_build_lane(client):
    text = "build a tool that reports the bounding box of each layer"
    resp = client.post("/api/nl-prompt", json={"text": text})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["lane"] == LANE_BUILD
    assert body["params"]["description"] == text


def test_endpoint_empty_text_is_bad_params(client):
    resp = client.post("/api/nl-prompt", json={"text": "   "})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert body["degraded_mode"] is False


def test_endpoint_missing_text_is_enveloped_validation_error(client):
    resp = client.post("/api/nl-prompt", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["error_code"] == "BAD_PARAMS"   # RequestValidationError -> enveloped
