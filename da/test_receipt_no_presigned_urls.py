#!/usr/bin/env python3
"""Ban test: no committed file under data/ may carry a presigned QUERY STRING.

WHAT THIS GUARDS
----------------
APS returns a WorkItem `reportUrl` that is a PRESIGNED S3 url. Its path is
harmless; its QUERY carries a live temporary AWS credential
(`X-Amz-Credential` / `X-Amz-Signature` / `X-Amz-Security-Token`) good for
`X-Amz-Expires` seconds. The da/ drivers write receipts into data/, and those
receipts are COMMITTED, so a receipt generated and committed inside the
credential's lifetime publishes a working credentialed url.

`da/redact.py` and the write-time redaction in the drivers (#786) are the FIX.
This file is the GATE: nothing stops a future driver, a hand-edited receipt, or
a new data/ artifact from re-introducing the leak, and the fix cannot prove
itself.

WHY IT ASSERTS ON QUERY PARAMETERS, NOT URL SHAPE
-------------------------------------------------
A shape assertion ("the url must end `/report`" or "must look redacted") is
WORSE THAN NOTHING: it passes any url that does not match the redacted shape,
including a LIVE credentialed one, and it breaks the moment a driver emits a
legitimately different url. So the ban is stated over the thing that actually
leaks — the SigV4 presigned query parameters — and it is stated over the RAW
file text, so a credential hidden in a key, a nested string, an error message,
or a field nobody has invented yet is caught the same way a `reportUrl` is.

SCHEMA-AGNOSTIC ON PURPOSE
--------------------------
data/ currently carries TWO redacted shapes, both credential-free and both
legitimate: `reportUrl_path` + `reportUrl_redacted: true` (the canonical
`redact.report_url_fields()` output, in arx_probe/write_spike receipts) and a
single `reportUrl` reduced to its path (blank_spike). This ban does not care
which one a receipt uses, and must not: a ban coupled to one receipt schema
goes green the moment a driver picks a different field name.

Every check here is offline: no APS, no network, no credentials, no dollars.
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redact  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(_HERE, "..", "data"))

# The SigV4 presigned query parameters that carry (or scope) the credential.
# Stated here rather than imported because da/redact.py deliberately exports no
# such list: it strips the WHOLE query rather than enumerating parameters, so a
# leak scanner that reused its internals would only ever find what it already
# knows how to remove.
PRESIGNED_QUERY_PARAMS = (
    "X-Amz-Signature",
    "X-Amz-Credential",
    "X-Amz-Security-Token",
    "X-Amz-Expires",
)

# Receipts that have carried a presigned reportUrl at some point. Named
# explicitly so a path typo or a directory move cannot turn this whole suite
# into a green no-op that scans nothing.
KNOWN_RECEIPTS = (
    "arx_probe_receipt.json",
    "write_spike_receipt.json",
    "blank_spike_receipt.json",
)

# A real presigned url, structurally identical to what APS returns, with the
# credential material replaced by obvious dummies. This is the NEGATIVE
# CONTROL: the scanner must flag it, or the scanner proves nothing.
SAMPLE_PRESIGNED = (
    "https://dasprod-store.s3.us-east-1.amazonaws.com/workItem/OWNER/ID/report"
    "?X-Amz-Expires=3600"
    "&X-Amz-Security-Token=DUMMY-SESSION-TOKEN"
    "&X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=DUMMYACCESSKEYID%2F20260718%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260718T024910Z"
    "&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=dummysignature"
)


def _data_json_paths() -> list:
    """Every *.json under data/, recursively. Recursive on purpose: a receipt
    parked one directory deeper must not escape the ban."""
    found = []
    for root, _dirs, files in os.walk(DATA_DIR):
        for name in files:
            if name.lower().endswith(".json"):
                found.append(os.path.join(root, name))
    return sorted(found)


def _presigned_params_in(text: str) -> list:
    """The banned query parameters present in `text`, case-insensitively.

    Raw-text, not JSON-value, on purpose: this is a leak scanner, and a leak
    does not promise to arrive in the field we expected.
    """
    low = text.lower()
    return [p for p in PRESIGNED_QUERY_PARAMS if p.lower() in low]


# --------------------------------------------------------------------------- #
# Negative control — a ban test never validated against known-bad input is not
# evidence. These run FIRST and fail loudly if the scanner is a no-op.
# --------------------------------------------------------------------------- #
def test_scanner_flags_a_known_bad_presigned_url():
    hits = _presigned_params_in(SAMPLE_PRESIGNED)
    assert set(hits) == set(PRESIGNED_QUERY_PARAMS), (
        "the scanner failed to flag a real presigned url shape; every check "
        f"below is worthless. missed={set(PRESIGNED_QUERY_PARAMS) - set(hits)}"
    )


def test_scanner_flags_a_presigned_url_buried_in_a_receipt_body():
    """The exact regression this bans: a credentialed url inside a JSON blob,
    including one hiding in an error string rather than in `reportUrl`."""
    body = json.dumps({
        "spike": "drawing.write",
        "pass": False,
        "error": "RuntimeError: write WorkItem w1 failed report=" + SAMPLE_PRESIGNED,
    })
    assert _presigned_params_in(body), (
        "a presigned url inside an error string escaped the scanner"
    )


def test_scanner_is_not_fooled_by_a_shape_only_check():
    """States the reason this ban is written over query parameters: the live
    credentialed url and the redacted one BOTH end in `/report`, so any check
    on url shape passes the leak. Only the query separates them."""
    live = SAMPLE_PRESIGNED
    redacted = redact.redact_url(SAMPLE_PRESIGNED)
    assert live.split("?")[0] == redacted, "the two forms share a path, as claimed"
    assert _presigned_params_in(live) and not _presigned_params_in(redacted)


def test_the_canonical_redacted_form_satisfies_the_ban():
    """Redaction must actually satisfy the ban — otherwise the fix and the gate
    disagree and this can never go green. Covers BOTH shapes now in data/."""
    fields = redact.report_url_fields({"reportUrl": SAMPLE_PRESIGNED})
    assert fields["reportUrl_redacted"] is True
    assert not _presigned_params_in(json.dumps(fields))
    assert not _presigned_params_in(redact.redact_url(SAMPLE_PRESIGNED))
    assert not _presigned_params_in(
        redact.redact_text("report=" + SAMPLE_PRESIGNED))


# --------------------------------------------------------------------------- #
# Coverage guards — the ban must be scanning something real
# --------------------------------------------------------------------------- #
def test_data_dir_exists_and_holds_json():
    assert os.path.isdir(DATA_DIR), f"data dir not found at {DATA_DIR}"
    assert _data_json_paths(), f"no *.json found under {DATA_DIR}; this ban scans nothing"


@pytest.mark.parametrize("name", KNOWN_RECEIPTS)
def test_known_receipts_are_actually_scanned(name):
    scanned = {os.path.basename(p) for p in _data_json_paths()}
    assert name in scanned, (
        f"{name} is not among the files this ban scans ({sorted(scanned)}). "
        "A moved or renamed receipt must not silently drop out of the ban."
    )


# --------------------------------------------------------------------------- #
# The ban itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", _data_json_paths(),
                         ids=lambda p: os.path.relpath(p, DATA_DIR).replace("\\", "/"))
def test_committed_json_carries_no_presigned_query_parameter(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    hits = _presigned_params_in(text)
    assert not hits, (
        f"{os.path.relpath(path, DATA_DIR)} carries presigned query parameter(s) "
        f"{hits}. That is a live AWS credential in a committed file. Route the "
        "value through da/redact.report_url_fields() AT WRITE TIME in the "
        "generator that produced this file — do not hand-edit the receipt and "
        "leave the generator emitting credentials."
    )


@pytest.mark.parametrize("path", _data_json_paths(),
                         ids=lambda p: os.path.relpath(p, DATA_DIR).replace("\\", "/"))
def test_committed_json_urls_carry_no_query_credentials(path):
    """Structured second pass: parse the JSON and check every url-shaped string
    by its actual parsed query parameters. Catches a credential that survived
    the raw scan through unusual encoding, and names the exact field when it
    fires."""
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{os.path.relpath(path, DATA_DIR)} is not valid JSON: {exc}")

    offenders = []

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{where}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}/{i}")
        elif isinstance(node, str) and "://" in node and "?" in node:
            for chunk in node.split():
                if "://" not in chunk or "?" not in chunk:
                    continue
                keys = {k.lower() for k in parse_qs(urlparse(chunk).query)}
                banned = sorted(p for p in PRESIGNED_QUERY_PARAMS
                                if p.lower() in keys)
                if banned:
                    offenders.append((where, banned))

    walk(doc, "")
    assert not offenders, (
        f"{os.path.relpath(path, DATA_DIR)} carries presigned credentials in "
        f"url query strings: {offenders}"
    )
