"""Pure project edits, readiness and invalidation for the Solar graph.

Two writers change graph["project"]: solar-settings (apply_project_changes: name, zip code and
coordinates) and a design preset commit (set_installation_design, through solar_preset_sync.sync).
Both run the one project invalidation rule (invalidate_project_dependents) on a material change.
"""
import copy
import math
import re

from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, entities
from solar_solve_results import DERIVED_KINDS


ZIP_RE = re.compile(r"^[0-9]{5}(-[0-9]{4})?$")
# The graph contract's closed project.installation_design enum (contract/solar-design-graph.v1.schema.json).
INSTALLATION_DESIGNS = ("Roof", "Ground")
SEED_VOC_COLD = {
    "passes": None, "override_accepted": False, "suggested_string_length": None,
    "per_module": None, "string_voltage": None, "max_dc_voltage": None,
}


def _coordinates_valid(latitude, longitude):
    if latitude is None and longitude is None:
        return True
    return all(type(value) in (int, float) and -limit <= value <= limit
               and math.isfinite(value)
               for value, limit in ((latitude, 90), (longitude, 180)))


def normalize_project_changes(patch):
    if (type(patch) is not dict or not 1 <= len(patch) <= 4
            or set(patch) - {"name", "zip_code", "latitude", "longitude"}):
        raise GraphValidationError("INVALID_PROJECT_REQUEST")
    result = dict(patch)
    for key, limit in (("name", 4096), ("zip_code", 10)):
        if key in result:
            value = result[key]
            if type(value) is not str or len(value) > limit:
                raise GraphValidationError("INVALID_PROJECT_REQUEST")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise GraphValidationError("INVALID_PROJECT_REQUEST") from None
            result[key] = value.strip()
    if "name" in result and not result["name"]:
        raise GraphValidationError("PROJECT_NAME_REQUIRED")
    if result.get("zip_code") and not ZIP_RE.fullmatch(result["zip_code"]):
        raise GraphValidationError("INVALID_PROJECT_ZIP")
    if (("latitude" in result) != ("longitude" in result)
            or not _coordinates_valid(result.get("latitude"), result.get("longitude"))):
        raise GraphValidationError("INVALID_PROJECT_COORDINATES")
    return result


def project_validity(project):
    reasons = []
    name = project.get("name")
    zip_code = project.get("zip_code", "")
    if type(name) is not str or not name.strip():
        reasons.append("project_name_required")
    if type(zip_code) is str and not zip_code.strip():
        reasons.append("project_zip_required")
    elif type(zip_code) is not str or not ZIP_RE.fullmatch(zip_code.strip()):
        reasons.append("invalid_project_zip")
    if not _coordinates_valid(project.get("latitude"), project.get("longitude")):
        reasons.append("invalid_project_coordinates")
    units = project.get("units")
    if type(units) is not dict or not units.get("drawing_units"):
        reasons.append("project_units_required")
    return {"state": "unknown" if reasons else "valid", "reasons": reasons}


def apply_project_changes(graph, patch):
    changes = normalize_project_changes(patch)
    project = graph["project"]
    before = canonical_bytes(project)
    if ("zip_code" in changes and changes["zip_code"] != project.get("zip_code")
            and "latitude" not in changes):
        changes.update(latitude=None, longitude=None)
    # Equal numeric values must retain their stored JSON representation for sizing digests.
    changes = {key: value for key, value in changes.items() if project.get(key) != value}
    project.update(changes)
    project["validity"] = project_validity(project)
    material = canonical_bytes(project) != before
    if material:
        invalidate_project_dependents(graph)
    return material


def invalidate_project_dependents(graph):
    """The project-change rule, in place: sizing confirmation cleared, settings.extra.string_sizing
    dropped, voc_cold reseeded on the settings and every electrical zone, and every derived entity
    not already stale marked stale with the one reason "project_changed" (an entity already stale
    keeps its first cause). One pass over the entities, no I/O."""
    settings = graph["settings"]
    settings["global_string_sizing_confirmed"] = False
    settings["extra"].pop("string_sizing", None)
    for target in [settings] + graph["electrical_zones"]:
        target["voc_cold"] = copy.deepcopy(SEED_VOC_COLD)
    for entity in entities(graph):
        if (entity["kind"] in DERIVED_KINDS
                and entity["validity"]["state"] != "stale"):
            entity["validity"] = {"state": "stale", "reasons": ["project_changed"]}


def set_installation_design(graph, design):
    """Set graph["project"]["installation_design"] to `design` ("Roof" or "Ground"), in place.

    Returns False and changes nothing when the drawing already has that design. A drawing with any
    frame cannot change design: the validator requires every frame to carry the project's design
    (INSTALLATION_DESIGN_MISMATCH), a Ground frame must carry a tracker and a Roof frame must not
    (the frame if/then/else in the graph contract), so no frame converts. That case raises
    INSTALLATION_DESIGN_MISMATCH; callers refuse it first with their own named code. Otherwise the
    design is written, the project validity recomputed and the project-change rule run
    (invalidate_project_dependents). A design outside INSTALLATION_DESIGNS is
    INVALID_PROJECT_REQUEST. Fails closed before any write."""
    if type(design) is not str or design not in INSTALLATION_DESIGNS:
        raise GraphValidationError("INVALID_PROJECT_REQUEST")
    project = graph["project"]
    if project["installation_design"] == design:
        return False
    if graph["frames"]:
        raise GraphValidationError("INSTALLATION_DESIGN_MISMATCH")
    project["installation_design"] = design
    project["validity"] = project_validity(project)
    invalidate_project_dependents(graph)
    return True
