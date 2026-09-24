"""S2 parity: the converter port reproduces the plugin's golden parse of the SolarEdge fixture.

Every section is compared by digest against docs/parity/evidence/solaredge/golden-digests.json
(see scripts/solaredge_golden_digest.py for the shared canonicalization). A small hand-built
page pins the pipeline end to end, the plugin quirks the port keeps, and the bounded refusals.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random
from pathlib import Path

import pytest

import solar_solaredge_parse as sp
from solar_solaredge_parse import SolarEdgeParseError
from solar_solaredge_pdf import PagePrimitives, extract_primitives


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "data" / "solaredge_1to1_demo.pdf"
DIGESTS = REPO_ROOT / "docs" / "parity" / "evidence" / "solaredge" / "golden-digests.json"
S2_SECTIONS = ["panels", "optimizers", "grids", "matrices", "panel_layout", "inverter_strings",
               "all_string_infos", "inverter_pdf_colors"]


def _load_digest_module():
    spec = importlib.util.spec_from_file_location(
        "solaredge_golden_digest", REPO_ROOT / "scripts" / "solaredge_golden_digest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DIGEST = _load_digest_module()


@pytest.fixture(scope="module")
def golden():
    return json.loads(DIGESTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_parse(golden):
    if not FIXTURE.exists():
        pytest.skip(f"fixture PDF absent: {FIXTURE}")
    data = FIXTURE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == golden["pdf_sha256"], "fixture PDF differs from the golden's"
    parse, matrices = sp.parse_primitives(extract_primitives(data), golden["base_name"])
    return sp.parse_to_golden(parse, matrices)


@pytest.mark.parametrize("section", S2_SECTIONS)
def test_section_equals_golden(fixture_parse, golden, section):
    want = golden["sections"][section]
    assert len(fixture_parse[section]) == want["count"]
    assert DIGEST.digest(fixture_parse[section]) == want["sha256"]


@pytest.mark.parametrize("scalar", ["legend_threshold", "has_position_keywords", "optimizer_ratio"])
def test_scalar_equals_golden(fixture_parse, golden, scalar):
    assert DIGEST.canonical(fixture_parse[scalar]) == golden["scalars"][scalar]


def test_fixture_headline_figures(fixture_parse):
    assert len(fixture_parse["panels"]) == 3526
    assert len(fixture_parse["optimizers"]) == 3526
    assert len(fixture_parse["grids"]) == 14
    assert len(fixture_parse["all_string_infos"]) == 116
    assert sorted(fixture_parse["inverter_strings"], key=int) == [str(i) for i in range(1, 14)]


# ---------------------------------------------------------------- hand-built page

RED = (1.0, 0.0, 0.0)
PANEL_FILL = (0.9, 0.95, 1.0)
H = 1000.0


def _panel_path(left, bottom):
    return (True, True, (left, bottom, left + 30.0, bottom + 60.0), PANEL_FILL, None)


def _optimizer_path(cx, cy_user):
    return (False, True, (cx - 5, cy_user - 5, cx + 5, cy_user + 5), None, RED)


def _word(text, x, y_user, width=5.0):
    letters = []
    for ch in text:
        w = 2.0 if ch == "." else width
        letters.append((ch, x, y_user, w))
        x += w
    return letters


def _page(with_second_grid=True):
    """Grid A: panels 0 and 1 side by side, strung 1.1; grid B: panel 2 alone, string 1.2."""
    paths = [_panel_path(100, 500), _panel_path(140, 500),
             _optimizer_path(115, 530), _optimizer_path(155, 530)]
    curves = [(115.0, 470.0, 125.0, 470.0, 145.0, 470.0, 155.0, 470.0, RED)]
    letters = _word("1.1", 112, 528) + _word("7", 152, 528) + _word("Legend", 940, 900)
    if with_second_grid:
        paths += [_panel_path(600, 100), _optimizer_path(615, 130)]
        curves.append((615.0, 870.0, 630.0, 870.0, 640.0, 870.0, 650.0, 870.0, RED))
        letters += _word("1.2", 612, 128)
    return PagePrimitives(page_width=1000.0, page_height=H, lines=[], curves=curves,
                          letters=letters, paths=paths)


def test_hand_built_page_end_to_end():
    parse, matrices = sp.parse_primitives(_page(), "demo")
    out = sp.parse_to_golden(parse, matrices)
    assert out["legend_threshold"] == 920.0
    assert [(p["cx"], p["cy"]) for p in out["panels"]] == [(115.0, 470.0), (155.0, 470.0),
                                                           (615.0, 870.0)]
    assert [(o["panel_ids"], o["label"]) for o in out["optimizers"]] == [([0], "1.1"), ([1], "7"),
                                                                       ([2], "1.2")]
    assert out["grids"] == [[0, 1], [2]]
    assert out["optimizer_ratio"] == {"type": "1:1", "frequency": 1, "two_panel": 0,
                                      "one_panel": 3}
    first = out["matrices"][0]
    assert first["Dwgname"] == "demo_grid1.dwg"
    assert first["Sequences"] == [2] and first["Modify"] == [2, 0]
    # Rows are emitted rotated 180 degrees: the last column first.
    assert [(c["Id"], c["Seq"], c["InverterId"], c["StringInputNumber"])
            for c in first["Rows"][0]["Panels"]] == [("0001", 2, 1, 1), ("0000", 1, 1, 1)]
    assert out["inverter_strings"]["1"][0]["PanelSeqs"] == [1, 2]
    assert [s["Label"] for s in out["inverter_strings"]["1"]] == ["1.1", "1.2"]
    assert out["inverter_pdf_colors"] == {"1": [1.0, 0.0, 0.0]}


def test_single_grid_page_keeps_the_plugin_quirk_of_no_rows():
    # MergeElectricallyConnectedGrids returns early for one grid and never assigns rows, so the
    # plugin emits a matrix with no Rows. The port keeps that.
    parse, matrices = sp.parse_primitives(_page(with_second_grid=False), "demo")
    assert len(matrices) == 1
    assert matrices[0]["Rows"] == [] and matrices[0]["Sequences"] == [2]
    assert {p.row for p in parse.panels} == {-1}


# ---------------------------------------------------------------- .NET semantics and bounds

def test_dotnet_sort_orders_and_is_stable_up_to_sixteen():
    rng = random.Random(7)
    for n in range(0, 200):
        items = [(rng.randrange(5), i) for i in range(n)]
        got = list(items)
        sp._dotnet_sort(got, lambda a, b: sp._cmp(a[0], b[0]))
        assert [k for k, _ in got] == sorted(k for k, _ in items)
        assert sorted(got) == sorted(items)
        if n <= 16 and n != 3:
            assert got == sorted(items, key=lambda kv: kv[0])


def test_dotnet_int_matches_int_parse():
    assert sp._dotnet_int("12") == 12
    assert sp._dotnet_int("12\n") == 12
    for bad in ("\u0663", "2147483648", ""):
        with pytest.raises(SolarEdgeParseError):
            sp._dotnet_int(bad)


def test_refuses_over_work_budget(monkeypatch):
    monkeypatch.setattr(sp, "MAX_WORK", 3)
    with pytest.raises(SolarEdgeParseError) as err:
        sp.parse_primitives(_page())
    assert err.value.code == "WORK_BUDGET_EXCEEDED"


def test_refuses_too_many_panels(monkeypatch):
    monkeypatch.setattr(sp, "MAX_PANELS", 2)
    with pytest.raises(SolarEdgeParseError) as err:
        sp.parse_primitives(_page())
    assert err.value.code == "TOO_MANY_PANELS"


def test_refuses_too_many_optimizers(monkeypatch):
    monkeypatch.setattr(sp, "MAX_OPTIMIZERS", 2)
    with pytest.raises(SolarEdgeParseError) as err:
        sp.parse_primitives(_page())
    assert err.value.code == "TOO_MANY_OPTIMIZERS"
