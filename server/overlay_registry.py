"""T1 overlay token registry — the closed vocabulary and its grammars.

WHY A CLOSED REGISTRY. The first draft of this design constrained token VALUES
and left token NAMES open. An adversarial review caught it immediately: a name
that reaches `--${key}: value` is a CSS injection, so the value grammar never
gets a chance to matter. Here the model does not name a token — it SELECTS one
from a fixed server-owned list. A submitted property name is never interpolated
anywhere, and an unknown id is refused rather than passed through.

WHY EACH TOKEN DECLARES ITS SINK. The consumer is part of the security
boundary: a perfectly valid colour is unsafe inside `url()`, `content`,
`filter`, `mask`, or a security-state indicator. A token therefore carries
where it is allowed to land, and the renderer honours that — validation alone
cannot make a value safe without knowing where it goes.

WHY SOME COPY IS EXCLUDED BY CLASS. A copy string can be flawless text and
still make the product LIE: relabel Delete as Save, forge a status, alter a
consent line, impersonate support. No amount of escaping addresses that, so
auth, billing, legal, permission, destructive-action, system-status and
accessibility strings are simply not in the registry. That is a property of the
vocabulary, never a reviewer's judgement call in the moment.

WHY VALIDATION RUNS ON THE COMPOSED OVERLAY. Individually-valid tokens compose
into an attack — set foreground and background to near-identical colours and a
warning disappears without a single invalid value. Cross-token constraints are
checked against the merged result, not the diff.

REFUSE, DO NOT SANITISE. Every parser here consumes the WHOLE input and emits
one canonical form. A value that does not parse is rejected; it is never
cleaned up and accepted. Sanitising invites a bypass hunt, refusing does not.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

# --------------------------------------------------------------------------- #
# Value kinds and sinks
# --------------------------------------------------------------------------- #
KIND_COLOR = "color"
KIND_COPY = "copy"

#: The ONLY places a token may be rendered. Anything not listed cannot be a
#: sink, which is what keeps a colour out of url()/content()/filter().
SINK_CSS_COLOR_PROP = "css_color_prop"   # `--token: #rrggbb` in :root, colour props only
SINK_TEXT_NODE = "text_node"             # React text child; never an attribute
SINK_QUOTED_ATTR = "quoted_attr"         # e.g. aria-label/title, escaped


class OverlayTokenError(RuntimeError):
    def __init__(self, code: str, status_code: int = 422, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = str(detail)


@dataclass(frozen=True)
class TokenSpec:
    token_id: str
    kind: str
    sink: str
    #: Operator-facing description; also what the approval card renders.
    label: str
    #: Copy only: hard cap. Short by design — a caption is not a document.
    max_len: int = 0


def _color(token_id: str, label: str) -> TokenSpec:
    return TokenSpec(token_id, KIND_COLOR, SINK_CSS_COLOR_PROP, label)


def _copy(token_id: str, label: str, max_len: int = 120) -> TokenSpec:
    return TokenSpec(token_id, KIND_COPY, SINK_TEXT_NODE, label, max_len)


#: THE CLOSED VOCABULARY. v1 is colours + copy only (operator decision,
#: 2026-08-04). Adding an entry is a reviewed code change, which is the point:
#: the blast radius of this lane is exactly this list.
#:
#: Deliberately ABSENT, and why — these are the strings that let safe text lie:
#:   auth / sign-in, billing and prices, legal and consent, permission and
#:   entitlement, destructive-action labels (delete/remove/revoke), system
#:   status ("saved", "secure", "offline"), and accessibility strings that a
#:   screen-reader user cannot cross-check against the visual.
REGISTRY: Dict[str, TokenSpec] = {
    spec.token_id: spec
    for spec in (
        _color("color.canvas.bg", "Workspace background"),
        _color("color.canvas.fg", "Workspace text"),
        _color("color.panel.bg", "Panel background"),
        _color("color.panel.fg", "Panel text"),
        _color("color.accent", "Accent"),
        _color("color.accent.fg", "Text on accent"),
        _color("color.border", "Borders"),
        _copy("copy.home.title", "Home heading", 80),
        _copy("copy.home.subtitle", "Home subheading", 160),
        _copy("copy.empty.result", "Empty result message", 160),
        _copy("copy.cta.primary", "Primary button label", 32),
    )
}

#: Pairs that must stay readable together. The composed-overlay check enforces
#: these AFTER merge, because each colour on its own is perfectly valid.
CONTRAST_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("color.canvas.fg", "color.canvas.bg"),
    ("color.panel.fg", "color.panel.bg"),
    ("color.accent.fg", "color.accent"),
)

#: WCAG AA for normal text. Below this, a warning can be hidden in plain sight.
MIN_CONTRAST = 4.5

#: The committed platform defaults, matching the shipped dark-emerald theme.
#:
#: These live here rather than being read from CSS because `validate_overlay`
#: REQUIRES a concrete value for the side of a contrast pair a proposal does
#: not touch. Without them, a change that darkens only the foreground has
#: nothing to be checked against and the composed contrast rule silently does
#: nothing, which is the failure the rule exists to prevent.
#:
#: They are the fallback, not the source of truth for rendering: the web app
#: still ships its own CSS defaults, and an overlay only ever OVERRIDES them.
#: If the two drift the contrast check is computed against a slightly stale
#: baseline, which is a warning that is too strict or too lax by a little, not
#: a wrong colour on screen.
PLATFORM_DEFAULTS: Dict[str, str] = {
    "color.canvas.bg": "#0b1210",
    "color.canvas.fg": "#e8f2ee",
    "color.panel.bg": "#111c19",
    "color.panel.fg": "#dfeae6",
    "color.accent": "#1f7a5a",
    "color.accent.fg": "#ffffff",
    "color.border": "#22322d",
}


def defaults() -> Dict[str, str]:
    """A copy of the committed defaults, so a caller cannot mutate the module
    constant and change what every later contrast check is measured against."""
    return dict(PLATFORM_DEFAULTS)


# --------------------------------------------------------------------------- #
# Colour grammar — full-input parse, one canonical output
# --------------------------------------------------------------------------- #
_HEX_RE = re.compile(r"\A#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")
_RGB_RE = re.compile(
    r"\Argb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)\Z")

#: Refused outright wherever they appear. `var(` would let one token reference
#: another (and escape the closed vocabulary); the rest are comment, escape,
#: statement-break and control characters that end the value's context.
_FORBIDDEN_SUBSTRINGS = ("/*", "*/", "\\", ";", "}", "{", "url(", "var(",
                         "expression(", "<", ">", "\x00")

#: CSS-wide keywords are not colours; accepting one lets a token inherit an
#: attacker-chosen value from an ancestor rather than carry its own.
_CSS_WIDE = frozenset({"inherit", "initial", "unset", "revert", "revert-layer"})


def parse_color(raw: object) -> str:
    """Return the ONE canonical form `#rrggbb`, or raise.

    Regenerating the output from the parse (rather than echoing the input) is
    what makes this safe: nothing the caller wrote survives into the stylesheet.
    """
    if not isinstance(raw, str):
        raise OverlayTokenError("color_invalid", 422, f"type={type(raw).__name__}")
    value = raw.strip()
    if not value or len(value) > 32:
        raise OverlayTokenError("color_invalid", 422, "empty_or_too_long")
    lowered = value.lower()
    if lowered in _CSS_WIDE:
        raise OverlayTokenError("color_invalid", 422, "css_wide_keyword")
    for bad in _FORBIDDEN_SUBSTRINGS:
        if bad in value:
            raise OverlayTokenError("color_invalid", 422, f"forbidden:{bad!r}")
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in value):
        raise OverlayTokenError("color_invalid", 422, "control_char")

    m = _HEX_RE.match(value)
    if m:
        digits = m.group(1)
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return "#" + digits.lower()

    m = _RGB_RE.match(value)
    if m:
        parts = [int(p) for p in m.groups()]
        if any(p > 255 for p in parts):
            raise OverlayTokenError("color_invalid", 422, "channel_out_of_range")
        return "#" + "".join(f"{p:02x}" for p in parts)

    raise OverlayTokenError("color_invalid", 422, "unrecognised_form")


# --------------------------------------------------------------------------- #
# Copy grammar — plain text, normalised, no invisible or directional tricks
# --------------------------------------------------------------------------- #
#: Bidi overrides/isolates reorder what the eye reads; zero-width and filler
#: characters are invisible. Both let copy display as something other than what
#: it is, which is the same class of problem as a homoglyph path.
_BIDI_AND_INVISIBLE = frozenset(
    "؜​‌‍‎‏⁠⁦⁧⁨⁩"
    "‪‫‬‭‮﻿­͏ㅤ"
)


def parse_copy(raw: object, *, max_len: int) -> str:
    """Return NFC-normalised plain text, or raise.

    Normalise first so a decomposed form cannot smuggle a character past the
    checks, then reject rather than strip — a stripped string is a different
    string from the one the operator approved.
    """
    if not isinstance(raw, str):
        raise OverlayTokenError("copy_invalid", 422, f"type={type(raw).__name__}")
    value = unicodedata.normalize("NFC", raw)
    if not value.strip():
        raise OverlayTokenError("copy_invalid", 422, "empty")
    if len(value) > max_len:
        raise OverlayTokenError("copy_invalid", 422, f"len>{max_len}")
    if "\n" in value or "\r" in value:
        # Single-line by construction: multi-line copy can fake a whole UI
        # region inside one label.
        raise OverlayTokenError("copy_invalid", 422, "multiline")
    for ch in value:
        if ch in _BIDI_AND_INVISIBLE:
            raise OverlayTokenError(
                "copy_invalid", 422, f"invisible_or_bidi:U+{ord(ch):04X}")
        if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"}:
            raise OverlayTokenError(
                "copy_invalid", 422, f"control_char:U+{ord(ch):04X}")
    return value


# --------------------------------------------------------------------------- #
# Per-token and composed validation
# --------------------------------------------------------------------------- #
def validate_token(token_id: object, value: object) -> Tuple[str, str]:
    """Validate ONE (id, value). The id must be a registry member — a name the
    caller invented is refused here, before it could reach any renderer."""
    if not isinstance(token_id, str) or token_id not in REGISTRY:
        raise OverlayTokenError("token_unknown", 422, f"id={token_id!r}")
    spec = REGISTRY[token_id]
    if spec.kind == KIND_COLOR:
        return token_id, parse_color(value)
    if spec.kind == KIND_COPY:
        return token_id, parse_copy(value, max_len=spec.max_len)
    raise OverlayTokenError("token_kind_unknown", 500, spec.kind)


def _luminance(hex_color: str) -> float:
    def channel(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def validate_overlay(
    proposed: Mapping[str, object],
    *,
    current: Optional[Mapping[str, str]] = None,
    defaults: Mapping[str, str],
) -> Dict[str, str]:
    """Validate a proposal AND the overlay it would produce.

    `defaults` is the committed platform default for every colour in a contrast
    pair — the composed check needs a concrete value for the side the proposal
    does not touch, or a change that darkens only the foreground would slip
    through by having nothing to compare against.
    """
    if not isinstance(proposed, Mapping) or not proposed:
        raise OverlayTokenError("overlay_empty", 422)
    if len(proposed) > len(REGISTRY):
        raise OverlayTokenError("overlay_too_large", 422, str(len(proposed)))

    clean: Dict[str, str] = {}
    for token_id, value in proposed.items():
        tid, parsed = validate_token(token_id, value)
        clean[tid] = parsed

    # COMPOSE, then check. Each value above is individually valid; the attack
    # lives in the combination.
    composed: Dict[str, str] = dict(defaults)
    if current:
        composed.update(current)
    composed.update(clean)

    for fg_id, bg_id in CONTRAST_PAIRS:
        fg, bg = composed.get(fg_id), composed.get(bg_id)
        if not fg or not bg:
            continue
        ratio = contrast_ratio(fg, bg)
        if ratio < MIN_CONTRAST:
            raise OverlayTokenError(
                "contrast_too_low", 422,
                f"{fg_id}/{bg_id}={ratio:.2f} < {MIN_CONTRAST}")
    return clean


def render_css_vars(overlay: Mapping[str, str]) -> str:
    """Serialise to `:root{...}`. Only colour-sink tokens render as CSS, and
    each value is re-parsed on the way out — the renderer never trusts that
    what it was handed came through validate_overlay().
    """
    parts: List[str] = []
    for token_id, value in sorted(overlay.items()):
        spec = REGISTRY.get(token_id)
        if spec is None or spec.sink != SINK_CSS_COLOR_PROP:
            continue
        safe = parse_color(value)
        parts.append(f"--{token_id.replace('.', '-')}:{safe}")
    return ":root{" + ";".join(parts) + "}" if parts else ""
