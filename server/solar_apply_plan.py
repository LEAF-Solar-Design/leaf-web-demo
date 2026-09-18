"""Pure, deterministic native plans for Solar Rooftop frames and strings.

This module produces data only. Execution, idempotency receipts, allocation and
publication belong to the existing capability/job rails. Private names and
field layouts are supplied by an adapter, never inferred from application IDs.
"""
from __future__ import annotations

import copy
import json
import re

try:
    from .solar_design_graph import validate_graph, COLLECTIONS
    from .solar_inspection import (
        canonical_bytes, digest, normalize_handle, parse_json, refuse,
        store_address, validate_geometry, validate_inspection, validate_schema,
        validate_units,
    )
except ImportError:
    from solar_design_graph import validate_graph, COLLECTIONS
    from solar_inspection import (
        canonical_bytes, digest, normalize_handle, parse_json, refuse,
        store_address, validate_geometry, validate_inspection, validate_schema,
        validate_units,
    )


_META = {"id", "kind", "rev", "provenance", "validity", "extra"}
_CONVERSIONS = {"identity", "metres", "point", "points", "integer", "decimal",
                "handle", "handles-csv", "native-number", "native-name", "enum",
                "length", "sequence-lengths"}


def _closed(value, required, optional=()):
    if (type(value) is not dict or not set(required) <= set(value)
            or set(value) - set(required) - set(optional)):
        refuse("INVALID_WRITE_ADAPTER")


def _tokens(pointer):
    if type(pointer) is not str or len(pointer) > 4096 or (pointer and not pointer.startswith("/")):
        refuse("INVALID_JSON_POINTER")
    parts = pointer.split("/")[1:]
    if any(re.search(r"~(?![01])", p) for p in parts):
        refuse("INVALID_JSON_POINTER")
    return [p.replace("~1", "/").replace("~0", "~") for p in parts]


def _get(value, pointer):
    for token in _tokens(pointer):
        if type(value) is dict and token in value:
            value = value[token]
        elif type(value) is list and re.fullmatch(r"0|[1-9][0-9]*", token) and int(token) < len(value):
            value = value[int(token)]
        else:
            refuse("UNRESOLVED_WRITE_FIELD")
    return copy.deepcopy(value)


def _put(value, pointer, replacement):
    parts = _tokens(pointer)
    if not parts:
        refuse("ROOT_REPLACEMENT_FORBIDDEN")
    current = value
    for i, token in enumerate(parts):
        final = i == len(parts) - 1
        next_value = replacement if final else ([] if parts[i + 1].isdigit() else {})
        if type(current) is list and re.fullmatch(r"0|[1-9][0-9]*", token):
            index = int(token)
            if index > 100000 or index > len(current):
                refuse("SPARSE_WRITE_FIELD")
            if index == len(current):
                current.append(copy.deepcopy(next_value))
            elif final:
                current[index] = copy.deepcopy(replacement)
            current = current[index]
        elif type(current) is dict:
            if final or token not in current:
                current[token] = copy.deepcopy(next_value)
            current = current[token]
        else:
            refuse("INVALID_WRITE_DESTINATION")


def _expand(context, pointer):
    result = [(context, ())]
    for token in _tokens(pointer):
        following = []
        for value, indices in result:
            if token == "*":
                if type(value) is not list:
                    refuse("INVALID_WRITE_WILDCARD")
                following.extend((item, indices + (str(i),)) for i, item in enumerate(value))
            else:
                following.append((_get(value, "/" + token.replace("~", "~0").replace("/", "~1")), indices))
        result = following
    return result


def validate_adapter(adapter):
    canonical_bytes(adapter)
    _closed(adapter, {"schema", "version", "write_version", "entity_kinds", "native_templates",
                      "store_codecs", "write_fields", "identity", "preservation", "evidence"})
    if adapter["schema"] != "leaf.solar-adapter.v1" or type(adapter["write_version"]) is not int or adapter["write_version"] != 1:
        refuse("UNSUPPORTED_WRITE_ADAPTER")
    if type(adapter["version"]) is not int or adapter["version"] < 1:
        refuse("INVALID_WRITE_ADAPTER")
    for section in ("entity_kinds", "native_templates", "store_codecs", "write_fields", "identity"):
        if type(adapter[section]) is not dict:
            refuse("INVALID_WRITE_ADAPTER")
    if set(adapter["native_templates"]) != {"frame", "string"}:
        refuse("UNSUPPORTED_NATIVE_TEMPLATE")
    _closed(adapter["preservation"], {"allowed_changed_paths", "read_only_codecs"})
    _closed(adapter["evidence"], {"source_revision", "rules", "compatibility_notes"})
    if (not adapter["evidence"]["source_revision"] or not adapter["evidence"]["rules"]
            or type(adapter["preservation"]["allowed_changed_paths"]) is not dict
            or type(adapter["preservation"]["read_only_codecs"]) is not list):
        refuse("INVALID_ADAPTER_EVIDENCE")
    for codec in adapter["store_codecs"].values():
        _closed(codec, {"scope", "dictionary_path", "key", "app", "encoding", "document_count",
                        "count_code", "string_code", "chunk_size", "legacy_encodings", "defaults"})
        if (codec["encoding"] not in {"json", "chunked-ascii(240)", "xdata-typed"}
                or type(codec["document_count"]) is not int or not 0 <= codec["document_count"] <= 100
                or codec["chunk_size"] != 240 or codec["count_code"] != 90 or codec["string_code"] != 1000
                or type(codec["legacy_encodings"]) is not list
                or set(codec["legacy_encodings"]) - {"json", "chunked-ascii(240)", "xdata-typed"}):
            refuse("INVALID_STORE_CODEC")
        if (codec["scope"] not in {"named-dictionary", "extension-dictionary", "entity-xdata"}
                or type(codec["dictionary_path"]) is not list or len(codec["dictionary_path"]) > 32
                or any(type(p) is not str or not p or len(p) > 4096 for p in codec["dictionary_path"])):
            refuse("INVALID_STORE_CODEC")
        if codec["scope"] == "entity-xdata":
            if (type(codec["app"]) is not str or not codec["app"] or codec["key"] is not None
                    or codec["dictionary_path"] or codec["encoding"] != "xdata-typed"):
                refuse("INVALID_STORE_CODEC")
        elif type(codec["key"]) is not str or not codec["key"] or codec["app"] is not None:
            refuse("INVALID_STORE_CODEC")
        expected = "records" if codec["encoding"] == "xdata-typed" else "documents"
        _closed(codec["defaults"], {expected})
        if type(codec["defaults"][expected]) is not list:
            refuse("INVALID_STORE_CODEC")
        if expected == "documents" and len(codec["defaults"][expected]) != codec["document_count"]:
            refuse("STORE_DOCUMENT_COUNT_MISMATCH")
    for rules in adapter["write_fields"].values():
        if type(rules) is not list:
            refuse("INVALID_WRITE_FIELDS")
        destinations = set()
        for rule in rules:
            _closed(rule, {"source", "destination", "conversion", "null", "default"}, {"values"})
            _tokens(rule["source"])
            _tokens(rule["destination"])
            if (rule["conversion"] not in _CONVERSIONS or rule["null"] not in {"reject", "preserve", "default"}
                    or rule["destination"] in destinations):
                refuse("INVALID_WRITE_FIELDS")
            destinations.add(rule["destination"])
    for kind, template in adapter["native_templates"].items():
        _closed(template, {"carriers", "stores"}, {"circuit"})
        if type(template["carriers"]) is not list or type(template["stores"]) is not list:
            refuse("INVALID_NATIVE_TEMPLATE")
        roles = set()
        for carrier in template["carriers"]:
            _closed(carrier, {"role", "kind", "layer_source", "geometry", "attributes", "fields"})
            if carrier["role"] in roles or carrier["fields"] not in adapter["write_fields"]:
                refuse("INVALID_CARRIER_ROLE")
            roles.add(carrier["role"])
            _tokens(carrier["layer_source"])
        for spec in template["stores"]:
            _closed(spec, {"role", "codec", "fields"})
            if (spec["role"] not in roles or spec["codec"] not in adapter["store_codecs"]
                    or spec["fields"] not in adapter["write_fields"]
                    or spec["codec"] in adapter["preservation"]["read_only_codecs"]):
                refuse("INVALID_TEMPLATE_STORE")
        identity = adapter["identity"].get(kind)
        _closed(identity, {"roles", "map_key", "discriminators"})
        if set(identity["roles"]) != roles or identity["map_key"] != "drawing-key/handle":
            refuse("INVALID_IDENTITY_RULE")
        if set(identity["discriminators"]) != roles:
            refuse("INVALID_IDENTITY_RULE")
        for rule in identity["discriminators"].values():
            _closed(rule, {"kind", "codec"})
            if rule["codec"] is not None and rule["codec"] not in adapter["store_codecs"]:
                refuse("INVALID_IDENTITY_RULE")
    return copy.deepcopy(adapter)


def _convert(value, rule, context, handles, index, scale):
    if value is None:
        if rule["null"] == "reject":
            refuse("UNRESOLVED_WRITE_FIELD")
        return copy.deepcopy(rule["default"] if rule["null"] == "default" else None)
    conversion = rule["conversion"]
    if conversion == "identity":
        return copy.deepcopy(value)
    if conversion == "metres":
        if type(value) not in (int, float):
            refuse("INVALID_METRE_VALUE")
        return value / scale
    if conversion == "point":
        if type(value) is not list or len(value) not in (2, 3) or any(type(v) not in (int, float) for v in value):
            refuse("INVALID_GRAPH_POINT")
        return [v / scale for v in value] + ([0] if len(value) == 2 else [])
    if conversion == "points":
        return [_convert(v, {**rule, "conversion": "point"}, context, handles, index, scale) for v in value]
    if conversion in {"integer", "decimal"}:
        if type(value) is int:
            number = value
        elif type(value) is str and re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
            number = int(value)
        else:
            refuse("INVALID_NATIVE_INTEGER")
        if not -(2 ** 31) <= number < 2 ** 31:
            refuse("INVALID_NATIVE_INTEGER")
        return str(number) if conversion == "decimal" else number
    if conversion == "enum":
        if value not in rule.get("values", {}):
            refuse("UNMAPPED_NATIVE_ENUM")
        return copy.deepcopy(rule["values"][value])
    if conversion == "length":
        return len(value)
    if conversion == "sequence-lengths":
        return [len(sequence["ordered_panel_refs"]) for sequence in value]
    if conversion in {"native-number", "native-name"}:
        target = index.get(value)
        field = "number" if conversion == "native-number" else "name"
        if target is None or field not in target:
            refuse("UNRESOLVED_NATIVE_REFERENCE")
        return target[field]
    if conversion == "handle":
        if value not in handles:
            refuse("UNRESOLVED_NATIVE_REFERENCE")
        return handles[value]
    if conversion == "handles-csv":
        csv = ",".join(_convert(v, {**rule, "conversion": "handle"}, context, handles, index, scale) for v in value)
        if len(csv) > 240:
            raise NotImplementedError("string long cable CSV chunking")
        return csv
    refuse("UNKNOWN_WRITE_CONVERSION")


def _project(target, fields, context, handles, index, scale):
    result = copy.deepcopy(target)
    for rule in fields:
        for value, indices in _expand(context, rule["source"]):
            destination = rule["destination"]
            for i, token in enumerate(indices):
                destination = destination.replace("{" + str(i) + "}", token)
            if "{" in destination or "}" in destination:
                refuse("UNRESOLVED_WRITE_DESTINATION")
            _put(result, destination, _convert(value, rule, context, handles, index, scale))
    return result


def _decode_store(store, codec):
    records = store["raw_records"]
    if codec["encoding"] == "xdata-typed":
        return "xdata-typed", {"records": [{"code": r["code"], "value": r["value"]} for r in records]}
    if all(r["code"] == codec["string_code"] and r["type"] == "string" for r in records):
        documents = [r["value"] for r in records]
        encoding = "json"
    else:
        documents, offset = [], 0
        encoding = "chunked-ascii(240)"
        while offset < len(records):
            count = records[offset]
            offset += 1
            if (count["code"] != codec["count_code"] or count["type"] != "int32"
                    or type(count["value"]) is not int or count["value"] < 1
                    or offset + count["value"] > len(records)):
                refuse("MALFORMED_CHUNK_COUNT")
            chunks = records[offset:offset + count["value"]]
            if any(r["code"] != codec["string_code"] or r["type"] != "string"
                   or len(r["value"].encode("utf-16-le")) // 2 > 240 for r in chunks):
                refuse("MALFORMED_CHUNK_RECORD")
            documents.append("".join(r["value"] for r in chunks))
            offset += count["value"]
    if len(documents) != codec["document_count"] or encoding not in codec["legacy_encodings"]:
        refuse("STORE_DOCUMENT_COUNT_MISMATCH")
    return encoding, {"documents": documents}


def _json_spans(raw):
    """Capture value spans so a changed field never rewrites unrelated tokens."""
    parse_json(raw)  # Reject duplicate keys before walking the text.
    decoder = json.JSONDecoder()
    spans = {}

    def ws(pos):
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        return pos

    def visit(pos, path):
        start = pos = ws(pos)
        if raw[pos] == "{":
            pos = ws(pos + 1)
            while raw[pos] != "}":
                key, pos = decoder.raw_decode(raw, pos)
                pos = ws(pos)
                pos = visit(pos + 1, path + (key,))
                pos = ws(pos)
                if raw[pos] == ",":
                    pos = ws(pos + 1)
                else:
                    break
            pos += 1
        elif raw[pos] == "[":
            pos, index = ws(pos + 1), 0
            while raw[pos] != "]":
                pos = visit(pos, path + (str(index),))
                index += 1
                pos = ws(pos)
                if raw[pos] == ",":
                    pos = ws(pos + 1)
                else:
                    break
            pos += 1
        else:
            _, pos = decoder.raw_decode(raw, pos)
        spans[path] = (start, pos)
        return pos

    visit(0, ())
    return spans


def patch_document(raw, updates):
    """Patch only supplied leaf paths, preserving raw unknown property tokens."""
    result = raw
    for pointer, value in sorted(updates.items()):
        parts = tuple(_tokens(pointer))
        spans = _json_spans(result)
        decoded = parse_json(result)
        replacement = canonical_bytes(value).decode("ascii")
        if parts in spans:
            start, end = spans[parts]
            if _get(decoded, pointer) == value:
                continue
            result = result[:start] + replacement + result[end:]
        else:
            parent = parts[:-1]
            if parent not in spans:
                refuse("CHANGED_STORE_STRUCTURE_UNSUPPORTED")
            parent_pointer = "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parent) if parent else ""
            container = _get(decoded, parent_pointer)
            if type(container) is not dict:
                refuse("CHANGED_STORE_STRUCTURE_UNSUPPORTED")
            _, end = spans[parent]
            addition = ("," if container else "") + json.dumps(parts[-1]) + ":" + replacement
            result = result[:end - 1] + addition + result[end - 1:]
    return result


def _leaves(value, prefix=""):
    if type(value) is dict and value:
        for key, child in value.items():
            yield from _leaves(child, prefix + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif type(value) is list and value:
        for i, child in enumerate(value):
            yield from _leaves(child, prefix + "/" + str(i))
    else:
        yield prefix, value


def reconcile_identity(mapping, inspection, adapter, drawing_key):
    """Resolve only committed, scoped carrier identities. Never mint on failure."""
    _closed(mapping, {"drawing_key", "entities"})
    if mapping["drawing_key"] != drawing_key or type(mapping["entities"]) is not list:
        refuse("MAPPING_DRAWING_MISMATCH")
    by_handle = {normalize_handle(e["handle"]): e for e in inspection["entities"]}
    resolved, occupied = {}, set()
    for entry in mapping["entities"]:
        _closed(entry, {"app_id", "role", "kind", "handle", "entity_sha256", "payload_sha256"})
        handle = normalize_handle(entry["handle"])
        key = (entry["app_id"], entry["role"])
        if key in resolved or handle in occupied:
            refuse("AMBIGUOUS_CARRIER_IDENTITY")
        occupied.add(handle)
        entity = by_handle.get(handle)
        if entity is None:
            refuse("MISSING_MAPPED_CARRIER")
        if entity["kind"] != entry["kind"] or entity["sha256"] != entry["entity_sha256"]:
            refuse("MAPPED_CARRIER_DRIFT")
        parts = entry["app_id"].split(":")
        if len(parts) != 3:
            refuse("INVALID_MAPPING_ID")
        kind = parts[1]
        if kind in adapter["identity"]:
            rule = adapter["identity"][kind]["discriminators"].get(entry["role"])
            if rule is None or rule["kind"] != entity["kind"]:
                refuse("CARRIER_ROLE_MISMATCH")
            if rule["codec"] is not None:
                codec = adapter["store_codecs"][rule["codec"]]
                address = {k: codec[k] for k in ("scope", "dictionary_path", "key", "app")}
                address["entity_handle"] = handle
                matches = [s for s in inspection["stores"] if store_address(s) == store_address(address)]
                if len(matches) != 1 or matches[0]["sha256"] != entry["payload_sha256"]:
                    refuse("CARRIER_PAYLOAD_IDENTITY_MISMATCH")
        resolved[key] = entity
    return resolved


def _typed_payload(payload):
    for record in payload["records"]:
        code, value = record["code"], record["value"]
        if code in {1070, 1071, 90}:
            bits = 16 if code == 1070 else 32
            if type(value) is not int or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                refuse("INVALID_TYPED_INTEGER")
        elif code == 1040:
            if type(value) not in (int, float):
                refuse("INVALID_TYPED_DOUBLE")
        elif code == 1005:
            normalize_handle(value)
        elif code in {1, 1000, 1001}:
            if type(value) is not str or (code in {1000, 1001} and len(value.encode("utf-16-le")) // 2 > 240):
                refuse("INVALID_TYPED_STRING")
        else:
            refuse("UNSUPPORTED_WRITE_RECORD_CODE")


def validate_plan(plan):
    validate_schema(plan, "apply")
    validate_units(plan["units"])
    ids, carriers, writes, entity_writes = set(), {}, {}, set()
    for op in plan["operations"]:
        if op["op_id"] in ids:
            refuse("DUPLICATE_OPERATION_ID")
        ids.add(op["op_id"])
        attributes = op.get("attributes", op.get("changes", {}).get("attributes", []))
        tags = [a["tag"] for a in attributes]
        if len(set(tags)) != len(tags):
            refuse("DUPLICATE_ATTRIBUTE_TAG")
        if op["op"] == "create_entity":
            key = (op["app_id"], op["role"])
            if key in carriers.values():
                refuse("DUPLICATE_CARRIER_OPERATION")
            carriers[op["op_id"]] = key
            if op["kind"] != op["geometry"]["kind"]:
                refuse("NATIVE_KIND_MISMATCH")
            if (op["kind"] == "block_reference") != (op["block_name"] is not None):
                refuse("INVALID_BLOCK_NAME")
            validate_geometry(op["geometry"])
        elif op["op"] == "update_entity":
            handle = normalize_handle(op["entity_handle"])
            if handle in entity_writes:
                refuse("CONFLICTING_ENTITY_WRITES")
            entity_writes.add(handle)
            if "geometry" in op["changes"]:
                validate_geometry(op["changes"]["geometry"])
        else:
            address = store_address(op)
            existing = writes.setdefault(address, [])
            if existing and (len(existing) > 1 or existing[0]["op"] == op["op"]
                             or existing[0]["expected_payload_sha256"] != op["expected_payload_sha256"]):
                refuse("CONFLICTING_STORE_WRITES")
            existing.append(op)
            if op["op"] == "set_store":
                payload = op["payload"]
                if payload["encoding"] == "xdata-typed":
                    _typed_payload(payload)
                    if op["scope"] == "entity-xdata" and payload["records"][0] != {"code": 1001, "value": op["app"]}:
                        refuse("XDATA_REGISTRATION_MISMATCH")
                    if op["scope"] != "entity-xdata" and any(r["code"] == 1001 for r in payload["records"]):
                        refuse("XRECORD_REGISTRATION_FORBIDDEN")
                else:
                    if op["scope"] == "entity-xdata":
                        refuse("XDATA_REQUIRES_TYPED_PAYLOAD")
                    for document in payload["documents"]:
                        parse_json(document)
    for addressed in writes.values():
        if len(addressed) == 1 and addressed[0]["op"] == "delete_store":
            refuse("DELETE_STORE_OWNERSHIP_REQUIRED")
    for op in plan["operations"]:
        if op["op"] != "set_store":
            continue
        if op["entity_create_ref"] is not None and op["entity_create_ref"] not in carriers:
            refuse("UNRESOLVED_CREATE_REFERENCE")
        pointers = set()
        for binding in op["bindings"]:
            if binding["pointer"] in pointers:
                refuse("CONFLICTING_BINDINGS")
            pointers.add(binding["pointer"])
            source = binding["source"]
            if "create_ref" in source and source["create_ref"] not in carriers:
                refuse("UNRESOLVED_CREATE_REFERENCE")
            decoded_payload = copy.deepcopy(op["payload"])
            if "documents" in decoded_payload:
                decoded_payload["documents"] = [parse_json(d) for d in decoded_payload["documents"]]
            _get(decoded_payload, binding["pointer"])
    return copy.deepcopy(plan)


def serialize_plan(plan):
    return canonical_bytes(validate_plan(plan))


def _semantic(node):
    result = {k: v for k, v in node.items() if k not in _META}
    # These graph relationships are persisted by frame/string stores, not by
    # creating or rewriting panel entities or inverter datasheets.
    if node["kind"] == "panel":
        for key in ("assignment", "frame_ref", "matrix_cell"):
            result.pop(key, None)
    if node["kind"] == "inverter":
        result.pop("input_assignments", None)
    if node["kind"] == "string":
        result["circuit_assignment"] = node["extra"].get("circuit_assignment")
    return result


def _context(node, graph, template, index):
    context = {"node": node, "graph": graph, "resolved": {}}
    if node["kind"] == "frame":
        width = node["module_columns"] * node["module_width_along_row"]
        height = node["module_rows"] * node["module_height_across_row"]
        context["resolved"]["outline"] = [[0, 0], [width, 0], [width, height], [0, height]]
        if "[" in node["name"]:
            refuse("INVALID_FRAME_NAME")
    else:
        assignment = node["extra"].get("circuit_assignment")
        if type(assignment) is not dict or len(node["route"]) < 2:
            refuse("RESOLVED_CIRCUIT_ASSIGNMENT_REQUIRED")
        circuit = template.get("circuit")
        _closed(circuit, {"parts", "separator", "inverter_number_source", "input_number_source", "input_number_offset"})
        context["resolved"] = {"assignment": assignment, "start_position": node["route"][-1],
                               "end_position": node["route"][0], "tag_position": node["route"][0]}
        tag = circuit["separator"].join(str(_get(context, p)) for p in circuit["parts"])
        inverter = index.get(node["inverter_ref"])
        if (tag != node["circuit_tag"] or inverter is None or inverter["kind"] != "inverter"
                or inverter["number"] != _get(context, circuit["inverter_number_source"])):
            refuse("CIRCUIT_ASSIGNMENT_MISMATCH")
        inputs = [a for a in inverter["input_assignments"] if a["string_ref"] == node["id"]]
        if (len(inputs) != 1 or inputs[0]["mppt_letter"] != assignment.get("mppt_letter")
                or inputs[0]["input_number"] + circuit["input_number_offset"] != _get(context, circuit["input_number_source"])):
            refuse("CIRCUIT_ASSIGNMENT_MISMATCH")
    return context


def _native_changes(entity, previous, projected, kind):
    """Overlay changed graph fields on captured native values, not defaults."""
    geometry = copy.deepcopy(entity["native_geometry"])
    for field, value in projected["geometry"].items():
        if previous["geometry"][field] == value:
            continue
        if field == "definition":
            raise NotImplementedError(kind + " block definition replacement")
        geometry[field] = copy.deepcopy(value)
    changes = {}
    if geometry != entity["native_geometry"]:
        changes["geometry"] = geometry
    existing = {a["tag"]: a for a in entity["attributes"]}
    prior_attributes = {a["tag"]: a for a in previous["attributes"]}
    updates = []
    for attribute in projected["attributes"]:
        tag = attribute["tag"]
        prior = prior_attributes.get(tag)
        if prior == attribute:
            continue
        if tag not in existing or prior is None:
            refuse("MISSING_MAPPED_ATTRIBUTE")
        updated = copy.deepcopy(existing[tag])
        for field, value in attribute.items():
            if prior[field] != value:
                updated[field] = copy.deepcopy(value)
        if updated != existing[tag]:
            updates.append(updated)
    if updates:
        changes["attributes"] = updates
    return changes


def build_apply_plan(before_graph, after_graph, adapter, inspection, mapping, *,
                     drawing_key, expected_revision, idempotency_key,
                     dwg_sha256=None, adapter_sha256=None, mapping_sha256=None):
    """Build a closed plan; inputs are copied and no caller-owned state changes.

    Mapping hashes bind canonical JSON bytes. Supplied digest preconditions are
    checked, never trusted as labels. Site revision remains a source precondition;
    allocating the next GUID is part of the parked native transaction.
    """
    before = validate_graph(before_graph)
    after = validate_graph(after_graph)
    adapter = validate_adapter(adapter)
    inspection = validate_inspection(inspection)
    _closed(expected_revision, {"graph_rev", "site_revision"})
    if (type(expected_revision["graph_rev"]) is not int
            or expected_revision["graph_rev"] != before["rev"]
            or expected_revision["site_revision"] != before["project"]["site_revision"]):
        refuse("EXPECTED_REVISION_MISMATCH")
    if after["rev"] not in {before["rev"], before["rev"] + 1}:
        refuse("INVALID_PLAN_REVISION")
    if before["project"]["id"] != after["project"]["id"]:
        refuse("GRAPH_ID_MISMATCH")
    source = inspection["dwg_sha256"]
    if (before["source_hash"] != source or after["source_hash"] != source
            or dwg_sha256 is not None and dwg_sha256 != source):
        refuse("DWG_SOURCE_MISMATCH")
    for supplied, actual in ((adapter_sha256, digest(adapter)), (mapping_sha256, digest(mapping))):
        if supplied is not None and supplied != actual:
            refuse("PLAN_BINDING_MISMATCH")
    units = before["project"]["units"]
    validate_units(units)
    if after["project"]["units"] != units or any(units[k] != v for k, v in inspection["units"].items()):
        refuse("SOLAR_UNITS_MISMATCH")
    for key in ("opaque_stores", "orphaned_xdata"):
        if before[key] != after[key]:
            refuse("UNTOUCHED_CONTENT_CHANGED")
    for key in ("project", "settings"):
        if _semantic(before[key]) != _semantic(after[key]):
            raise NotImplementedError(key)
    prior = {n["id"]: n for c in COLLECTIONS for n in before[c]}
    index = {n["id"]: n for c in COLLECTIONS for n in after[c]}
    changed = []
    for app_id in sorted(set(prior) | set(index)):
        old, new = prior.get(app_id), index.get(app_id)
        if old is not None and new is not None and _semantic(old) == _semantic(new):
            continue
        kind = (new or old)["kind"]
        if kind not in {"frame", "string"}:
            raise NotImplementedError(kind)
        if new is None:
            raise NotImplementedError(kind + " removal (entity erasure is outside W1)")
        changed.append(new)
    changed_ids = {node["id"] for node in changed}
    for node in after["inverters"]:
        old = prior.get(node["id"])
        if old and old["input_assignments"] != node["input_assignments"]:
            old_inputs = {a["string_ref"]: a for a in old["input_assignments"]}
            new_inputs = {a["string_ref"]: a for a in node["input_assignments"]}
            if any(old_inputs.get(key) != new_inputs.get(key) and key not in changed_ids
                   for key in set(old_inputs) | set(new_inputs)):
                raise NotImplementedError("inverter input_assignments without string projection")
    resolved = reconcile_identity(mapping, inspection, adapter, drawing_key)
    handles = {}
    for (app_id, role), entity in resolved.items():
        if app_id not in prior:
            refuse("MAPPING_APPLICATION_ID_MISMATCH")
        if role == "primary":
            handles[app_id] = normalize_handle(entity["handle"])
    operations, stores = [], []
    scale = units["meters_per_unit"]
    captured = {store_address(s): s for s in inspection["stores"]}
    for node in changed:
        app_id, kind = node["id"], node["kind"]
        template = adapter["native_templates"][kind]
        context = _context(node, after, template, index)
        previous_context = _context(prior[app_id], before, template, prior) if app_id in prior else None
        carrier_targets = {}
        for carrier in template["carriers"]:
            role = carrier["role"]
            projected = _project({"geometry": carrier["geometry"], "attributes": carrier["attributes"]},
                                 adapter["write_fields"][carrier["fields"]], context, handles, index, scale)
            geometry, attributes = projected["geometry"], projected["attributes"]
            if geometry["kind"] == "polyline":
                for field in ("bulges", "start_widths", "end_widths"):
                    if not geometry[field]:
                        geometry[field] = [0] * len(geometry["vertices"])
            layer = _get(context, carrier["layer_source"])
            entity = resolved.get((app_id, role))
            op_id = "entity:" + app_id + ":" + role
            if app_id in prior:
                if entity is None:
                    refuse("MISSING_MAPPED_CARRIER")
                carrier_targets[role] = {"handle": normalize_handle(entity["handle"])}
                if entity["kind"] != carrier["kind"]:
                    refuse("CARRIER_ROLE_MISMATCH")
                previous = _project({"geometry": carrier["geometry"], "attributes": carrier["attributes"]},
                                    adapter["write_fields"][carrier["fields"]], previous_context, handles, prior, scale)
                if previous["geometry"]["kind"] == "polyline":
                    for field in ("bulges", "start_widths", "end_widths"):
                        if not previous["geometry"][field]:
                            previous["geometry"][field] = [0] * len(previous["geometry"]["vertices"])
                changes = _native_changes(entity, previous, projected, kind)
                if changes:
                    operations.append({"op": "update_entity", "op_id": op_id,
                                       "entity_handle": entity["handle"],
                                       "expected_entity_sha256": entity["sha256"], "changes": changes})
            else:
                if entity is not None:
                    refuse("NEW_ENTITY_ALREADY_MAPPED")
                carrier_targets[role] = {"create_ref": op_id}
                operations.append({"op": "create_entity", "op_id": op_id, "app_id": app_id,
                                   "role": role, "kind": carrier["kind"], "layer": layer,
                                   "geometry": geometry, "attributes": attributes,
                                   "block_name": geometry["definition"]["name"] if carrier["kind"] == "block_reference" else None})
        for spec in template["stores"]:
            codec = adapter["store_codecs"][spec["codec"]]
            target = carrier_targets[spec["role"]]
            address = {k: copy.deepcopy(codec[k]) for k in ("scope", "dictionary_path", "key", "app")}
            address.update(entity_handle=target.get("handle"), entity_create_ref=target.get("create_ref"))
            old_store = captured.get(store_address(address))
            encoding = codec["encoding"]
            defaults = copy.deepcopy(codec["defaults"])
            fields = adapter["write_fields"][spec["fields"]]
            allowed = adapter["preservation"]["allowed_changed_paths"].get(spec["codec"], [])
            if any(rule["destination"] not in allowed for rule in fields):
                refuse("UNDECLARED_CHANGED_PATH")
            projected = _project(defaults, fields, context, handles, index, scale)
            bindings = []
            # References to sibling carriers are explicit plan bindings. No
            # allocation or deferred serialization is executed in this module.
            for i, record in enumerate(projected.get("records", [])):
                value = record["value"]
                if type(value) is str and value.startswith("@role:"):
                    source_ref = carrier_targets.get(value[6:])
                    if source_ref is None or record["code"] != 1005:
                        refuse("UNRESOLVED_CARRIER_BINDING")
                    record["value"] = source_ref.get("handle", "0")
                    bindings.append({"pointer": "/records/" + str(i) + "/value", "source": source_ref,
                                     "representation": "handle"})
            if encoding != "xdata-typed":
                documents = [canonical_bytes(d).decode("ascii") for d in projected["documents"]]
                if old_store:
                    encoding, decoded = _decode_store(old_store, codec)
                    documents = []
                    # Expand just the declared fields, not defaults or whole
                    # objects. Unrelated fields and original number lexemes stay.
                    for i, raw in enumerate(decoded["documents"]):
                        updates = {}
                        for rule in fields:
                            for value, indices in _expand(context, rule["source"]):
                                pointer = rule["destination"]
                                for j, token in enumerate(indices):
                                    pointer = pointer.replace("{" + str(j) + "}", token)
                                prefix = "/documents/" + str(i)
                                if pointer.startswith(prefix + "/"):
                                    converted = _convert(value, rule, context, handles, index, scale)
                                    updates[pointer[len(prefix):]] = converted
                        documents.append(patch_document(raw, updates))
                    if encoding == "json" and any(len(d.encode("utf-16-le")) // 2 > 240 for d in documents):
                        encoding = "chunked-ascii(240)"
                payload = {"encoding": encoding, "documents": documents}
            else:
                if old_store:
                    _, decoded = _decode_store(old_store, codec)
                    old_records = decoded["records"]
                    if len(old_records) < len(projected["records"]):
                        refuse("MALFORMED_TYPED_STORE")
                    if any(a["code"] != b["code"] for a, b in zip(old_records, projected["records"])):
                        refuse("TYPED_STORE_LAYOUT_MISMATCH")
                    projected["records"].extend(old_records[len(projected["records"]):])
                payload = {"encoding": encoding, "records": projected["records"]}
            if old_store:
                old_encoding, old_payload = _decode_store(old_store, codec)
                if payload == {"encoding": old_encoding, **old_payload} and not any("create_ref" in b["source"] for b in bindings):
                    continue
            payload_text = canonical_bytes(payload).decode("ascii")
            if any(identifier in payload_text for identifier in index):
                refuse("APPLICATION_ID_IN_NATIVE_STORE")
            stores.append({"op": "set_store", "op_id": "store:" + app_id + ":" + spec["codec"],
                           **address, "expected_payload_sha256": old_store["sha256"] if old_store else None,
                           "payload": payload, "bindings": bindings})
    plan = {"schema": "leaf.solar-apply.v1", "graph_id": before["project"]["id"],
            "drawing_key": drawing_key, "expected_revision": copy.deepcopy(expected_revision),
            "dwg_sha256": source, "adapter_sha256": digest(adapter), "mapping_sha256": digest(mapping),
            "idempotency_key": idempotency_key, "units": copy.deepcopy(units),
            "preserve_untouched": True, "operations": operations + stores}
    return validate_plan(plan)
