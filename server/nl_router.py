"""
NL prompt router (CONTRACT-ADDENDUM section 12) — MATRIX frontend gap #2.

Turns ONE free-text prompt into a lane decision the three-lane product shell
can dispatch:

    classify(text, tools) -> {
        "lane":       "run" | "solve" | "build",
        "tool":       <registered tool name> | None,
        "params":     { ...prefilled from the text... },
        "confidence": 0.0 .. 1.0,
        "rationale":  "<one calm, user-visible sentence>",
    }

DESIGN — v1 is deliberately ZERO-LLM (resolves the MATRIX tension in favour of
the zero-LLM runtime: no model sits in the request hot path). Matching is a pure
deterministic function over the LIVE catalog (names, engine_ops, descriptions,
param schemas, capabilities) using normalised token/synonym overlap, with a
name-token-dominant weighting so grouping intent ("count panels PER LAYER" ->
`count-by-layer`) beats a shallower panel-name match. Numeric values and DWG
handles are lifted straight out of the text into the matched tool's params.
Explicit solve intent binds the best matching tool declaring the solve capability
for confirmation, or offers available solvers when none matches the request.

    ┌─────────────────────── LLM-classifier seam (documented, OFF in v1) ───────┐
    │ `classify(..., llm_classifier=fn)` lets a FUTURE lane inject an optional  │
    │ LLM classifier. It is consulted ONLY when the deterministic result is     │
    │ ambiguous (confidence < LLM_ESCALATION_CONF) AND a classifier was passed  │
    │ — so the zero-LLM runtime is the default and the LLM never enters the hot │
    │ path unless a caller explicitly opts a request into it. v1 always passes  │
    │ None.  Contract for the callable:                                         │
    │     fn(text, tools, deterministic) -> dict | None   (None => keep det.)   │
    └──────────────────────────────────────────────────────────────────────────┘

This module has NO FastAPI / HTTP dependency so it is unit-testable offline
(see tests/test_nl_router.py). routers/prompt.py is the thin endpoint wrapper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------- #
# lanes
# --------------------------------------------------------------------------- #
LANE_RUN = "run"
LANE_SOLVE = "solve"
LANE_BUILD = "build"

# confidence below which a future LLM classifier (if injected) is consulted.
LLM_ESCALATION_CONF = 0.55
MIN_RUN_MATCH_CONF = 0.55

# --------------------------------------------------------------------------- #
# normalisation vocabulary
# --------------------------------------------------------------------------- #
_STOPWORDS: Set[str] = {
    "the", "a", "an", "of", "on", "in", "to", "for", "with", "and", "or",
    "me", "my", "our", "please", "that", "this", "is", "are", "be", "it",
    "all", "every", "at", "as", "from", "into", "then", "some", "any", "which",
    "whose", "do", "does", "give", "get", "show",  # 'show' too generic to match on
}

# multi-word rewrites applied to the raw lowercased string BEFORE tokenising.
_PHRASE_SYNONYMS: List[Tuple[str, str]] = [
    ("per layer", "by layer"),
    ("for each", "by"),
    ("grouped by", "by"),
    ("broken down by", "by"),
    ("square feet", "area"),
    ("square foot", "area"),
    ("sq ft", "area"),
    ("sq. ft", "area"),
    ("how many", "count"),
    ("number of", "count"),
    ("close to", "near"),
    ("next to", "near"),
]

# single-token rewrites applied AFTER tokenising, BEFORE stemming/stopwords.
# Maps user vocabulary onto the catalog's canonical vocabulary.
_TOKEN_SYNONYMS: Dict[str, str] = {
    "per": "by", "each": "by", "grouped": "by",
    "within": "near", "close": "near", "nearby": "near", "closest": "near",
    "remove": "delete", "erase": "delete", "del": "delete", "drop": "delete",
    "module": "panel", "modules": "panel", "pv": "panel", "solarpanel": "panel",
    "sqft": "area", "footage": "area",
    "tally": "count",
    "flag": "highlight", "mark": "highlight", "select": "highlight",
    "enumerate": "list",
    "measurement": "measure",
    "boundary": "edge", "perimeter": "edge",
}

# authoring intent — "build/make a tool that ..."
_AUTHORING_VERBS: Set[str] = {
    "build", "make", "create", "author", "generate", "write",
    "add", "need", "want", "design", "develop", "scaffold",
}
_AUTHORING_NOUNS: Set[str] = {
    "tool", "tools", "command", "capability", "widget", "feature",
    "function", "script", "utility", "macro", "report", "something",
}
# strong, unambiguous authoring phrasing (verb-independent).
_AUTHORING_PHRASE = re.compile(r"\btools?\s+(that|to|which|for)\b", re.IGNORECASE)

# Explicit requests for a new tool can contain the name of a registered tool.
_EXPLICIT_BUILD_RE = re.compile(
    r"\b(?:make|build|create|author|write|add|need|want|develop|code)"
    r"(?:\s+(?:me|us))?(?:\s+(?:a|an|the|new|one))?"
    r"(?:\s+(?:new|brand-new|branch|autocad|c#|csharp|custom|small|simple|quick|studio|cad|plugin)){0,3}"
    r"\s+(?:tools?|commands?|script|plugin|macro)\b"
    r"|^\s*new\s+(?:(?:branch|autocad|studio)\s+)?(?:tool|command)\s*:"
    r"|\b(?:brand-new\s+tool|new\s+branch\s+tool|new\s+autocad\s+command)\b",
    re.IGNORECASE,
)
# Scaffolding tokens stripped before checking whether the text just NAMES an existing
# tool (normalised vocabulary — matches _norm_tokens output).
_BUILD_SCAFFOLD: Set[str] = {
    "make", "build", "create", "author", "generate", "write", "need", "want",
    "design", "develop", "scaffold", "new", "tool", "command", "capability",
    "widget", "feature", "function", "script", "utility", "macro", "report",
    "something",
}

# explicit optimisation / solver intent (binds solve-capable catalog tools).
_SOLVE_VERBS: Set[str] = {
    "optimize", "optimise", "solve", "maximize", "maximise",
    "minimize", "minimise", "pack", "arrange", "reposition",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# a number, optionally decimal (extraction operates on the RAW text).
_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)")


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def _norm_tokens(text: str) -> List[str]:
    """Lowercase -> phrase rewrite -> tokenise -> synonym -> stem -> drop
    stopwords and pure numbers. Order is preserved (for exact-phrase checks)."""
    if not text:
        return []
    s = text.lower()
    for a, b in _PHRASE_SYNONYMS:
        if a in s:
            s = s.replace(a, b)
    out: List[str] = []
    for raw in _TOKEN_RE.findall(s):
        tok = _TOKEN_SYNONYMS.get(raw, raw)
        if len(tok) > 3 and tok.endswith("s"):  # tiny stemmer: panels -> panel
            tok = tok[:-1]
        if tok in _STOPWORDS:
            continue
        if tok.isdigit():  # numbers are for extraction, not matching
            continue
        out.append(tok)
    return out


def _raw_tokens(text: str) -> List[str]:
    """Lowercased alphanumeric tokens with NO synonym/stopword processing —
    used for intent-verb detection so 'optimize'/'maximize' match literally."""
    return _TOKEN_RE.findall((text or "").lower())


def _is_internal(tool: Dict[str, Any]) -> bool:
    """Mirror catalog.py's server-side filter (internal flag or qa-/_ prefix)."""
    if tool.get("internal"):
        return True
    name = str(tool.get("name", ""))
    return name.startswith("qa-") or name.startswith("_")


def _solve_capable(tool: Dict[str, Any]) -> bool:
    capabilities = tool.get("capabilities")
    return isinstance(capabilities, (list, tuple)) and "solve" in capabilities


# --------------------------------------------------------------------------- #
# per-tool token document + scoring
# --------------------------------------------------------------------------- #
@dataclass
class _ToolDoc:
    tool: Dict[str, Any]
    name: List[str]
    op: Set[str]
    other: Set[str]  # description + param names/descriptions + capabilities


def _tool_doc(tool: Dict[str, Any]) -> _ToolDoc:
    name = _norm_tokens(str(tool.get("name", "")))
    op = set(_norm_tokens(str(tool.get("engine_op", ""))))
    other: Set[str] = set(_norm_tokens(str(tool.get("description", ""))))
    props = ((tool.get("params") or {}).get("properties") or {})
    if isinstance(props, dict):
        for key, spec in props.items():
            other |= set(_norm_tokens(str(key)))
            if isinstance(spec, dict):
                other |= set(_norm_tokens(str(spec.get("description", ""))))
    for cap in (tool.get("capabilities") or []):
        other |= set(_norm_tokens(str(cap)))
    return _ToolDoc(tool=tool, name=name, op=op, other=other)


# field weights: the NAME is the canonical short label of what a tool does, so a
# name-token hit is worth 3x an engine_op / description hit. This is what makes
# "per layer" (grouping) route to the tool whose NAME encodes the grouping.
_W_NAME = 3.0
_W_OP = 1.0
_W_OTHER = 1.0


@dataclass
class _Score:
    doc: _ToolDoc
    raw: float
    name_cov: float
    q_cov: float
    exact: bool


def _score(doc: _ToolDoc, q: Set[str], q_seq: List[str]) -> _Score:
    name_set = set(doc.name)
    name_hit = q & name_set
    op_hit = (q & doc.op) - name_hit
    other_hit = (q & doc.other) - name_hit - op_hit
    raw = _W_NAME * len(name_hit) + _W_OP * len(op_hit) + _W_OTHER * len(other_hit)

    name_cov = len(name_hit) / max(1, len(name_set))
    matched_any = name_hit | op_hit | other_hit
    q_cov = len(matched_any) / max(1, len(q))
    exact = bool(doc.name) and _contiguous(doc.name, q_seq)
    return _Score(doc=doc, raw=raw, name_cov=name_cov, q_cov=q_cov, exact=exact)


def _contiguous(needle: Sequence[str], hay: Sequence[str]) -> bool:
    """Does the tool's name-token sequence appear contiguously in the query?"""
    n, h = list(needle), list(hay)
    if not n or len(n) > len(h):
        return False
    for i in range(len(h) - len(n) + 1):
        if h[i:i + len(n)] == n:
            return True
    return False


def _confidence(s: _Score) -> float:
    if s.exact:
        return 0.97
    # calibrated so: exact ~0.97, strong overlap 0.7-0.9, weak <=0.5.
    conf = 0.15 + 0.60 * s.name_cov + 0.25 * s.q_cov
    return round(min(conf, 0.95), 2)


def _ranked(q: Set[str], q_seq: List[str], docs: List[_ToolDoc]) -> List[_Score]:
    """All tools with any overlap, best-first. Deterministic ranking: raw, then name
    coverage, then a SHORTER name (more specific), then alphabetical for stability."""
    scored = [_score(d, q, q_seq) for d in docs]
    scored = [s for s in scored if s.raw > 0]
    scored.sort(key=lambda s: (
        -s.raw, -s.name_cov, len(s.doc.name), str(s.doc.tool.get("name", "")),
    ))
    return scored


def _alternatives(ranked: List[_Score], exclude_first: bool) -> List[Dict[str, Any]]:
    """Top ≤3 non-winning tool matches as [{tool, confidence}] (Contract 4a). For a
    bound RUN or SOLVE match the winner is ranked[0] (excluded); without a winning
    tool, the top matches are all 'non-winning'. May be empty."""
    items = ranked[1:] if exclude_first else ranked
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for s in items:
        name = str(s.doc.tool.get("name"))
        if name in seen:
            continue
        seen.add(name)
        out.append({"tool": name, "confidence": _confidence(s)})
        if len(out) >= 3:
            break
    return out


def _explicit_build(text: str) -> bool:
    """Whether the request explicitly frames its description as a new tool."""
    return _EXPLICIT_BUILD_RE.search(text or "") is not None


def _names_tool_exactly(best: "_Score", text: str) -> bool:
    """True iff, after stripping authoring scaffolding, the remaining CONTENT tokens are
    all within the matched tool's name tokens — i.e. the text essentially NAMES that
    existing tool exactly (e.g. 'make a tool: count panels'), so the RUN match is honoured
    even under explicit build phrasing. A longer novel description (extra content tokens
    beyond the name, e.g. '... smaller than 10 sqft') is NOT an exact name -> Build."""
    name = set(best.doc.name)
    description = _EXPLICIT_BUILD_RE.sub(" ", text, count=1)
    residual = [t for t in _norm_tokens(description) if t not in _BUILD_SCAFFOLD]
    if not residual:
        return False
    return set(residual) <= name


# --------------------------------------------------------------------------- #
# param extraction (numbers -> numeric params; hex-ish token -> handle param)
# --------------------------------------------------------------------------- #
def _extract_params(tool: Dict[str, Any], text: str) -> Dict[str, Any]:
    props = ((tool.get("params") or {}).get("properties") or {})
    if not isinstance(props, dict) or not props:
        return {}
    params: Dict[str, Any] = {}
    consumed: Set[str] = set()

    numbers = _NUMBER_RE.findall(text)

    # handle param first (DWG handles are hex strings; keep them as STRINGS).
    if "handle" in props:
        m = re.search(
            r"(?:handle|panel|entity|#)\s*[:#]?\s*([0-9A-Fa-f]{2,})", text, re.IGNORECASE
        )
        handle = m.group(1) if m else (numbers[0] if numbers else None)
        if handle is not None:
            params["handle"] = str(handle)
            consumed.add(str(handle))

    # numeric params, in schema order, take the remaining numbers in text order.
    remaining = [n for n in numbers if n not in consumed]
    for key, spec in props.items():
        if key in params or not isinstance(spec, dict):
            continue
        if tool.get("name") == "count-modules" and key in {
            "layer_count_at_least", "layer_share_above", "exclude_highest_count_tiers",
        }:
            continue
        if spec.get("type") in ("number", "integer") and remaining:
            val = remaining.pop(0)
            params[key] = float(val) if "." in val else int(val)
    return params


# --------------------------------------------------------------------------- #
# result shape
# --------------------------------------------------------------------------- #
@dataclass
class Classification:
    lane: str
    tool: Optional[str]
    params: Dict[str, Any]
    confidence: float
    rationale: str
    alternatives: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lane": self.lane,
            "tool": self.tool,
            "params": dict(self.params),
            "confidence": round(float(self.confidence), 2),
            "rationale": self.rationale,
            "alternatives": [dict(a) for a in self.alternatives],
        }


def _params_phrase(params: Dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


# --------------------------------------------------------------------------- #
# intent detection
# --------------------------------------------------------------------------- #
def _is_authoring(text: str, raw_toks: List[str]) -> bool:
    if _AUTHORING_PHRASE.search(text or ""):
        return True
    toks = set(raw_toks)
    return bool(_AUTHORING_VERBS & toks) and bool(_AUTHORING_NOUNS & toks)


def _is_solve(raw_toks: List[str]) -> bool:
    return bool(_SOLVE_VERBS & set(raw_toks))


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def classify(
    text: str,
    tools: List[Dict[str, Any]],
    llm_classifier: Optional[Callable[[str, List[Dict[str, Any]], Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Route a free-text prompt to a lane. Pure, deterministic, offline.

    `tools` is the LIVE catalog (deps.all_tools()); internal/QA tools are
    filtered here exactly as /api/capabilities does. `llm_classifier` is the
    documented OPTIONAL seam (see module header) — None in v1.
    """
    text = (text or "").strip()
    q_seq = _norm_tokens(text)
    q: Set[str] = set(q_seq)
    raw_toks = _raw_tokens(text)

    catalog = [t for t in (tools or []) if isinstance(t, dict) and t.get("name") and not _is_internal(t)]
    docs = [_tool_doc(t) for t in catalog]
    ranked = _ranked(q, q_seq, docs)
    best = ranked[0] if ranked else None

    result = _decide(text, q_seq, raw_toks, best, ranked, docs)

    # --- documented LLM-classifier seam (OFF in v1) -------------------------- #
    if llm_classifier is not None and result.confidence < LLM_ESCALATION_CONF:
        try:
            override = llm_classifier(text, catalog, result.to_dict())
        except Exception:  # a broken classifier must never take down routing
            override = None
        if isinstance(override, dict) and override.get("lane") in (LANE_RUN, LANE_SOLVE, LANE_BUILD):
            merged = result.to_dict()
            merged.update(override)
            return Classification(
                lane=merged["lane"],
                tool=merged.get("tool"),
                params=merged.get("params") or {},
                confidence=float(merged.get("confidence", result.confidence)),
                rationale=str(merged.get("rationale", result.rationale)),
            ).to_dict()

    return result.to_dict()


# Only a complete grouped panel request can preselect a role. Extra constraints
# must remain on the ordinary confirmation path.
_GROUPED_PANELS = re.compile(
    r"(?:please\s+)?(?:count|tally|how many|give me the number of)\s+"
    r"(?:(?P<only>only|just)\s+)?(?:the\s+)?(?:solar\s+|pv\s+|photovoltaic\s+)?"
    r"(?:panels?|modules?)\s+(?:are\s+)?(?:on each|in each|for each|per|by)\s+"
    r"(?P<roof>roof(?:top)?\s+)?layers?"
    r"(?P<exclude>\s*(?:,|and)?\s*(?:excluding|without|leaving out|leave out|ignoring)"
    r"\s+(?:the\s+)?(?:roof\s+)?outlines?)?[.!?]?", re.I,
)


_VISIBLE_PANEL_LAYERS = re.compile(
    r"(?:please\s+)?(?:count|tally|how many|give me the number of)\s+"
    r"(?:(?:only|just)\s+)?(?:the\s+)?(?:solar\s+|pv\s+|photovoltaic\s+)?"
    r"(?:panels?|modules?)\s+(?:are\s+)?(?:on|in|from)\s+"
    r"(?:(?:only|just)\s+)?(?:the\s+)?visible\s+layers?(?:\s+only)?[.!?]?",
    re.I,
)


_COORDINATE = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_COVERAGE_EXCLUSION = re.compile(
    r"\b(?:excluding|exclude|without|omitting|omit|leaving out|leave out|ignoring|ignore)"
    r"\s+(?:the\s+)?(?:layers?\s+)?(?P<names>.+?)[.!?]*$", re.I,
)
_PAIR = rf"\(\s*{_COORDINATE}\s*,\s*{_COORDINATE}\s*\)"
_SHARE_WINDOW = re.compile(
    rf"\b(?:within|inside|in|between)\s+(?:(?:the\s+)?(?:window|region|box)\s+)?"
    rf"(?:from\s+)?{_PAIR}\s+(?:to|and)\s+{_PAIR}", re.I)
_SHARE_SELECTION = re.compile(
    r"(?:^|(?<=[,.?]))\s*(?:select\s+(?:only\s+)?|only\s+|for\s+)(?:the\s+)?"
    r"(?P<names>[^.?!]+?)\s+(?:(?:panel|module)\s+sets?|layers?)\b", re.I)
_SHARE_QUERY = re.compile(
    r"(?:(?:what(?:'s| is)?|show(?: me)?|give me)\s+(?:the\s+)?"
    r"(?:percentage|fraction|share|%)\s+of\s+(?:(?:all|the selected)\s+)?"
    r"(?:panels?|modules?|that selection)\s+(?:is\s+)?(?:on each|in each|per|by)\s+layer"
    r"|how are (?:panels|modules) distributed (?:across|among) layers"
    r"|(?:show(?: me)?\s+)?(?:the\s+)?(?:per-layer (?:panel |module )?(?:percentage|share)"
    r"|(?:panel |module )?layer share breakdown))", re.I)


_CUMULATIVE_SHARE_QUERY = re.compile(
    r"(?:(?:rank|sort|order) layers by (?:panel|module) count(?: then name)? and "
    r"(?:give|show|report) (?:the )?(?:running|cumulative) (?:share|percentage|fraction)"
    r"(?: after each layer)?|(?:show|give|report|list) (?:the )?(?:running|cumulative) "
    r"(?:panel|module) (?:share|percentage|fraction) (?:by|per) layer"
    r"(?:, largest first)?(?:\. break ties by layer name)?)", re.I,
)


def _share_change_params(text):
    if not (re.search(r"\bpercentage[ -]points?\b", text, re.I)
            and re.search(r"\blayers?\b", text, re.I)):
        return None
    match = re.search(r"\b(?:compare\s+|between\s+|from\s+)(.+?)\s+"
                      r"(?:against|with|versus|and|to)\s+(.+)", text, re.I)
    if not match:
        return None
    sides = []
    for side in match.groups():
        name_list = re.split(r"[,;.!?]|\s+(?:(?:in|as)\s+percentage[ -]points?"
                             r"|(?:by|per)\s+layer)\b", side, maxsplit=1, flags=re.I)[0]
        names = []
        for name in re.split(r"\s+plus\s+", name_list, flags=re.I):
            cleaned = re.sub(r"^the\s+(.+?)\s+blocks?$", r"\1", name.strip(), flags=re.I)
            if not cleaned:
                return None
            names.append(cleaned)
        sides.append(names)
    return {"by": "block", "before": sides[0], "after": sides[1]}


def _share_params(text):
    remaining = text.strip()
    params = {"layer_shares": True, "group_by": "layer", "panel_scope": "all_layers"}
    window = _SHARE_WINDOW.search(remaining)
    if window:
        params["region"] = [float(value) for value in window.groups()]
        remaining = remaining[:window.start()] + " " + remaining[window.end():]
    selection = _SHARE_SELECTION.search(remaining)
    if selection:
        params["layer_names"] = re.split(r",\s*(?:and\s+)?|\s+and\s+", selection["names"], flags=re.I)
        remaining = remaining[:selection.start()] + " " + remaining[selection.end():]
    query = remaining.strip(" ,.?!")
    if _CUMULATIVE_SHARE_QUERY.fullmatch(query):
        params["cumulative_shares"] = True
    elif not _SHARE_QUERY.fullmatch(query):
        return None
    if re.search(r"\b(?:selected|that selection)\b", query, re.I) and not (window or selection):
        return None
    return params


_LAYER_COUNT_THRESHOLD = re.compile(
    r"\b(?:(?:physical\s+)?(?:module\s+|panel\s+)?count\s+of\s+at\s+least\s+(?P<count>\d{1,9})(?=\s|$)"
    r"|(?:at\s+least|no\s+fewer\s+than)\s+(?P<modules>\d{1,9})\s+(?:physical\s+)?(?:modules?|panels?)\b)",
    re.I,
)
_LAYER_SHARE_THRESHOLD = re.compile(
    r"\b(?:more\s+than|greater\s+than|above|over|exceeds?)\s+(\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)",
    re.I,
)


def _layer_threshold_params(text):
    if not re.search(r"\blayers?\b", text, re.I):
        return None
    count = _LAYER_COUNT_THRESHOLD.search(text)
    share = next((
        match for match in _LAYER_SHARE_THRESHOLD.finditer(text)
        if re.search(r"\b(?:share|fraction)\s*(?:of\s+)?$", text[:match.start()], re.I)
        or re.match(
            r"\s+of\s+(?:(?:all|the|total|selected|physical)\s+)*(?:modules?|panels?|selection|total)\b",
            text[match.end():], re.I,
        )
    ), None)
    if not count or not share:
        return None
    first, second = sorted((count, share), key=lambda match: match.start())
    conjunction = text[first.end():second.start()]
    if not re.search(r"\band\b", conjunction, re.I) or re.search(r"\bor\b", conjunction, re.I):
        return None
    scope = "roof_layers" if re.search(r"\broof(?:top)?\s+layers?\b", text, re.I) else "all_layers"
    return {"group_by": "layer", "panel_scope": scope,
            "layer_count_at_least": int(count["count"] or count["modules"]),
            "layer_share_above": float(share[1])}


_LAYER_LEADERS = re.compile(
    r"(?:which|what)\s+(?P<roof>roof(?:top)?\s+)?layers?\s+"
    r"(?:(?:has|have|holds?|contains?)\s+(?:the\s+)?most\s+(?:solar\s+)?(?:modules?|panels?)"
    r"|(?:is|are)\s+(?:tied\s+for\s+)?(?:the\s+)?(?:largest|highest|greatest)\s+"
    r"(?:solar\s+)?(?:module|panel)\s+count)[?.!]?", re.I,
)
_TOP_LAYER_COUNT = re.compile(
    r"(?:rank|show|list|give me)\s+(?:the\s+)?top\s+(?P<n>\d{1,9}|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s+(?P<roof>roof(?:top)?\s+)?layers?\s+by\s+(?:solar\s+)?(?:module|panel)\s+count"
    r"(?:,?\s+(?:highest first|descending))?[?.!]?", re.I,
)


_RANKED_LAYER_HIGHLIGHTS = re.compile(
    r"(?:\s+and\s+|[,.!?;]\s*|\s+)\s*"
    r"(?:highlight\s+only|only\s+highlight)\s+(?:the\s+)?"
    r"(?:(?:ranked|top|winning)\s+layers|(?:tied\s+)?winners)[?.!]?$",
    re.I,
)


def _layer_ranking_params(text):
    query = text.strip()
    highlights = _RANKED_LAYER_HIGHLIGHTS.search(query)
    if highlights:
        query = query[:highlights.start()].rstrip(" ,.")
    match = _LAYER_LEADERS.fullmatch(query)
    limit = 1
    if match is None:
        match = _TOP_LAYER_COUNT.fullmatch(query)
        if match is None:
            return None
        number = match["n"].lower()
        words = "one two three four five six seven eight nine ten".split()
        limit = int(number) if number.isdecimal() else words.index(number) + 1
    scope = "roof_layers" if match["roof"] else "all_layers"
    return {"group_by": "layer", "panel_scope": scope, "top_layers": limit,
            **({"highlight_ranked_layers": True} if highlights else {})}


def _decide(
    text: str,
    q_seq: List[str],
    raw_toks: List[str],
    best: Optional[_Score],
    ranked: List[_Score],
    docs: List[_ToolDoc],
) -> Classification:
    # 1. AUTHORING intent -> Build lane (unless the user named an EXISTING tool
    #    almost exactly, in which case honour the run match).
    explicit_build = _explicit_build(text)
    if explicit_build or _is_authoring(text, raw_toks):
        honor_run = False
        if best is not None and best.exact:
            # An explicit build can still just name a registered tool.
            if not explicit_build or _names_tool_exactly(best, text):
                honor_run = True
        if not honor_run:
            strong = _AUTHORING_PHRASE.search(text or "") is not None
            return Classification(
                lane=LANE_BUILD,
                tool=None,
                params={"description": text},
                confidence=0.90 if strong else 0.82,
                rationale=("This describes a new tool to build, so it is routed "
                           "to the Build lane; nothing runs until it is authored."),
                alternatives=_alternatives(ranked, exclude_first=False),
            )

    # 2. Explicit SOLVE / optimisation intent -> bind a solve-capable tool.
    if _is_solve(raw_toks):
        solvers = [doc for doc in docs if _solve_capable(doc.tool)]
        if not solvers:
            return Classification(
                lane=LANE_SOLVE,
                tool=None,
                params={"description": text},
                confidence=0.80,
                rationale="A solver is not available in this catalog, so nothing runs.",
                alternatives=_alternatives(ranked, exclude_first=False),
            )
        ranking = _ranked(set(q_seq), q_seq, solvers)
        if ranking:
            best = ranking[0]
            tool_name = str(best.doc.tool.get("name"))
            return Classification(
                lane=LANE_SOLVE,
                tool=tool_name,
                params=_extract_params(best.doc.tool, text),
                confidence=_confidence(best),
                rationale=f"Matched the '{tool_name}' solver capability; confirm before it runs.",
                alternatives=_alternatives(ranking, exclude_first=True),
            )
        return Classification(
            lane=LANE_SOLVE,
            tool=None,
            params={"description": text},
            confidence=0.80,
            rationale="No solver matches this request; please pick one of the available solvers.",
            alternatives=[
                {"tool": str(doc.tool.get("name")), "confidence": 0.15}
                for doc in sorted(solvers, key=lambda doc: str(doc.tool.get("name")))[:3]
            ],
        )

    minimum_layers = re.search(
        r"\b(?:fewest layers|smallest set of layers|minimum number of layers)\b", text, re.I
    )
    if minimum_layers:
        target = re.search(r"\b(?:cover(?:ing)?|reach(?:ing)?)\s+(?:at least\s+)?(\d+(?:\.\d+)?)\s*(?:%|percent\b)", text, re.I)
        if target and any(doc.tool.get("name") == "count-modules" for doc in docs):
            options = {"group_by": "layer", "panel_scope": "all_layers", "coverage_percent": float(target[1])}
            exclusion = _COVERAGE_EXCLUSION.search(text[target.end():].strip())
            if exclusion:
                names = re.split(r'\s*("(?:[^"]+)"|\x27[^\x27]+\x27)\s*', exclusion["names"])
                excluded_layers = []
                for index, name in enumerate(names):
                    if index % 2:
                        excluded_layers.append(name[1:-1])
                    else:
                        excluded_layers.extend(part.strip() for part in
                                               re.split(r",|\s*\band\b\s*", name, flags=re.I) if part.strip())
                if excluded_layers:
                    options["coverage_excluded_layers"] = excluded_layers
            return Classification(lane=LANE_RUN, tool="count-modules", params=options, confidence=0.95,
                                  rationale="Find the minimum-size layer sets that reach the module coverage target.")

    if (re.search(r"\b(?:re)?rank(?:s|ing)?\b", text, re.I) and re.search(r"\blayers?\b", text, re.I)
            and re.search(r"\b(?:module|panel)s?\b", text, re.I)
            and re.search(r"\b(?:windows?|regions?)\b", text, re.I)):
        windows = re.findall(rf"{_PAIR}\s+to\s+{_PAIR}", text, re.I)
        if len(windows) >= 2 and any(doc.tool.get("name") == "count-modules" for doc in docs):
            options = {"group_by": "layer", "panel_scope": "all_layers",
                       "rank_windows": [[float(value) for value in bounds] for bounds in windows]}
            tier_words = "one two three four five six seven eight nine ten".split()
            tier_amount = rf"[0-9]{{1,9}}|{'|'.join(tier_words)}"
            exclusion = re.search(
                r"\b(?:exclude|drop|omit)\s+(?:the\s+)?"
                rf"(?:({tier_amount})\s+(?:highest|top)|(?:highest|top)\s+({tier_amount}))"
                r"\s+(?:distinct\s+)?(?:physical\s+)?"
                r"(?:(?:module|panel)\s+)?count\s+tiers?\b",
                text, re.I,
            )
            if exclusion:
                amount = (exclusion[1] or exclusion[2]).lower()
                options["exclude_highest_count_tiers"] = (
                    int(amount) if amount.isdecimal() else tier_words.index(amount) + 1
                )
            return Classification(lane=LANE_RUN, tool="count-modules", params=options, confidence=0.95,
                                  rationale="Compare physical module counts by layer across the drawing windows.")

    comparison = _share_change_params(text)
    if comparison and any(doc.tool.get("name") == "count-modules" for doc in docs):
        options = {"compare_selections": comparison}
        return Classification(lane=LANE_RUN, tool="count-modules", params=options, confidence=0.95,
                              rationale="Analyze selected physical modules by layer.")

    shares = _share_params(text)
    if shares and any(doc.tool.get("name") == "count-modules" for doc in docs):
        return Classification(lane=LANE_RUN, tool="count-modules", params=shares,
                              confidence=0.95, rationale="Compute each selected layer's share of the physical panel count.")

    thresholds = _layer_threshold_params(text)
    if thresholds and any(doc.tool.get("name") == "count-modules" for doc in docs):
        return Classification(lane=LANE_RUN, tool="count-modules", params=thresholds,
                              confidence=0.95, rationale="Highlight layers meeting the count and share thresholds.")

    ranking_params = _layer_ranking_params(text)
    if ranking_params and any(doc.tool.get("name") == "count-modules" for doc in docs):
        return Classification(lane=LANE_RUN, tool="count-modules", params=ranking_params,
                              confidence=0.95, rationale="Rank selected physical module counts by layer.")

    if _VISIBLE_PANEL_LAYERS.fullmatch(text.strip()) and any(
        doc.tool.get("name") == "count-modules" for doc in docs
    ):
        return Classification(
            lane=LANE_RUN, tool="count-modules",
            params={"group_by": "layer", "panel_scope": "all_layers", "visible_layers_only": True},
            confidence=0.95, rationale="Count panel sets on layers that are on and thawed.",
        )

    grouped = _GROUPED_PANELS.fullmatch(text.strip())
    if grouped and (grouped["only"] or grouped["exclude"] or grouped["roof"]):
        if any(doc.tool.get("name") == "count-modules" for doc in docs):
            scope = "roof_layers" if grouped["roof"] else "all_layers"
            return Classification(
                lane=LANE_RUN, tool="count-modules",
                params={"group_by": "layer", "panel_scope": scope}, confidence=0.95,
                rationale="Count identified panel sets by layer; unclear sets require a selection.",
            )

    # 3. RUN lane — a catalog match (possibly low confidence).
    if best is not None and _confidence(best) >= MIN_RUN_MATCH_CONF:
        tool_name = str(best.doc.tool.get("name"))
        params = _extract_params(best.doc.tool, text)
        conf = _confidence(best)
        if conf >= 0.70:
            if params:
                rationale = f"Matched the '{tool_name}' capability; prefilled {_params_phrase(params)}."
            else:
                rationale = f"Matched the '{tool_name}' capability from your request."
        else:
            rationale = (f"Closest match is the '{tool_name}' capability, but I'm not "
                         f"certain — confirm it or pick another.")
        return Classification(
            lane=LANE_RUN, tool=tool_name, params=params,
            confidence=conf, rationale=rationale,
            alternatives=_alternatives(ranked, exclude_first=True),
        )

    # 4. Nothing matched — honest low-confidence Run with no tool.
    return Classification(
        lane=LANE_RUN,
        tool=None,
        params={},
        confidence=0.10,
        rationale=("I couldn't match this to an existing capability — try rephrasing, "
                   "or describe a tool to build."),
        alternatives=[],
    )
