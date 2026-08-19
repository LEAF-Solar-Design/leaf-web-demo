"""Card ENG-CORPUS: DXF round-trip corpus harness (ENG1 slice).

Oracle (frozen, card ENG-CORPUS):
  - A committed corpus of small DXF fixtures (hand-authored, license-clean,
    no copied third-party files) plus a harness that round-trips each
    fixture through any engine adapter behind a stable interface and
    reports per-fixture byte/entity fidelity.
  - The harness runs with NO engine enabled: it proves the corpus +
    comparison machinery against the identity adapter (the existing python
    DXF path, server/dxf_intake.py) and defines the exact receipt shape an
    enabled engine must later produce (fidelity table + rollback assertion).
  - Bounded: corpus size capped and enumerated in docs/ENGINE-CORPUS.md;
    harness runtime bounded per fixture.
  - No flag, no production wiring, no selector; test-and-tooling only.

Drives the REAL engine/corpus_harness.py module against the REAL committed
engine/corpus/ fixtures and the REAL server/dxf_intake.py parser -- never a
reimplementation of either.

Run:  cd server && python -m pytest tests/test_engine_corpus_harness.py -q
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = PROJECT_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import corpus_harness as harness  # noqa: E402
import dxf_intake  # noqa: E402

# Bounded, enumerated corpus (mirrors docs/ENGINE-CORPUS.md's own list).
# Grown or shrunk in this file AND that doc together -- never one without
# the other, since the doc's enumeration is the "corpus size capped and
# enumerated in the doc" half of the oracle.
EXPECTED_FIXTURES = (
    "01_closed_lwpolyline_single_layer.dxf",
    "02_open_lwpolyline_two_layers.dxf",
    "03_classic_polyline_vertex_seqend.dxf",
    "04_empty_entities_section.dxf",
)


class _CorruptingAdapter(harness.EngineAdapter):
    """Drops the last entity's worth of bytes -- a real fidelity loss the
    entity-count check must catch."""

    name = "corrupting"

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        text = dxf_bytes.decode("utf-8")
        # Delete everything from the first entity's "0\nLWPOLYLINE"/"0\nPOLYLINE"
        # marker onward, up to (but not including) the terminal ENDSEC/EOF,
        # i.e. drop all entities while keeping the file structurally parseable.
        marker = "0\nENDSEC\n0\nEOF\n"
        assert marker in text
        head = "0\nSECTION\n2\nENTITIES\n"
        return (head + marker).encode("utf-8")


class _FailingAdapter(harness.EngineAdapter):
    """Simulates an engine that raises mid-round-trip. Exists to prove the
    rollback assertion holds even when the adapter never returns bytes."""

    name = "failing"

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        raise RuntimeError("simulated engine failure")


def _fixture_paths() -> List[Path]:
    return harness.discover_corpus()


def test_corpus_is_bounded_and_matches_the_documented_enumeration():
    names = tuple(p.name for p in _fixture_paths())
    assert names == EXPECTED_FIXTURES
    assert len(names) <= 8  # card's own "corpus size capped" bound


def test_every_fixture_is_license_clean_hand_authored_ascii_dxf():
    """No binary sentinel, no third-party CAD sample markers -- the license
    scanner's own concerns, proven directly on the corpus text."""
    for path in _fixture_paths():
        raw = path.read_bytes()
        assert not raw.startswith(dxf_intake.BINARY_SENTINEL)
        text = raw.decode("utf-8")
        assert "opencadstudio" not in text.lower()
        assert "GPL-3.0" not in text
        assert "acadrust" not in text.lower()


def test_every_fixture_parses_cleanly_via_the_real_existing_dxf_path():
    """Honesty check: the identity-adapter baseline requires every committed
    fixture to be readable by the REAL server/dxf_intake.py parser."""
    for path in _fixture_paths():
        intake = dxf_intake.parse_dxf_bytes(path.read_bytes(), source_name=path.name)
        assert isinstance(intake["layers"], list)
        assert isinstance(intake["polylines"], list)


def test_identity_adapter_round_trips_every_fixture_with_full_fidelity():
    """The core ENG1 proof: no engine enabled, identity adapter, full corpus,
    byte-identical output, matching entity/layer/vertex counts."""
    receipts = harness.run_corpus(harness.IdentityAdapter())
    assert len(receipts) == len(EXPECTED_FIXTURES)
    for receipt in receipts:
        assert receipt["adapter"] == "identity"
        assert receipt["ok"] is True
        assert receipt["error"] is None
        assert receipt["fidelity"]["byte_identical"] is True
        assert receipt["fidelity"]["entity_count_match"] is True
        assert receipt["fidelity"]["layers_match"] is True
        assert receipt["fidelity"]["vertex_count_match"] is True
        assert receipt["fidelity"]["score"] == 1.0


def test_receipt_shape_is_the_exact_fidelity_table_plus_rollback_assertion_schema():
    """Locks the receipt shape a future enabled engine must produce -- the
    schema-drift guard the card's acceptance oracle calls for."""
    receipt = harness.run_fixture(harness.IdentityAdapter(), _fixture_paths()[0])
    assert tuple(receipt.keys()) == harness.RECEIPT_KEYS
    assert tuple(receipt["fidelity"].keys()) == harness.FIDELITY_KEYS
    assert tuple(receipt["rollback"].keys()) == harness.ROLLBACK_KEYS


def test_harness_runtime_is_bounded_per_fixture():
    for receipt in harness.run_corpus(harness.IdentityAdapter()):
        assert receipt["timing_ms"] <= harness.FIXTURE_TIMEOUT_MS


def test_rollback_assertion_holds_even_when_the_adapter_never_returns_bytes():
    """Rollback proof shape: a failing/raising adapter still leaves the
    on-disk fixture provably untouched, and the receipt says so honestly
    (ok=False, error set) rather than papering over the failure."""
    path = _fixture_paths()[0]
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    receipt = harness.run_fixture(_FailingAdapter(), path)

    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after
    assert receipt["rollback"]["source_untouched"] is True
    assert receipt["rollback"]["source_sha256_before"] == before
    assert receipt["rollback"]["source_sha256_after"] == after
    assert receipt["ok"] is False
    assert receipt["error"] is not None
    assert receipt["fidelity"] is None


def test_fidelity_table_catches_a_non_identity_adapter_dropping_entities():
    """The comparison machinery is not vacuous: an adapter that actually
    loses entities must score below 1.0 and fail entity_count_match, on a
    fixture that has entities to lose."""
    path = next(p for p in _fixture_paths() if p.name == "01_closed_lwpolyline_single_layer.dxf")
    receipt = harness.run_fixture(_CorruptingAdapter(), path)

    assert receipt["error"] is None
    assert receipt["fidelity"]["entity_count_before"] == 1
    assert receipt["fidelity"]["entity_count_after"] == 0
    assert receipt["fidelity"]["entity_count_match"] is False
    assert receipt["fidelity"]["byte_identical"] is False
    assert receipt["fidelity"]["score"] < 1.0
    assert receipt["ok"] is False
    # The corrupting adapter still only ever saw bytes -- source on disk is
    # untouched regardless of how unfaithful its output was.
    assert receipt["rollback"]["source_untouched"] is True


def test_stable_adapter_interface_rejects_a_bare_engine_adapter():
    """Any engine adapter is driven behind the SAME `EngineAdapter.round_trip`
    interface -- the base class itself must refuse to be used directly."""
    with pytest.raises(NotImplementedError):
        harness.EngineAdapter().round_trip(b"irrelevant")


def test_main_cli_exits_zero_over_the_full_corpus_with_identity_adapter(capsys):
    exit_code = harness.main()
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ALL FIXTURES OK" in captured.out
