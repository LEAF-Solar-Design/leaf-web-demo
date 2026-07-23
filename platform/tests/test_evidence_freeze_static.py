"""Freeze validator: contract/EVIDENCE.md <-> code agreement (wave-3 L4).

DB-free (_static suffix rides the conftest exemption + the platform-static
suite): evidence.py imports clean; signing.py is checked by SOURCE parse
(importing it pulls the db module). The doc is parsed for its frozen
literals so drift in EITHER direction fails loud. The strongest check
reimplements the DOCUMENTED merkle algorithm from EVIDENCE.md §1.3 and must
reproduce build()'s root byte-for-byte.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# File-path load (the test_ledger_static.py pattern): the gate invokes pytest
# from the repo parent, where platform/ is neither a package on sys.path nor
# importable by name (and would shadow the stdlib `platform` if it were).
_EVIDENCE_PATH = Path(__file__).resolve().parents[1] / "evidence.py"
_spec = importlib.util.spec_from_file_location("leaf_evidence_freeze", _EVIDENCE_PATH)
evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evidence)

REPO = Path(__file__).resolve().parents[2]
DOC = (REPO / "contract" / "EVIDENCE.md").read_text(encoding="utf-8")
SIGNING_SOURCE = (REPO / "platform" / "signing.py").read_text(encoding="utf-8")


def _doc_backticked(section_heading: str, stop_heading: str) -> set:
    start = DOC.index(section_heading)
    stop = DOC.index(stop_heading, start)
    return set(re.findall(r"`([^`\n]+)`", DOC[start:stop]))


# --------------------------------------------------------------------------
# 1. Identifiers and domains
# --------------------------------------------------------------------------

def test_bundle_version_algorithm_and_domains_match_doc():
    assert evidence.BUNDLE_VERSION == "leaf.evidence.v1"
    assert "`leaf.evidence.v1`" in DOC
    assert "`sha256-merkle-v1`" in DOC
    assert evidence._LEAF_DOMAIN == b"leaf-evidence-leaf-v1\0"
    assert evidence._NODE_DOMAIN == b"leaf-evidence-node-v1\0"
    assert 'b"leaf-evidence-leaf-v1\\0"' in DOC
    assert 'b"leaf-evidence-node-v1\\0"' in DOC
    assert re.search(r"SIGNATURE_CONTRACT\s*=\s*\"leaf\.review-signature\.v1\"", SIGNING_SOURCE)
    assert "`leaf.review-signature.v1`" in DOC


# --------------------------------------------------------------------------
# 2. The documented algorithm reproduces the code's root (independent
#    reimplementation from EVIDENCE.md §1.3, including the odd-duplication
#    and header pseudo-leaf rules)
# --------------------------------------------------------------------------

def test_merkle_root_reproducible_from_documented_algorithm():
    blobs = {
        "records/a.json": b'{"x":1}',
        "records/b.json": b'{"y":2}',
        "records/c.json": b'{"z":3}',  # 3 entries + header = odd level exercised
    }
    metadata = {"projectId": "p-1", "frozenAt": "2026-07-23T00:00:00+00:00"}
    manifest = evidence.build(blobs, metadata=metadata)

    # ---- doc §1.3, step by step, sharing NOTHING with evidence._root ----
    header = {"bundleVersion": "leaf.evidence.v1", "algorithm": "sha256-merkle-v1",
              "metadata": metadata}
    header_sha = hashlib.sha256(evidence.canonical_bytes(header)).hexdigest()

    def leaf(path, content_sha):
        return hashlib.sha256(b"leaf-evidence-leaf-v1\0" + path.encode("utf-8")
                              + b"\0" + bytes.fromhex(content_sha)).digest()

    level = [leaf("@manifest-metadata", header_sha)] + [
        leaf(path, hashlib.sha256(blobs[path]).hexdigest()) for path in sorted(blobs)]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(b"leaf-evidence-node-v1\0" + level[i] + level[i + 1]).digest()
                 for i in range(0, len(level), 2)]
    assert manifest["rootSha256"] == level[0].hex()
    assert evidence.verify(manifest, blobs) == {
        "valid": True, "errors": [], "rootSha256": manifest["rootSha256"]}


# --------------------------------------------------------------------------
# 3. Verification error vocabulary is closed and documented
# --------------------------------------------------------------------------

def test_verify_error_vocabulary_matches_doc():
    # Slice exactly the vocabulary list (between "draws only from:" and the
    # closing sentence) — the section's surrounding prose backticks return
    # shapes, not error codes.
    start = DOC.index("draws only from:")
    stop = DOC.index("An empty error list", start)
    documented = set(re.findall(r"`([^`\n]+)`", DOC[start:stop]))
    blobs = {"a.json": b"{}", "b.json": b"[]"}
    manifest = evidence.build(blobs, metadata={"k": "v"})

    def errors_of(mutate_manifest=None, mutate_blobs=None):
        m = {**manifest, "entries": [dict(e) for e in manifest["entries"]]}
        b = dict(blobs)
        if mutate_manifest:
            mutate_manifest(m)
        if mutate_blobs:
            mutate_blobs(b)
        return evidence.verify(m, b)["errors"]

    produced = set()
    produced |= set(errors_of(lambda m: m.update(bundleVersion="leaf.evidence.v2")))
    produced |= set(errors_of(lambda m: m.update(entries=[])))
    produced |= set(errors_of(lambda m: m.update(entries=list(reversed(m["entries"])))))
    produced |= set(errors_of(mutate_blobs=lambda b: b.update({"extra.json": b"1"})))
    produced |= set(errors_of(mutate_blobs=lambda b: b.update({"a.json": b"{ }"})))
    produced |= set(errors_of(lambda m: m.update(rootSha256="0" * 64)))
    produced |= set(errors_of(lambda m: m.update(metadata=None)))
    produced |= set(errors_of(lambda m: m["entries"].append("bogus")))
    produced |= set(errors_of(mutate_blobs=lambda b: b.pop("b.json")))

    # every produced error is documented (parametrized ones by prefix)
    for error in produced:
        token = error
        if error.startswith("missing:"):
            token = "missing:<path>"
        elif error.startswith("digest_mismatch:"):
            token = "digest_mismatch:<path>"
        assert token in documented, f"verify() produced undocumented error {error!r}"
    # and every documented code is provokable (nothing stale in the doc)
    fixed = {t for t in documented if ":" not in t}
    assert fixed <= produced, f"documented but not provoked: {fixed - produced}"


# --------------------------------------------------------------------------
# 4. Canonicalization laws (§1.2)
# --------------------------------------------------------------------------

def test_canonical_bytes_laws():
    out = evidence.canonical_bytes({"b": 1, "a": [1, 2], "u": "é"})
    assert out == '{"a":[1,2],"b":1,"u":"é"}'.encode("utf-8")  # sorted, compact, unicode kept
    assert evidence.canonical_bytes(uuid.UUID(int=1)) == b'"00000000-0000-0000-0000-000000000001"'
    stamp = datetime(2026, 7, 23, tzinfo=timezone.utc)
    assert evidence.canonical_bytes(stamp) == b'"2026-07-23T00:00:00+00:00"'
    # json.dumps serializes the default-hook's str RETURN as a JSON string:
    # Decimal reaches the wire as "1.10" (quoted), never a float — offline
    # verifiers must reproduce exactly this.
    assert evidence.canonical_bytes(Decimal("1.10")) == b'"1.10"'
    with pytest.raises(TypeError):
        evidence.canonical_bytes({1, 2})


def test_build_input_laws():
    with pytest.raises(ValueError):
        evidence.build({}, metadata={"k": "v"})            # empty bundle
    with pytest.raises(ValueError):
        evidence.build({"a": b"1"}, metadata={})           # empty metadata
    with pytest.raises(ValueError):
        evidence.build({"/abs": b"1"}, metadata={"k": "v"})  # absolute path
    with pytest.raises(ValueError):
        evidence.build({"a/../b": b"1"}, metadata={"k": "v"})  # traversal
    with pytest.raises(ValueError):
        evidence.build({"a": "not-bytes"}, metadata={"k": "v"})  # type


# --------------------------------------------------------------------------
# 5. Review-signature payloads (source-parsed; importing signing.py needs db)
# --------------------------------------------------------------------------

def _dict_literal_keys(anchor: str) -> list:
    start = SIGNING_SOURCE.index(anchor)
    block = SIGNING_SOURCE[start:SIGNING_SOURCE.index("}", start)]
    return re.findall(r'"(\w+)":', block)

def _doc_table_keys(section_heading: str, stop_heading: str) -> list:
    start = DOC.index(section_heading)
    stop = DOC.index(stop_heading, start)
    return re.findall(r"^\| `(\w+)` \|", DOC[start:stop], re.M)


def test_signed_payload_field_set_frozen():
    code_keys = _dict_literal_keys("payload = {")
    doc_keys = _doc_table_keys("### 2.1", "### 2.2")
    assert code_keys == ["signatureContract", "bundleId", "rootSha256",
                        "credentialId", "signedAt"]
    assert doc_keys == code_keys, (doc_keys, code_keys)
    # what gets signed is the canonical encoding of exactly that payload
    assert "encoded = evidence.canonical_bytes(payload)" in SIGNING_SOURCE


def test_history_payload_field_set_frozen():
    code_keys = _dict_literal_keys("history_payload = {")
    assert code_keys == ["signatureId", "bundleId", "rootSha256", "credentialId",
                        "signatureContract", "signatureAlgorithm", "signatureSha256"]
    for key in code_keys:
        assert f"`{key}`" in DOC, f"history key {key} undocumented"
    assert '"review.bundle.countersigned"' in SIGNING_SOURCE
    assert "`review.bundle.countersigned`" in DOC


def test_readiness_reason_vocabulary_matches_doc():
    documented = _doc_backticked("### 2.3", "### 2.4")
    code_reasons = set(re.findall(r'"reason": "(\w+)"', SIGNING_SOURCE))
    expected = {"active_credential_required", "credential_revoked", "credential_expired",
                "signature_provider_unavailable", "signature_provider_mismatch"}
    assert code_reasons == expected, code_reasons
    assert expected <= documented, f"undocumented reasons: {expected - documented}"
