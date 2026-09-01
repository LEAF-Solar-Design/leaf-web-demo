"""da/test_hardening_1f.py — F13 parity gate for the ONE shared tenant-id rule.

Proves the three id validators that used to DISAGREE now AGREE, and that the
cross-tenant COLLISION they caused is gone:

  * server/tenant_id_validator.is_valid_tenant_id   (the canonical rule)
  * da/store.sanitize_id                            (reject-or-passthrough)
  * server/tenant_paths._safe_component             (None-or-passthrough)
  * harness safeBase (TS)                           (regex mirrored, checked by source)

Offline / zero-network (no APS, no server). Run:
    cd C:/tmp/leaf-web-demo/da && python -m pytest test_hardening_1f.py -q
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)                                   # da/
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "server"))  # server/

import store  # noqa: E402
import tenant_id_validator as tid  # noqa: E402
import tenant_paths  # noqa: E402


# --------------------------------------------------------------------------- #
# Corpus: (id, expected_valid). VALID ids MUST be accepted by all three call
# sites; INVALID ids MUST be rejected by all three.
# --------------------------------------------------------------------------- #
VALID = [
    "demo-tenant",       # the demo tenant (must stay valid)
    "org_leaf_demo",     # real Auth0 tenant id (data/tenants.sample.json) — underscores
    "org_acme_solar",    # real Auth0 tenant id — underscores are load-bearing
    "t1", "t2", "acme", "other", "a", "x", "0abc",
    "acme-corp",         # hyphen form
    "acme_corp",         # underscore form — DISTINCT from acme-corp (no longer collides)
    "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",  # a UUIDv7 drawing id
    "a" * 63,            # max length
]

INVALID = [
    "",                  # empty
    "   ",               # whitespace
    "Acme Corp",         # space + uppercase (historically collapsed to acme-corp)
    "Acme-Corp",         # uppercase
    "ACME",              # uppercase
    "acme.corp",         # dot
    "acme corp",         # space
    "-leading",          # must start alphanumeric
    "_leading",          # must start alphanumeric
    "a" * 64,            # over max length
    "../etc",            # traversal
    "a/b", "a\\b",       # separators
    "café",         # unicode
    "..", ".",
]


def _verdicts(s):
    """The accept/reject verdict of each of the three Python call sites for `s`."""
    v_validator = tid.is_valid_tenant_id(s)
    try:
        store.sanitize_id(s)
        v_store = True
    except ValueError:
        v_store = False
    v_paths = tenant_paths._safe_component(s) is not None
    return v_validator, v_store, v_paths


@pytest.mark.parametrize("s", VALID)
def test_valid_ids_accepted_by_all_three(s):
    v_validator, v_store, v_paths = _verdicts(s)
    assert v_validator is True, f"validator rejected legal id {s!r}"
    assert v_store is True, f"store.sanitize_id rejected legal id {s!r}"
    assert v_paths is True, f"tenant_paths._safe_component rejected legal id {s!r}"
    # passthrough (no transform) — the id is returned UNCHANGED where it is returned.
    assert store.sanitize_id(s) == s
    assert tid.validate_tenant_id(s) == s
    assert tenant_paths._safe_component(s) == s


@pytest.mark.parametrize("s", INVALID)
def test_invalid_ids_rejected_by_all_three(s):
    v_validator, v_store, v_paths = _verdicts(s)
    assert v_validator is False, f"validator accepted illegal id {s!r}"
    assert v_store is False, f"store.sanitize_id accepted illegal id {s!r}"
    assert v_paths is False, f"tenant_paths._safe_component accepted illegal id {s!r}"


def test_three_call_sites_agree_on_every_corpus_item():
    """The load-bearing parity property: for EVERY id the three verdicts are identical."""
    for s in VALID + INVALID:
        v_validator, v_store, v_paths = _verdicts(s)
        assert v_validator == v_store == v_paths, (
            f"validators disagree on {s!r}: "
            f"validator={v_validator} store={v_store} tenant_paths={v_paths}"
        )


# --------------------------------------------------------------------------- #
# The collision F13 closes.
# --------------------------------------------------------------------------- #
def test_underscore_and_hyphen_ids_no_longer_collide():
    """`acme_corp` and `acme-corp` are DISTINCT tenants -> DISTINCT store keys.

    Before F13, sanitize_id collapsed BOTH to `acme-corp`, so the two tenants shared
    one namespace (cross-tenant mingling). Reject-don't-collapse keeps them separate.
    """
    k_hyphen = store.drawing_version_key("acme-corp", "d", 1)
    k_under = store.drawing_version_key("acme_corp", "d", 1)
    assert k_hyphen != k_under
    assert k_hyphen == "tenants/acme-corp/drawings/d/v/00000001.dwg"
    assert k_under == "tenants/acme_corp/drawings/d/v/00000001.dwg"
    # manifests too (the checkout-lock / version-index object).
    assert store.manifest_key("acme-corp", "d") != store.manifest_key("acme_corp", "d")


def test_dirty_ids_that_used_to_collapse_are_now_rejected():
    """The dirty inputs that historically folded onto `acme-corp` are rejected, so they
    can never silently land in another tenant's namespace."""
    for dirty in ("Acme Corp", "Acme-Corp", "acme corp", "ACME_CORP"):
        with pytest.raises(ValueError):
            store.sanitize_id(dirty)
        assert tenant_paths._safe_component(dirty) is None
        assert tid.is_valid_tenant_id(dirty) is False


# --------------------------------------------------------------------------- #
# Cross-language parity: the TS safeBase mirrors the SAME canonical pattern.
# --------------------------------------------------------------------------- #
def test_ts_safebase_mirrors_the_canonical_pattern():
    """harness safeBase (oauthGrantProvider.ts) must carry the EXACT canonical regex.

    Tying the Python pattern to the TS source string means a change to
    TENANT_ID_PATTERN that is not mirrored in the TS file fails this test.
    """
    # The module became a strangler shim on 2026-08-06 (mushy-code
    # extraction); the ACTIVE regex lives in the vendored implementation the
    # shim re-exports. Pin the vendored source — pinning the shim pinned
    # nothing, and this test sat red from the move until 2026-08-31.
    ts_path = os.path.join(_PROJECT_ROOT, "harness", "src", "vendor",
                           "mushy-author", "ports", "impl",
                           "oauthGrantProvider.ts")
    src = open(ts_path, encoding="utf-8").read()
    expected_literal = "/" + tid.TENANT_ID_PATTERN + "/"  # /^[a-z0-9][a-z0-9_-]{0,62}$/
    assert expected_literal in src, (
        f"TS safeBase does not mirror the canonical rule {expected_literal!r} "
        "(server/tenant_id_validator.TENANT_ID_PATTERN drifted from the TS regex)"
    )
    # and the OLD permissive regex LITERAL is gone (the prose comment that documents
    # the change may still mention it in backticks — we only forbid the active /.../ form).
    assert "/^[A-Za-z0-9._-]+$/" not in src
