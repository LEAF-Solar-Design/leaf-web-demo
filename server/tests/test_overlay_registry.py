"""T1 overlay token registry — closed vocabulary, grammars, composed checks.

Every test names the attack it prevents. The four the adversarial review of the
spec called out by name are sections 1, 3, 4 and 5.

Run:  cd server && python -m pytest tests/test_overlay_registry.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import overlay_registry as reg  # noqa: E402

DEFAULTS = {
    "color.canvas.bg": "#ffffff",
    "color.canvas.fg": "#1a1a1a",
    "color.panel.bg": "#f5f5f5",
    "color.panel.fg": "#1a1a1a",
    "color.accent": "#0f6e56",
    "color.accent.fg": "#ffffff",
    "color.border": "#d0d0d0",
}


# --------------------------------------------------------------------------- #
# 1. THE finding: token NAMES are closed, never interpolated
# --------------------------------------------------------------------------- #
def test_unknown_token_id_is_refused():
    """The v1 draft constrained values and left names open, so `--${key}:`
    was a CSS injection regardless of how good the value grammar was."""
    with pytest.raises(reg.OverlayTokenError) as exc:
        reg.validate_token("color.evil; } body { background: red", "#fff")
    assert exc.value.code == "token_unknown"


def test_injection_shaped_ids_cannot_reach_the_renderer():
    for bad in ("--x: red; --y", "color.canvas.bg}", "a{b", "url(x)",
                "color.canvas.bg\n", "", None, 42):
        with pytest.raises(reg.OverlayTokenError):
            reg.validate_token(bad, "#ffffff")


def test_rendered_css_only_contains_registry_derived_names():
    css = reg.render_css_vars({"color.canvas.bg": "#ffffff"})
    assert css == ":root{--color-canvas-bg:#ffffff}"


def test_renderer_revalidates_rather_than_trusting_its_input():
    """Defence in depth: a value that reached the renderer without passing
    validate_overlay still cannot emit raw CSS."""
    with pytest.raises(reg.OverlayTokenError):
        reg.render_css_vars({"color.canvas.bg": "red; } body { display:none"})


# --------------------------------------------------------------------------- #
# 2. Colour grammar: one canonical output, whole-input parse
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("#fff", "#ffffff"),
    ("#FFF", "#ffffff"),
    ("#0F6E56", "#0f6e56"),
    ("  #abcdef  ", "#abcdef"),
    ("rgb(15, 110, 86)", "#0f6e56"),
    ("rgb(0,0,0)", "#000000"),
])
def test_colors_are_reserialised_to_one_form(raw, expected):
    """The stored value is regenerated from the parse — nothing the caller
    wrote survives into the stylesheet."""
    assert reg.parse_color(raw) == expected


@pytest.mark.parametrize("bad", [
    "red",                          # named colours are not in the grammar
    "inherit", "initial", "unset", "revert",   # CSS-wide keywords
    "#fff;color:red",               # statement break
    "#fff /* c */",                 # comment
    "#fff\\",                       # escape
    "url(javascript:alert(1))",
    "var(--other)",                 # would escape the closed vocabulary
    "expression(alert(1))",
    "rgb(300,0,0)",                 # out of range
    "rgb(0,0,0) extra",             # trailing tokens
    "#ffff",                        # wrong digit count
    "#gggggg",
    "#fff\x00",
    "rgba(0,0,0,0.5)",              # alpha not supported in v1
    "",
    None,
    123,
])
def test_bad_colors_are_refused_not_cleaned(bad):
    with pytest.raises(reg.OverlayTokenError) as exc:
        reg.parse_color(bad)
    assert exc.value.code == "color_invalid"


# --------------------------------------------------------------------------- #
# 3. Copy grammar: invisible and directional tricks refused
# --------------------------------------------------------------------------- #
def test_plain_copy_is_normalised_and_kept():
    assert reg.parse_copy("Run a tool", max_len=32) == "Run a tool"


@pytest.mark.parametrize("bad,why", [
    ("a‮b", "RLO reverses what the eye reads"),
    ("a​b", "zero-width space"),
    ("a‎b", "LRM"),
    ("a﻿b", "BOM"),
    ("a­b", "soft hyphen"),
    ("aㅤb", "Hangul filler is invisible"),
    ("line\nbreak", "multi-line copy can fake a UI region"),
    ("nul\x00here", "control char"),
])
def test_invisible_and_directional_copy_refused(bad, why):
    with pytest.raises(reg.OverlayTokenError) as exc:
        reg.parse_copy(bad, max_len=120)
    assert exc.value.code == "copy_invalid", why


def test_copy_length_is_capped_per_token():
    spec = reg.REGISTRY["copy.cta.primary"]
    assert spec.max_len == 32
    with pytest.raises(reg.OverlayTokenError):
        reg.validate_token("copy.cta.primary", "x" * 33)


# --------------------------------------------------------------------------- #
# 4. The strings that let safe text LIE are absent by construction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("forbidden_id", [
    "copy.auth.signin", "copy.billing.price", "copy.legal.consent",
    "copy.permission.grant", "copy.action.delete", "copy.status.saved",
    "copy.a11y.label", "copy.security.badge",
])
def test_lying_string_classes_are_not_in_the_vocabulary(forbidden_id):
    """No escaping addresses 'relabel Delete as Save' — the only fix is that
    such strings are not overlayable at all."""
    assert forbidden_id not in reg.REGISTRY
    with pytest.raises(reg.OverlayTokenError) as exc:
        reg.validate_token(forbidden_id, "Save")
    assert exc.value.code == "token_unknown"


def test_every_registry_entry_declares_a_known_sink():
    """The consumer is part of the boundary: a token with no declared sink
    could be rendered anywhere."""
    allowed = {reg.SINK_CSS_COLOR_PROP, reg.SINK_TEXT_NODE, reg.SINK_QUOTED_ATTR}
    for spec in reg.REGISTRY.values():
        assert spec.sink in allowed
        assert spec.kind in (reg.KIND_COLOR, reg.KIND_COPY)
        if spec.kind == reg.KIND_COPY:
            assert spec.max_len > 0


# --------------------------------------------------------------------------- #
# 5. Composed validation: individually-valid tokens that combine into an attack
# --------------------------------------------------------------------------- #
def test_each_color_valid_but_the_pair_hides_the_text():
    """THE composition attack: no single value is invalid, yet the result
    makes content invisible — a warning disappears in plain sight."""
    with pytest.raises(reg.OverlayTokenError) as exc:
        reg.validate_overlay(
            {"color.canvas.fg": "#fefefe", "color.canvas.bg": "#ffffff"},
            defaults=DEFAULTS)
    assert exc.value.code == "contrast_too_low"


def test_a_one_sided_change_is_checked_against_the_composed_result():
    """Touching only the foreground still composes with the existing
    background — that is why validation runs on the merge, not the diff."""
    with pytest.raises(reg.OverlayTokenError) as exc:
        reg.validate_overlay({"color.canvas.fg": "#fdfdfd"}, defaults=DEFAULTS)
    assert exc.value.code == "contrast_too_low"


def test_a_readable_light_mode_passes():
    """The motivating request must actually work."""
    out = reg.validate_overlay(
        {"color.canvas.bg": "#ffffff", "color.canvas.fg": "#111111",
         "copy.home.title": "Light mode"},
        defaults=DEFAULTS)
    assert out["color.canvas.bg"] == "#ffffff"
    assert out["copy.home.title"] == "Light mode"


def test_composition_accounts_for_an_already_approved_overlay():
    """A second innocuous-looking proposal must not combine with the LIVE
    overlay to erase contrast."""
    current = {"color.panel.bg": "#101010"}
    with pytest.raises(reg.OverlayTokenError):
        reg.validate_overlay({"color.panel.fg": "#121212"},
                             current=current, defaults=DEFAULTS)


def test_contrast_ratio_is_symmetric_and_bounded():
    assert reg.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert reg.contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
    assert reg.contrast_ratio("#777777", "#777777") == pytest.approx(1.0, abs=0.01)


def test_empty_or_oversized_overlays_refused():
    with pytest.raises(reg.OverlayTokenError):
        reg.validate_overlay({}, defaults=DEFAULTS)
    with pytest.raises(reg.OverlayTokenError):
        reg.validate_overlay({f"k{i}": "#fff" for i in range(99)}, defaults=DEFAULTS)
