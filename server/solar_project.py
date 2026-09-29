"""Pure project edits, readiness and invalidation for the Solar graph."""
import copy
import math
import re

from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, entities
from solar_solve_results import DERIVED_KINDS


ZIP_RE = re.compile(r"^[0-9]{5}(-[0-9]{4})?$")
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
        settings = graph["settings"]
        settings["global_string_sizing_confirmed"] = False
        settings["extra"].pop("string_sizing", None)
        for target in [settings] + graph["electrical_zones"]:
            target["voc_cold"] = copy.deepcopy(SEED_VOC_COLD)
        for entity in entities(graph):
            if (entity["kind"] in DERIVED_KINDS
                    and entity["validity"]["state"] != "stale"):
                entity["validity"] = {"state": "stale", "reasons": ["project_changed"]}
    return material
