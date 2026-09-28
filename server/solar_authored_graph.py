"""The pinned W1 design graph as a tenant-authored tool's sandboxed intake.

An authored record that declares ``graph_input: "solar-w1-graph"`` receives, in
place of the drawing intake, one immutable snapshot of the requesting tenant's own
stored graph at the job's pinned version. Nothing here executes tenant code: the
broker hands the intake to its existing ``run_tool_dynamic`` call, which is the
sandbox seam (``tool_loader``). The trusted read kind (``solar_local_read``) is a
different path and is never reached from here.

Contract: fails closed on any malformed input with a ``GraphValidationError``
code, reads the graph once through ``resolve_graph_context`` (tenant scoped, so
another tenant's drawing resolves nothing), and bounds the intake by the sandbox's
own fixed input limit before any sandbox starts.
"""
import copy
import hashlib
import json
from collections.abc import Mapping

from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from tool_record_fields import GRAPH_INPUT_SOLAR_W1

INTAKE_SCHEMA = "leaf.solar-graph-intake.v1"
# Equal to tool_loader._SANDBOX_LIMITS["input_bytes"], the sandbox's own intake cap.
MAX_INTAKE_BYTES = 8_388_608
# Refusals that mean "this tenant has no readable graph at that version": the
# broker answers them 409, the same status the jobs router gives an unreadable head.
CONTEXT_REFUSALS = frozenset((
    "GRAPH_CONTEXT_UNAVAILABLE", "GRAPH_NOT_EMBEDDED", "GRAPH_DIGEST_MISMATCH",
    "PROJECT_MISMATCH",
))


def reads_graph(tool):
    """True only for a non-trusted record that declares the W1 graph intake.

    A record the solar registry owns (a trusted solar block, whatever its adapter)
    and a drawing.write record are never graph-intake tools, so trusted read and
    commit kinds keep their own paths unchanged.
    """
    if not isinstance(tool, Mapping):
        return False
    if type(tool.get("graph_input")) is not str or tool["graph_input"] != GRAPH_INPUT_SOLAR_W1:
        return False
    if "solar" in tool:
        return False
    import solar_tools
    import product_capability_availability as capability_catalog

    name = tool.get("name")
    if solar_tools.get(name) is not None or capability_catalog.capability_adapter(name) is not None:
        return False
    capabilities = tool.get("capabilities") or []
    return not (isinstance(capabilities, list) and "drawing.write" in capabilities)


def build_graph_intake(backend, tenant_id, drawing_id, source_version):
    """Return ``{schema, drawing_id, source_version, graph_sha256, graph}`` or raise.

    ``graph`` is a deep copy of the stored graph at exactly ``source_version``;
    ``graph_sha256`` is sha256 over its canonical bytes, the digest the read kind's
    receipts carry.
    """
    if type(source_version) is not int or source_version < 1:
        raise GraphValidationError("INVALID_SOURCE_VERSION")
    if type(drawing_id) is not str or not drawing_id:
        raise GraphValidationError("DRAWING_ID_CONFLICT")
    context = resolve_graph_context(backend, tenant_id, drawing_id, source_version)
    if context["resolved_version"] != source_version:
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE")
    graph = copy.deepcopy(context["graph"])
    try:
        graph_bytes = canonical_bytes(graph)
    except (TypeError, ValueError, RecursionError):
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
    intake = {
        "schema": INTAKE_SCHEMA,
        "drawing_id": drawing_id,
        "source_version": source_version,
        "graph_sha256": hashlib.sha256(graph_bytes).hexdigest(),
        "graph": graph,
    }
    # Exactly the bytes the sandbox measures against its own input limit
    # (tool_loader, the e2b micro-VM runner): compact separators, UTF-8.
    if len(json.dumps(intake, separators=(",", ":")).encode("utf-8")) > MAX_INTAKE_BYTES:
        raise GraphValidationError("GRAPH_INTAKE_TOO_LARGE")
    return intake
