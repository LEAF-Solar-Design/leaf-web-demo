"""Synthetic X05 bundle checks, no archive, network or customer fixtures."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from benches import x05_bundle as bundle

FIXTURES = ["alpha", "beta"]
STAGES = ["import", "grids", "strings", "grouping", "table", "deltas", "reopened"]
VERSIONS = {"alpha": "design-a", "beta": "design-b"}


def synthetic_bundle():
    members, payloads, references = [], {}, []
    for fixture in FIXTURES:
        for stage in STAGES:
            name = f"{fixture}/{stage}.json"
            payload = b'{"panels":1}'  # Equal totals and bytes never establish fixture ownership.
            payloads[name] = payload
            members.append(dict(name=name, fixture=fixture, stage=stage,
                                byte_length=len(payload), sha256=sha256(payload).hexdigest(),
                                design_version=VERSIONS[fixture]))
            references.append(dict(name=name, fixture=fixture, stage=stage, design_version=VERSIONS[fixture]))
    return members, payloads, references


def inventory(members):
    return bundle.validate_inventory(members, expected_fixture_ids=FIXTURES, required_stages=STAGES)


def audit(members, payloads):
    return bundle.audit_bundle_bytes(members, payloads, expected_fixture_ids=FIXTURES, required_stages=STAGES)


def refs(members, references):
    return bundle.check_stage_references(members, references, expected_fixture_versions=VERSIONS, required_stages=STAGES)


def codes(result):
    return result["diagnostic_counts"]


def test_complete_bundle_has_integrity_but_no_origin_authentication_or_grading():
    members, payloads, references = synthetic_bundle()
    before = deepcopy((members, payloads, references, FIXTURES, STAGES, VERSIONS))
    assert inventory(members)["ok"] and refs(members, references)["ok"]
    assert inventory(members)["integrity_verified"] is False
    result = audit(members, payloads)
    assert result["ok"] and result["integrity_verified"]
    assert result["origin_authenticated"] is False
    assert "verdict" not in result
    assert all(m["bytes_equal"] for m in result["members"])
    assert (members, payloads, references, FIXTURES, STAGES, VERSIONS) == before


@pytest.mark.parametrize("field,value,code", [
    ("fixture", None, "MEMBER_FIXTURE_INVALID"), ("fixture", [], "MEMBER_FIXTURE_INVALID"),
    ("stage", "foreign", "MEMBER_STAGE_INVALID"), ("stage", {}, "MEMBER_STAGE_INVALID"),
    ("design_version", "", "MEMBER_VERSION_INVALID"),
    ("sha256", "F" * 64, "MEMBER_SHA256_INVALID"), ("sha256", "0" * 63, "MEMBER_SHA256_INVALID"),
    ("sha256", None, "MEMBER_SHA256_INVALID"), ("byte_length", True, "MEMBER_SIZE_INVALID"),
])
def test_explicit_member_metadata_is_required(field, value, code):
    members, _, _ = synthetic_bundle()
    members[0][field] = value
    result = inventory(members)
    assert not result["ok"] and code in codes(result)


def test_duplicate_names_duplicate_stages_and_missing_stages():
    members, _, _ = synthetic_bundle()
    duplicate = dict(members[0], name="alpha/other.json")
    assert "FIXTURE_STAGE_DUPLICATE" in codes(inventory(members + [duplicate]))
    assert "MEMBER_NAME_DUPLICATE" in codes(inventory(members + [dict(members[0])]))
    assert codes(inventory(members[1:]))["FIXTURE_STAGE_MISSING"] == 1


@pytest.mark.parametrize("fixtures,stages", [(None, STAGES), ([], STAGES), (["alpha", "alpha"], STAGES),
                                          (FIXTURES, []), (FIXTURES, ["../x"]), (FIXTURES, [True])])
def test_expected_sets_are_explicit_unique_and_bounded(fixtures, stages):
    assert codes(bundle.validate_inventory([], expected_fixture_ids=fixtures, required_stages=stages)) == {"EXPECTED_SET_INVALID": 1}


@pytest.mark.parametrize("name", ["/root/x", "../x", "a/../x", "a/./x", "a//x", "a/", "", ".", "..",
                                  "C:/x", "C:x", "\\\\host\\share\\x", "\\root\\x", "//host/x",
                                  "a\x00x", "a\nx", "a/%2e%2e/x", "a:" , "x" * 161, None, []])
def test_member_names_reject_nonliteral_or_escaping_names(name):
    assert codes(bundle.validate_member_names([name])) == {"MEMBER_NAME_INVALID": 1}


def test_slash_normalization_collisions_are_rejected_without_extracting():
    assert bundle.validate_member_names(["alpha/table.json", "beta\\table.json"])["ok"]
    result = bundle.validate_member_names(["alpha/table.json", "alpha\\table.json"])
    assert codes(result) == {"MEMBER_NAME_DUPLICATE": 1}
    assert "alpha" not in json.dumps(result)


def test_resource_budget_exact_edges_and_one_over():
    members = [{"byte_length": 3}, {"byte_length": 2}]
    options = dict(max_members=2, max_member_bytes=3, max_total_bytes=5)
    result = bundle.check_resource_budget(members, **options)
    assert result["ok"] and result["declared_bytes"] == 5
    assert "MEMBER_COUNT_EXCEEDED" in codes(bundle.check_resource_budget(members + [{"byte_length": 0}], **options))
    assert "MEMBER_BYTES_EXCEEDED" in codes(bundle.check_resource_budget([{"byte_length": 4}], **options))
    assert "TOTAL_BYTES_EXCEEDED" in codes(bundle.check_resource_budget([{"byte_length": 3}] * 2, **options))
    assert bundle.check_resource_budget([], max_members=0, max_member_bytes=0, max_total_bytes=0)["ok"]


@pytest.mark.parametrize("size", [None, True, -1, 1.5, "1", float("nan"), float("inf"), 10**1000])
def test_bad_sizes_are_rejected_before_payload_access_or_hashing(size, monkeypatch):
    members, _, _ = synthetic_bundle()
    members[0]["byte_length"] = size
    def forbidden(*args, **kwargs):
        raise AssertionError("payload/hash accessed before metadata budget")
    class PayloadTrap(dict):
        __iter__ = items = __getitem__ = forbidden
    monkeypatch.setattr(bundle, "sha256", forbidden)
    result = audit(members, PayloadTrap())
    assert not result["ok"]
    assert any(c in codes(result) for c in ("MEMBER_SIZE_INVALID", "MEMBER_BYTES_EXCEEDED"))
    assert len(json.dumps(result)) < 2000


@pytest.mark.parametrize("option,value", [("max_members", True), ("max_member_bytes", -1),
                                          ("max_total_bytes", 10**1000), ("max_members", 129)])
def test_invalid_budget_limits(option, value):
    assert codes(bundle.check_resource_budget([], **{option: value})) == {"BUDGET_LIMIT_INVALID": 1}


def test_byte_audit_localizes_missing_foreign_truncated_and_substituted_members():
    members, payloads, _ = synthetic_bundle()
    del payloads[members[0]["name"]]
    payloads[members[1]["name"]] = payloads[members[1]["name"]][:-1]
    payloads[members[2]["name"]] = b'{"panels":2}'
    payloads["foreign/data.json"] = b""
    result = audit(members, payloads)
    assert codes(result) == {"PAYLOAD_FOREIGN": 1, "PAYLOAD_LENGTH_MISMATCH": 1,
                             "PAYLOAD_MISSING": 1, "PAYLOAD_SHA256_MISMATCH": 2}
    assert result["integrity_verified"] is False and result["origin_authenticated"] is False
    assert [m["bytes_equal"] for m in result["members"]] == [False] * 3 + [True] * 11
    assert result == audit(members, dict(reversed(list(payloads.items()))))


def test_supplied_pins_can_be_rewritten_without_authenticating_origin():
    members, payloads, _ = synthetic_bundle()
    payloads[members[0]["name"]] = b'{"panels":2}'
    members[0]["sha256"] = sha256(payloads[members[0]["name"]]).hexdigest()
    result = audit(members, payloads)
    assert result["integrity_verified"] and not result["origin_authenticated"]


def test_missing_inventory_stage_retains_unchecked_siblings_without_hashing():
    members, payloads, _ = synthetic_bundle()
    del payloads[members[0]["name"]]
    result = audit(members[1:], payloads)
    assert codes(result) == {"FIXTURE_STAGE_MISSING": 1}
    assert all(m["bytes_equal"] is None for m in result["members"])


def test_stage_references_retain_independent_conflicts_even_for_equal_bytes():
    members, _, references = synthetic_bundle()
    references[0]["name"] = "beta/import.json"
    references[1]["name"] = "alpha/absent.json"
    references[2]["design_version"] = "other-version"
    members[3]["design_version"] = "mixed-version"
    before = deepcopy((members, references))
    result = refs(members, references)
    assert codes(result) == {"MEMBER_DESIGN_VERSION_CONFLICT": 1, "REFERENCE_CROSS_FIXTURE": 1,
                             "REFERENCE_DANGLING": 1, "REFERENCE_DESIGN_VERSION_CONFLICT": 1,
                             "REFERENCE_MEMBER_VERSION_CONFLICT": 3}
    assert (members, references) == before
    assert result == refs(members, references)


def test_reference_coverage_duplicates_and_wrong_stage_are_named():
    members, _, references = synthetic_bundle()
    references[0]["name"] = "alpha/table.json"
    references.pop()
    references.append(dict(references[1]))
    result = refs(members, references)
    assert codes(result) == {"REFERENCE_STAGE_CONFLICT": 1, "REFERENCE_STAGE_DUPLICATE": 1, "REFERENCE_STAGE_MISSING": 1}


def test_exact_diagnostic_totals_survive_bounded_examples_and_composition():
    members, payloads, references = synthetic_bundle()
    for member in members:
        member["fixture"] = "foreign"
        member["stage"] = "foreign"
        member["sha256"] = "bad"
    result = audit(members, payloads)
    assert len(result["diagnostics"]) == bundle.MAX_DIAGNOSTICS
    assert result["diagnostic_total"] == 56
    assert sum(codes(result).values()) == 56
    assert all(m["bytes_equal"] is None for m in result["members"])
    # Invalid metadata prevents hashing. Fourteen errors in each of fixture,
    # stage, hash syntax and missing coverage are still counted separately.


def json_inventory(members=None):
    if members is None:
        members = synthetic_bundle()[0]
    return json.dumps(dict(members=members, expected_fixture_ids=FIXTURES, required_stages=STAGES))


@pytest.mark.parametrize("text,code", [
    ('{"members":[],"members":[]}', "JSON_DUPLICATE_KEY"),
    ('{"x":{"a":0,"a":1}}', "JSON_DUPLICATE_KEY"),
    ('{"x":NaN}', "JSON_NONFINITE_NUMBER"),
    ('{"x":Infinity}', "JSON_NONFINITE_NUMBER"),
    ('{"x":-Infinity}', "JSON_NONFINITE_NUMBER"),
    ('{"x":1e9999}', "JSON_NONFINITE_NUMBER"),
    ('[0]', "INVENTORY_FIELDS_INVALID"), ('{}', "INVENTORY_FIELDS_INVALID"),
    ('{"members":[]}', "INVENTORY_FIELDS_INVALID"),
    ('{"x":', "JSON_INVALID"), ('{}{}', "JSON_INVALID"),
    ('[' * 17 + '0' + ']' * 17, "JSON_DEPTH_EXCEEDED"),
    (' ' * (bundle.MAX_JSON_CHARS + 1), "JSON_INPUT_UNREADABLE"),
    (None, "JSON_INPUT_UNREADABLE"), (b'{}', "JSON_INPUT_UNREADABLE"),
])
def test_strict_json_rejections_are_sanitized_before_auditing(text, code, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("hash or byte audit reached invalid JSON")
    monkeypatch.setattr(bundle, "sha256", forbidden)
    monkeypatch.setattr(bundle, "audit_bundle_bytes", forbidden)
    result = bundle.audit_inventory_json(text, object())
    assert codes(result) == {code: 1}
    assert result["inventory"] is None and not result["origin_authenticated"]


def test_strict_json_accepts_exact_limit_ignores_quoted_brackets_and_rejects_missing_fields():
    members, payloads, _ = synthetic_bundle()
    text = json_inventory(members)
    assert bundle.audit_inventory_json(text, payloads)["integrity_verified"]
    padded = text + ' ' * (bundle.MAX_JSON_CHARS - len(text))
    assert bundle.decode_inventory_json(padded)["ok"]
    assert codes(bundle.decode_inventory_json('[' * 16 + '0' + ']' * 16)) == {"INVENTORY_FIELDS_INVALID": 1}
    assert codes(bundle.decode_inventory_json(json.dumps({'x': '[\\\"' * 40}))) == {"INVENTORY_FIELDS_INVALID": 1}
    for field in members[0]:
        changed = deepcopy(members); del changed[0][field]
        assert codes(bundle.decode_inventory_json(json_inventory(changed))) == {"MEMBER_FIELDS_INVALID": 1}
    sentinel = 'SYNTHETIC_PRIVATE_X05'
    result = bundle.decode_inventory_json('{"x":"' + sentinel + '","x":0}')
    assert sentinel not in json.dumps(result)


# Literal reference vectors, independently cross-checked using Windows .NET
# SHA256 rather than calculating expected values with this module's digest.
REFERENCE_VECTORS = [
    (b'', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
    (b'\x00', '6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d'),
    (b'\xff', 'a8100ae6aa1940d0b663bb31cd466142ebbdbd5187131b92d93818987832eb89'),
]


@pytest.mark.parametrize("data,digest", REFERENCE_VECTORS)
def test_literal_binary_reference_vectors_and_one_byte_changes(data, digest):
    member = dict(name='unit/saved.bin', fixture='unit', stage='saved', design_version='v1',
                  byte_length=len(data), sha256=digest)
    options = dict(expected_fixture_ids=['unit'], required_stages=['saved'])
    assert bundle.audit_bundle_bytes([member], {member['name']: data}, **options)["integrity_verified"]
    changed = bytes([data[0] ^ 1]) + data[1:] if data else b'\x00'
    result = bundle.audit_bundle_bytes([member], {member['name']: changed}, **options)
    assert codes(result)["PAYLOAD_SHA256_MISMATCH"] == 1
    assert codes(result).get("PAYLOAD_LENGTH_MISMATCH", 0) == (0 if data else 1)


@pytest.mark.parametrize("mutation", ['name', 'fixture', 'stage', 'hash', 'version', 'size', 'coverage', 'count', 'total'])
def test_all_invalid_inventory_metadata_short_circuits_hashes(mutation, monkeypatch):
    members, payloads, _ = synthetic_bundle()
    if mutation == 'name': members[0]['name'] = '../bad'
    elif mutation == 'fixture': members[0]['fixture'] = 'foreign'
    elif mutation == 'stage': members[0]['stage'] = 'foreign'
    elif mutation == 'hash': members[0]['sha256'] = 'bad'
    elif mutation == 'version': members[0]['design_version'] = None
    elif mutation == 'size': members[0]['byte_length'] = True
    elif mutation == 'coverage': members.pop()
    elif mutation == 'count': members *= 10
    elif mutation == 'total':
        for member in members: member['byte_length'] = bundle.MAX_MEMBER_BYTES
    calls = []
    monkeypatch.setattr(bundle, 'sha256', lambda data: calls.append(data) or sha256(data))
    assert not audit(members, payloads)['ok']
    assert calls == []
    assert not bundle.audit_inventory_json(json_inventory(members), payloads)['ok']
    assert calls == []


def test_valid_members_are_hashed_once_and_foreign_payloads_are_not(monkeypatch):
    members, payloads, _ = synthetic_bundle()
    calls = []
    monkeypatch.setattr(bundle, 'sha256', lambda data: calls.append(data) or sha256(data))
    assert audit(members, payloads)['ok']; assert len(calls) == len(members)
    calls.clear(); payloads['foreign/data.bin'] = b'foreign'
    assert codes(audit(members, payloads)) == {'PAYLOAD_FOREIGN': 1}
    assert len(calls) == len(members) and b'foreign' not in calls


def diagnostic_signature(result):
    return {k: result[k] for k in ('ok', 'diagnostic_total', 'diagnostic_counts', 'diagnostics', 'origin_authenticated')}


def test_bundle_permutations_preserve_exact_conflicts_past_example_cap_and_inputs():
    members, payloads, references = synthetic_bundle()
    for member in members:
        payloads[member['name']] = b'{"panels":22}'
        member['design_version'] = 'other-version'
    for i in range(10): payloads[f'foreign/{i}.bin'] = b''
    references *= 4
    references = deepcopy(references)
    references[0]['name'] = 'beta/import.json'
    references[1]['name'] = 'alpha/absent.json'
    expected_audit = diagnostic_signature(audit(members, payloads))
    expected_refs = diagnostic_signature(refs(members, references))
    assert expected_audit['diagnostic_total'] == 38
    assert expected_refs['diagnostic_total'] > bundle.MAX_DIAGNOSTICS
    for shift in (0, 1, 5, 13):
        permuted_members = list(reversed(members[shift:] + members[:shift]))
        permuted_payloads = dict(reversed(list(payloads.items())))
        permuted_refs = list(reversed(references[shift:] + references[:shift]))
        before = deepcopy((permuted_members, permuted_payloads, permuted_refs))
        assert diagnostic_signature(audit(permuted_members, permuted_payloads)) == expected_audit
        assert diagnostic_signature(refs(permuted_members, permuted_refs)) == expected_refs
        assert (permuted_members, permuted_payloads, permuted_refs) == before


@pytest.mark.parametrize("bad", ['text', None, [], {}, bytearray(b'x'), memoryview(b'x')])
def test_payload_type_uncertainty_keeps_binary_siblings_and_recovers(bad):
    members, payloads, _ = synthetic_bundle()
    original = dict(payloads)
    payloads[members[0]['name']] = bad
    payloads[members[1]['name']] = None
    result = audit(members, payloads)
    assert codes(result) == {'PAYLOAD_TYPE_INVALID': 2}
    assert [m['bytes_equal'] for m in result['members']] == [False] * 2 + [True] * 12
    assert payloads[members[0]['name']] is bad and payloads[members[1]['name']] is None
    payloads.update(original)  # Repair the same caller-owned mapping, not a fresh auditor instance.
    assert audit(members, payloads)['integrity_verified']
    assert not audit(members, payloads)['origin_authenticated']


def reconciliation_bundle(fixture_ids=('alpha',), failed=()):
    # Reuse the existing fully populated seven-step positive fixture, rather
    # than replacing reconciliation with an injected verdict callback.
    from .test_x05_reconcile import bound_seven_envelope
    members, payloads, references, versions = [], {}, [], {}
    for fixture in fixture_ids:
        record = bound_seven_envelope()
        assurance = record['envelope']['assurance']
        version = 'design-' + fixture
        versions[fixture] = version
        claims = [record[k] for k in ('manifest', 'import_binding', 'grid_binding')]
        claims += [assurance['manifest'], assurance['evidence']] + list(assurance['bindings'].values())
        for claim in claims: claim['fixture_id'] = fixture
        for identity in [assurance['identity']] + list(assurance['stage_identities'].values()):
            identity['version_id'] = version
        if fixture in failed: record['envelope']['import']['normal_user'] = False
        raw = json.dumps(record, sort_keys=True).encode()
        name = fixture + '/seven.json'
        payloads[name] = raw
        members.append(dict(name=name, fixture=fixture, stage='seven_step', byte_length=len(raw),
                            sha256=sha256(raw).hexdigest(), design_version=version))
        references.append(dict(name=name, fixture=fixture, stage='seven_step', design_version=version))
    return dict(members=members, payloads=payloads, references=references,
                expected_fixture_versions=versions, required_stages=['seven_step'], evidence_stage='seven_step')


def replay_envelope(source):
    return dict(inventory=dict(members=deepcopy(source['members']),
                               expected_fixture_ids=sorted(source['expected_fixture_versions']),
                               required_stages=source['required_stages']),
                references=deepcopy(source['references']), expected_fixture_versions=source['expected_fixture_versions'],
                evidence_stage=source['evidence_stage'], member_bytes_hex={k: v.hex() for k, v in source['payloads'].items()})


@pytest.mark.parametrize('failed,verdict', [((), 'PASS'), (('alpha',), 'FAIL')])
def test_adapter_preserves_integrity_separately_from_real_semantics(failed, verdict):
    source = reconciliation_bundle(failed=failed)
    before = deepcopy(source)
    result = bundle.reconcile_bundle(**source)
    assert result['integrity']['integrity_verified'] and result['bindings']['ok']
    assert result['semantic']['verdict'] == verdict
    assert result['semantic']['fixtures']['alpha']['result']['reconciliation']['steps']['1']['verdict'] == verdict
    assert result['origin_authenticated'] is False and source == before
    assert bundle.replay_bundle_json(json.dumps(replay_envelope(source))) == result


@pytest.mark.parametrize('mutation', ['missing', 'substituted', 'unbound'])
def test_adapter_never_decodes_or_reconciles_unreadable_members(mutation, monkeypatch):
    source = reconciliation_bundle()
    name = source['members'][0]['name']
    if mutation == 'missing': del source['payloads'][name]
    elif mutation == 'substituted': source['payloads'][name] += b' '
    else: source['references'][0]['name'] = 'foreign/seven.json'
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError('semantic path reached unverified or unbound member')
    monkeypatch.setattr(bundle.reconciliation, 'decode_evidence_json', forbidden)
    monkeypatch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', forbidden)
    result = bundle.reconcile_bundle(**source)
    assert result['semantic']['verdict'] == 'UNREADABLE' and calls == []
    assert result['integrity']['integrity_verified'] == (mutation == 'unbound')


def test_multi_fixture_replay_retains_clean_failure_and_unreadable_siblings_under_permutations():
    from itertools import permutations
    source = reconciliation_bundle(('alpha', 'beta', 'gamma'), failed=('gamma',))
    source['payloads']['beta/seven.json'] += b' '
    before = deepcopy(source)
    expected = bundle.reconcile_bundle(**source)
    assert expected['semantic']['verdict'] == 'UNREADABLE'
    assert {k: v['verdict'] for k, v in expected['semantic']['fixtures'].items()} == {
        'alpha': 'PASS', 'beta': 'UNREADABLE', 'gamma': 'FAIL'}
    for order in permutations(source['members']):
        changed = dict(source, members=list(order), payloads=dict(reversed(list(source['payloads'].items()))),
                       references=list(reversed(source['references'])))
        assert bundle.reconcile_bundle(**changed) == expected
        assert bundle.replay_bundle_json(json.dumps(replay_envelope(changed))) == expected
    assert source == before


@pytest.mark.parametrize('mutation,reason', [('duplicate', 'json_duplicate_key'),
    ('fixture', 'bundle_evidence_fixture_conflict'), ('version', 'bundle_evidence_design_version_conflict'),
    ('stage_version', 'bundle_evidence_design_version_conflict'), ('utf8', 'bundle_evidence_utf8_unreadable')])
def test_hash_correct_evidence_cannot_cross_fixture_or_version_boundary(mutation, reason, monkeypatch):
    source = reconciliation_bundle()
    member = source['members'][0]
    record = json.loads(source['payloads'][member['name']])
    if mutation == 'duplicate': raw = b'{"envelope":{},"envelope":{}}'
    elif mutation == 'utf8': raw = b'\xff'
    else:
        if mutation == 'fixture': record['manifest']['fixture_id'] = 'foreign'
        elif mutation == 'version': record['envelope']['assurance']['identity']['version_id'] = 'foreign'
        else: record['envelope']['assurance']['stage_identities']['csv']['version_id'] = 'foreign'
        raw = json.dumps(record).encode()
    source['payloads'][member['name']] = raw
    member.update(byte_length=len(raw), sha256=sha256(raw).hexdigest())
    before = deepcopy(source)
    def forbidden(*args, **kwargs): raise AssertionError('foreign or ambiguous evidence reconciled')
    monkeypatch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', forbidden)
    result = bundle.reconcile_bundle(**source)
    assert result['integrity']['integrity_verified'] and result['semantic']['verdict'] == 'UNREADABLE'
    assert result['semantic']['fixtures']['alpha']['result']['reason'] == reason
    assert not result['origin_authenticated'] and source == before


@pytest.mark.parametrize('value', ['0', 'gg', 'ab cd', None, [], {}, True])
def test_replay_hex_invalidity_precedes_all_byte_allocation(value, monkeypatch):
    envelope = replay_envelope(reconciliation_bundle())
    envelope['member_bytes_hex']['alpha/seven.json'] = value
    def forbidden(*args, **kwargs): raise AssertionError('invalid hex allocated')
    monkeypatch.setattr(bundle, '_member_bytes_from_hex', forbidden)
    result = bundle.decode_replay_json(json.dumps(envelope))
    assert codes(result) == {'MEMBER_HEX_INVALID': 1} and result['envelope'] is None


def tiny_replay():
    members, references = [], []
    for fixture in ('alpha', 'beta'):
        name = fixture + '/seven.bin'
        members.append(dict(name=name, fixture=fixture, stage='seven_step', byte_length=1,
                            sha256=sha256(b'0').hexdigest(), design_version='v1'))
        references.append(dict(name=name, fixture=fixture, stage='seven_step', design_version='v1'))
    return dict(inventory=dict(members=members, expected_fixture_ids=['alpha', 'beta'], required_stages=['seven_step']),
                references=references, expected_fixture_versions={'alpha': 'v1', 'beta': 'v1'}, evidence_stage='seven_step',
                member_bytes_hex={'alpha/seven.bin': '30', 'beta/seven.bin': '30'})


@pytest.mark.parametrize('sizes,expected', [((9, 1), 'MEMBER_HEX_BYTES_EXCEEDED'), ((7, 7), 'MEMBER_HEX_TOTAL_EXCEEDED')])
def test_replay_decoded_budgets_are_checked_before_any_allocation(sizes, expected, monkeypatch):
    envelope = tiny_replay()
    for key, size in zip(envelope['member_bytes_hex'], sizes): envelope['member_bytes_hex'][key] = '00' * size
    def forbidden(*args, **kwargs): raise AssertionError('over-budget decoded bytes allocated')
    monkeypatch.setattr(bundle, '_member_bytes_from_hex', forbidden)
    result = bundle.decode_replay_json(json.dumps(envelope), max_member_bytes=8, max_total_bytes=12)
    assert codes(result) == {expected: 1} and result['envelope'] is None


def test_replay_exact_decoded_boundaries_allocate_each_member_once(monkeypatch):
    envelope = tiny_replay()
    envelope['member_bytes_hex'] = {'alpha/seven.bin': '00' * 8, 'beta/seven.bin': '00' * 4}
    calls = []
    def allocated(text): calls.append(len(text) // 2); return bytes.fromhex(text)
    monkeypatch.setattr(bundle, '_member_bytes_from_hex', allocated)
    result = bundle.decode_replay_json(json.dumps(envelope), max_member_bytes=8, max_total_bytes=12)
    assert result['ok'] and calls == [8, 4] and not result['origin_authenticated']
    assert [len(v) for v in result['envelope']['payloads'].values()] == [8, 4]


@pytest.mark.parametrize('mutation,code', [('fields', 'REPLAY_FIELDS_INVALID'), ('fixtures', 'REPLAY_FIXTURES_INVALID'),
    ('stage', 'REPLAY_STAGE_INVALID'), ('reference', 'REFERENCE_STAGE_MISSING'), ('hex_map', 'MEMBER_HEX_UNREADABLE')])
def test_replay_envelope_rejects_missing_and_foreign_bindings(mutation, code, monkeypatch):
    envelope = tiny_replay()
    if mutation == 'fields': del envelope['references']
    elif mutation == 'fixtures': envelope['expected_fixture_versions']['foreign'] = 'v1'
    elif mutation == 'stage': envelope['evidence_stage'] = 'foreign'
    elif mutation == 'reference': envelope['references'].pop()
    else: envelope['member_bytes_hex'] = None
    def forbidden(*args, **kwargs): raise AssertionError('unbound envelope allocated bytes')
    monkeypatch.setattr(bundle, '_member_bytes_from_hex', forbidden)
    result = bundle.decode_replay_json(json.dumps(envelope))
    assert codes(result) == {code: 1} and not result['origin_authenticated']


@pytest.mark.parametrize('text,code', [('{}', 'REPLAY_FIELDS_INVALID'),
    ('{"inventory":{},"inventory":{}}', 'JSON_DUPLICATE_KEY'), ('{"x":NaN}', 'JSON_NONFINITE_NUMBER')])
def test_replay_json_is_strict_and_never_reaches_semantics(text, code, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('bad replay reached semantic decoding')
    monkeypatch.setattr(bundle.reconciliation, 'decode_evidence_json', forbidden)
    result = bundle.replay_bundle_json(text)
    assert codes(result['integrity']) == {code: 1}
    assert result['semantic']['verdict'] == 'UNREADABLE' and not result['origin_authenticated']


def cli_payload(kind='PASS', *, unicode_fixture=False):
    source = reconciliation_bundle(failed=('alpha',) if kind == 'FAIL' else ())
    if kind == 'UNREADABLE': source['payloads']['alpha/seven.json'] += b' '
    envelope = replay_envelope(source)
    if unicode_fixture:
        # The bundle's explicit ASCII identifier policy is preserved. Unicode
        # IDs must be rejected identically under every interpreter encoding.
        envelope['inventory']['expected_fixture_ids'] = ['\u00e5\u2600']
        envelope['inventory']['members'][0]['fixture'] = '\u00e5\u2600'
        envelope['expected_fixture_versions'] = {'\u00e5\u2600': 'design-alpha'}
        envelope['references'][0]['fixture'] = '\u00e5\u2600'
    return json.dumps(envelope, ensure_ascii=False).encode('utf-8') if kind != 'malformed' else b'{'


def cli_command(*args):
    import sys
    return [sys.executable, '-P', '-B', str(__import__('pathlib').Path(bundle.__file__).resolve()), *args]


@pytest.mark.parametrize('kind,exit_code', [('PASS', 0), ('FAIL', 1), ('UNREADABLE', 4), ('malformed', 4)])
@pytest.mark.parametrize('args', [[], ['--replay']])
def test_stdin_cli_subprocess_verdicts_are_single_ascii_json(tmp_path, kind, exit_code, args):
    import subprocess
    raw = cli_payload(kind)
    expected = bundle.reconciliation.serialize_result(bundle.replay_bundle_json(raw.decode('utf-8'))).encode('ascii') + b'\n'
    result = subprocess.run(cli_command(*args), input=raw, capture_output=True, cwd=tmp_path, timeout=15)
    assert result.returncode == exit_code and result.stderr == b''
    assert result.stdout == expected and len(result.stdout.splitlines()) == 1
    assert not json.loads(result.stdout)['origin_authenticated']


class ShortInput:
    def __init__(self, data, width=7, failure=None):
        self.data, self.width, self.failure, self.pos, self.requests = data, width, failure, 0, []
    def read(self, count):
        self.requests.append(count)
        if self.pos == len(self.data) and self.failure is not None:
            raise self.failure('synthetic private read failure')
        result = self.data[self.pos:self.pos + min(count, self.width)]
        self.pos += len(result)
        return result


def in_process_cli(monkeypatch, reader, stdout=None):
    import io
    import sys
    from types import SimpleNamespace
    output = io.BytesIO()
    errors = io.StringIO()
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=reader))
    monkeypatch.setattr(sys, 'stdout', stdout if stdout is not None else SimpleNamespace(buffer=output))
    monkeypatch.setattr(sys, 'stderr', errors)
    code = bundle.main(['--replay'])
    return code, output.getvalue(), errors.getvalue()


@pytest.mark.parametrize('width', [1, 2, 7, 8192])
@pytest.mark.parametrize('unicode_fixture', [False, True])
def test_cli_short_reads_preserve_utf8_and_json_boundaries(monkeypatch, width, unicode_fixture):
    raw = cli_payload(unicode_fixture=unicode_fixture)
    reader = ShortInput(raw, width)
    original, calls = bundle.replay_bundle_json, []
    def replayed(text): calls.append(text); return original(text)
    monkeypatch.setattr(bundle, 'replay_bundle_json', replayed)
    expected = bundle.reconciliation.serialize_result(original(raw.decode('utf-8'))).encode('ascii') + b'\n'
    code, output, errors = in_process_cli(monkeypatch, reader)
    assert code == (4 if unicode_fixture else 0) and output == expected and not errors
    assert calls == [raw.decode('utf-8')] and reader.pos == len(raw)
    assert all(0 < count <= 8192 for count in reader.requests)


@pytest.mark.parametrize('failure', [OSError, ValueError])
@pytest.mark.parametrize('prefix', [False, True])
def test_cli_read_failures_never_replay_a_valid_prefix(monkeypatch, failure, prefix):
    reader = ShortInput(cli_payload() if prefix else b'', failure=failure)
    def forbidden(*args): raise AssertionError('read failure reached replay')
    monkeypatch.setattr(bundle, 'replay_bundle_json', forbidden)
    code, output, errors = in_process_cli(monkeypatch, reader)
    assert code == 4 and not errors
    assert codes(json.loads(output)['integrity']) == {'CLI_INPUT_UNREADABLE': 1}
    assert b'synthetic private' not in output and len(output.splitlines()) == 1


def test_cli_enforces_exact_wire_limit_before_replay(monkeypatch):
    raw = cli_payload()
    monkeypatch.setattr(bundle, 'MAX_STDIN_BYTES', len(raw))
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
    assert code == 0 and json.loads(output)['semantic']['verdict'] == 'PASS' and not errors
    def forbidden(*args): raise AssertionError('over-limit bytes reached replay')
    monkeypatch.setattr(bundle, 'replay_bundle_json', forbidden)
    reader = ShortInput(raw + b' ')
    code, output, errors = in_process_cli(monkeypatch, reader)
    assert code == 4 and not errors and reader.pos == len(raw) + 1
    assert codes(json.loads(output)['integrity']) == {'CLI_INPUT_TOO_LARGE': 1}


@pytest.mark.parametrize('raw', [b'\xff', b'\xf0\x9f\x99', b'{"x":"\xff"}'])
def test_cli_invalid_utf8_is_sanitized(monkeypatch, raw):
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw, 1))
    assert code == 4 and not errors and codes(json.loads(output)['integrity']) == {'CLI_INPUT_UNREADABLE': 1}


@pytest.mark.parametrize('kind', ['PASS', 'FAIL', 'UNREADABLE'])
@pytest.mark.parametrize('stage', ['write', 'flush'])
def test_cli_delivery_failure_overrides_every_semantic_exit(monkeypatch, kind, stage):
    class FailedOutput:
        def write(self, data):
            if stage == 'write': raise OSError('synthetic private output failure')
            return len(data)
        def flush(self):
            raise ValueError('synthetic private flush failure')
    code, output, errors = in_process_cli(monkeypatch, ShortInput(cli_payload(kind)), FailedOutput())
    assert code == 5 and not output and not errors


@pytest.mark.parametrize('binary', [False, True])
def test_cli_positive_short_writes_deliver_one_complete_result(monkeypatch, binary):
    from types import SimpleNamespace
    class Partial:
        def __init__(self): self.parts, self.flushes = [], 0
        def write(self, value): self.parts.append(value[:7]); return len(value[:7])
        def flush(self): self.flushes += 1
    sink = Partial()
    code, _, errors = in_process_cli(monkeypatch, ShortInput(cli_payload()), SimpleNamespace(buffer=sink) if binary else sink)
    output = (b''.join(sink.parts) if binary else ''.join(sink.parts).encode('ascii'))
    assert code == 0 and not errors and sink.flushes == 1
    assert len(output.splitlines()) == 1 and json.loads(output)['semantic']['verdict'] == 'PASS'


def test_real_closed_stdout_pipe_returns_five_without_traceback(tmp_path):
    import subprocess
    process = subprocess.Popen(cli_command('--replay'), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path)
    try:
        process.stdout.close(); process.stdout = None
        _, errors = process.communicate(input=cli_payload(), timeout=15)
        assert process.returncode == 5 and errors == b''
    finally:
        if process.poll() is None:
            process.kill(); process.communicate(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None: pipe.close()


@pytest.mark.parametrize('args,expected', [(['--help'], 0), (['--unknown'], 2), (['payload.json'], 2),
    (['--replay', 'payload.json'], 2), (['--replay=path.json'], 2)])
def test_cli_usage_never_reads_stdin_or_replays(monkeypatch, args, expected):
    import io
    import sys
    class ForbiddenInput:
        @property
        def buffer(self): raise AssertionError('usage consumed stdin')
    output, errors = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, 'stdin', ForbiddenInput())
    monkeypatch.setattr(sys, 'stdout', output); monkeypatch.setattr(sys, 'stderr', errors)
    def forbidden(*args): raise AssertionError('usage reached replay')
    monkeypatch.setattr(bundle, 'replay_bundle_json', forbidden)
    with pytest.raises(SystemExit) as stopped: bundle.main(args)
    assert stopped.value.code == expected
    if expected == 0:
        assert '0 PASS; 1 FAIL; 2 usage; 4 UNREADABLE; 5 output failure.' in output.getvalue()
        assert errors.getvalue() == ''
    else:
        assert output.getvalue() == '' and 'usage:' in errors.getvalue()


@pytest.mark.parametrize('unicode_fixture,expected_exit', [(False, 0), (True, 4)])
def test_cli_is_byte_identical_across_explicit_encoding_and_hash_profiles(tmp_path, unicode_fixture, expected_exit):
    import os
    import subprocess
    outputs = []
    for seed in ('0', '19'):
        for utf8 in ('0', '1'):
            for encoding in ('ascii:strict', 'utf-8'):
                # Do not read or copy the operator environment or credentials.
                env = dict(PYTHONHASHSEED=seed, PYTHONUTF8=utf8, PYTHONIOENCODING=encoding)
                if os.name == 'nt': env['SystemRoot'] = 'C:\\Windows'
                result = subprocess.run(cli_command('--replay'), input=cli_payload(unicode_fixture=unicode_fixture),
                                        capture_output=True, cwd=tmp_path, env=env, timeout=15)
                assert result.returncode == expected_exit and result.stderr == b''
                assert all(value < 128 for value in result.stdout)
                assert not json.loads(result.stdout)['origin_authenticated']
                outputs.append(result.stdout)
    assert len(set(outputs)) == 1


@pytest.mark.parametrize('invalid', ['none', 'text', 'bytearray', 'oversized'])
def test_stdin_return_contract_after_complete_valid_prefix_and_clean_recovery(monkeypatch, invalid):
    raw = cli_payload()
    class InvalidReturn(ShortInput):
        def read(self, count):
            if self.pos < len(self.data): return super().read(count)
            self.requests.append(count)
            return {'none': None, 'text': 'synthetic private stdin text',
                    'bytearray': bytearray(b'x'), 'oversized': b'x' * (count + 1)}[invalid]
    calls, original = [], bundle.replay_bundle_json
    def replayed(text): calls.append(text); return original(text)
    monkeypatch.setattr(bundle, 'replay_bundle_json', replayed)
    reader = InvalidReturn(raw)
    code, output, errors = in_process_cli(monkeypatch, reader)
    assert code == 4 and not errors and calls == [] and reader.pos == len(raw)
    assert codes(json.loads(output)['integrity']) == {'CLI_INPUT_UNREADABLE': 1}
    assert b'synthetic private' not in output and len(output.splitlines()) == 1
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
    assert code == 0 and not errors and calls == [raw.decode('utf-8')]
    assert json.loads(output)['semantic']['verdict'] == 'PASS'


@pytest.mark.parametrize('reported', [0, -1, True, None, 'overreported'])
@pytest.mark.parametrize('binary', [False, True])
def test_stdout_invalid_progress_after_prefix_stops_once(monkeypatch, reported, binary):
    from types import SimpleNamespace
    raw = cli_payload()
    expected = bundle.reconciliation.serialize_result(bundle.replay_bundle_json(raw.decode())) + '\n'
    class InvalidProgress:
        def __init__(self): self.parts, self.calls, self.flushes = [], 0, 0
        def write(self, data):
            self.calls += 1
            if self.calls == 1: self.parts.append(data[:7]); return 7
            return len(data) + 1 if reported == 'overreported' else reported
        def flush(self): self.flushes += 1
    sink = InvalidProgress()
    code, _, errors = in_process_cli(monkeypatch, ShortInput(raw), SimpleNamespace(buffer=sink) if binary else sink)
    delivered = b''.join(sink.parts).decode('ascii') if binary else ''.join(sink.parts)
    assert code == 5 and not errors and sink.calls == 2 and sink.flushes == 0
    assert delivered == expected[:7]


def borrowed_cli(monkeypatch, raw, *, args=('--replay',), fail_write=False):
    import io
    import sys
    class Buffer(io.BytesIO):
        closes = 0
        def close(self): self.closes += 1; super().close()
        def write(self, data):
            if fail_write: raise OSError('synthetic interrupted borrowed output')
            return super().write(data)
    class Stream:
        def __init__(self, data=b''): self.buffer, self.closes, self.text = Buffer(data), 0, []
        def write(self, value): self.text.append(value); return len(value)
        def flush(self): pass
        def close(self): self.closes += 1; self.buffer.close()
        def fileno(self): raise OSError('borrowed memory stream has no descriptor')
    incoming, outgoing, errors = Stream(raw), Stream(), Stream()
    monkeypatch.setattr(sys, 'stdin', incoming); monkeypatch.setattr(sys, 'stdout', outgoing); monkeypatch.setattr(sys, 'stderr', errors)
    try:
        code = bundle.main(list(args))
    except SystemExit as stopped:
        code = stopped.code
    assert all(s.closes == 0 and s.buffer.closes == 0 and not s.buffer.closed for s in (incoming, outgoing, errors))
    return code, outgoing.buffer.getvalue(), incoming, outgoing, errors


@pytest.mark.parametrize('kind,expected', [('PASS', 0), ('malformed', 4), ('delivery', 5), ('help', 0), ('usage', 2)])
def test_cli_never_closes_borrowed_streams_or_buffers(monkeypatch, kind, expected):
    args = ('--help',) if kind == 'help' else ('--unknown',) if kind == 'usage' else ('--replay',)
    code, output, incoming, outgoing, errors = borrowed_cli(monkeypatch,
        cli_payload('malformed' if kind == 'malformed' else 'PASS'), args=args, fail_write=kind == 'delivery')
    assert code == expected
    if kind in ('help', 'usage'): assert incoming.buffer.tell() == 0 and output == b''
    if kind == 'PASS': assert json.loads(output)['semantic']['verdict'] == 'PASS'
    if kind == 'malformed': assert codes(json.loads(output)['integrity']) == {'JSON_INVALID': 1}


def test_interrupted_stdin_after_partial_progress_is_sanitized_and_recovers(monkeypatch):
    raw = cli_payload()
    calls, original = [], bundle.replay_bundle_json
    def replayed(text): calls.append(text); return original(text)
    monkeypatch.setattr(bundle, 'replay_bundle_json', replayed)
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw[:len(raw)//2], failure=InterruptedError))
    assert code == 4 and not errors and calls == []
    assert codes(json.loads(output)['integrity']) == {'CLI_INPUT_UNREADABLE': 1}
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
    assert code == 0 and not errors and len(calls) == 1 and json.loads(output)['semantic']['verdict'] == 'PASS'


@pytest.mark.parametrize('stage', ['write', 'flush'])
@pytest.mark.parametrize('binary', [False, True])
def test_interrupted_delivery_does_not_repeat_prefix_or_replay(monkeypatch, stage, binary):
    from types import SimpleNamespace
    raw = cli_payload()
    calls, original = [], bundle.replay_bundle_json
    expected = bundle.reconciliation.serialize_result(original(raw.decode())) + '\n'
    def replayed(text): calls.append(text); return original(text)
    monkeypatch.setattr(bundle, 'replay_bundle_json', replayed)
    class Interrupted:
        def __init__(self): self.parts, self.writes, self.flushes = [], 0, 0
        def write(self, data):
            self.writes += 1
            if stage == 'write' and self.writes == 2: raise InterruptedError('synthetic private interrupted write')
            piece = data[:7]; self.parts.append(piece); return len(piece)
        def flush(self): self.flushes += 1; raise InterruptedError('synthetic private interrupted flush')
    sink = Interrupted()
    code, _, errors = in_process_cli(monkeypatch, ShortInput(raw), SimpleNamespace(buffer=sink) if binary else sink)
    delivered = b''.join(sink.parts).decode('ascii') if binary else ''.join(sink.parts)
    assert code == 5 and not errors and len(calls) == 1
    assert delivered == (expected[:7] if stage == 'write' else expected)
    assert sink.flushes == (0 if stage == 'write' else 1)
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
    assert code == 0 and not errors and len(calls) == 2 and output.decode('ascii') == expected


def exact_decimal_cli_payload(exponent, direction):
    source = reconciliation_bundle()
    member = source['members'][0]
    record = json.loads(source['payloads'][member['name']])
    record['envelope']['assurance']['evidence']['saved_strings'][0]['optimizers'] = 9007199254740991
    record['envelope']['grid']['dc_power'][0]['kw_dc'] = (
        '4.' + '0' * (exponent - 1) + '1' if direction > 0 else '3.' + '9' * exponent)
    raw = json.dumps(record, sort_keys=True).encode()
    source['payloads'][member['name']] = raw
    member.update(byte_length=len(raw), sha256=sha256(raw).hexdigest())
    return json.dumps(replay_envelope(source)).encode()


@pytest.mark.parametrize('exponent', [18, 30, 60])
@pytest.mark.parametrize('direction', [-1, 1])
def test_cli_serializes_tiny_deltas_and_large_counts_exactly_without_context_changes(monkeypatch, exponent, direction):
    from decimal import Decimal, getcontext, localcontext, ROUND_FLOOR
    from fractions import Fraction
    raw = exact_decimal_cli_payload(exponent, direction)
    def context_state(ctx):
        return (ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax, ctx.capitals, ctx.clamp, dict(ctx.flags), dict(ctx.traps))
    prior = context_state(getcontext())
    with localcontext() as ctx:
        ctx.prec = 7; ctx.rounding = ROUND_FLOOR; ctx.clear_flags()
        before = context_state(ctx)
        code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
        assert context_state(getcontext()) == before
    assert context_state(getcontext()) == prior
    result = json.loads(output, parse_float=Decimal)
    assert code == 1 and not errors and result['integrity']['integrity_verified']
    assert result['semantic']['verdict'] == 'FAIL' and not result['origin_authenticated']
    def nodes(value):
        yield value
        if isinstance(value, dict):
            for child in value.values(): yield from nodes(child)
        elif isinstance(value, list):
            for child in value: yield from nodes(child)
    observed = list(nodes(result))
    assert any(type(value) is int and value == 9007199254740991 for value in observed)
    assert any(type(value) is int and value == 9007199254740998 for value in observed)
    tiny = Fraction(direction, 10**exponent)
    matching = [row for row in observed if isinstance(row, dict) and 'delta' in row
                and isinstance(row['delta'], (Decimal, int)) and Fraction(row['delta']) == tiny]
    assert matching
    assert any(Fraction(row['saved']) == Fraction(48, 5) + tiny for row in matching if 'saved' in row)


def encoder_args(source):
    return dict(inventory=dict(members=source['members'], expected_fixture_ids=sorted(source['expected_fixture_versions']),
                               required_stages=source['required_stages']), references=source['references'], member_bytes=source['payloads'],
                expected_fixture_versions=source['expected_fixture_versions'], evidence_stage=source['evidence_stage'])


ENCODER_WIRE_VECTOR = (
    '{"evidence_stage":"saved","expected_fixture_versions":{"unit":"v1"},'
    '"inventory":{"expected_fixture_ids":["unit"],"members":[{"byte_length":$length,'
    '"design_version":"v1","fixture":"unit","name":"unit/saved.bin",'
    '"sha256":"$digest","stage":"saved"}],"required_stages":["saved"]},'
    '"member_bytes_hex":{"unit/saved.bin":"$hex"},"references":[{"design_version":"v1",'
    '"fixture":"unit","name":"unit/saved.bin","stage":"saved"}]}')


def literal_encoder_case(data, digest):
    member = dict(name='unit/saved.bin', fixture='unit', stage='saved', byte_length=len(data), sha256=digest, design_version='v1')
    return dict(inventory=dict(members=[member], expected_fixture_ids=['unit'], required_stages=['saved']),
                references=[dict(name=member['name'], fixture='unit', stage='saved', design_version='v1')],
                member_bytes={member['name']:data}, expected_fixture_versions={'unit':'v1'}, evidence_stage='saved')


@pytest.mark.parametrize('data,digest,raw_hex,length', [(data,digest,h,n) for (data,digest),(h,n) in
    zip(REFERENCE_VECTORS,[('',0),('00',1),('ff',1)])])
def test_encoder_matches_independent_literal_wire_vectors(data,digest,raw_hex,length,monkeypatch):
    args = literal_encoder_case(data,digest)
    before = deepcopy(args)
    def forbidden(*args): raise AssertionError('encoder recomputed a supplied digest')
    monkeypatch.setattr(bundle,'sha256',forbidden)
    expected = ENCODER_WIRE_VECTOR.replace('$length',str(length)).replace('$digest',digest).replace('$hex',raw_hex)
    result = bundle.encode_replay_json(**args)
    assert result['ok'] and result['text']==expected and result['wire_bytes']==len(expected.encode('ascii'))
    assert not result['integrity_verified'] and not result['origin_authenticated']
    assert json.loads(result['text'])['inventory']['members'][0]['sha256']==digest and args==before


def test_encoder_exact_wire_and_backslash_escape_boundaries_precede_output_allocation(monkeypatch):
    data,digest=REFERENCE_VECTORS[1]
    args=literal_encoder_case(data,digest)
    name='unit\\saved.bin'
    args['inventory']['members'][0]['name']=name;args['references'][0]['name']=name
    args['member_bytes']={name:data}
    expected=ENCODER_WIRE_VECTOR.replace('$length','1').replace('$digest',digest).replace('$hex','00').replace('unit/saved.bin',r'unit\\saved.bin')
    limit=len(expected.encode('ascii'))
    result=bundle.encode_replay_json(**args,max_wire_bytes=limit)
    assert result['ok'] and result['text']==expected and result['wire_bytes']==limit
    def forbidden(*args):raise AssertionError('over-budget encoder allocated output')
    monkeypatch.setattr(bundle,'_encode_member_hex',forbidden);monkeypatch.setattr(bundle,'_dump_replay',forbidden)
    result=bundle.encode_replay_json(**args,max_wire_bytes=limit-1)
    assert codes(result)=={'ENCODER_WIRE_BUDGET_EXCEEDED':1} and result['text'] is None
    assert result['wire_bytes_lower_bound']==limit


@pytest.mark.parametrize('limit',[True,-1,bundle.MAX_STDIN_BYTES+1,None])
def test_encoder_rejects_invalid_wire_limits(limit):
    args=literal_encoder_case(*REFERENCE_VECTORS[0])
    assert codes(bundle.encode_replay_json(**args,max_wire_bytes=limit))=={'ENCODER_WIRE_LIMIT_INVALID':1}


@pytest.mark.parametrize('kind,exit_code',[('PASS',0),('FAIL',1),('UNREADABLE',4)])
def test_encoder_round_trips_real_replay_and_cli_without_changing_pins(tmp_path,kind,exit_code):
    import subprocess
    source=reconciliation_bundle(('alpha','beta'),failed=('alpha',) if kind=='FAIL' else ())
    if kind=='UNREADABLE':source['payloads']['alpha/seven.json']+=b' '
    args=encoder_args(source);before=deepcopy(args)
    result=bundle.encode_replay_json(**args)
    assert result['ok'] and args==before
    encoded=json.loads(result['text'])
    assert encoded['inventory']['members']==sorted(source['members'],key=lambda row:row['name'])
    expected=bundle.reconcile_bundle(**source)
    assert bundle.replay_bundle_json(result['text'])==expected
    completed=subprocess.run(cli_command('--replay'),input=result['text'].encode('ascii'),capture_output=True,cwd=tmp_path,timeout=15)
    assert completed.returncode==exit_code and completed.stderr==b''
    assert completed.stdout==(bundle.reconciliation.serialize_result(expected)+'\n').encode('ascii')
    assert not json.loads(completed.stdout)['origin_authenticated']
    changed=deepcopy(args)
    changed['inventory']['members'].reverse();changed['inventory']['expected_fixture_ids'].reverse();changed['references'].reverse()
    changed['member_bytes']=dict(reversed(list(changed['member_bytes'].items())))
    changed['expected_fixture_versions']=dict(reversed(list(changed['expected_fixture_versions'].items())))
    assert bundle.encode_replay_json(**changed)['text']==result['text']


def test_encoder_preserves_explicitly_mismatched_length_and_digest_without_hashing(monkeypatch):
    args=literal_encoder_case(b'\x00','0'*64)
    args['inventory']['members'][0]['byte_length']=9
    def forbidden(*args):raise AssertionError('encoder replaced an explicit supplied pin')
    monkeypatch.setattr(bundle,'sha256',forbidden)
    encoded=bundle.encode_replay_json(**args)
    assert encoded['ok']
    member=json.loads(encoded['text'])['inventory']['members'][0]
    assert member['byte_length']==9 and member['sha256']=='0'*64


@pytest.mark.parametrize('mutation,code',[('metadata','MEMBER_FIELDS_INVALID'),('name','MEMBER_NAME_INVALID'),
    ('payload','PAYLOAD_TYPE_INVALID'),('missing','ENCODER_MEMBER_COVERAGE_INVALID'),
    ('reference','REFERENCE_DANGLING'),('version','REFERENCE_DESIGN_VERSION_CONFLICT'),
    ('extra_reference','ENCODER_REFERENCE_FIELDS_INVALID')])
def test_encoder_failures_are_named_immutable_and_recover(mutation,code,monkeypatch):
    args=literal_encoder_case(*REFERENCE_VECTORS[1]);original=deepcopy(args)
    if mutation=='metadata':del args['inventory']['members'][0]['sha256']
    elif mutation=='name':args['inventory']['members'][0]['name']='../bad'
    elif mutation=='payload':args['member_bytes']['unit/saved.bin']=None
    elif mutation=='missing':args['member_bytes'].clear()
    elif mutation=='reference':args['references'][0]['name']='unit/foreign.bin'
    elif mutation=='version':args['references'][0]['design_version']='foreign'
    else:args['references'][0]['extra']='foreign'
    before=deepcopy(args)
    with monkeypatch.context() as patch:
        def forbidden(*args):raise AssertionError('invalid encoder allocated output')
        patch.setattr(bundle,'_encode_member_hex',forbidden);patch.setattr(bundle,'_dump_replay',forbidden)
        result=bundle.encode_replay_json(**args)
    assert not result['ok'] and code in codes(result) and result['text'] is None
    assert args==before and len(json.dumps(result))<4096
    args.clear();args.update(original)
    assert bundle.encode_replay_json(**args)['ok']


@pytest.mark.parametrize('bad',['text',[],{},bytearray(b'x')])
def test_encoder_rejects_nonbinary_payloads_without_consuming_or_mutating_them(bad):
    args=literal_encoder_case(*REFERENCE_VECTORS[1]);args['member_bytes']['unit/saved.bin']=bad
    result=bundle.encode_replay_json(**args)
    assert codes(result)=={'PAYLOAD_TYPE_INVALID':1} and args['member_bytes']['unit/saved.bin'] is bad


def test_literal_encoder_vectors_are_stable_across_explicit_hash_profiles(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    script='''import json,sys
sys.path.insert(0,sys.argv[1])
from benches import x05_bundle as b
d=b.decode_replay_json(sys.stdin.buffer.read().decode('ascii'))['envelope']
i=dict(members=d['members'],expected_fixture_ids=sorted(d['expected_fixture_versions']),required_stages=d['required_stages'])
r=b.encode_replay_json(i,d['references'],d['payloads'],expected_fixture_versions=d['expected_fixture_versions'],evidence_stage=d['evidence_stage'])
assert r['ok'] and not r['origin_authenticated']
sys.stdout.buffer.write(r['text'].encode('ascii'))
'''
    for (data,digest),raw_hex,length in [(REFERENCE_VECTORS[0],'',0),(REFERENCE_VECTORS[1],'00',1),(REFERENCE_VECTORS[2],'ff',1)]:
        expected=ENCODER_WIRE_VECTOR.replace('$length',str(length)).replace('$digest',digest).replace('$hex',raw_hex)
        raw=dict(reversed(list(json.loads(expected).items())))
        raw['inventory']['members'][0]=dict(reversed(list(raw['inventory']['members'][0].items())))
        for seed in ('0','19'):
            env={'PYTHONHASHSEED':seed,'PYTHONUTF8':'1','PYTHONIOENCODING':'ascii:strict'}
            if os.name=='nt':env['SystemRoot']='C:\\Windows'
            completed=subprocess.run([sys.executable,'-P','-B','-c',script,str(Path(bundle.__file__).resolve().parents[1])],
                input=json.dumps(raw).encode('ascii'),capture_output=True,cwd=tmp_path,env=env,timeout=15)
            assert completed.returncode==0 and completed.stderr==b'' and completed.stdout==expected.encode('ascii')
