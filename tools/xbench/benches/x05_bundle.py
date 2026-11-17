"""Pure X05 evidence inventory checks. No archive extraction, disk or transport.

Caller-supplied SHA-256 pins establish byte equality only. Even a clean audit
does not authenticate an origin, establish oracle independence or grade X05.
Names use slash normalization solely for comparison, never as filesystem paths.
"""

from collections import Counter
from hashlib import sha256
import json
import math
import re

MAX_MEMBERS = 128
MAX_MEMBER_BYTES = 1_048_576
MAX_TOTAL_BYTES = 8_388_608
MAX_DIAGNOSTICS = 32
MAX_JSON_CHARS = 262_144
MAX_JSON_DEPTH = 16
_MEMBER_FIELDS = {"name", "fixture", "stage", "byte_length", "sha256", "design_version"}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9_./\\-]{1,160}\Z")


def _token(value):
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _name(value):
    if type(value) is not str or not _NAME.fullmatch(value):
        return None
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or any(p in ("", ".", "..") for p in normalized.split("/")):
        return None
    return normalized


def _result(errors, *, previous=(), **details):
    counts = Counter(e["code"] for e in errors)
    examples, total = [], len(errors)
    for result in previous:
        counts.update(result["diagnostic_counts"])
        total += result["diagnostic_total"]
        examples.extend(result["diagnostics"])
    examples.extend(errors)
    examples.sort(key=lambda e: tuple((key, e[key]) for key in sorted(e)))
    return {"ok": total == 0, "diagnostic_total": total,
            "diagnostic_counts": dict(sorted(counts.items())),
            "diagnostics": examples[:MAX_DIAGNOSTICS], "integrity_verified": False,
            "origin_authenticated": False, **details}


def _error(errors, code, **indices):
    errors.append({"code": code, **indices})


def _sort_value(value):
    if type(value) is str:
        return ("str", (_name(value) or value)[:161])
    if type(value) is int:
        return ("int", max(-1, min(value, MAX_TOTAL_BYTES + 1)))
    return (type(value).__name__, "")


def _ordered_rows(rows):
    """Canonical diagnostic ranks, with original positions kept separately."""
    def key(pair):
        row = pair[1]
        if type(row) is not dict:
            return (_sort_value(row),)
        return tuple(_sort_value(row.get(field)) for field in
                     ("name", "fixture", "stage", "design_version", "sha256", "byte_length"))
    return [(rank, original, row) for rank, (original, row) in
            enumerate(sorted(enumerate(rows), key=key))]


def validate_member_names(names):
    """Validate bounded literal names and reject slash-normalized duplicates."""
    errors, seen = [], set()
    if type(names) is not list or len(names) > MAX_MEMBERS:
        return _result([{"code": "MEMBER_NAMES_UNREADABLE"}])
    for index, value in enumerate(sorted(names, key=_sort_value)):
        name = _name(value)
        if name is None:
            _error(errors, "MEMBER_NAME_INVALID", member_index=index)
        elif name in seen:
            _error(errors, "MEMBER_NAME_DUPLICATE", member_index=index)
        else:
            seen.add(name)
    return _result(errors)


def check_resource_budget(members, *, max_members=MAX_MEMBERS,
                          max_member_bytes=MAX_MEMBER_BYTES, max_total_bytes=MAX_TOTAL_BYTES):
    """Check metadata only, before any supplied payload lookup or hashing."""
    errors, total = [], 0
    for value, ceiling in ((max_members, MAX_MEMBERS), (max_member_bytes, MAX_MEMBER_BYTES),
                           (max_total_bytes, MAX_TOTAL_BYTES)):
        if type(value) is not int or not 0 <= value <= ceiling:
            return _result([{"code": "BUDGET_LIMIT_INVALID"}])
    if type(members) is not list:
        return _result([{"code": "MEMBERS_UNREADABLE"}])
    if len(members) > max_members:
        return _result([{"code": "MEMBER_COUNT_EXCEEDED"}])
    for index, _, member in _ordered_rows(members):
        size = member.get("byte_length") if type(member) is dict else None
        if type(size) is not int or size < 0:
            _error(errors, "MEMBER_SIZE_INVALID", member_index=index)
        else:
            total = min(max_total_bytes + 1, total + min(size, max_total_bytes + 1))
            if size > max_member_bytes:
                _error(errors, "MEMBER_BYTES_EXCEEDED", member_index=index)
    if total > max_total_bytes:
        _error(errors, "TOTAL_BYTES_EXCEEDED")
    return _result(errors, member_count=len(members), declared_bytes=total)


def _contract(expected_fixture_ids, required_stages):
    for items in (expected_fixture_ids, required_stages):
        if type(items) is not list or not 1 <= len(items) <= 16:
            return False
        if any(not _token(item) for item in items) or len(set(items)) != len(items):
            return False
    return len(expected_fixture_ids) * len(required_stages) <= MAX_MEMBERS


def validate_inventory(members, *, expected_fixture_ids, required_stages):
    """Require one explicitly pinned member per expected fixture and stage.

    Each member contains name, fixture, stage, byte_length, sha256 and
    design_version. Callers supply the expected fixture/stage sets explicitly.
    """
    if not _contract(expected_fixture_ids, required_stages):
        return _result([{"code": "EXPECTED_SET_INVALID"}])
    budget = check_resource_budget(members)
    errors = []
    if not budget["ok"]:
        return budget
    names = validate_member_names([m.get("name") if type(m) is dict else None for m in members])
    covered = set()
    for index, _, member in _ordered_rows(members):
        if type(member) is not dict:
            _error(errors, "MEMBER_METADATA_INVALID", member_index=index)
            continue
        fixture, stage = member.get("fixture"), member.get("stage")
        if not _token(fixture) or fixture not in expected_fixture_ids:
            _error(errors, "MEMBER_FIXTURE_INVALID", member_index=index)
        if not _token(stage) or stage not in required_stages:
            _error(errors, "MEMBER_STAGE_INVALID", member_index=index)
        if not _token(member.get("design_version")):
            _error(errors, "MEMBER_VERSION_INVALID", member_index=index)
        if type(member.get("sha256")) is not str or not _HASH.fullmatch(member["sha256"]):
            _error(errors, "MEMBER_SHA256_INVALID", member_index=index)
        if _token(fixture) and _token(stage) and fixture in expected_fixture_ids and stage in required_stages:
            key = (fixture, stage)
            if key in covered:
                _error(errors, "FIXTURE_STAGE_DUPLICATE", member_index=index)
            covered.add(key)
    for fixture_index, fixture in enumerate(expected_fixture_ids):
        for stage_index, stage in enumerate(required_stages):
            if (fixture, stage) not in covered:
                _error(errors, "FIXTURE_STAGE_MISSING", fixture_index=fixture_index, stage_index=stage_index)
    return _result(errors, previous=(names,), member_count=len(members))


def audit_bundle_bytes(members, payloads, *, expected_fixture_ids, required_stages):
    """Audit explicitly supplied bytes; preserve readable siblings on failures."""
    # Never touch payloads if the declared resource budget is invalid.
    budget = check_resource_budget(members)
    if not budget["ok"]:
        return budget
    inventory = validate_inventory(members, expected_fixture_ids=expected_fixture_ids, required_stages=required_stages)
    if not inventory["ok"]:
        return {**inventory, "members": [{"member_index": i, "bytes_equal": None} for i in range(len(members))]}
    errors = []
    if type(payloads) is not dict or len(payloads) > MAX_MEMBERS or any(type(k) is not str for k in payloads):
        return _result([{"code": "PAYLOADS_UNREADABLE"}], previous=(inventory,))
    names = validate_member_names(list(payloads))
    grouped = {}
    for raw, value in payloads.items():
        name = _name(raw)
        if name is not None:
            grouped.setdefault(name, []).append(value)
    expected_names = {_name(m.get("name")) for m in members if type(m) is dict}
    for payload_index, name in enumerate(sorted(grouped)):
        if name not in expected_names:
            _error(errors, "PAYLOAD_FOREIGN", payload_index=payload_index)
    actual_total, results = 0, []
    for index, original_index, member in _ordered_rows(members):
        name = _name(member.get("name")) if type(member) is dict else None
        found = grouped.get(name, [])
        codes = []
        if name is None:
            codes.append("MEMBER_NAME_INVALID")
        elif not found:
            codes.append("PAYLOAD_MISSING")
        elif len(found) != 1:
            codes.append("PAYLOAD_AMBIGUOUS")
        else:
            value = found[0]
            if type(value) is not bytes:
                codes.append("PAYLOAD_TYPE_INVALID")
            elif len(value) > MAX_MEMBER_BYTES:
                codes.append("PAYLOAD_BYTES_EXCEEDED")
            else:
                actual_total += len(value)
                if actual_total > MAX_TOTAL_BYTES:
                    codes.append("PAYLOAD_TOTAL_EXCEEDED")
                else:
                    if len(value) != member.get("byte_length"):
                        codes.append("PAYLOAD_LENGTH_MISMATCH")
                    if sha256(value).hexdigest() != member.get("sha256"):
                        codes.append("PAYLOAD_SHA256_MISMATCH")
        for code in codes:
            _error(errors, code, member_index=index)
        results.append({"member_index": original_index, "bytes_equal": not codes})
    results.sort(key=lambda m: m["member_index"])
    result = _result(errors, previous=(inventory, names), members=results)
    result["integrity_verified"] = result["ok"]
    return result


def check_stage_references(members, references, *, expected_fixture_versions, required_stages):
    """Bind stage references to explicit fixture/version ownership, never totals."""
    if (type(expected_fixture_versions) is not dict or not 1 <= len(expected_fixture_versions) <= 16
            or any(not _token(v) for v in expected_fixture_versions.values())):
        return _result([{"code": "EXPECTED_VERSIONS_INVALID"}])
    fixtures = list(expected_fixture_versions)
    inventory = validate_inventory(members, expected_fixture_ids=fixtures, required_stages=required_stages)
    if not _contract(fixtures, required_stages) or type(members) is not list or len(members) > MAX_MEMBERS:
        return inventory
    errors = []
    if type(references) is not list or len(references) > MAX_MEMBERS:
        return _result([{"code": "REFERENCES_UNREADABLE"}], previous=(inventory,))
    lookup = {}
    for index, _, member in _ordered_rows(members):
        if type(member) is not dict:
            continue
        name = _name(member.get("name"))
        if name is not None:
            lookup.setdefault(name, []).append(member)
        fixture = member.get("fixture")
        if _token(fixture) and fixture in expected_fixture_versions and member.get("design_version") != expected_fixture_versions[fixture]:
            _error(errors, "MEMBER_DESIGN_VERSION_CONFLICT", member_index=index)
    covered = set()
    for index, _, ref in _ordered_rows(references):
        if type(ref) is not dict:
            _error(errors, "REFERENCE_INVALID", reference_index=index)
            continue
        fixture, stage, name = ref.get("fixture"), ref.get("stage"), _name(ref.get("name"))
        if not _token(fixture) or fixture not in fixtures or not _token(stage) or stage not in required_stages or name is None:
            _error(errors, "REFERENCE_INVALID", reference_index=index)
            continue
        key = (fixture, stage)
        if key in covered:
            _error(errors, "REFERENCE_STAGE_DUPLICATE", reference_index=index)
        covered.add(key)
        if ref.get("design_version") != expected_fixture_versions[fixture]:
            _error(errors, "REFERENCE_DESIGN_VERSION_CONFLICT", reference_index=index)
        targets = lookup.get(name, [])
        if not targets:
            _error(errors, "REFERENCE_DANGLING", reference_index=index)
        elif len(targets) != 1:
            _error(errors, "REFERENCE_AMBIGUOUS", reference_index=index)
        else:
            member = targets[0]
            if member.get("fixture") != fixture:
                _error(errors, "REFERENCE_CROSS_FIXTURE", reference_index=index)
            if member.get("stage") != stage:
                _error(errors, "REFERENCE_STAGE_CONFLICT", reference_index=index)
            if member.get("design_version") != ref.get("design_version"):
                _error(errors, "REFERENCE_MEMBER_VERSION_CONFLICT", reference_index=index)
    for fixture_index, fixture in enumerate(fixtures):
        for stage_index, stage in enumerate(required_stages):
            if (fixture, stage) not in covered:
                _error(errors, "REFERENCE_STAGE_MISSING", fixture_index=fixture_index, stage_index=stage_index)
    return _result(errors, previous=(inventory,))


class _DecodeError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DecodeError("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _nonfinite(_):
    raise _DecodeError("JSON_NONFINITE_NUMBER")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise _DecodeError("JSON_NONFINITE_NUMBER")
    return value


def decode_inventory_json(text):
    """Decode one bounded JSON inventory envelope, never accessing payloads.

    The envelope has exactly members, expected_fixture_ids and required_stages.
    Valid data is returned as inventory; rejected input is never echoed.
    """
    def rejected(code):
        return _result([{"code": code}], inventory=None)
    if type(text) is not str or len(text) > MAX_JSON_CHARS:
        return rejected("JSON_INPUT_UNREADABLE")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return rejected("JSON_DEPTH_EXCEEDED")
        elif char in "]}":
            depth -= 1
            if depth < 0:
                return rejected("JSON_INVALID")
    try:
        data = json.loads(text, object_pairs_hook=_unique_object,
                          parse_constant=_nonfinite, parse_float=_finite_float)
    except _DecodeError as error:
        return rejected(str(error))
    except (ValueError, RecursionError):
        return rejected("JSON_INVALID")
    if type(data) is not dict or set(data) != {"members", "expected_fixture_ids", "required_stages"}:
        return rejected("INVENTORY_FIELDS_INVALID")
    members = data["members"]
    if type(members) is not list or len(members) > MAX_MEMBERS:
        return rejected("INVENTORY_MEMBERS_INVALID")
    errors = [{"code": "MEMBER_FIELDS_INVALID", "member_index": index}
              for index, _, row in _ordered_rows(members)
              if type(row) is not dict or set(row) != _MEMBER_FIELDS]
    if errors:
        return _result(errors, inventory=None)
    result = validate_inventory(members, expected_fixture_ids=data["expected_fixture_ids"],
                                required_stages=data["required_stages"])
    return {**result, "inventory": data if result["ok"] else None}


def audit_inventory_json(text, payloads):
    """Strict decoding and full metadata validation precede every byte hash."""
    decoded = decode_inventory_json(text)
    if not decoded["ok"]:
        return decoded
    return audit_bundle_bytes(payloads=payloads, **decoded["inventory"])
