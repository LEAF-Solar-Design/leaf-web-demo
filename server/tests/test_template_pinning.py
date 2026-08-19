"""Acceptance tests for card C2-2: template version pinning + exact-version receipts.

Acceptance oracle:
    Every read/clone names the exact template version; receipts carry
    (template_id, version, content digest); digest verified on read.

Offline, in-process (direct function calls against the real ``templates``
module and ``routers.templates``), mirroring ``tests/test_checkpoints.py``'s
sys.path setup.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("LEAF_AUTH_LIVE", "0")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import templates  # noqa: E402
from routers import templates as templates_router  # noqa: E402


# --------------------------------------------------------------------------- #
# registry sanity
# --------------------------------------------------------------------------- #
def test_list_templates_names_every_known_version():
    listing = templates.list_templates()
    assert listing, "the registry must not be empty"
    for entry in listing:
        assert entry["latest_version"] in entry["versions"]
        assert len(entry["versions"]) == len(set(entry["versions"])), "no duplicate versions"


def test_registered_versions_have_distinct_content_and_digests():
    """Pinning is meaningless if every version is identical -- the fixture
    registry must actually exercise different content per version."""
    template = templates._REGISTRY["rooftop-standard-string"]
    v1 = template.versions["1.0.0"]
    v2 = template.versions["1.1.0"]
    assert v1.content != v2.content
    assert v1.digest != v2.digest


# --------------------------------------------------------------------------- #
# ORACLE: every read/clone names the EXACT version
# --------------------------------------------------------------------------- #
def test_read_without_version_names_the_exact_latest_version_not_the_word_latest():
    template = templates._REGISTRY["rooftop-standard-string"]
    result = templates.read_template("rooftop-standard-string")
    assert result.version == template.latest_version
    assert result.version != "latest"
    assert result.version in template.versions


def test_read_with_version_pins_that_exact_version_and_returns_its_content():
    result = templates.read_template("rooftop-standard-string", version="1.0.0")
    assert result.version == "1.0.0"
    assert result.content == templates._REGISTRY["rooftop-standard-string"].versions["1.0.0"].content


def test_read_older_version_is_unaffected_by_a_newer_version_existing():
    """Pinning to v1.0.0 must never silently serve v1.1.0's content."""
    pinned = templates.read_template("rooftop-standard-string", version="1.0.0")
    latest = templates.read_template("rooftop-standard-string", version="1.1.0")
    assert pinned.content != latest.content
    assert pinned.version == "1.0.0"
    assert latest.version == "1.1.0"


def test_clone_names_the_exact_version_requested():
    result = templates.clone_template("ground-mount-fixed-tilt", version="1.0.0")
    assert result.version == "1.0.0"
    assert result.template_id == "ground-mount-fixed-tilt"


def test_clone_without_version_names_the_exact_latest_version():
    template = templates._REGISTRY["ground-mount-fixed-tilt"]
    result = templates.clone_template("ground-mount-fixed-tilt")
    assert result.version == template.latest_version
    assert result.version != "latest"


def test_read_unknown_template_raises():
    with pytest.raises(templates.TemplateNotFoundError):
        templates.read_template("does-not-exist")


def test_read_unknown_version_raises():
    with pytest.raises(templates.TemplateVersionNotFoundError):
        templates.read_template("rooftop-standard-string", version="9.9.9")


def test_clone_unknown_version_raises():
    with pytest.raises(templates.TemplateVersionNotFoundError):
        templates.clone_template("rooftop-standard-string", version="9.9.9")


# --------------------------------------------------------------------------- #
# ORACLE: receipts carry (template_id, version, content digest)
# --------------------------------------------------------------------------- #
def test_read_receipt_carries_template_id_version_and_content_digest():
    result = templates.read_template("rooftop-standard-string", version="1.0.0")
    tv = templates._REGISTRY["rooftop-standard-string"].versions["1.0.0"]
    assert result.receipt["template_id"] == "rooftop-standard-string"
    assert result.receipt["version"] == "1.0.0"
    assert result.receipt["content_digest"] == tv.digest
    assert result.receipt["action"] == "read"


def test_clone_receipt_carries_template_id_version_and_content_digest():
    result = templates.clone_template("ground-mount-fixed-tilt", version="1.0.0")
    tv = templates._REGISTRY["ground-mount-fixed-tilt"].versions["1.0.0"]
    assert result.receipt["template_id"] == "ground-mount-fixed-tilt"
    assert result.receipt["version"] == "1.0.0"
    assert result.receipt["content_digest"] == tv.digest
    assert result.receipt["action"] == "clone"


def test_content_digest_is_a_sha256_hex_digest():
    digest = templates.content_digest({"a": 1})
    assert isinstance(digest, str)
    assert len(digest) == 64
    int(digest, 16)  # must be valid hex


def test_content_digest_is_stable_regardless_of_key_order():
    assert templates.content_digest({"a": 1, "b": 2}) == templates.content_digest({"b": 2, "a": 1})


# --------------------------------------------------------------------------- #
# ORACLE: digest verified on read (and, transitively, on clone)
# --------------------------------------------------------------------------- #
def test_verify_digest_passes_for_an_untampered_version():
    tv = templates._REGISTRY["rooftop-standard-string"].versions["1.0.0"]
    templates.verify_digest(tv)  # must not raise


def test_verify_digest_rejects_tampered_content():
    tv = templates._REGISTRY["rooftop-standard-string"].versions["1.0.0"]
    tampered = templates.TemplateVersion(
        template_id=tv.template_id, version=tv.version,
        content={**tv.content, "string_length_max": 999},  # content mutated
        digest=tv.digest,  # digest left stale on purpose
    )
    with pytest.raises(templates.TemplateDigestMismatchError):
        templates.verify_digest(tampered)


def test_read_fails_closed_when_the_registry_content_has_been_tampered_with(monkeypatch):
    """The end-to-end oracle assertion: `read_template` itself must refuse to
    serve content whose digest it cannot reproduce, never just the standalone
    verify_digest helper."""
    template = templates._REGISTRY["rooftop-standard-string"]
    original = template.versions["1.0.0"]
    tampered = templates.TemplateVersion(
        template_id=original.template_id, version=original.version,
        content={**original.content, "setback_ft": 0},
        digest=original.digest,
    )
    monkeypatch.setitem(template.versions, "1.0.0", tampered)

    with pytest.raises(templates.TemplateDigestMismatchError):
        templates.read_template("rooftop-standard-string", version="1.0.0")


def test_clone_also_fails_closed_on_tampered_content(monkeypatch):
    template = templates._REGISTRY["ground-mount-fixed-tilt"]
    original = template.versions["1.0.0"]
    tampered = templates.TemplateVersion(
        template_id=original.template_id, version=original.version,
        content={**original.content, "row_spacing_ft": 0},
        digest=original.digest,
    )
    monkeypatch.setitem(template.versions, "1.0.0", tampered)

    with pytest.raises(templates.TemplateDigestMismatchError):
        templates.clone_template("ground-mount-fixed-tilt", version="1.0.0")


# --------------------------------------------------------------------------- #
# router: flag gate + wire-through of the same guarantees
# --------------------------------------------------------------------------- #
def test_router_flag_off_by_default_returns_404(monkeypatch):
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    resp = templates_router.list_templates(tenant="demo-tenant")
    assert resp.status_code == 404
    resp = templates_router.read_template("rooftop-standard-string", tenant="demo-tenant")
    assert resp.status_code == 404
    resp = templates_router.clone_template(
        "rooftop-standard-string",
        templates_router.CloneTemplateRequest(version=None),
        tenant="demo-tenant",
    )
    assert resp.status_code == 404


def test_router_list_returns_the_registry_when_flag_on(monkeypatch):
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    body = templates_router.list_templates(tenant="demo-tenant")
    assert body["templates"]


def test_router_read_names_the_exact_version_and_receipt(monkeypatch):
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")

    body = templates_router.read_template(
        "rooftop-standard-string", version="1.0.0", tenant="demo-tenant")
    assert body["version"] == "1.0.0"
    assert body["receipt"] == {
        "template_id": "rooftop-standard-string",
        "version": "1.0.0",
        "content_digest": templates._REGISTRY["rooftop-standard-string"].versions["1.0.0"].digest,
        "action": "read",
    }


def test_router_clone_names_the_exact_version_and_receipt(monkeypatch):
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    import json

    resp = templates_router.clone_template(
        "ground-mount-fixed-tilt",
        templates_router.CloneTemplateRequest(version="1.0.0"),
        tenant="demo-tenant",
    )
    assert resp.status_code == 201
    body = json.loads(resp.body)
    assert body["version"] == "1.0.0"
    assert body["receipt"] == {
        "template_id": "ground-mount-fixed-tilt",
        "version": "1.0.0",
        "content_digest": templates._REGISTRY["ground-mount-fixed-tilt"].versions["1.0.0"].digest,
        "action": "clone",
    }


def test_router_read_unknown_template_returns_404(monkeypatch):
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    resp = templates_router.read_template("does-not-exist", tenant="demo-tenant")
    assert resp.status_code == 404


def test_router_read_unknown_version_returns_404(monkeypatch):
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    resp = templates_router.read_template(
        "rooftop-standard-string", version="9.9.9", tenant="demo-tenant")
    assert resp.status_code == 404


def test_router_read_fails_closed_500_on_digest_mismatch(monkeypatch):
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    template = templates._REGISTRY["rooftop-standard-string"]
    original = template.versions["1.1.0"]
    tampered = templates.TemplateVersion(
        template_id=original.template_id, version=original.version,
        content={**original.content, "string_length_max": 1},
        digest=original.digest,
    )
    monkeypatch.setitem(template.versions, "1.1.0", tampered)

    resp = templates_router.read_template(
        "rooftop-standard-string", version="1.1.0", tenant="demo-tenant")
    assert resp.status_code == 500
