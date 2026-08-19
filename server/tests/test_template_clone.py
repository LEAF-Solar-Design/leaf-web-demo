"""Acceptance tests for card C2-3: project-owned template clone.

Acceptance oracle:
    Clone lands an isolated project-owned copy bound to the cloning tenant +
    project; mutating the clone never touches the template; clone receipt
    links source version digest.

Offline, in-process (direct function calls against the real ``templates``
module), mirroring ``tests/test_template_pinning.py``'s sys.path setup.
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


@pytest.fixture(autouse=True)
def _clean_project_clone_store(monkeypatch):
    """Every test starts and ends with an empty clone store, flag enabled."""
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    monkeypatch.setattr(templates, "_PROJECT_CLONES", {})
    yield


# --------------------------------------------------------------------------- #
# ORACLE: clone lands an isolated project-owned copy bound to tenant+project
# --------------------------------------------------------------------------- #
def test_clone_lands_a_copy_bound_to_the_cloning_tenant_and_project():
    clone = templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
        version="1.0.0",
    )
    assert clone.tenant_id == "tenant-a"
    assert clone.project_id == "project-1"
    assert clone.template_id == "rooftop-standard-string"
    assert clone.version == "1.0.0"
    stored = templates.get_project_clone(clone.clone_id)
    assert stored == clone


def test_clone_without_version_names_the_exact_latest_version():
    template = templates._REGISTRY["ground-mount-fixed-tilt"]
    clone = templates.clone_template_for_project(
        "ground-mount-fixed-tilt", tenant_id="tenant-a", project_id="project-1",
    )
    assert clone.version == template.latest_version
    assert clone.version != "latest"


def test_list_project_clones_filters_by_tenant_and_project():
    a = templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
        version="1.0.0",
    )
    templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-b", project_id="project-1",
        version="1.0.0",
    )
    templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-a", project_id="project-2",
        version="1.0.0",
    )
    scoped = templates.list_project_clones("tenant-a", "project-1")
    assert [c.clone_id for c in scoped] == [a.clone_id]


def test_clone_requires_a_non_empty_tenant_id():
    with pytest.raises(ValueError):
        templates.clone_template_for_project(
            "rooftop-standard-string", tenant_id="", project_id="project-1",
        )
    assert templates.list_project_clones("", "project-1") == []


def test_clone_requires_a_non_empty_project_id():
    with pytest.raises(ValueError):
        templates.clone_template_for_project(
            "rooftop-standard-string", tenant_id="tenant-a", project_id="   ",
        )


def test_get_unknown_clone_id_raises():
    with pytest.raises(templates.ProjectTemplateCloneNotFoundError):
        templates.get_project_clone("does-not-exist")


def test_clone_of_unknown_template_raises_and_lands_nothing():
    with pytest.raises(templates.TemplateNotFoundError):
        templates.clone_template_for_project(
            "does-not-exist", tenant_id="tenant-a", project_id="project-1",
        )
    assert templates.list_project_clones("tenant-a", "project-1") == []


def test_clone_of_unknown_version_raises_and_lands_nothing():
    with pytest.raises(templates.TemplateVersionNotFoundError):
        templates.clone_template_for_project(
            "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
            version="9.9.9",
        )
    assert templates.list_project_clones("tenant-a", "project-1") == []


# --------------------------------------------------------------------------- #
# ORACLE: mutating the clone never touches the template
# --------------------------------------------------------------------------- #
def test_mutating_clone_content_never_touches_the_template():
    clone = templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
        version="1.0.0",
    )
    original_template_content = dict(
        templates._REGISTRY["rooftop-standard-string"].versions["1.0.0"].content
    )

    clone.content["setback_ft"] = 999999
    clone.content["new_key_only_on_the_clone"] = "mutated"

    # Fresh read straight from the registry: the template must be untouched.
    reread = templates.read_template("rooftop-standard-string", version="1.0.0")
    assert reread.content == original_template_content
    assert "new_key_only_on_the_clone" not in reread.content
    assert reread.content["setback_ft"] == original_template_content["setback_ft"]


def test_clone_content_is_a_deep_copy_not_a_shared_nested_reference(monkeypatch):
    """A nested structure in the source content must not alias the clone's
    copy -- proves deepcopy isolation, not just a shallow top-level dict()."""
    nested_content = {"array_type": "rooftop", "rows": [{"tilt": 20, "tags": ["a", "b"]}]}
    nested_version = templates.TemplateVersion(
        template_id="rooftop-standard-string", version="9.0.0",
        content=nested_content, digest=templates.content_digest(nested_content),
    )
    monkeypatch.setitem(
        templates._REGISTRY["rooftop-standard-string"].versions, "9.0.0", nested_version
    )

    clone = templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
        version="9.0.0",
    )
    clone.content["rows"][0]["tilt"] = 0
    clone.content["rows"][0]["tags"].append("mutated")

    assert nested_content["rows"][0]["tilt"] == 20
    assert nested_content["rows"][0]["tags"] == ["a", "b"]


def test_two_clones_of_the_same_source_are_independent_of_each_other():
    first = templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
        version="1.0.0",
    )
    second = templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
        version="1.0.0",
    )
    assert first.clone_id != second.clone_id
    first.content["setback_ft"] = -1
    assert second.content["setback_ft"] != -1


# --------------------------------------------------------------------------- #
# ORACLE: clone receipt links source version digest
# --------------------------------------------------------------------------- #
def test_clone_receipt_links_the_stored_source_version_digest():
    tv = templates._REGISTRY["rooftop-standard-string"].versions["1.0.0"]
    clone = templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
        version="1.0.0",
    )
    assert clone.receipt["template_id"] == "rooftop-standard-string"
    assert clone.receipt["version"] == "1.0.0"
    assert clone.receipt["content_digest"] == tv.digest
    assert clone.receipt["action"] == "clone"
    assert clone.receipt["tenant_id"] == "tenant-a"
    assert clone.receipt["project_id"] == "project-1"
    assert clone.receipt["clone_id"] == clone.clone_id


def test_clone_receipt_digest_is_the_frozen_registry_digest_not_recomputed_over_the_copy(
    monkeypatch,
):
    """Even if recomputing over the (re-serialized) clone copy would drift
    from the registry digest, the receipt must still carry the STORED
    source digest -- copied, never recomputed."""
    tv = templates._REGISTRY["ground-mount-fixed-tilt"].versions["1.0.0"]
    real_content_digest = templates.content_digest

    def _wrong_digest_if_called_on_a_copy(content):
        # Detect a call made against a deepcopy (a different dict object)
        # rather than the registry's own stored content dict, and return an
        # obviously-wrong value -- the receipt must never surface this.
        if content is not tv.content:
            return "recomputed-over-the-clone-would-be-wrong"
        return real_content_digest(content)

    monkeypatch.setattr(templates, "content_digest", _wrong_digest_if_called_on_a_copy)

    clone = templates.clone_template_for_project(
        "ground-mount-fixed-tilt", tenant_id="tenant-a", project_id="project-1",
        version="1.0.0",
    )
    assert clone.receipt["content_digest"] == tv.digest
    assert clone.receipt["content_digest"] != "recomputed-over-the-clone-would-be-wrong"


def test_clone_fails_closed_on_digest_mismatch_before_landing_anything(monkeypatch):
    template = templates._REGISTRY["rooftop-standard-string"]
    original = template.versions["1.1.0"]
    tampered = templates.TemplateVersion(
        template_id=original.template_id, version=original.version,
        content={**original.content, "string_length_max": 1},
        digest=original.digest,
    )
    monkeypatch.setitem(template.versions, "1.1.0", tampered)

    with pytest.raises(templates.TemplateDigestMismatchError):
        templates.clone_template_for_project(
            "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
            version="1.1.0",
        )
    assert templates.list_project_clones("tenant-a", "project-1") == []


# --------------------------------------------------------------------------- #
# Flag fence: solar_template_beta gates the write, checked before any landing
# --------------------------------------------------------------------------- #
def test_clone_refuses_when_the_flag_is_off_and_lands_nothing(monkeypatch):
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    with pytest.raises(templates.TemplateBetaDisabledError):
        templates.clone_template_for_project(
            "rooftop-standard-string", tenant_id="tenant-a", project_id="project-1",
        )
    assert templates.list_project_clones("tenant-a", "project-1") == []
    assert templates._PROJECT_CLONES == {}
