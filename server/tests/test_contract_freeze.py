"""
Contract-freeze guard for the agent-spine waves (plan verdict 3 / wire §8).

The §12 deterministic prompt lane is the documented DEGRADED-MODE FLOOR: with
the harness stopped, this surface IS the product. These tests pin the frozen
surfaces so no spine wave can drift them:

  * `nl_router.classify` still returns the documented five-key shape and
    routes "count panels per layer" to the run lane / count-by-layer;
  * POST /api/nl-prompt answers with EXACTLY the §12+§10 envelope fields —
    no new required fields, no tenant requirement (global, stateless);
  * ErrorCode keeps every pre-existing member AND gains exactly the four §18
    codes (guards against renames — the frontend keys on these strings);
  * §18 stays FROZEN (census #12 chip 5): the header marker, the SSE event
    vocabulary (§18.3 table ↔ HarnessTurnEvent union), the §2.1 no-packet
    ConverseTurnInput field set, and the parked ContextPacket schema
    (field set + size-discipline constants).

Run:  cd server && python -m pytest tests/test_contract_freeze.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs`/`app` import anywhere.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="freeze-jobs-")) / "jobs.db"))

import nl_router  # noqa: E402
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode  # noqa: E402

# The documented classify() shape — the §12 five keys plus `alternatives`
# (added pre-spine by CONTRACT-ADDENDUM §D "Contract 4"). Frozen as shipped.
CLASSIFY_KEYS = {"lane", "tool", "params", "confidence", "rationale", "alternatives"}

# The known-tool fixture from the §12 acceptance suite (test_nl_router.py) —
# grouping intent must keep beating the shallower panel-name match.
KNOWN_TOOLS = [
    {"name": "count-by-layer", "engine_op": "count_by_layer",
     "description": "Counts every model-space entity per layer.",
     "params": {"type": "object", "properties": {}}},
    {"name": "count-panels", "engine_op": "count_by_layer",
     "description": "count panels on layer Panels",
     "params": {"type": "object", "properties": {"layer": {"type": "string"}}}},
]


# --------------------------------------------------------------------------- #
# nl_router.classify — documented shape + lane, untouched
# --------------------------------------------------------------------------- #
def test_classify_shape_and_lane_frozen():
    r = nl_router.classify("count panels per layer", KNOWN_TOOLS)
    assert set(r) == CLASSIFY_KEYS, "classify() shape is §12-frozen — no new/renamed keys"
    assert r["lane"] == nl_router.LANE_RUN
    assert r["tool"] == "count-by-layer"
    assert 0.8 <= r["confidence"] <= 1.0
    assert isinstance(r["params"], dict)
    assert isinstance(r["rationale"], str) and r["rationale"]
    assert isinstance(r["alternatives"], list)


# --------------------------------------------------------------------------- #
# POST /api/nl-prompt — exact envelope, global + stateless (no tenant needed)
# --------------------------------------------------------------------------- #
def test_nl_prompt_envelope_frozen_no_tenant_required():
    from fastapi.testclient import TestClient

    from app import app
    c = TestClient(app, raise_server_exceptions=False)
    # deliberately NO X-Tenant-Id and no auth: §12 is global and stateless.
    r = c.post("/api/nl-prompt", json={"text": "count panels per layer"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert set(b) == CLASSIFY_KEYS | {"error", "degraded_mode"}, (
        "the §12 response body is frozen: classify keys + the §10 envelope pair only"
    )
    assert b["error"] is None
    assert b["degraded_mode"] is False
    assert b["lane"] == "run"
    assert b["tool"] == "count-by-layer"


# --------------------------------------------------------------------------- #
# ErrorCode enum — every pre-existing member AND the four §18 additions
# --------------------------------------------------------------------------- #
# name -> wire value, exactly as shipped before the agent spine (frozen for the
# platform frontend; QUOTA_EXCEEDED's lowercase promotion predates the spine).
PREEXISTING_CODES = {
    "UNKNOWN_TOOL": "UNKNOWN_TOOL",
    "BAD_PARAMS": "BAD_PARAMS",
    "APS_UNAVAILABLE": "APS_UNAVAILABLE",
    "BROKER_UNREACHABLE": "BROKER_UNREACHABLE",
    "WORKITEM_FAILED": "WORKITEM_FAILED",
    "TIMEOUT": "TIMEOUT",
    "TENANT_DISABLED": "TENANT_DISABLED",
    "GRANT_REQUIRED": "GRANT_REQUIRED",
    "ENTITLEMENT_REQUIRED": "ENTITLEMENT_REQUIRED",
    "QUOTA_EXCEEDED": "quota_exceeded",
    "INTERNAL": "INTERNAL",
}

# The §18 additions — wire values lowercase like quota_exceeded (wire §8).
NEW_CODES = {
    "LLM_QUOTA_EXHAUSTED": "llm_quota_exhausted",
    "LLM_RATE_LIMITED": "llm_rate_limited",
    "TURN_IN_PROGRESS": "turn_in_progress",
    "SESSION_NOT_FOUND": "session_not_found",
}


def test_error_code_members_frozen_plus_four_new():
    for name, value in {**PREEXISTING_CODES, **NEW_CODES}.items():
        assert hasattr(ErrorCode, name), f"ErrorCode.{name} was renamed/removed"
        assert getattr(ErrorCode, name) == value, (
            f"ErrorCode.{name} wire value drifted from {value!r}")
        assert value in ErrorCode.ALL, f"{value!r} missing from ErrorCode.ALL"


def test_new_codes_default_http_status():
    assert DEFAULT_HTTP_STATUS[ErrorCode.LLM_QUOTA_EXHAUSTED] == 429
    assert DEFAULT_HTTP_STATUS[ErrorCode.LLM_RATE_LIMITED] == 429
    assert DEFAULT_HTTP_STATUS[ErrorCode.TURN_IN_PROGRESS] == 409
    assert DEFAULT_HTTP_STATUS[ErrorCode.SESSION_NOT_FOUND] == 404


# --------------------------------------------------------------------------- #
# §18 FROZEN (census #12 chip 5, 2026-07-23): vocabulary + ContextPacket schema
#
# The addendum's §18 is promoted proposed→frozen. These tests are the freeze
# gate: the header marker, the SSE event vocabulary (doc table ↔ the harness
# NDJSON union), the §2.1 no-packet ConverseTurnInput field set, and the
# ContextPacket schema (field set + size-discipline constants) of the PARKED
# server/context_packet.py — no live caller, but a frozen schema may not
# drift while it awaits spine unification.
# --------------------------------------------------------------------------- #
REPO_ROOT = SERVER_DIR.parent
ADDENDUM = (SERVER_DIR / "CONTRACT-ADDENDUM.md").read_text(encoding="utf-8")


def _strip_ts_comments(source: str) -> str:
    """Remove /* */ blocks and // line comments BEFORE any structural parsing,
    so comment-embedded delimiters ('// =', '// }') cannot steer the
    freeze-gate parsers."""
    import re as _re_mod
    source = _re_mod.sub(r"/\*.*?\*/", "", source, flags=_re_mod.S)
    return _re_mod.sub(r"//[^\n]*", "", source)


CONVERSE_TS = _strip_ts_comments(
    (REPO_ROOT / "harness" / "src" / "ports" / "converse.ts").read_text(encoding="utf-8"))

# The full §18.3 stream vocabulary (both hops), frozen.
S18_STREAM_TYPES = {
    "turn_started", "text_delta", "tool_call", "tool_result", "job_linked",
    "proposed_run", "confirmation_required", "question_required", "confirmation_resolved",
    "turn_usage", "turn_complete", "session_state", "error",
}
# The harness NDJSON union (ports/converse.ts HarnessTurnEvent), frozen. The
# three app-side extras are synthesized by the app/store, never by POST /turn.
HARNESS_EVENT_TYPES = S18_STREAM_TYPES - {"turn_started", "confirmation_resolved", "session_state"}

# Durable session events OUTSIDE the §18.3 turn vocabulary. They are appended
# to the transcript and pushed over the same SSE channel, but they belong to no
# turn, so they are deliberately NOT added to S18_STREAM_TYPES — that set is
# frozen against the §18.3 doc table and must keep describing turn events only.
#
# They still have to be SUBSCRIBED, and that is the distinction this repo has
# twice failed to hold. `turn_queued` and `turn_queue_dropped` are appended by
# turn_runner and read by composer.js, yet reached the browser only when the
# transcript poll beat SSE. Listing them here puts them under the same gate.
NON_TURN_STREAM_TYPES = {
    "turn_queued", "turn_queue_dropped",
    "overlay_proposed", "overlay_decided", "overlay_revoked",
}

#: What the web client MUST register an EventSource listener for. EventSource
#: dispatches by event name and silently drops anything unlistened, so this is
#: the real contract between the two ends — not the §18.3 table alone.
SUBSCRIBABLE_STREAM_TYPES = S18_STREAM_TYPES | NON_TURN_STREAM_TYPES
STOP_REASONS = {"end_turn", "awaiting_approval", "cap_hit", "llm_rate_limited",
                "llm_quota_exhausted", "error", "timeout"}


import re as _re


def _ts_balanced_block(source: str, anchor: str) -> str:
    """Anchor through its MATCHING closing brace (brace-counted), so nested
    inline object types can neither truncate nor extend the slice."""
    start = source.index(anchor)
    open_idx = source.index("{", start)
    depth = 0
    for i in range(open_idx, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unbalanced braces after {anchor!r}")


def _ts_union_literals(source: str, anchor: str) -> set:
    """The members of a TS union from `anchor` to its terminating ';'.
    EVERY member must be a quoted lowercase literal — a permissive member
    (`| string`, a template type, anything unquoted) FAILS the freeze
    instead of being silently ignored."""
    start = source.index(anchor)
    body = source[start:source.index(";", start)]
    # Drop the introducer (`type X =` / `type:`), keep the union body.
    intro_end = max(body.find("="), body.find(":"))
    members = [m.strip() for m in body[intro_end + 1:].split("|") if m.strip()]
    literals = set()
    for member in members:
        match = _re.fullmatch(r'"([a-z_]+)"', member)
        assert match, f"non-literal union member {member!r} under {anchor!r}"
        literals.add(match.group(1))
    return literals


def _ts_field_names(block: str) -> set:
    """Field names declared at depth 1 of a brace-balanced, comment-stripped
    interface block. WHITELIST parsing, fail-loud: every depth-1 line must be
    a plain identifier field or pure structure — an index signature
    ([key: string]: ...), a quoted key ("rogue"?: ...), a computed key, or
    any construct this parser does not recognize FAILS the freeze instead of
    being silently ignored."""
    names = set()
    depth = 0
    for raw in block.splitlines():
        line = raw.strip()
        if depth == 1 and line:
            field = _re.match(r"(\w+)\??\s*:", line)
            structural = _re.fullmatch(r"[{}\[\];,)>]*", line) or line.endswith("{") \
                or _re.fullmatch(r"export interface \w+ \{", line)
            if field:
                names.add(field.group(1))
            elif not structural:
                raise AssertionError(
                    f"unrecognized construct at interface depth 1 (freeze "
                    f"parser whitelist): {line[:60]!r}")
        depth += raw.count("{") - raw.count("}")
    return names


def _s183_table_types() -> list:
    """Every §18.3 table row's event type. Each data row's first cell must
    contain EXACTLY ONE backticked lowercase name — a decorated rogue row and
    a two-names-in-one-cell row both fail loudly."""
    s183 = ADDENDUM[ADDENDUM.index("### 18.3"):ADDENDUM.index("### 18.4")]
    types = []
    for line in s183.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        first_cell = stripped.split("|")[1].strip()
        if first_cell in ("type", "") or set(first_cell) <= {"-"}:
            continue  # header / separator rows
        found = _re.findall(r"`([a-z_]+)`", first_cell)
        assert len(found) == 1, (
            f"§18.3 type cell must carry exactly one backticked name, "
            f"got {found}: {stripped[:60]!r}")
        types.append(found[0])
    return types


def test_s18_header_frozen():
    section = ADDENDUM[ADDENDUM.index("## §18 Conversational agent sessions"):]
    intro = section[:section.index("### 18.1")]
    assert "FROZEN" in intro, "§18 header lost its FROZEN marker"
    assert "Proposed (not yet frozen)" not in intro, (
        "§18 intro still says proposed — the freeze was reverted without "
        "flipping this gate")


def test_s18_sse_vocabulary_frozen():
    doc_types = _s183_table_types()
    assert len(doc_types) == len(S18_STREAM_TYPES) and set(doc_types) == S18_STREAM_TYPES, (
        f"§18.3 table drifted: rows={doc_types}")
    union = _ts_union_literals(
        _ts_balanced_block(CONVERSE_TS, "export interface HarnessTurnEvent"), "type:")
    assert union == HARNESS_EVENT_TYPES, (
        f"HarnessTurnEvent union drifted: {sorted(union ^ HARNESS_EVENT_TYPES)}")
    stop = _ts_union_literals(CONVERSE_TS, "export type StopReason")
    assert stop == STOP_REASONS, f"StopReason drifted: {sorted(stop ^ STOP_REASONS)}"


def _web_stream_event_types() -> set:
    """The event names web/src/converse.js registers SSE listeners for.

    Comments are stripped FIRST. A review found the naive extraction read a
    commented-out entry as live: turning `'overlay_revoked'` into
    `// 'overlay_revoked'` left the test passing while the browser silently
    dropped every revoke. A gate that reads dead code is worse than no gate,
    because it reports safety it is not checking.
    """
    source = (REPO_ROOT / "web" / "src" / "converse.js").read_text(encoding="utf-8")
    block = source[source.index("export const STREAM_EVENT_TYPES"):]
    block = block[:block.index("]")]
    block = _re.sub(r"/\*.*?\*/", "", block, flags=_re.S)   # block comments
    block = _re.sub(r"//[^\n]*", "", block)                  # line comments

    # The list must be PURE string literals. Round 2 escaped the comment
    # stripper with `'overlay_revoked' + '//typo'`: JavaScript registers the
    # concatenated name `overlay_revoked//typo`, but stripping removed the tail
    # and extraction still reported a clean `overlay_revoked`. So after
    # removing the literals themselves, only list punctuation may remain — a
    # `+`, a template literal, an identifier or a spread leaves residue and
    # fails HERE rather than being quietly mis-read.
    residue = _re.sub(r"'[^'\n]*'", "", block)
    residue = residue[residue.index("[") + 1:] if "[" in residue else residue
    leftover = _re.sub(r"[\s,]", "", residue)
    assert leftover == "", (
        "STREAM_EVENT_TYPES is not a plain list of string literals "
        f"({leftover!r}) — the gate cannot verify a computed subscription list")

    return set(_re.findall(r"'([a-z_]+)'", block))


def test_the_subscription_gate_cannot_be_fooled_by_a_comment():
    """The gate's own failure mode, pinned. If commenting an entry out stopped
    failing the gate, the gate would certify a browser that drops the event."""
    live = _web_stream_event_types()
    assert "overlay_revoked" in live

    source = (REPO_ROOT / "web" / "src" / "converse.js").read_text(encoding="utf-8")
    block = source[source.index("export const STREAM_EVENT_TYPES"):]
    block = block[:block.index("]")]
    commented = block.replace("'overlay_revoked'", "// 'overlay_revoked'")
    commented = _re.sub(r"//[^\n]*", "", commented)
    assert "overlay_revoked" not in set(_re.findall(r"'([a-z_]+)'", commented)), (
        "a commented-out listener still reads as live — the gate is decorative")


def test_the_gate_refuses_a_computed_subscription_list():
    """Round 2's escape from the comment stripper.

    `'overlay_revoked' + '//typo'` makes JavaScript register the CONCATENATED
    name `overlay_revoked//typo`, which the browser then has no listener for —
    while a comment-stripping parse deleted the tail and reported a clean
    `overlay_revoked`. The gate would have certified a subscription that does
    not exist. It now refuses any list that is not plain literals.
    """
    source = (REPO_ROOT / "web" / "src" / "converse.js").read_text(encoding="utf-8")
    block = source[source.index("export const STREAM_EVENT_TYPES"):]
    block = block[:block.index("]")]
    sneaky = block.replace("'overlay_revoked'", "'overlay_revoked' + '//typo'")

    sneaky = _re.sub(r"//[^\n]*", "", sneaky)
    residue = _re.sub(r"'[^'\n]*'", "", sneaky)
    residue = residue[residue.index("[") + 1:]
    assert _re.sub(r"[\s,]", "", residue) != "", (
        "a concatenated event name left no residue — the gate would certify a "
        "subscription the browser never makes")


def test_web_client_subscribes_to_every_emittable_event():
    """THE gate that was missing, and the reason two bugs shipped.

    EventSource delivers only what the client called addEventListener for.
    An emitted-but-unsubscribed type is not an error and not a warning — it is
    dropped in silence, and the feature then works only when the transcript
    poll wins a race against SSE. That is what happened to `question_required`
    (a card that appeared intermittently) and to `turn_queued` /
    `turn_queue_dropped` (durably appended, read by composer.js, never
    subscribed).

    Equality in BOTH directions is deliberate:
      * missing from the client  -> the silent-drop bug above;
      * extra in the client      -> a listener for something nothing emits,
        which is a rename or a deletion that left dead code behind, and the
        next reader cannot tell it from a real event.
    """
    client = _web_stream_event_types()
    assert client == SUBSCRIBABLE_STREAM_TYPES, (
        "web/src/converse.js STREAM_EVENT_TYPES drifted from the server "
        f"vocabulary: {sorted(client ^ SUBSCRIBABLE_STREAM_TYPES)}")


def test_overlay_event_names_come_from_the_module_that_emits_them():
    """The names above are copied text; this proves they match the emitter.
    Without it the freeze could pass while the server emits something else."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "overlay_stream", REPO_ROOT / "server" / "overlay_stream.py")
    overlay_stream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(overlay_stream)

    assert set(overlay_stream.OVERLAY_EVENT_TYPES) == {
        t for t in NON_TURN_STREAM_TYPES if t.startswith("overlay_")}


def test_s21_turn_input_field_set_frozen_no_packet():
    """The live wire is §2.1 ConverseTurnInput. The field SET is exact: an added
    field fails, not just a removed one. `model` + `credential_grant` are the
    ADDITIVE, OPTIONAL "mount your LLM" fields (both absent-safe on the wire);
    `images` is the same kind of addition for inline vision blocks, optional and
    absent-safe, carried for one turn and never stored as prior context. A
    ContextPacket field remains explicitly forbidden (that is the §2.1 packet
    decision, distinct from these per-session additions).

    This gate is why the field set is written out rather than derived: adding
    `images` had to be a deliberate edit here, in the same commit, instead of a
    silent wire change nobody reviewed."""
    block = _ts_balanced_block(CONVERSE_TS, "export interface ConverseTurnInput")
    assert _ts_field_names(block) == {"tenant_id", "session_id", "turn_id",
                                      "drawing_id", "messages", "text",
                                      "confirm", "model", "credential_grant",
                                      "images"}, (
        f"ConverseTurnInput field set drifted: {sorted(_ts_field_names(block))}")
    assert "contextPacket" not in block and "context_packet" not in block, (
        "ConverseTurnInput grew a packet field — that is a §2.1 wire change, "
        "not a drive-by")


def test_context_packet_schema_frozen(monkeypatch, tmp_path):
    """Frozen field set, ROW sub-shapes, and size-discipline constants of the
    parked module — a projection row gaining or losing a key fails here."""
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.delenv("LEAF_CONVERSE_HARNESS_URL", raising=False)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    import context_packet
    import deps as deps_mod
    import jobs as jobs_mod
    monkeypatch.setattr(deps_mod, "all_tools", lambda tid=None: [
        {"name": "tool-000", "version": "1.0.0",
         "description": "counts panels per layer",
         "capabilities": ["drawing.read"],
         "engine_op": "count_by_layer"}])
    # One live job with EXTRA upstream keys: the projection must keep exactly
    # {job_id, tool, status} — nothing more leaks, nothing gets dropped.
    monkeypatch.setattr(jobs_mod, "list_jobs", lambda tid, limit=25: [
        {"job_id": "job-1", "tool": "tool-000", "status": "queued",
         "params": {"secret": "never"}, "attempt": 1}])
    packet = context_packet.build_packet("freeze-tenant", "freeze-drawing")
    assert set(packet) == {"catalog", "catalog_hash", "drawing", "versions",
                           "checkout", "entitlements", "active_jobs", "grant",
                           "classifier_hint"}, "ContextPacket field set drifted"
    assert set(packet["drawing"]) == {"id", "head_version", "layers", "entity_total"}
    assert set(packet["checkout"]) == {"held_by", "expires_at"}
    assert set(packet["grant"]) == {"kind", "degraded"}
    # Row sub-shapes (frozen):
    assert [set(row) for row in packet["catalog"]] == [{"name", "description", "capabilities"}]
    assert [set(row) for row in packet["active_jobs"]] == [{"job_id", "tool", "status"}]
    # Entitlement KEY SET = the frozen §11.3 capability vocabulary (AUTH.md).
    assert set(packet["entitlements"]) == {
        "run_read", "run_write", "solve", "build", "converse",
        "agent_write_autopilot", "deploy", "platform_customize", "upload",
    }, "entitlement capability key set drifted from the §11.3 freeze"
    assert packet["classifier_hint"] is None
    # Catalog cap semantics + the {"more": N} marker, exercised DIRECTLY:
    entries = context_packet._catalog_entries([
        {"name": f"t{i}", "description": "d", "capabilities": []} for i in range(3)])
    capped = context_packet._capped_catalog(entries, 2)
    assert capped[:2] == entries[:2] and capped[2] == {"more": 1}
    assert [set(row) for row in capped[:2]] == [{"name", "description", "capabilities"}] * 2
    # versions/layer row shapes have no hermetic manifest to build from —
    # frozen by parsing the EXACT key set out of each projection literal
    # (an added key inside either dict literal fails; substring pins alone
    # would not catch an addition).
    src = (SERVER_DIR / "context_packet.py").read_text(encoding="utf-8")
    versions_lit = src[src.index('out["versions"] = [{'):]
    versions_lit = versions_lit[:versions_lit.index("} for e in")]
    assert set(_re.findall(r'"(\w+)":', versions_lit)) == {"version", "ts", "tool"}, (
        "versions row projection key set drifted")
    lr_start = src.index("layer_rows: List[Dict[str, Any]] = [{")
    layer_row_lit = src[src.index("= [{", lr_start):]
    layer_row_lit = layer_row_lit[:layer_row_lit.index("for n, c")]
    assert set(_re.findall(r'"(\w+)":', layer_row_lit)) == {"name", "count"}, (
        "layer row projection key set drifted")
    more_lit = src[src.index("layer_rows.append({"):]
    more_lit = more_lit[:more_lit.index("})")]
    assert set(_re.findall(r'"(\w+)":', more_lit)) == {"more"}, (
        "layer more-marker key set drifted")
    assert context_packet.MAX_PACKET_CHARS == 8000
    assert context_packet.CATALOG_CAP == 60
    assert context_packet.VERSIONS_TAIL == 3
    assert context_packet.ACTIVE_JOBS_CAP == 5
    assert context_packet.LAYERS_CAP == 20
    assert context_packet.DESCRIPTION_CAP == 140


# --------------------------------------------------------------------------- #
# script runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
