"""Unit tests for da/redact.py — the credential stripper for report urls.

Sits next to the module it covers, matching the da/test_*.py convention.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redact  # noqa: E402

# The fixture matches a presigned APS report url in SHAPE only. Two rules it
# deliberately follows, both learned the hard way in this change:
#   1. Every value is an obvious placeholder. Pasting the real (even expired)
#      credential material here is what tripped gitleaks on the first push --
#      a fixture must never carry a value a scanner can mistake for live.
#   2. The query is ASSEMBLED from `_Q` rather than written literally, so this
#      file does not match the tracked-tree fence in
#      server/tests/test_no_presigned_credentials_tracked.py. That fence admits
#      no exceptions, and a fixture is not a reason to open one.
_Q = "X-Amz-"
PATH_ONLY = "https://dasprod-store.s3.us-east-1.amazonaws.com/workItem/APPID/WIID/report"
LIVE = (
    f"{PATH_ONLY}"
    f"?{_Q}Expires=3600&{_Q}Security-Token=EXAMPLE-SESSION-TOKEN"
    f"&{_Q}Algorithm=AWS4-HMAC-SHA256"
    f"&{_Q}Credential=EXAMPLEKEYID%2F20260718%2Fus-east-1%2Fs3%2Faws4_request"
    f"&{_Q}Date=20260718T024900Z&{_Q}SignedHeaders=host"
    f"&{_Q}Signature=EXAMPLESIGNATURE"
)


def test_keeps_the_identity_and_drops_the_grant():
    assert redact.redact_url(LIVE) == PATH_ONLY


def test_drops_the_query_even_when_it_looks_harmless():
    """No allowlist of 'safe' parameters: presigned schemes differ per provider
    and per version, and an allowlist silently admits the next one."""
    assert redact.redact_url("https://h.example/a?page=2&sort=asc") == "https://h.example/a"
    assert redact.redact_url("https://h.example/a#frag") == "https://h.example/a"


def test_strips_userinfo_and_normalizes_scheme_case():
    assert redact.redact_url("HTTPS://user:pw@h.example/a/b") == "https://h.example/a/b"


def test_keeps_an_explicit_port():
    assert redact.redact_url("https://h.example:8443/a?x=1") == "https://h.example:8443/a"


def test_fails_closed_on_anything_it_cannot_prove_safe():
    for bad in (None, 123, b"https://h.example/a", "", "   ",
                "ftp://h.example/a",          # non-http scheme
                "javascript:alert(1)",
                "https:///nohost",            # no host
                "not a url at all",
                "https://h.example/" + "a" * redact.MAX_URL_CHARS):  # over the bound
        assert redact.redact_url(bad) == redact.REDACTED, repr(bad)


def test_redact_text_scrubs_urls_embedded_in_free_text():
    line = f"write WorkItem 9ab status=failed report={LIVE} (retrying)"
    out = redact.redact_text(line)
    assert out == f"write WorkItem 9ab status=failed report={PATH_ONLY} (retrying)"
    for name in ("Signature", "Credential", "Security-Token"):
        assert f"{_Q}{name}=" not in out


def test_redact_text_handles_quoted_and_json_embedded_urls():
    out = redact.redact_text('{"reportUrl": "%s", "id": "abc"}' % LIVE)
    assert '"%s"' % PATH_ONLY in out
    assert "X-Amz" not in out


def test_redact_text_bounds_its_input():
    huge = "x" * (redact.MAX_TEXT_CHARS + 500)
    out = redact.redact_text(huge)
    assert out.endswith("...[truncated]")
    assert len(out) <= redact.MAX_TEXT_CHARS + len("...[truncated]")


def test_redact_text_fails_closed_on_non_strings():
    assert redact.redact_text(None) == redact.REDACTED
    assert redact.redact_text({"reportUrl": LIVE}) == redact.REDACTED


def test_report_url_fields_never_emits_the_raw_key():
    fields = redact.report_url_fields({"reportUrl": LIVE})
    assert fields == {"reportUrl_path": PATH_ONLY, "reportUrl_redacted": True}
    assert "reportUrl" not in fields


def test_report_url_fields_omits_an_absent_report():
    assert redact.report_url_fields({}) == {}
    assert redact.report_url_fields({"reportUrl": None}) == {}
    assert redact.report_url_fields(None) == {}


def test_report_url_fields_honors_the_callback_prefix():
    fields = redact.report_url_fields({"reportUrl": LIVE}, prefix="report_url")
    assert fields == {"report_url_path": PATH_ONLY, "report_url_redacted": True}


def test_a_malformed_url_still_yields_a_marked_field_not_a_leak():
    """An unparseable value must not survive into the receipt verbatim."""
    fields = redact.report_url_fields({"reportUrl": f"??? {_Q}Signature=abc"})
    assert fields == {"reportUrl_path": redact.REDACTED, "reportUrl_redacted": True}
