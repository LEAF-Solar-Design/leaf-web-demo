"""Trusted Python startup capture; installed only through the CI scratch directory.

Audit hooks observe Python reads. They do not prove native or descendant coverage.
Isolated interpreters ignore this bootstrap and have no completion shard.
"""

import atexit
import os
from pathlib import Path
import sys


def _install():
    root = os.environ.get("LEAF_READSET_ROOT")
    suite = os.environ.get("LEAF_READSET_SUITE")
    output = os.environ.get("LEAF_READSET_DIR")
    if not (root and suite and output):
        return
    if os.environ.get("LEAF_PROCESS_CAPTURE") == "1":
        # S15a: readsets/ holds only the supervisor's *-process.json shards; the Python read sets
        # become diagnostics beside it, content unchanged.
        output = str(Path(output).parent / "diagnostics" / "readsets")
    # This file and trace_reads.py are extracted from the same frozen commit.
    import trace_reads

    capture = trace_reads.Capture(root).install()
    capture.snapshot_modules()
    attempt = int(os.environ.get("LEAF_READSET_ATTEMPT", "1"))
    worker = trace_reads.encoded_suite(os.environ.get("PYTEST_XDIST_WORKER", "main"))
    destination = (Path(output) / trace_reads.encoded_suite(suite) / str(attempt) /
                   (worker + "-" + str(os.getpid()) + ".json"))
    previous_hook = sys.excepthook

    def exception_hook(kind, value, traceback):
        capture.mark_incomplete("unhandled_exception")
        previous_hook(kind, value, traceback)

    sys.excepthook = exception_hook

    def finish():
        try:
            capture.snapshot_modules()
            trace_reads.write_readset(
                destination, capture=capture, suite_id=suite, complete=True,
                run_id=os.environ.get("LEAF_READSET_RUN"),
                source_sha=os.environ.get("LEAF_READSET_SOURCE_SHA"),
                source_tree=os.environ.get("LEAF_READSET_SOURCE_TREE"),
                capture_sha=os.environ.get("LEAF_READSET_CAPTURE_SHA"),
                catalog_sha256=os.environ.get("LEAF_READSET_CATALOG_SHA256"),
                attempt=attempt, worker=worker, python_only=False,
                outcomes_ref=os.environ.get("LEAF_TEST_REPORT_DIR"))
        except Exception:
            # A missing shard is incomplete evidence, never a replacement verdict.
            print("WARNING: leaf readset capture incomplete", file=sys.stderr)
        finally:
            capture.close()

    atexit.register(finish)


try:
    _install()
except Exception:
    print("WARNING: leaf readset bootstrap unavailable", file=sys.stderr)
