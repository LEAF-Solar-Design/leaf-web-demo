"""No tracked file may carry a presigned URL's credential material.

An APS Design Automation WorkItem's `reportUrl` is a presigned S3 URL whose
query string embeds a temporary AWS credential valid for ONE HOUR. Two receipts
(`data/arx_probe_receipt.json`, `data/write_spike_receipt.json`) were committed
with three each; GitHub secret scanning opened alerts #2-#7 the moment
LEAF-Solar-Design/leaf-web-demo went public on 2026-08-24. Those particular
tokens had expired 37 days earlier and pointed at Autodesk's own bucket, not a
Leaf-owned account, so nothing was exposed. The PATTERN is the defect: a
process that writes a receipt and commits it inside the hour publishes a LIVE
credential.

`da/redact.py` closes it at write time. This test is the regression fence: it
scans what git actually tracks, so a receipt (or a log dump, or a fixture)
committed by a future lane fails HERE instead of in a public secret-scanning
alert. Shape follows the tracked-tree scan in
`test_platform_customize.py::test_every_tracked_repo_path_passes_the_charset`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DA_DIR = REPO_ROOT / "da"
if str(DA_DIR) not in sys.path:
    sys.path.insert(0, str(DA_DIR))

import redact  # noqa: E402

# Credential-bearing query parameters of the AWS SigV4 presigned scheme.
# ASSEMBLED from `_Q` rather than written literally so this file does not match
# its own ban -- which is what lets the fence run with NO allowlist.
_Q = "X-Amz-"
BANNED_PARAMS = ("Signature", "Credential", "Security-Token")
BANNED_MARKERS = tuple(f"{_Q}{name}=" for name in BANNED_PARAMS)

# The fence bans the RISK, not the string. Two rules, because the string alone
# is not the risk: the repo legitimately carries test fixtures that assert a
# redactor removed these params (da/test_blank_spike.py uses
# "Signature=DEADBEEF", test_on_submitted_hook.py "Credential=must-not-leak").
# Banning the bare marker everywhere would fail on those and teach the next
# author to add an allowlist entry, which is how a fence like this rots.
#
# RULE 1 -- any tracked file: the marker followed by CREDENTIAL-SHAPED material.
#   SigV4 signatures are 64 lowercase hex; access-key ids are ASIA/AKIA + 16
#   uppercase alnum; session tokens are long base64. Every one of the six
#   values that opened alerts #2-#7 matches; every fixture above does not.
CREDENTIAL_SHAPED = (
    rf"{_Q}Signature=[0-9a-f]{{64}}",
    rf"{_Q}Credential=(%2F|/)?(ASIA|AKIA)[A-Z0-9]{{16}}",
    rf"{_Q}Security-Token=[A-Za-z0-9%2BF/+=_-]{{100,}}",
)
# RULE 2 -- data artifacts: the bare marker, no exceptions. A receipt, a JSON
# blob, or a captured log has no legitimate reason to hold a placeholder
# presigned URL, and these are the files a run actually writes and commits.
DATA_PATHSPECS = ("data/", "*.json", "*.jsonl", "*.log", "*.txt", "*.har")

# A string that IS present in the tracked tree, used to prove the scan actually
# reaches files (see test_git_grep_is_actually_scanning_this_tree). Written by
# da/redact.report_url_fields into every redacted receipt.
PRESENT_SENTINEL = "reportUrl_redacted"

def _git_grep(pattern: str, *, fixed: bool = True, pathspecs=()) -> list[str]:
    """Tracked files matching `pattern`, as repo-relative paths.

    Fails closed: git grep exits 0 with matches, 1 with none, and >1 on error.
    An error is raised, never silently read as "clean".
    """
    cmd = ["git", "grep", "--fixed-strings" if fixed else "--extended-regexp",
           "--ignore-case", "-I", "--files-with-matches", "-e", pattern, "--",
           *(pathspecs or ())]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        raise RuntimeError(
            f"git grep failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return [p for p in proc.stdout.decode("utf-8", "replace").splitlines() if p]


def test_git_grep_is_actually_scanning_this_tree():
    """Guard the guard: a string that IS in the tree must be found, so a broken
    or mis-rooted invocation cannot pass the bans below by finding nothing
    anywhere and reading that silence as clean."""
    found = _git_grep(PRESENT_SENTINEL)
    assert "data/arx_probe_receipt.json" in found, found
    assert "data/write_spike_receipt.json" in found, found


@pytest.mark.parametrize("pattern", CREDENTIAL_SHAPED)
def test_no_tracked_file_carries_real_credential_material(pattern):
    """RULE 1: nowhere in the tree, in any file type."""
    offenders = sorted(_git_grep(pattern, fixed=False))
    assert offenders == [], (
        f"{pattern!r} matched tracked file(s) {offenders}. That is a LIVE AWS "
        f"credential shape, not a placeholder. Route the value through "
        f"da/redact.py (report_url_fields for receipts, redact_url/redact_text "
        f"for logs and error strings) at WRITE time. Do not add an exception."
    )


@pytest.mark.parametrize("marker", BANNED_MARKERS)
def test_no_data_artifact_carries_a_presigned_marker(marker):
    """RULE 2: not even a placeholder, in anything a run writes and commits."""
    offenders = sorted(_git_grep(marker, pathspecs=DATA_PATHSPECS))
    assert offenders == [], (
        f"{marker!r} found in data artifact(s) {offenders}. A committed receipt "
        f"or log must never carry a presigned query string at all -- that is the "
        f"exact defect that opened alerts #2-#7. Redact at write time."
    )


def test_the_two_known_receipts_stay_redacted():
    """The specific files the alerts fired on, pinned by shape rather than by
    the generic scan, so a partial revert names itself."""
    for rel in ("data/arx_probe_receipt.json", "data/write_spike_receipt.json"):
        import json
        doc = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
        blocks = [v for v in doc.values()
                  if isinstance(v, dict) and "reportUrl_path" in v]
        assert len(blocks) == 3, f"{rel}: expected 3 WorkItem report blocks, got {len(blocks)}"
        for block in blocks:
            assert block["reportUrl_redacted"] is True, rel
            assert "?" not in block["reportUrl_path"], rel
            assert block["reportUrl_path"].startswith("https://"), rel
            assert "reportUrl" not in block, f"{rel}: raw reportUrl key came back"


def test_the_receipt_writers_cannot_emit_a_raw_report_url():
    """report_url_fields is the only receipt shape, and it never returns the
    raw key — so a `**report_url_fields(status)` splat cannot reintroduce it."""
    live = ("https://dasprod-store.s3.us-east-1.amazonaws.com/workItem/app/wi/report"
            f"?{_Q}Expires=3600&{_Q}Credential=ASIAEXAMPLE%2F20260718%2Fus-east-1"
            f"%2Fs3%2Faws4_request&{_Q}Signature=deadbeef")
    fields = redact.report_url_fields({"reportUrl": live})
    assert fields == {
        "reportUrl_path": "https://dasprod-store.s3.us-east-1.amazonaws.com/workItem/app/wi/report",
        "reportUrl_redacted": True,
    }
    assert redact.report_url_fields({}) == {}
    assert redact.report_url_fields({"reportUrl": None}) == {}
    for marker in BANNED_MARKERS:
        assert marker not in str(fields)
