#!/usr/bin/env python3
"""da/redact.py - the ONE presigned-URL redactor every committed receipt uses.

WHY THIS MODULE EXISTS
----------------------
APS hands back a WorkItem `reportUrl` that is a PRESIGNED S3 url: the object
path is harmless, but the query string carries a live temporary AWS credential
(`X-Amz-Credential`, `X-Amz-Signature`, `X-Amz-Security-Token`) that is valid
for `X-Amz-Expires` seconds. Several da/ drivers write that value straight into
a JSON receipt under `data/`, and those receipts are COMMITTED. This repository
is PUBLIC as of 2026-08-24, so a receipt generated and committed inside the
credential's lifetime publishes a working credentialed url.

`redact_report_url` was written for da/blank_spike.py (PR #778) and proven by
data/blank_spike_receipt.json, which is on public main carrying zero
`X-Amz-*` parameters. It lives here now so da/arx_probe.py and da/write_spike.py
reuse the SAME redaction instead of growing a second, subtly different one.
da/blank_spike.py re-exports it, so its public surface and receipt shape are
unchanged.

CONTRACT: drop the WHOLE query string, keep the object path. Redaction happens
at WRITE TIME, before the value can reach a receipt - never as a later cleanup
pass, because a cleanup pass that is forgotten once publishes a credential.
"""
from __future__ import annotations

# The query parameters that make a url credential-bearing. This is the ban list
# da/test_receipt_no_presigned_urls.py asserts on: a receipt carrying ANY of
# these has published a credential, regardless of how the url is shaped.
PRESIGNED_QUERY_PARAMS = (
    "X-Amz-Signature",
    "X-Amz-Credential",
    "X-Amz-Security-Token",
    "X-Amz-Expires",
)

REDACTED_QUERY = "?<redacted-presigned>"


def redact_report_url(url):
    """Drop the query string from a WorkItem reportUrl before it is recorded.

    APS hands back a PRESIGNED S3 url whose query carries a temporary AWS
    credential (X-Amz-Security-Token, X-Amz-Signature, ...). This receipt is
    committed to the repo, so the query must never land in git. The path is
    kept because it still identifies the report object exactly (owner + work
    item id), and the credential expires within the hour anyway, so nothing of
    diagnostic value is lost.

    Non-strings and falsy values pass through untouched, so this is safe to
    wrap around any `status.get("reportUrl")` whether or not APS returned one.
    """
    if not url or not isinstance(url, str):
        return url
    base, sep, _query = url.partition("?")
    return base + (REDACTED_QUERY if sep else "")
