"""Closed civil operation requests over the drawing's physical head."""
import copy
import math
import re

import solar_frames_piles as fp
import solar_ground_buildout as bo
import solar_physical_head as ph
import solar_physical_state as ps

MAX_CIVIL_BODY_BYTES = 2_400_000
RESULT_SCHEMA = "leaf.solar-civil-operation.v1"
VIEW_SCHEMA = "leaf.solar-civil-view-response.v1"
GRADE = "grade-pad"
OPERATIONS = fp.OPERATIONS + (GRADE,)
CODES = frozenset({"CIVIL_GRADE_INPUT_INVALID", "CIVIL_GRADE_LIMIT_EXCEEDED",
                   "CIVIL_GRADE_NO_PAD", "CIVIL_GRADE_FAILED"})
_FIELDS = {
    fp.GENERATE: {"boundary", "preset", "drawing_units"},
    fp.COLLISION: set(), fp.PILING: {"preset", "pile_template"},
    fp.RANGE: {"preset"}, GRADE: {"boundary"},
}


class CivilOperationError(ps.PhysicalStateError):
    """A payload-free operation or envelope refusal."""


def validate_body(body):
    if type(body) is not dict:
        raise CivilOperationError("TERRAIN_BODY_INVALID")
    operation = body.get("operation")
    if type(operation) is not str or operation not in OPERATIONS:
        raise CivilOperationError("TERRAIN_OPERATION_INVALID")
    required = {"operation"} | _FIELDS[operation]
    allowed = required | {"expected_head"} | ({"mode", "value_du"} if operation == GRADE else set())
    if not required <= set(body) or not set(body) <= allowed:
        raise CivilOperationError("TERRAIN_BODY_INVALID")
    if ("expected_head" not in body or (body["expected_head"] is not None
            and (type(body["expected_head"]) is not str
                 or not re.fullmatch(r"[0-9a-f]{64}", body["expected_head"])))):
        raise CivilOperationError("TERRAIN_EXPECTED_HEAD_INVALID")
    return body


def _grade_inputs(boundary, mode, value_du):
    boundary = fp._boundary(boundary)
    valid = type(mode) is str and mode in ("Auto", "Manual", "Clearance")
    if value_du is not None:
        try:
            valid = (valid and type(value_du) in (int, float) and math.isfinite(value_du)
                     and abs(value_du) <= fp.MAX_ABS_COORDINATE and mode != "Auto"
                     and (mode != "Clearance" or value_du >= 0))
        except OverflowError:
            valid = False
    if not valid:
        raise CivilOperationError("CIVIL_GRADE_INPUT_INVALID")
    return boundary


def _grade_result(outcome, created, drawing_id, project, base, document, head, summary):
    state = document["state"]
    result = fp._result(GRADE, outcome, created, drawing_id, project, base, document, head,
                        fp._terrain_view(state), summary,
                        fp.terrain_standing(state, document["units"]["meters_per_unit"]))
    result["schema"] = RESULT_SCHEMA
    result["terrain"]["sampled"] = True
    return result


def grade_pad(backend, tenant_id, drawing_id, *, base, boundary, mode="Auto", value_du=None,
              project_id=None):
    base = fp._common(project_id, base)
    boundary = _grade_inputs(boundary, mode, value_du)
    project = fp._context(backend, tenant_id, drawing_id, project_id)["project_id"]
    head, document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project)
    digest = fp.canonical_sha256({"operation": GRADE, "base": base,
                                  "boundary": [[float(v) + 0.0 for v in point] for point in boundary],
                                  "mode": mode, "value_du": None if value_du is None else float(value_du) + 0.0})
    if (head is not None and head["parent"] == base and document["capability"] == GRADE
            and document["source"]["sha256"] == digest):
        return _grade_result("retry", False, drawing_id, project, base, document, head, None)
    if (None if head is None else head["state"]["artifact_id"]) != base:
        raise fp.FramesPilesError("FRAMES_PILES_STALE_BASE")
    if head is None:
        raise fp.FramesPilesError("FRAMES_PILES_STATE_REQUIRED")
    state = copy.deepcopy(document["state"])
    fp.terrain_sampler(state, required=True)
    fp.terrain_standing(state, document["units"]["meters_per_unit"])
    settings = state.get("grading_settings", {})
    pads = state.get("grade_pads", [])
    if type(settings) is not dict or type(pads) is not list:
        raise fp.FramesPilesError("FRAMES_PILES_STATE_INVALID")
    try:
        result = bo.grade_multi(state["grid"], [boundary], document["units"]["meters_per_unit"],
                                mode=mode, value_du=value_du, runtime=bo.RUNTIME_NET8)
        if not result["succeeded"] or len(result["pads"]) != 1:
            raise CivilOperationError("CIVIL_GRADE_NO_PAD")
        pad = result["pads"][0]
        settings.update(result["settings"])
        state["grading_settings"] = settings
        state["grade_pads"] = pads + [pad]
        summary = {"pads_added": 1, "grade_pads": len(state["grade_pads"]), "mode": result["mode"],
                   "elevation_m": pad["elevation_m"], "label": pad["label"]["text"],
                   "total_cut_m3": result["total_cut_m3"], "total_fill_m3": result["total_fill_m3"],
                   "net_m3": result["net_m3"]}
    except bo.BuildoutBoundsError:
        raise CivilOperationError("CIVIL_GRADE_LIMIT_EXCEEDED") from None
    except bo.BuildoutInputError:
        raise CivilOperationError("CIVIL_GRADE_INPUT_INVALID") from None
    except CivilOperationError:
        raise
    except Exception:
        raise CivilOperationError("CIVIL_GRADE_FAILED") from None
    if repr(state) == repr(document["state"]):
        return _grade_result("unchanged", False, drawing_id, project, base, document, head, summary)
    child = ps.physical_document(state, drawing_units=document["units"]["drawing_units"],
                                 source_sha256=digest, capability=GRADE, parent=base,
                                 frame=document["frame"])
    published = ph.publish_physical_state(backend, tenant_id, drawing_id, child, project_id=project)
    created = published["created"]
    return _grade_result("published" if created else "retry", created, drawing_id, project,
                         base, child, published["head"], summary if created else None)


def operate(backend, tenant_id, drawing_id, body, *, project_id=None):
    b = validate_body(body)
    operation = b["operation"]
    kwargs = {key: value for key, value in b.items() if key not in ("operation", "expected_head")}
    call = {fp.GENERATE: fp.generate_frames, fp.COLLISION: fp.detect_collisions,
            fp.PILING: fp.generate_piles, fp.RANGE: fp.check_pile_lengths, GRADE: grade_pad}[operation]
    return call(backend, tenant_id, drawing_id, base=b["expected_head"], project_id=project_id, **kwargs)


def load_civil(backend, tenant_id, drawing_id, *, project_id=None):
    project = fp._context(backend, tenant_id, drawing_id, project_id)["project_id"]
    head, document, preview = fp.load_frames_piles(backend, tenant_id, drawing_id, project_id=project)
    return {"schema": VIEW_SCHEMA, "stored": head is not None, "head": head, "preview": preview,
            "standing": None if head is None else fp.terrain_standing(
                document["state"], document["units"]["meters_per_unit"]),
            "grade_pads": 0 if head is None else len(document["state"].get("grade_pads", []))}
