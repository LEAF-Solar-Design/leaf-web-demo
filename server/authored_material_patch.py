"""Author ordered edits to one verified array of identified JSON records."""
from decimal import Decimal
import json
import re

from authored_material_selection import bind_material_selection


_PREFIX = re.compile(r"(?:edit|patch|update) (?:the |my )?(?:selected |imported )?(?:json material|material records|json records):\s*", re.I)
_INDEX = re.compile(r"(?:0|[1-9][0-9]{0,8})\b")
_SEPARATOR = re.compile(r",\s*(?:then\s+)?|\s+then\s+", re.I)
MAX_OPERATIONS = 100
MAX_POINTER_SEGMENTS = 16


def _parse(description):
    if not isinstance(description, str):
        return None
    if len(description) > 4000:
        return "Material patch description budget exceeded."
    prefix = _PREFIX.match(description)
    if not prefix:
        return None
    tail = description[prefix.end():].strip().removesuffix(".")
    ops = []
    while tail:
        if len(ops) == MAX_OPERATIONS:
            return "Material patch operation budget exceeded."
        verb = re.match(r"(insert|remove|move|copy|set|replace|test)\s+", tail, re.I)
        if not verb:
            return None
        requested = verb[1].lower()
        action = "set" if requested == "replace" else requested
        try:
            value, end = _DECODER.raw_decode(tail[verb.end():])
        except (ValueError, RecursionError, OverflowError):
            return None
        # A positional row move uses "from index". Pointer moves use "to".
        if action == "move" and re.match(r"\s+to\s+", tail[verb.end()+end:], re.I):
            action = "move_field"
        if action in ("copy", "set", "test", "move_field"):
            if type(value) is not str:
                return None
            argument = tail[verb.end()+end:]
            relation = "equals" if action == "test" else "with" if requested == "replace" else "to"
            label = re.match(r"\s+" + relation + r"\s+", argument, re.I)
            if not label:
                return None
            try:
                other, end = _DECODER.raw_decode(argument[label.end():])
            except (ValueError, RecursionError, OverflowError):
                return None
            if action in ("copy", "move_field") and type(other) is not str:
                return None
            # A slash inside an escaped property (~1) is data, not another segment.
            pointers = (value, other) if action in ("copy", "move_field") else (value,)
            if any(pointer.count("/") > MAX_POINTER_SEGMENTS for pointer in pointers):
                return "Material patch pointer depth budget exceeded."
            ops.append((action, value, other))
            tail = argument[label.end()+end:]
        elif action == "insert":
            if type(value) is not dict or type(value.get("id")) not in (str, int):
                return None
        elif type(value) not in (str, int):
            return None
        if action not in ("copy", "set", "test", "move_field"):
            tail = tail[verb.end()+end:]
            positions = []
            for relation in (("from", "to") if action == "move" else ("at",)):
                label = re.match(r"\s+" + relation + r"\s+index\s+", tail, re.I)
                index = _INDEX.match(tail, label.end()) if label else None
                if not index:
                    return None
                positions.append(int(index[0]))
                tail = tail[index.end():]
            ops.append((action, value, positions))
        if not tail:
            return ops
        separator = _SEPARATOR.match(tail)
        if not separator:
            return None
        tail = tail[separator.end():]
    return None


def _decimal(token):
    number = float(token)
    if Decimal(str(number)) != Decimal(token):
        raise ValueError("Inserted number loses JSON precision.")
    return number


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate record property.")
    return result


def _invalid(token):
    raise ValueError("Invalid JSON number.")


_DECODER = json.JSONDecoder(parse_float=_decimal, object_pairs_hook=_object, parse_constant=_invalid)


def matches(description):
    # Keep malformed edit requests on this path so they cannot become unrelated drawing tools.
    return isinstance(description, str) and bool(_PREFIX.match(description))


def bind_selection(intake, org_id, project_id, actor_id, material_id, context):
    return bind_material_selection(intake, org_id, project_id, actor_id, material_id, context,
                                   include_row_values=True, exact_numbers=True)


def build_body(description):
    operations = _parse(description)
    if isinstance(operations, str):
        # Do not emit a partially executable plan for an over-budget request.
        return "def run(intake, params):\n    raise ValueError(" + repr(operations) + ")\n"
    return _BODY.replace("__OPERATIONS__", repr(operations))


_BODY = """
from copy import deepcopy
from decimal import Decimal
import json
import re


def location(rows, pointer, *, add=False, read_only=False, protect_records=True):
    if read_only and pointer == "":
        return [rows], 0
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("Invalid material JSON pointer.")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    if protect_records and not read_only and (len(parts) < 2 or parts[1] == "id"):
        raise ValueError("Select a field inside a record; its ID is immutable.")
    parent = rows
    for index, key in enumerate(parts):
        final = index == len(parts)-1
        if type(parent) is list:
            if key == "-" and final and add:
                key = len(parent)
            elif re.fullmatch(r"0|[1-9][0-9]{0,8}", key):
                key = int(key)
            else:
                raise ValueError("Invalid material array index.")
            if key > len(parent) or (key == len(parent) and not (add and final)):
                raise ValueError("Material array index is absent.")
        elif type(parent) is not dict or (key not in parent and not (add and final)):
            raise ValueError("Material field is absent.")
        if final:
            return parent, key
        parent = parent[key]


def json_equal(actual, wanted):
    if type(actual) in (int, float) and type(wanted) in (int, float):
        return Decimal(str(actual)) == Decimal(str(wanted))
    if type(actual) is not type(wanted):
        return False
    if type(actual) is dict:
        return actual.keys() == wanted.keys() and all(json_equal(actual[k], wanted[k]) for k in actual)
    if type(actual) is list:
        return len(actual) == len(wanted) and all(json_equal(a, b) for a, b in zip(actual, wanted))
    return actual == wanted


def record_ids(rows):
    if type(rows) is not list or any(type(row) is not dict or type(row.get("id")) not in (int, str) for row in rows):
        raise ValueError("Record IDs are required.")
    identities = {row["id"] for row in rows}
    if len(identities) != len(rows):
        raise ValueError("Record IDs must be unique.")
    return identities


def run(intake, params):
    selected = intake.get("checker_material_selection") or {}
    original = selected.get("record_values")
    operations = __OPERATIONS__
    if not operations or params or type(original) is not list or selected.get("record_exact_numbers"):
        raise ValueError("Select editable JSON records and specify ordered insert, remove or move operations.")
    rows = deepcopy(original)
    identities = record_ids(rows)
    replaced_root = False
    for action, value, positions in operations:
        if action == "test":
            try:
                container, key = location(rows, value, read_only=True)
            except ValueError:
                raise ValueError("Material test failed: missing or invalid pointer.") from None
            if not json_equal(container[key], positions):
                raise ValueError("Material test failed: value differs.")
            continue
        if action in ("copy", "move_field", "set"):
            if action in ("copy", "move_field"):
                if action == "move_field":
                    if "" in (value, positions):
                        if value == positions:
                            continue
                        raise ValueError("Cannot move or replace the material selection root.")
                    if positions.startswith(value + "/"):
                        raise ValueError("Cannot move a material field into its descendant.")
                source, key = location(rows, value, protect_records=not replaced_root)
                if action == "move_field" and value == positions:
                    continue
                # The destination index is evaluated after removing the source.
                item = source.pop(key) if action == "move_field" else deepcopy(source[key])
                parent, dest = location(rows, positions, add=True, protect_records=not replaced_root)
                if type(parent) is list:
                    parent.insert(dest, item)
                else:
                    parent[dest] = item
            elif value == "":
                rows = deepcopy(positions)
                replaced_root = True
            else:
                target, key = location(rows, value, protect_records=not replaced_root)
                target[key] = positions
            # Repeated nested copies may grow exponentially even with a short edit list.
            if len(json.dumps(rows, separators=(",", ":")).encode()) > 1048576:
                raise ValueError("Edited material exceeds the sandbox result limit.")
            continue
        index = positions[0]
        if replaced_root:
            identities = record_ids(rows)
        if action == "insert":
            if index > len(rows) or value["id"] in identities:
                raise ValueError("Stale insertion index or duplicate record ID.")
            rows.insert(index, value)
            identities.add(value["id"])
        else:
            if index >= len(rows) or rows[index]["id"] != value:
                raise ValueError("Stale source index for the requested record.")
            removed = rows.pop(index)
            if action == "move":
                destination = positions[1]
                if destination > len(rows):
                    raise ValueError("Stale destination index after removal.")
                rows.insert(destination, removed)
            else:
                identities.remove(value)
    return {"material_id": selected["material_id"], "source_sha256": selected["sha256"],
            "records_pointer": selected["records_pointer"], "records": rows}, None
"""
