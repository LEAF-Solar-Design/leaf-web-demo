"""The combiner intake import (wave-18 record sf-w2-combiners-intake-producer): stores the drawing's recorded
LEAFCOMBINERAUTO input (format combiner-intake-v1) and its panel-group outlines on a NEW version of an
intake-backed drawing, so the solar-combiners tool (builtins/solar_combiners.py) finds them through the
source_intake trusted input.

  import_combiner_intake(backend, tenant_id, drawing_id, data, *, project_id=None, authorize=None) -> result

Frozen decisions (sf-w2-combiners-intake-producer):
  - What is stored. The body is one JSON object with exactly the keys "combiner_intake" and "panel_groups",
    the two keys builtins/solar_combiners.py reads. The new version's intake is the head version's intake
    with those two keys set (replaced when present) and nothing else changed: the CAD data, the embedded
    solar_design_graph and its solar_design_graph_sha256 are carried byte-for-byte in value, so the graph
    revision does not move and the tool's expected_rev is unchanged.
    An import needs at least one panel group, because the tool cannot route feeders without an outline.
  - Bound before stored. The dump is bound to the head graph with solar_combiner_graph.bind_intake (units,
    L2 numbers and positions, string endpoints, L1/L2 context, no existing combiners) and the outlines are
    checked with the kernel's own validate_outlines before anything is written; a dump that does not describe
    the head graph is refused with the binding module's own code. The tool binds again at run time.
    The dump must name schema leaf.combiner-placement-dump.v1 before it can be stored.
  - Usable after storing. The merged intake must pass solar_design_graph._bounded_json (the bound every later
    graph commit applies to the whole intake, through solar_solve_results.version_companion), or the import
    is refused with COMBINER_IMPORT_INTAKE_TOO_LARGE: a stored intake that no commit can carry is never written.
  - Same input twice. When the head intake already carries equal values for both keys, nothing is written and
    the result names the head version with created false.
  - One write. The version is written by write_loop._put_bytes_version with parent = the head version read
    here and require_parent_is_head, under the drawing-mutation guard and the caller's checkout identity
    (authorize() returns (holder, fence)); a head that moved in between is COMBINER_IMPORT_HEAD_MOVED.

Contract: fails closed; every refusal is CombinerIntakeImportError whose code is one of CODES or one of the
binding codes in PASSTHROUGH_CODES. No clock, no network. Cost: one bounded UTF-8 decode and JSON parse of at
most MAX_IMPORT_BYTES, two iterative bound passes, one head read, one binding (linear in strings and L2s),
one canonical encode and one store write.
"""
import hashlib
import json

import write_loop  # first: puts da/ (store) on sys.path
import store
import solar_combiner_graph as cg
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, _bounded_json
from solar_graph_context import resolve_graph_context

RESULT_SCHEMA = "leaf.solar-combiner-intake-import.v1"
DUMP_SCHEMA = "leaf.combiner-placement-dump.v1"
TOOL = "solar-combiner-intake-import"
NOTE_PREFIX = "solar-combiner-intake:"
MAX_IMPORT_BYTES = 16_777_216
MAX_PROJECT_ID_CHARS = 100
MAX_GROUPS = 10_000
MAX_HANDLE = 64                      # builtins/solar_combiners.MAX_HANDLE
BODY_KEYS = frozenset({"combiner_intake", "panel_groups"})
GROUP_KEYS = frozenset({"handle", "outlines"})
CODES = frozenset({
    "COMBINER_IMPORT_PROJECT_ID_INVALID", "COMBINER_IMPORT_EMPTY", "COMBINER_IMPORT_TOO_LARGE",
    "COMBINER_IMPORT_ENCODING_INVALID", "COMBINER_IMPORT_JSON_INVALID", "COMBINER_IMPORT_BODY_INVALID",
    "COMBINER_IMPORT_DRAWING_NOT_FOUND", "COMBINER_IMPORT_GRAPH_REQUIRED", "COMBINER_IMPORT_GRAPH_NOT_LOCAL",
    "COMBINER_IMPORT_PROJECT_MISMATCH", "COMBINER_IMPORT_SOURCE_CORRUPT", "COMBINER_IMPORT_INTAKE_TOO_LARGE",
    "COMBINER_IMPORT_HEAD_MOVED", "COMBINER_IMPORT_CHECKOUT_DENIED", "COMBINER_IMPORT_CHECKOUT_UNAVAILABLE",
    "COMBINER_IMPORT_WRITES_DRAINED", "COMBINER_IMPORT_STORE_UNAVAILABLE", "COMBINER_IMPORT_WRITE_REFUSED",
})
# solar_combiner_graph codes bind_intake and the outline check can raise; they reach the caller unchanged.
PASSTHROUGH_CODES = frozenset({
    "COMBINER_L2_MODE_REQUIRED", "COMBINER_EXISTING_L1", "COMBINER_INTAKE_INVALID",
    "COMBINER_INTAKE_UNITS_MISMATCH", "COMBINER_INTAKE_CONTEXT_MISMATCH", "COMBINER_INTAKE_L2_MISMATCH",
    "COMBINER_INTAKE_STRING_MISMATCH", "COMBINER_OUTLINES_INVALID",
})


class CombinerIntakeImportError(ValueError):
    """A refusal; `code` is one of CODES or PASSTHROUGH_CODES."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _refuse(code):
    raise CombinerIntakeImportError(code)


def _sha(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _reject_constant(_name):
    raise ValueError("non-finite number")


def _parse(data):
    """The body as (combiner_intake, panel_groups); bounded before decode, fails closed."""
    if type(data) is not bytes or not data:
        _refuse("COMBINER_IMPORT_EMPTY")
    if len(data) > MAX_IMPORT_BYTES:
        _refuse("COMBINER_IMPORT_TOO_LARGE")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _refuse("COMBINER_IMPORT_ENCODING_INVALID")
    if text.startswith("\ufeff"):
        _refuse("COMBINER_IMPORT_ENCODING_INVALID")
    try:
        body = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        _refuse("COMBINER_IMPORT_JSON_INVALID")
    if type(body) is not dict or set(body) != BODY_KEYS:
        _refuse("COMBINER_IMPORT_BODY_INVALID")
    try:
        _bounded_json(body)
    except GraphValidationError:
        _refuse("COMBINER_IMPORT_BODY_INVALID")
    intake, groups = body["combiner_intake"], body["panel_groups"]
    if type(intake) is not dict or type(groups) is not list or len(groups) > MAX_GROUPS:
        _refuse("COMBINER_IMPORT_BODY_INVALID")
    for group in groups:
        if (type(group) is not dict or set(group) != GROUP_KEYS
                or type(group["handle"]) is not str or not 1 <= len(group["handle"]) <= MAX_HANDLE
                or type(group["outlines"]) is not list):
            _refuse("COMBINER_IMPORT_BODY_INVALID")
    if not groups:
        _refuse("COMBINER_OUTLINES_INVALID")
    try:
        polygons = cg.cab.validate_outlines(groups)
    except (cg.cab.InverterCablingError, OverflowError, TypeError, ValueError):
        _refuse("COMBINER_OUTLINES_INVALID")
    if type(intake.get("schema")) is not str or intake["schema"] != DUMP_SCHEMA:
        _refuse("COMBINER_INTAKE_INVALID")
    return intake, groups, sum(len(polygon) for polygon in polygons)


_CONTEXT_CODES = {"GRAPH_CONTEXT_UNAVAILABLE": "COMBINER_IMPORT_DRAWING_NOT_FOUND",
                  "GRAPH_NOT_EMBEDDED": "COMBINER_IMPORT_GRAPH_REQUIRED",
                  "PROJECT_MISMATCH": "COMBINER_IMPORT_PROJECT_MISMATCH"}


def _head(backend, tenant_id, drawing_id, project_id):
    """(context, head intake, head entry) of the drawing's head, which must be an intake-backed graph."""
    try:
        context = resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
    except GraphValidationError as exc:
        _refuse(_CONTEXT_CODES.get(exc.code, "COMBINER_IMPORT_SOURCE_CORRUPT"))
    if context["representation"] != "intake":
        _refuse("COMBINER_IMPORT_GRAPH_NOT_LOCAL")
    try:
        _, key, entry = store.resolve_version_entry(backend, tenant_id, drawing_id, context["resolved_version"])
        raw = backend.get(key)
    except (KeyError, ValueError, TypeError, OSError):
        _refuse("COMBINER_IMPORT_STORE_UNAVAILABLE")
    try:
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("digest")
        intake = json.loads(raw)
        if type(intake) is not dict or intake.get("solar_design_graph_sha256") != context["graph_sha256"]:
            raise ValueError("graph")
    except (KeyError, TypeError, ValueError, RecursionError):
        _refuse("COMBINER_IMPORT_SOURCE_CORRUPT")
    return context, intake, entry


def _result(drawing_id, context, created, version, parent, stored_sha, binding, groups, vertices):
    return {"schema_version": RESULT_SCHEMA, "drawing_id": drawing_id, "project_id": context["project_id"],
            "created": created, "version": version, "parent_version": parent,
            "graph_sha256": context["graph_sha256"], "graph_rev": context["graph"]["rev"],
            "intake_sha256": stored_sha, "combiner_intake_sha256": binding["intake_sha256"],
            "panel_groups_sha256": _sha(groups),
            "bound": {"l2_inverters": len(binding["l2"]), "strings": len(binding["strings"])},
            "panel_groups": len(groups), "outline_vertices": vertices}


def import_combiner_intake(backend, tenant_id, drawing_id, data, *, project_id=None, authorize=None):
    """Store `data` (see the module docstring) on a new version of the drawing's head; returns the result."""
    if project_id is not None and (type(project_id) is not str
                                   or not 1 <= len(project_id) <= MAX_PROJECT_ID_CHARS):
        _refuse("COMBINER_IMPORT_PROJECT_ID_INVALID")
    intake, groups, vertices = _parse(data)
    context, head, entry = _head(backend, tenant_id, drawing_id, project_id)
    try:
        binding = cg.bind_intake(context["graph"], intake)
    except cg.CombinerGraphError as exc:
        _refuse(str(exc))
    except GraphValidationError:
        _refuse("COMBINER_IMPORT_SOURCE_CORRUPT")
    version = context["resolved_version"]
    if head.get("combiner_intake") == intake and head.get("panel_groups") == groups:
        return _result(drawing_id, context, False, version, entry.get("parent"), entry["sha256"], binding,
                       groups, vertices)
    merged = dict(head, combiner_intake=intake, panel_groups=groups)
    try:
        _bounded_json(merged)
    except GraphValidationError:
        _refuse("COMBINER_IMPORT_INTAKE_TOO_LARGE")
    payload = canonical_bytes(merged)
    holder, fence = authorize() if authorize is not None else (None, None)
    note = NOTE_PREFIX + _sha({"combiner_intake": binding["intake_sha256"], "panel_groups": _sha(groups)})
    try:
        with write_loop.drawing_mutation_refusal_guard() as refusal:
            if refusal is not None:
                _refuse("COMBINER_IMPORT_WRITES_DRAINED")
            new_version = write_loop._put_bytes_version(
                backend, tenant_id, drawing_id, payload, parent_version=version,
                meta={"tool": TOOL, "note": note}, holder=holder, fence=fence, require_parent_is_head=True)
    except store.CheckoutDenied:
        _refuse("COMBINER_IMPORT_CHECKOUT_DENIED")
    except CombinerIntakeImportError:
        raise
    except ValueError as exc:
        if str(exc).startswith(("stale parent", "stale drawing head")):
            _refuse("COMBINER_IMPORT_HEAD_MOVED")
        _refuse("COMBINER_IMPORT_WRITE_REFUSED")
    except OSError:
        _refuse("COMBINER_IMPORT_STORE_UNAVAILABLE")
    return _result(drawing_id, context, True, new_version, version, hashlib.sha256(payload).hexdigest(),
                   binding, groups, vertices)
