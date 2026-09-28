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
import export_fidelity as fidelity  # noqa: E402
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


# T1 through T8: source bytes, polyline count, insert count, real and unkeyed handles.
_TRACKED_CASES = (
    ("web/public/sample.dxf", 424391, 2345, 0, 2345, 0),
    ("web/e2e/fixtures/block-fixture.dxf", 280, 0, 1, 1, 0),
    ("web/e2e/fixtures/distinctive-panel.dxf", 146, 1, 0, 1, 0),
    ("vendor/acadrust-worker/fixtures/one_line.dxf", 140, 1, 0, 0, 1),
    ("engine/corpus/01_closed_lwpolyline_single_layer.dxf", 132, 1, 0, 1, 0),
    ("engine/corpus/02_open_lwpolyline_two_layers.dxf", 215, 2, 0, 2, 0),
    ("engine/corpus/03_classic_polyline_vertex_seqend.dxf", 170, 1, 0, 1, 0),
    ("engine/corpus/04_empty_entities_section.dxf", 36, 0, 0, 0, 0),
)


def _tracked_counts(polylines=0, inserts=0):
    return {
        "polylines": polylines, "circles": 0, "arcs": 0, "texts": 0,
        "dimensions": 0, "inserts": inserts, "mleaders": 0,
    }


def _tracked_expected(case, adapter="identity", **changes):
    drawing, size, polylines, inserts, handles, unkeyed = case
    expected = {
        "drawing": drawing, "adapter": adapter, "ok": True,
        "bytes_before": size, "bytes_after": size, "byte_identical": True,
        "entities_before": _tracked_counts(polylines, inserts),
        "entities_after": _tracked_counts(polylines, inserts),
        "source_handles": handles, "handles_preserved": handles,
        "handles_missing": [], "handles_changed": [],
        "unkeyed_before": unkeyed, "unkeyed_matched": unkeyed,
        "blocks_match": True,
        "block_records": 3 if drawing == "web/e2e/fixtures/block-fixture.dxf" else 0,
        "block_records_preserved": 3 if drawing == "web/e2e/fixtures/block-fixture.dxf" else 0,
        "block_records_missing": [], "block_records_changed": [], "layers_match": True,
        "source_untouched": True, "error": None,
    }
    expected.update(changes)
    return expected


class _TrackedTransform(harness.EngineAdapter):
    name = "tracked-transform"

    def __init__(self, old: bytes, new: bytes):
        self.old, self.new = old, new

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        assert self.old in dxf_bytes
        return dxf_bytes.replace(self.old, self.new, 1)


def _assert_tracked_transform(case, old, new, **changes):
    adapter = _TrackedTransform(old, new)
    receipt = fidelity.run_drawing(adapter, case[0])
    assert {k: v for k, v in receipt.items() if k != "timing_ms"} == _tracked_expected(
        case, adapter.name, byte_identical=False, **changes,
    )


def test_tracked_drawings_cover_every_dxf_in_the_tracked_dirs():
    assert fidelity.untracked_drawings() == []
    assert all(path.is_file() for path in fidelity.tracked_paths())
    assert len(fidelity.TRACKED_DRAWINGS) == 8


def test_identity_no_change_round_trip_preserves_every_tracked_drawing():
    receipts = fidelity.run_tracked(harness.IdentityAdapter())
    assert len(receipts) == len(_TRACKED_CASES)
    for receipt, case in zip(receipts, _TRACKED_CASES):
        assert {k: v for k, v in receipt.items() if k != "timing_ms"} == _tracked_expected(case)


def test_tracked_receipt_shape_is_frozen():
    assert fidelity.TRACKED_RECEIPT_KEYS == (
        "drawing", "adapter", "ok", "timing_ms", "bytes_before", "bytes_after",
        "byte_identical", "entities_before", "entities_after", "source_handles",
        "handles_preserved", "handles_missing", "handles_changed", "unkeyed_before",
        "unkeyed_matched", "blocks_match", "block_records", "block_records_preserved",
        "block_records_missing", "block_records_changed", "layers_match",
        "source_untouched", "error",
    )
    for receipt in fidelity.run_tracked(harness.IdentityAdapter()):
        assert tuple(receipt) == fidelity.TRACKED_RECEIPT_KEYS
        assert tuple(receipt["entities_before"]) == fidelity.ENTITY_KINDS


def test_a_renamed_handle_is_reported_missing():
    _assert_tracked_transform(
        _TRACKED_CASES[2], b"5\nABCD\n", b"5\nABCE\n",
        ok=False, handles_preserved=0, handles_missing=["ABCD"],
    )


def test_a_moved_vertex_is_reported_changed():
    _assert_tracked_transform(
        _TRACKED_CASES[2], b"10\n111.25\n", b"10\n111.5\n",
        bytes_after=145, ok=False, handles_preserved=0, handles_changed=["ABCD"],
    )


def test_an_engine_assigned_handle_on_a_handle_less_entity_is_not_a_loss():
    _assert_tracked_transform(
        _TRACKED_CASES[3], b"0\nLINE\n", b"0\nLINE\n5\n1F\n", bytes_after=145,
    )


def test_a_normalizing_writer_that_keeps_every_handle_passes():
    prefix = (
        b"0\nSECTION\n2\nTABLES\n0\nTABLE\n2\nLAYER\n0\nLAYER\n2\n0\n"
        b"0\nLAYER\n2\nPanels\n0\nENDTAB\n0\nENDSEC\n"
    )
    _assert_tracked_transform(_TRACKED_CASES[2], b"", prefix, bytes_after=228)


def test_a_dropped_insert_fails_the_block_fixture():
    _assert_tracked_transform(
        _TRACKED_CASES[1], b"0\nINSERT\n5\n500\n8\n0\n2\nFixture\n10\n10\n20\n20\n30\n0\n", b"",
        bytes_after=234, entities_after=_tracked_counts(), handles_preserved=0,
        handles_missing=["500"], layers_match=False, ok=False,
    )


def test_a_tracked_adapter_failure_folds_into_the_receipt():
    class _TrackedFailure(harness.EngineAdapter):
        name = "tracked-failure"

        def round_trip(self, dxf_bytes: bytes) -> bytes:
            raise RuntimeError("boom")

    adapter = _TrackedFailure()
    receipt = fidelity.run_drawing(adapter, _TRACKED_CASES[2][0])
    null_fields = {key: None for key in fidelity.TRACKED_RECEIPT_KEYS[5:-2]}
    assert {k: v for k, v in receipt.items() if k != "timing_ms"} == _tracked_expected(
        _TRACKED_CASES[2], adapter.name, ok=False, error="RuntimeError: boom", **null_fields,
    )


def test_export_fidelity_cli_exits_zero_with_identity_adapter(capsys):
    assert fidelity.main(["--adapter=identity"]) == 0
    assert "# RESULT: ALL DRAWINGS OK (8 drawings, adapter='identity')" in capsys.readouterr().out


def test_a_block_child_coordinate_change_fails_the_block_fixture():
    receipt = fidelity.run_drawing(
        _TrackedTransform(b"11\n4\n", b"11\n4.0004\n"), _TRACKED_CASES[1][0],
    )
    assert receipt["error"] is None
    assert receipt["ok"] is False
    assert receipt["block_records_changed"] == ["100"]
    assert receipt["block_records_preserved"] == 2


def test_a_renamed_block_child_handle_is_reported_missing():
    receipt = fidelity.run_drawing(
        _TrackedTransform(b"5\n100\n", b"5\n101\n"), _TRACKED_CASES[1][0],
    )
    assert receipt["error"] is None
    assert receipt["ok"] is False
    assert receipt["block_records_missing"] == ["100"]
    assert receipt["block_records_preserved"] == 2


def test_blocks_match_detects_a_damaged_block_child():
    receipt = fidelity.run_drawing(
        _TrackedTransform(b"11\n4\n", b"11\n9\n"), _TRACKED_CASES[1][0],
    )
    assert receipt["error"] is None
    assert receipt["blocks_match"] is False
    assert receipt["ok"] is False


def test_a_normalizing_writer_keeps_every_block_record():
    drawing = _TRACKED_CASES[1][0]
    original = (PROJECT_ROOT / drawing).read_bytes()
    start = b"0\nSECTION\n2\nBLOCKS\n"
    old_section = start + original.split(start, 1)[1].split(b"0\nENDSEC\n", 1)[0]
    normalized = (
        start
        + b"0\nBLOCK\n5\n40\n330\nA0\n100\nAcDbEntity\n8\n0\n"
        b"100\nAcDbBlockBegin\n2\nFixture\n70\n0\n10\n1.0\n20\n2.0\n30\n0.0\n"
        b"0\nLINE\n5\n100\n330\n40\n100\nAcDbEntity\n8\n0\n6\nByLayer\n"
        b"100\nAcDbLine\n10\n1.0\n20\n2.0\n30\n0.0\n11\n4.0\n21\n2.0\n31\n0.0\n"
        b"0\nENDBLK\n5\n41\n330\nA0\n100\nAcDbEntity\n8\n0\n"
        b"100\nAcDbBlockEnd\n30\n0.0\n"
        b"0\nBLOCK\n5\nA1\n330\nA0\n100\nAcDbEntity\n8\n0\n"
        b"100\nAcDbBlockBegin\n2\n*Model_Space\n70\n0\n10\n0.0\n20\n0.0\n30\n0.0\n"
        b"0\nENDBLK\n5\nA2\n330\nA0\n100\nAcDbEntity\n8\n0\n100\nAcDbBlockEnd\n"
    )
    receipt = fidelity.run_drawing(_TrackedTransform(old_section, normalized), drawing)
    assert receipt["error"] is None
    assert receipt["ok"] is True
    assert receipt["byte_identical"] is False
    assert receipt["blocks_match"] is True
    assert receipt["block_records"] == receipt["block_records_preserved"] == 3
    assert receipt["block_records_missing"] == receipt["block_records_changed"] == []
