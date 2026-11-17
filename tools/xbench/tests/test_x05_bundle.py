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


def test_missing_inventory_stage_preserves_verified_siblings():
    members, payloads, _ = synthetic_bundle()
    del payloads[members[0]["name"]]
    result = audit(members[1:], payloads)
    assert codes(result) == {"FIXTURE_STAGE_MISSING": 1}
    assert all(m["bytes_equal"] for m in result["members"])


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
    assert result["diagnostic_total"] == 70
    assert sum(codes(result).values()) == 70
    assert all(not m["bytes_equal"] for m in result["members"])
    # Fourteen fixture and stage errors, fourteen bad hashes, fourteen absent
    # expected slots, and fourteen byte hash mismatches are counted separately.
