"""Pin every inline CodeQL taint-barrier literal to its canonical rule.

The 2026-08-31 alert sweep rewrote the id/path guards that already existed
into the ONE shape static analysis proves (an inline LITERAL `re.fullmatch`
guard + `.group(0)` rebind — measured on PR #843's CodeQL run: the same
pattern behind a compiled constant or a helper predicate earns no barrier
credit). A literal restated in many files is a drift hazard, so this test is
the coupling: each restatement must equal the rule it restates, forever.

Source-text pins, like test_save_edited_version's route-literal pin: no
heavy imports, and a refactor that silently deletes a barrier fails loudly.
"""
from __future__ import annotations

import re
from pathlib import Path

import tenant_id_validator

REPO = Path(__file__).resolve().parents[2]

# The canonical tenant/opaque-id rule, without its anchors (fullmatch anchors
# implicitly): restated in store.sanitize_id, customization_service._tenant_id
# and tenant_paths._safe_component.
CANONICAL_CORE = tenant_id_validator.TENANT_ID_PATTERN.removeprefix("^").removesuffix("$")

# The broker's bare-drawing-name rule, restated in its two resolvers and in
# guest_uploads._safe_path_id (the two ends of the SAME staged filename).
BROKER_CORE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"


def _source(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _fullmatch_literals(source: str) -> list[str]:
    return re.findall(r're\.fullmatch\(\s*r"([^"]+)"', source)


def test_store_sanitize_id_literal_is_the_canonical_rule():
    literals = _fullmatch_literals(_source("da/store.py"))
    assert CANONICAL_CORE in literals, (
        "da/store.py sanitize_id lost its inline literal barrier "
        f"(expected {CANONICAL_CORE!r} among {literals!r})")


def test_customization_tenant_id_literal_is_the_canonical_rule():
    literals = _fullmatch_literals(_source("server/customization_service.py"))
    assert CANONICAL_CORE in literals, (
        "customization_service._tenant_id lost its inline literal barrier")


def test_tenant_paths_safe_component_literal_is_the_canonical_rule():
    literals = _fullmatch_literals(_source("server/tenant_paths.py"))
    assert CANONICAL_CORE in literals, (
        "tenant_paths._safe_component lost its inline literal barrier")


def test_broker_literals_equal_the_compiled_dwg_rule():
    source = _source("server/broker.py")
    compiled = re.search(r'_DWG_NAME_RE = re\.compile\(r"([^"]+)"\)', source)
    assert compiled, "broker._DWG_NAME_RE vanished"
    assert compiled.group(1) == BROKER_CORE + r"\Z", (
        "broker._DWG_NAME_RE drifted from the barrier literal rule")
    occurrences = [l for l in _fullmatch_literals(source) if l == BROKER_CORE]
    # _resolve_live_dwg: guard + rebind; _resolve_upload_dwg: two guards + two
    # rebinds. Fewer means a barrier was deleted.
    assert len(occurrences) >= 6, (
        f"broker resolver barrier literals dropped to {len(occurrences)} "
        "(expected 6: each guard AND each rebind is load-bearing)")


def test_guest_uploads_literals_match_the_broker_rule():
    source = _source("server/guest_uploads.py")
    assert BROKER_CORE in _fullmatch_literals(source), (
        "guest_uploads._safe_path_id lost its inline literal barrier")
    # staged_path's ext allowlist is written as a LITERAL tuple for barrier
    # credit; it must stay equal to ACCEPTED_EXTENSIONS.
    accepted = re.search(r'ACCEPTED_EXTENSIONS = \(([^)]+)\)', source)
    assert accepted, "guest_uploads.ACCEPTED_EXTENSIONS vanished"
    staged = re.search(r'def staged_path.*?return uploads_dir', source, re.DOTALL)
    assert staged, "guest_uploads.staged_path vanished"
    assert f"ext not in ({accepted.group(1)})" in staged.group(0), (
        "staged_path's literal ext allowlist drifted from ACCEPTED_EXTENSIONS")


def test_author_slug_literal_admits_only_slugify_output():
    source = _source("server/routers/author.py")
    literal = next((l for l in _fullmatch_literals(source) if l.startswith("[a-z0-9]")), None)
    assert literal == r"[a-z0-9][a-z0-9-]{0,127}", (
        "author.py lost the inline literal barrier on the authored tool "
        f"filename (found {literal!r})")


def test_approval_path_literal_restates_the_alnum_collapse():
    literals = _fullmatch_literals(_source("server/agent_gate.py"))
    assert r"[A-Za-z0-9]{0,64}" in literals, (
        "agent_gate._approval_path lost its inline literal barrier")


def test_deps_surface_config_root_literal_is_the_canonical_rule():
    literals = _fullmatch_literals(_source("server/deps.py"))
    assert CANONICAL_CORE in literals, (
        "deps._contained_tenant_root lost its inline literal barrier")
