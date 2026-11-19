"""Author ordered edits to one verified array of identified JSON records."""
from decimal import Decimal
import json
import re

from authored_material_selection import bind_material_selection


_PREFIX = re.compile(r"(?:edit|patch|update) (?:the |my )?(?:selected |imported )?(?:json material|material records|json records):\s*", re.I)
_INDEX = re.compile(r"(?:0|[1-9][0-9]{0,8})\b")
_SEPARATOR = re.compile(r",\s*(?:then\s+)?|\s+then\s+", re.I)


def _parse(description):
    if not isinstance(description, str) or len(description) > 4000:
        return None
    prefix = _PREFIX.match(description)
    if not prefix:
        return None
    tail = description[prefix.end():].strip().removesuffix(".")
    ops = []
    while tail and len(ops) < 100:
        verb = re.match(r"(insert|remove|move)\s+", tail, re.I)
        if not verb:
            return None
        action = verb[1].lower()
        try:
            value, end = _DECODER.raw_decode(tail[verb.end():])
        except (ValueError, RecursionError, OverflowError):
            return None
        if action == "insert":
            if type(value) is not dict or type(value.get("id")) not in (str, int):
                return None
        elif type(value) not in (str, int):
            return None
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
    return _BODY.replace("__OPERATIONS__", repr(_parse(description)))


_BODY = """
def run(intake, params):
    selected = intake.get("checker_material_selection") or {}
    original = selected.get("record_values")
    operations = __OPERATIONS__
    if not operations or params or type(original) is not list or selected.get("record_exact_numbers"):
        raise ValueError("Select editable JSON records and specify ordered insert, remove or move operations.")
    rows = list(original)
    if any(type(row) is not dict or type(row.get("id")) not in (int, str) for row in rows):
        raise ValueError("Record IDs are required.")
    identities = {row["id"] for row in rows}
    if len(identities) != len(rows):
        raise ValueError("Record IDs must be unique.")
    for action, value, positions in operations:
        index = positions[0]
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
