"""Independent checks for the complete APS concurrency history document."""

from pathlib import Path
import re


DOCUMENT_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "aps-concurrency-raise-request.md"
)


def read_document():
    return DOCUMENT_PATH.read_text(encoding="utf-8")


def test_no_conflict_markers():
    assert not re.search(r"^(?:<{7}|={7}|>{7}|\|{7})", read_document(), re.MULTILINE), (
        "Conflict markers must be removed from the document"
    )


def test_no_gmail_style_identifiers():
    assert not re.search(r"[0-9a-f]{16,}", read_document(), re.IGNORECASE), (
        "Correspondence identifiers must be fully redacted"
    )


def test_no_long_digit_runs():
    assert not re.search(r"[0-9]{12,}", read_document()), (
        "Long numeric identifiers must be removed"
    )
    assert not re.search(r"[0-9](?:[ .-]*[0-9]){8,}", read_document()), (
        "Numeric identifiers with nine or more digits must be removed, including formatted values"
    )
    assert not re.search(
        r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{10,}\b", read_document(), re.IGNORECASE
    ), "IBAN-like account identifiers must be removed"
    assert not re.search(r"[0-9]{4}(?:[ .-]+[0-9]{4}){3}", read_document()), (
        "Card-like digit groups must be removed"
    )


def test_no_email_addresses():
    assert not re.search(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", read_document()
    ), "Email addresses must be redacted throughout the document"


def test_answer_retained_explicitly():
    normalized = " ".join(read_document().casefold().split())
    assert "no fixed concurrency limit" in normalized, (
        "Autodesk's written concurrency answer must be stated explicitly"
    )


def test_reply_draft_dropped():
    draft_name = "aps-madhu-reply-draft-20260909.md"
    assert not (DOCUMENT_PATH.parent / draft_name).exists(), (
        "The superseded reply draft must not exist"
    )
    assert draft_name not in read_document(), (
        "The document must not reference the superseded reply draft"
    )
