"""Author a provenance report for a checker selection bound by its project host."""
import hashlib
import json
import re


def matches(description):
    prompt = description.casefold()
    records = re.search(r"\b(?:records?|rows?|ids?|defects?)\b", prompt)
    usage = re.search(r"\b(?:used?|selected|consumed|came from|sourced from)\b", prompt)
    source = re.search(r"\b(?:checker|check|import(?:ed)?|json|log|project material)\b", prompt)
    return bool(records and usage and source)


def _selected_rows(document, pointer, count):
    parts = [segment.replace("~1", "/").replace("~0", "~") for segment in pointer[1:].split("/")]
    parent = document
    # validate_material_context has already validated the pointer in the envelope.
    # Walk the original independently so the envelope cannot substitute its rows.
    for index, part in enumerate(parts):
        if type(parent) is list:
            if not re.fullmatch(r"0|[1-9][0-9]*", part) or int(part) >= len(parent):
                raise ValueError("Selection pointer is absent from the original JSON.")
            key = int(part)
        elif type(parent) is dict and part in parent:
            key = part
        else:
            raise ValueError("Selection pointer is absent from the original JSON.")
        value = parent[key]
        if index == len(parts) - 1:
            if type(value) is not list or len(value) != count:
                raise ValueError("Selected record count does not match the original JSON.")
            parent[key] = {"schema": "leaf.selected-records-reference.v1",
                           "records_pointer": pointer, "row_count": count}
            return value
        parent = value


def bind_material_selection(intake, org_id, project_id, actor_id, material_id, context, *,
                            include_row_digests=False, include_row_values=False):
    """Bind a host-provided selection against a separately authorized material ID."""
    from integration_tool_service import validate_material_context
    import platform_link
    import project_reference_materials as originals

    validate_material_context(context)
    source = context["source"]
    if source["material_id"] != str(material_id):
        raise ValueError("Selected material does not match the host-bound material.")
    platform_link._ensure_platform_package()
    from leaf_platform import project_reference_materials as metadata
    material = metadata.get_material(org_id, project_id, actor_id, material_id)
    if (not material or material.get("material_id") != str(material_id)
            or material.get("org_id") != str(org_id) or material.get("project_id") != str(project_id)
            or material.get("media_type") != "application/json"
            or material.get("sha256") != source["sha256"] or material.get("byte_size") != source["byte_length"]):
        raise ValueError("Selection does not match the project's original JSON material.")
    document = json.loads(originals.read_content(org_id, project_id, material).decode("utf-8-sig"))
    pointer = context["selection"]["records_pointer"]
    rows = _selected_rows(document, pointer, context["row_count"])
    # JSON comparison preserves distinctions such as false versus 0.
    if json.dumps(document, sort_keys=True) != json.dumps(context["envelope"], sort_keys=True):
        raise ValueError("Selection envelope differs from the verified original JSON.")
    identities = []
    for row in rows:
        identity = row.get("id") if type(row) is dict else None
        identities.append(identity if type(identity) in (str, int) else None)
    selection = {**source, "records_pointer": pointer, "record_ids": identities}
    if include_row_digests:
        selection["record_sha256"] = [
            hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                      allow_nan=False).encode("utf-8")).hexdigest()
            for row in rows
        ]
    if include_row_values:
        selection["record_values"] = rows
    return {**intake, "checker_material_selection": selection}


def build_body(description):
    return _BODY


_BODY = """
def run(intake, params):
    selected = intake.get("checker_material_selection")
    if not selected:
        raise ValueError("This report needs the checker's host-bound project material selection.")
    pointer = selected["records_pointer"]
    records = []
    for index, identity in enumerate(selected["record_ids"]):
        row = {"pointer": pointer + "/" + str(index)}
        if identity is not None:
            row["id"] = identity
        records.append(row)
    return {"material_id": selected["material_id"], "sha256": selected["sha256"],
            "byte_length": selected["byte_length"], "records_pointer": pointer,
            "row_count": len(records), "records": records}, None
"""
