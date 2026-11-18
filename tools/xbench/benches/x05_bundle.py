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

if __package__:
    from . import x05_reconcile as reconciliation
else:
    # Direct script execution with -P must not depend on the caller's cwd or
    # mutate its import path. Load only this trusted peer source module.
    import importlib.util
    from pathlib import Path
    _spec = importlib.util.spec_from_file_location("x05_bundle_reconciliation", Path(__file__).with_name("x05_reconcile.py"))
    reconciliation = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(reconciliation)

MAX_MEMBERS = 128
MAX_MEMBER_BYTES = 1_048_576
MAX_TOTAL_BYTES = 8_388_608
MAX_DIAGNOSTICS = 32
DIGEST_CHUNK_BYTES = 8192
MAX_JSON_CHARS = 262_144
MAX_JSON_DEPTH = 16
MAX_REPLAY_JSON_CHARS = 2 * MAX_TOTAL_BYTES + MAX_JSON_CHARS
MAX_STDIN_BYTES = MAX_REPLAY_JSON_CHARS
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
                    try:
                        digest = sha256(b"")
                        for offset in range(0, len(value), DIGEST_CHUNK_BYTES):
                            digest.update(value[offset:offset + DIGEST_CHUNK_BYTES])
                        actual_digest = digest.hexdigest()
                    except Exception:
                        # Provider failures are uncertainty, never byte equality.
                        # Exception text may contain supplied/private material.
                        codes.append("PAYLOAD_DIGEST_UNREADABLE")
                    else:
                        if actual_digest != member.get("sha256"):
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


def _decode_json(text, max_chars):
    if type(text) is not str or len(text) > max_chars:
        return None, "JSON_INPUT_UNREADABLE"
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
                return None, "JSON_DEPTH_EXCEEDED"
        elif char in "]}":
            depth -= 1
            if depth < 0:
                return None, "JSON_INVALID"
    try:
        data = json.loads(text, object_pairs_hook=_unique_object,
                          parse_constant=_nonfinite, parse_float=_finite_float)
    except _DecodeError as error:
        return None, str(error)
    except (ValueError, RecursionError):
        return None, "JSON_INVALID"
    return data, None


def _inventory_envelope(data):
    def rejected(code):
        return _result([{"code": code}], inventory=None)
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


def decode_inventory_json(text):
    """Strict bounded inventory decoding; rejected data is never echoed."""
    data, error = _decode_json(text, MAX_JSON_CHARS)
    if error:
        return _result([{"code": error}], inventory=None)
    return _inventory_envelope(data)


def diff_inventories(before, after):
    """Compare explicit inventories, never payloads or evidence origins.

    Changes are facts: one member can be both repinned and reassigned. Paths
    use the auditor's slash normalization. Examples share one bounded cap.
    """
    checked = [_inventory_envelope(value) for value in (before, after)]
    if any(not result['ok'] for result in checked):
        previous = [{**result, 'diagnostics': [dict(row, side=side) for row in result['diagnostics']]}
                    for side, result in zip(('before', 'after'), checked)]
        return _result([], previous=previous, inventory_equal=None,
                       difference_total=None, difference_counts=None, changed_member_total=None, differences=[])
    left, right = [{_name(row['name']): row for row in value['members']} for value in (before, after)]
    differences = []
    for name in sorted(left.keys() | right.keys()):
        if name not in left:
            differences.append(dict(name=name, kind='added'))
        elif name not in right:
            differences.append(dict(name=name, kind='removed'))
        else:
            if any(left[name][key] != right[name][key] for key in ('byte_length', 'sha256')):
                differences.append(dict(name=name, kind='repinned'))
            if any(left[name][key] != right[name][key] for key in ('fixture', 'stage', 'design_version')):
                differences.append(dict(name=name, kind='reassigned'))
    differences.sort(key=lambda row: (row['name'], row['kind']))
    counts = Counter(row['kind'] for row in differences)
    return _result([], inventory_equal=not differences, difference_total=len(differences),
                   difference_counts={kind: counts[kind] for kind in ('added', 'removed', 'repinned', 'reassigned')},
                   changed_member_total=len({row['name'] for row in differences}), differences=differences[:MAX_DIAGNOSTICS])


def _diff_unknown(code):
    return _result([dict(code=code)], verdict='UNREADABLE', inventory_equal=None,
                   difference_total=None, difference_counts=None, changed_member_total=None, differences=[])


def diff_inventory_json(text):
    """Strict stdin envelope containing explicit before and after inventories."""
    data, error = _decode_json(text, 2 * MAX_JSON_CHARS)
    if error:
        return _diff_unknown(error)
    if type(data) is not dict or set(data) != {'before', 'after'}:
        return _diff_unknown('INVENTORY_DIFF_FIELDS_INVALID')
    result = diff_inventories(data['before'], data['after'])
    if not result['ok']:
        verdict = 'UNREADABLE'
    elif result['inventory_equal']:
        verdict = 'PASS'
    else:
        verdict = 'FAIL'
    return dict(result, verdict=verdict)


def audit_inventory_json(text, payloads):
    """Strict decoding and full metadata validation precede every byte hash."""
    decoded = decode_inventory_json(text)
    if not decoded["ok"]:
        return decoded
    return audit_bundle_bytes(payloads=payloads, **decoded["inventory"])


def _semantic_unknown(reason):
    return {"verdict": "UNREADABLE", "reason": reason}


def _adapter_result(integrity, bindings, fixtures):
    verdicts = {row["verdict"] for row in fixtures.values()}
    verdict = "UNREADABLE" if not verdicts or "UNREADABLE" in verdicts else "FAIL" if "FAIL" in verdicts else "PASS"
    return {"integrity": {k: v for k, v in integrity.items() if k not in {"members", "inventory", "envelope"}},
            "bindings": bindings, "semantic": {"verdict": verdict, "fixtures": fixtures},
            "origin_authenticated": False}


def _evidence_binding_problem(record, fixture, version):
    if type(record) is not dict or set(record) != {"envelope", "manifest", "import_binding", "grid_binding"}:
        return "bundle_evidence_binding_unreadable"
    envelope = record["envelope"]
    assurance = envelope.get("assurance") if type(envelope) is dict else None
    if type(assurance) is not dict:
        return "bundle_evidence_binding_unreadable"
    evidence, stages = assurance.get("evidence"), assurance.get("bindings")
    identities, identity = assurance.get("stage_identities"), assurance.get("identity")
    if (type(evidence) is not dict or type(stages) is not dict or set(stages) != set(reconciliation.PROVENANCE_STAGES)
            or type(identities) is not dict or set(identities) != set(reconciliation.IDENTITY_STAGES)):
        return "bundle_evidence_binding_unreadable"
    claims = [record[k] for k in ("manifest", "import_binding", "grid_binding")]
    claims += [assurance.get("manifest"), evidence] + list(stages.values())
    versions = [identity] + list(identities.values())
    if any(type(c) is not dict or "fixture_id" not in c for c in claims) or any(type(c) is not dict or "version_id" not in c for c in versions):
        return "bundle_evidence_binding_unreadable"
    if any(c["fixture_id"] != fixture for c in claims):
        return "bundle_evidence_fixture_conflict"
    if any(c["version_id"] != version for c in versions):
        return "bundle_evidence_design_version_conflict"
    return None


def reconcile_bundle(members, payloads, references, *, expected_fixture_versions, required_stages, evidence_stage):
    """Audit first, then reconcile only individually intact and bound fixtures.

    Offline semantic UNREADABLE precedence is separate from byte integrity and
    the live grader. Neither caller-supplied bindings nor hashes authenticate.
    """
    if (type(expected_fixture_versions) is not dict or not 1 <= len(expected_fixture_versions) <= 16
            or any(not _token(v) for v in expected_fixture_versions.values())):
        invalid = _result([{"code": "EXPECTED_VERSIONS_INVALID"}])
        return _adapter_result(invalid, invalid, {})
    fixtures = list(expected_fixture_versions)
    if not _contract(fixtures, required_stages) or not _token(evidence_stage) or evidence_stage not in required_stages:
        invalid = _result([{"code": "REPLAY_CONTRACT_INVALID"}])
        return _adapter_result(invalid, invalid, {})
    fixtures, stages = sorted(fixtures), sorted(required_stages)
    integrity = audit_bundle_bytes(members, payloads, expected_fixture_ids=fixtures, required_stages=stages)
    bindings = check_stage_references(members, references,
        expected_fixture_versions=expected_fixture_versions, required_stages=stages)
    inventory = validate_inventory(members, expected_fixture_ids=fixtures, required_stages=stages)
    shared_bytes_problem = any(code in integrity["diagnostic_counts"] for code in
        ("PAYLOADS_UNREADABLE", "MEMBER_NAME_INVALID", "MEMBER_NAME_DUPLICATE", "PAYLOAD_FOREIGN"))
    shared_refs_problem = (type(references) is not list or len(references) > MAX_MEMBERS or any(
        type(ref) is not dict or not _token(ref.get("fixture")) or ref["fixture"] not in fixtures for ref in references))
    normalized = {_name(k): v for k, v in payloads.items()} if type(payloads) is dict and len(payloads) <= MAX_MEMBERS else {}
    checked = {row["member_index"]: row["bytes_equal"] for row in integrity.get("members", [])}
    results = {}
    for fixture in fixtures:
        local_members = [m for m in members if type(m) is dict and m.get("fixture") == fixture] if inventory["ok"] else []
        local_refs = [r for r in references if r["fixture"] == fixture] if not shared_refs_problem else []
        local_binding = check_stage_references(local_members, local_refs,
            expected_fixture_versions={fixture: expected_fixture_versions[fixture]}, required_stages=stages)
        positions = [i for i, m in enumerate(members) if type(m) is dict and m.get("fixture") == fixture] if inventory["ok"] else []
        intact = inventory["ok"] and bool(positions) and all(checked.get(i) is True for i in positions)
        if not intact or shared_bytes_problem:
            semantic = _semantic_unknown("bundle_bytes_unreadable")
        elif shared_refs_problem or not local_binding["ok"]:
            semantic = _semantic_unknown("bundle_stage_references_unreadable")
        else:
            ref = next(r for r in local_refs if r["stage"] == evidence_stage)
            raw = normalized[_name(ref["name"])]
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                semantic = _semantic_unknown("bundle_evidence_utf8_unreadable")
            else:
                decoded = reconciliation.decode_evidence_json(text)
                if decoded["verdict"] != "PASS":
                    semantic = decoded
                else:
                    record = decoded["evidence"]
                    problem = _evidence_binding_problem(record, fixture, expected_fixture_versions[fixture])
                    semantic = _semantic_unknown(problem) if problem else reconciliation.reconcile_bound_seven_steps(
                        record["envelope"], record["manifest"], record["import_binding"], record["grid_binding"])
        results[fixture] = {"integrity_verified": intact, "binding": local_binding,
                            "verdict": semantic["verdict"], "result": semantic}
    return _adapter_result(integrity, bindings, results)


def _member_bytes_from_hex(text):
    return bytes.fromhex(text)


def decode_replay_json(text, *, max_member_bytes=MAX_MEMBER_BYTES, max_total_bytes=MAX_TOTAL_BYTES):
    """Bound encoded text and every decoded length before allocating any bytes."""
    def rejected(code):
        return _result([{"code": code}], envelope=None)
    if (type(max_member_bytes) is not int or not 0 <= max_member_bytes <= MAX_MEMBER_BYTES
            or type(max_total_bytes) is not int or not 0 <= max_total_bytes <= MAX_TOTAL_BYTES):
        return rejected("BUDGET_LIMIT_INVALID")
    data, error = _decode_json(text, min(MAX_REPLAY_JSON_CHARS, 2 * max_total_bytes + MAX_JSON_CHARS))
    if error:
        return rejected(error)
    fields = {"inventory", "references", "expected_fixture_versions", "evidence_stage", "member_bytes_hex"}
    if type(data) is not dict or set(data) != fields:
        return rejected("REPLAY_FIELDS_INVALID")
    decoded = _inventory_envelope(data["inventory"])
    if not decoded["ok"]:
        return {k: v for k, v in {**decoded, "envelope": None}.items() if k != "inventory"}
    inventory = decoded["inventory"]
    budget = check_resource_budget(inventory["members"], max_member_bytes=max_member_bytes, max_total_bytes=max_total_bytes)
    if not budget["ok"]:
        return {**budget, "envelope": None}
    versions, refs = data["expected_fixture_versions"], data["references"]
    if type(versions) is not dict or set(versions) != set(inventory["expected_fixture_ids"]):
        return rejected("REPLAY_FIXTURES_INVALID")
    bindings = check_stage_references(inventory["members"], refs,
        expected_fixture_versions=versions, required_stages=inventory["required_stages"])
    if not bindings["ok"]:
        return {**bindings, "envelope": None}
    if not _token(data["evidence_stage"]) or data["evidence_stage"] not in inventory["required_stages"]:
        return rejected("REPLAY_STAGE_INVALID")
    encoded = data["member_bytes_hex"]
    if type(encoded) is not dict or len(encoded) > MAX_MEMBERS:
        return rejected("MEMBER_HEX_UNREADABLE")
    names = validate_member_names(list(encoded))
    if not names["ok"]:
        return {**names, "envelope": None}
    errors, total = [], 0
    for index, key in enumerate(sorted(encoded)):
        value = encoded[key]
        if type(value) is not str or len(value) % 2:
            _error(errors, "MEMBER_HEX_INVALID", member_index=index)
            continue
        size = len(value) // 2
        total += size
        if size > max_member_bytes:
            _error(errors, "MEMBER_HEX_BYTES_EXCEEDED", member_index=index)
        elif re.fullmatch(r"[0-9a-fA-F]*", value) is None:
            _error(errors, "MEMBER_HEX_INVALID", member_index=index)
    if total > max_total_bytes:
        _error(errors, "MEMBER_HEX_TOTAL_EXCEEDED")
    if errors:
        return _result(errors, envelope=None)
    payloads = {key: _member_bytes_from_hex(encoded[key]) for key in sorted(encoded)}
    return _result([], envelope=dict(members=inventory["members"], payloads=payloads, references=refs,
        expected_fixture_versions=versions, required_stages=inventory["required_stages"], evidence_stage=data["evidence_stage"]))


def replay_bundle_json(text):
    decoded = decode_replay_json(text)
    if not decoded["ok"]:
        return _adapter_result(decoded, _result([{"code": "REPLAY_NOT_DECODED"}]), {})
    return reconcile_bundle(**decoded["envelope"])


class _WireLimit(ValueError):
    pass


def _wire_size(value, limit):
    """Exact compact ASCII JSON size, treating bytes as unallocated hex text."""
    used = 0
    def add(count):
        nonlocal used
        used += count
        if used > limit:
            raise _WireLimit()
    def visit(item):
        if type(item) is bytes:
            add(2 + 2 * len(item))
        elif type(item) is str:
            add(2)
            for char in item:
                ordinal = ord(char)
                add(2 if char in '\\"\b\f\n\r\t' else 6 if ordinal < 32 or 127 <= ordinal <= 65535
                    else 12 if ordinal > 65535 else 1)
        elif type(item) is int:
            add(len(str(item)))
        elif type(item) is list:
            add(2 + max(0, len(item) - 1))
            for child in item: visit(child)
        elif type(item) is dict:
            add(2 + max(0, len(item) - 1))
            for key, child in item.items():
                visit(key); add(1); visit(child)
        else:
            raise ValueError("wire_type_invalid")
    visit(value)
    return used


def _encode_member_hex(data):
    return data.hex()


def _dump_replay(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def encode_replay_json(inventory, references, member_bytes, *, expected_fixture_versions,
                       evidence_stage, max_wire_bytes=MAX_STDIN_BYTES):
    """Encode explicit supplied pins unchanged, including integrity conflicts.

    No hashing or semantic evaluation occurs. Both byte budgets and exact hex/
    JSON escape expansion are checked before allocating output strings.
    """
    def rejected(code):
        return _result([{"code": code}], text=None)
    if type(max_wire_bytes) is not int or not 0 <= max_wire_bytes <= min(MAX_STDIN_BYTES, MAX_REPLAY_JSON_CHARS):
        return rejected("ENCODER_WIRE_LIMIT_INVALID")
    decoded = _inventory_envelope(inventory)
    if not decoded["ok"]:
        return {k: v for k, v in {**decoded, "text": None}.items() if k != "inventory"}
    if type(expected_fixture_versions) is not dict or set(expected_fixture_versions) != set(inventory["expected_fixture_ids"]):
        return rejected("REPLAY_FIXTURES_INVALID")
    bindings = check_stage_references(inventory["members"], references,
        expected_fixture_versions=expected_fixture_versions, required_stages=inventory["required_stages"])
    if not bindings["ok"]:
        return {**bindings, "text": None}
    if any(set(ref) != {"name", "fixture", "stage", "design_version"} for ref in references):
        return rejected("ENCODER_REFERENCE_FIELDS_INVALID")
    if not _token(evidence_stage) or evidence_stage not in inventory["required_stages"]:
        return rejected("REPLAY_STAGE_INVALID")
    if type(member_bytes) is not dict or len(member_bytes) > MAX_MEMBERS:
        return rejected("ENCODER_BYTES_INVALID")
    names = validate_member_names(list(member_bytes))
    if not names["ok"]:
        return {**names, "text": None}
    expected = {_name(m["name"]) for m in inventory["members"]}
    if {_name(name) for name in member_bytes} != expected:
        return rejected("ENCODER_MEMBER_COVERAGE_INVALID")
    errors, total = [], 0
    for index, name in enumerate(sorted(member_bytes)):
        raw = member_bytes[name]
        if type(raw) is not bytes:
            _error(errors, "PAYLOAD_TYPE_INVALID", member_index=index)
            continue
        total += len(raw)
        if len(raw) > MAX_MEMBER_BYTES:
            _error(errors, "PAYLOAD_BYTES_EXCEEDED", member_index=index)
    if total > MAX_TOTAL_BYTES:
        _error(errors, "PAYLOAD_TOTAL_EXCEEDED")
    if errors:
        return _result(errors, text=None)
    canonical = dict(inventory=dict(members=[row for _, _, row in _ordered_rows(inventory["members"])],
        expected_fixture_ids=sorted(inventory["expected_fixture_ids"]), required_stages=sorted(inventory["required_stages"])),
        references=[row for _, _, row in _ordered_rows(references)], expected_fixture_versions=expected_fixture_versions,
        evidence_stage=evidence_stage, member_bytes_hex=member_bytes)
    try:
        wire_bytes = _wire_size(canonical, max_wire_bytes)
    except _WireLimit:
        return _result([{"code": "ENCODER_WIRE_BUDGET_EXCEEDED"}], text=None,
                       wire_bytes_lower_bound=max_wire_bytes + 1)
    except (ValueError, RecursionError):
        return rejected("ENCODER_WIRE_UNREADABLE")
    canonical["member_bytes_hex"] = {name: _encode_member_hex(member_bytes[name]) for name in sorted(member_bytes)}
    text = _dump_replay(canonical)
    return _result([], text=text, wire_bytes=wire_bytes)


def _cli_unknown(code):
    return _adapter_result(_result([{"code": code}]), _result([{"code": "REPLAY_NOT_DECODED"}]), {})


def _read_stdin(stream):
    data, size = bytearray(), 0
    while size <= MAX_STDIN_BYTES:
        requested = min(8192, MAX_STDIN_BYTES + 1 - size)
        chunk = stream.read(requested)
        if type(chunk) is not bytes or len(chunk) > requested:
            raise ValueError("stdin_unreadable")
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        size += len(chunk)
    return None


def _write_all(stream, payload):
    offset = 0
    while offset < len(payload):
        piece = payload[offset:offset + 8192]
        count = stream.write(piece)
        if type(count) is not int or not 0 < count <= len(piece):
            raise ValueError("stdout_unreadable")
        offset += count
    stream.flush()


def _mute_failed_stdout():
    # Prevent interpreter shutdown from retrying a real broken stdout pipe.
    import os
    import sys
    try:
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
    except Exception:
        pass


def main(argv=None):
    """Stdin-only CLI. Exits:0 PASS,1 FAIL,2 usage,4 UNREADABLE,5 output failure."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="x05_bundle", description="Offline audited X05 bundle replay from stdin; no origin authentication.",
        epilog="Exits: 0 PASS; 1 FAIL; 2 usage; 4 UNREADABLE; 5 output failure.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--replay", action="store_true", help="audit and reconcile one stdin envelope (default)")
    modes.add_argument("--inventory-diff", action="store_true", help="compare explicit before/after inventories from stdin")
    try:
        options = parser.parse_args(argv)
    except (OSError, UnicodeError, ValueError):
        _mute_failed_stdout()
        return 5
    unknown = _diff_unknown if options.inventory_diff else _cli_unknown
    decode = diff_inventory_json if options.inventory_diff else replay_bundle_json
    try:
        raw = _read_stdin(sys.stdin.buffer)
        result = unknown("CLI_INPUT_TOO_LARGE") if raw is None else decode(raw.decode("utf-8", errors="strict"))
    except Exception:
        result = unknown("CLI_INPUT_UNREADABLE")
    try:
        output = reconciliation.serialize_result(result)
    except Exception:
        result = unknown("CLI_RESULT_UNREADABLE")
        output = json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False)
    try:
        binary = getattr(sys.stdout, "buffer", None)
        _write_all(sys.stdout, output + "\n") if binary is None else _write_all(binary, output.encode("ascii") + b"\n")
    except Exception:
        _mute_failed_stdout()
        return 5
    verdict = result['verdict'] if options.inventory_diff else result['semantic']['verdict']
    return {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[verdict]


if __name__ == "__main__":
    raise SystemExit(main())
