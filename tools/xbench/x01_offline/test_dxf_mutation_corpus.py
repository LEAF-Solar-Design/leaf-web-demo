"""Fixed, capped synthetic DXF corpus; parsing is isolated behind a timeout.

Duplicate handles are accepted occurrence identity in this offline checker,
not a global DXF uniqueness check. No customer drawings are consulted.
"""

import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

try:
    from . import check_plot_layers as layers
except ImportError:  # Direct execution and discovery from this directory.
    import check_plot_layers as layers


SEED = 0x580104
MAX_CASES = 64
MAX_CASE_BYTES = 4096
TIMEOUT_SECONDS = 20


def corpus():
    rng = random.Random(SEED)
    cases = []
    for index in range(8):
        leaf, root, inner = [f"{rng.randrange(0x1000, 0xFFFF):X}{suffix}"
                             for suffix in ("A", "B", "C")]
        block = f"B{index}"
        base = layers.fixture(entities=[layers.entity(leaf)])

        def add(name, data, verdict="UNREADABLE", diagnostics=(), inspected=0):
            cases.append((f"{index:02d}/{name}", data, dict(
                schema=layers.SCHEMA, verdict=verdict,
                inspected_occurrences=inspected, diagnostics=list(diagnostics))))

        add("valid", base, "PASS", inspected=1)
        # Remove only the value line of the final group pair.
        add("truncated_group_pair", base[:-4], diagnostics=[
            layers.diagnostic("malformed_group_pair")])
        add("unclosed_entities_section",
            base.replace(b"0\nENDSEC\n0\nEOF\n", b"0\nEOF\n"), diagnostics=[
                layers.diagnostic("unclosed_section", section="ENTITIES")])
        add("duplicate_section", base.replace(b"2\nBLOCKS\n", b"2\nTABLES\n"),
            diagnostics=[layers.diagnostic("duplicate_section", section="TABLES")])
        add("invalid_envelope", base.replace(b"0\nEOF\n", b"0\nENDSEC\n"),
            diagnostics=[layers.diagnostic("invalid_dxf_envelope")])

        # Deliberately identical handles: both leaves count, with no invented
        # duplicate_handle diagnostic or uniqueness requirement.
        add("duplicate_handles_accepted", layers.fixture(entities=[
            layers.entity(leaf), layers.entity(leaf)]), "PASS", inspected=2)

        insert = layers.entity(root, "MODULES", "INSERT", [(2, block)])
        missing = f"ABSENT{index}"
        add("negative_missing_insert_reference", layers.fixture(
            entities=[insert], blocks=[(block, [layers.entity(
                inner, "0", "INSERT", [(2, missing)])])]), diagnostics=[
                    layers.diagnostic("missing_block", handle=inner,
                                      path=[root, inner], layer="MODULES",
                                      block=missing.casefold())])
        add("negative_cyclic_insert_reference", layers.fixture(
            entities=[insert], blocks=[(block, [layers.entity(
                inner, "0", "INSERT", [(2, block)])])]), diagnostics=[
                    layers.diagnostic("cyclic_block", handle=inner,
                                      path=[root, inner], layer="MODULES",
                                      block=block.casefold())])
    return cases


def exercise_corpus(scratch):
    """Called only by the bounded child, including both file and bytes APIs."""
    check = unittest.TestCase()
    cases = corpus()
    check.assertEqual(cases, corpus())
    check.assertEqual(len(cases), MAX_CASES)
    check.assertEqual(len({name for name, _, _ in cases}), len(cases))
    snapshots = []
    # Keep all child scratch under the parent's cleanup boundary, including
    # when a parser hangs and subprocess.run kills the child on timeout.
    with tempfile.TemporaryDirectory(prefix="synthetic mutations ", dir=scratch) as directory:
        for repetition in range(2):
            results = []
            for index, (name, data, expected) in enumerate(cases):
                check.assertLessEqual(len(data), MAX_CASE_BYTES, name)
                path = Path(directory) / f"synthetic-{index:02d}.dxf"
                path.write_bytes(data)
                digest = hashlib.sha256(data).hexdigest()
                by_bytes = layers.check_bytes(data)
                by_file = layers.check_file(path)
                check.assertEqual(by_bytes, expected, name)
                check.assertEqual(by_file, expected, name)
                check.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, name)
                results.append(dict(case=name, sha256=digest, result=by_file))
            snapshots.append(results)
        check.assertEqual(snapshots[0], snapshots[1])
    return dict(evidence_kind="synthetic_offline", live_grade=False,
                seed=SEED, repetitions=2, cases=snapshots[0])


class DxfMutationCorpusTests(unittest.TestCase):
    def test_frozen_corpus_in_bounded_subprocess(self):
        with tempfile.TemporaryDirectory(prefix="x01 corpus parent ") as directory:
            child = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()),
                 "--corpus-worker", directory],
                cwd=directory, capture_output=True, timeout=TIMEOUT_SECONDS,
                check=False)
        self.assertEqual(child.returncode, 0,
                         child.stderr.decode("utf-8", errors="replace"))
        evidence = json.loads(child.stdout)
        self.assertEqual(evidence["evidence_kind"], "synthetic_offline")
        self.assertIs(evidence["live_grade"], False)
        self.assertEqual(evidence["seed"], SEED)
        self.assertEqual(evidence["repetitions"], 2)
        self.assertEqual(len(evidence["cases"]), MAX_CASES)
        # Retain the named synthetic evidence in the verification transcript.
        print(json.dumps(evidence, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--corpus-worker":
        print(json.dumps(exercise_corpus(sys.argv[2]), sort_keys=True, allow_nan=False))
    else:
        unittest.main(verbosity=2)
