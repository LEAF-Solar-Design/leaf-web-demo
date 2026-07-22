"""Stable drawing identity and version-chain ownership gates."""
from __future__ import annotations

import pytest

import leaf_platform.store as store


def test_default_artifact_is_stable_and_versions_are_hydrated(make_org):
    org = make_org("Drawing identity org")
    project = store.create_project(org.org_id, "Drawing identity project")
    first = store.create_drawing_version(org.org_id, project.project_id,
                                         oss_object="drawings/v1.dwg")
    second = store.create_drawing_version(org.org_id, project.project_id,
                                          oss_object="drawings/v2.dwg")
    assert first.drawing_id == second.drawing_id
    assert (first.seq, second.seq) == (1, 2)
    hydrated = store.hydrate_project(org.org_id, project.project_id)
    assert hydrated is not None
    assert len(hydrated["drawing_artifacts"]) == 1
    assert hydrated["drawing_artifacts"][0]["drawing_id"] == str(first.drawing_id)
    assert {item["drawing_id"] for item in hydrated["drawing_versions"]} == {
        str(first.drawing_id)
    }


def test_independent_artifacts_have_independent_version_sequences(make_org):
    org = make_org("Multi drawing org")
    project = store.create_project(org.org_id, "Multi drawing project")
    first_artifact = store.create_drawing_artifact(org.org_id, project.project_id, "Array A")
    second_artifact = store.create_drawing_artifact(org.org_id, project.project_id, "Array B")
    first = store.create_drawing_version(org.org_id, project.project_id,
                                         drawing_id=first_artifact.drawing_id)
    second = store.create_drawing_version(org.org_id, project.project_id,
                                          drawing_id=second_artifact.drawing_id)
    assert first.seq == second.seq == 1


def test_foreign_drawing_artifact_fails_closed(make_org):
    org = make_org("Drawing isolation org")
    first_project = store.create_project(org.org_id, "First drawing project")
    second_project = store.create_project(org.org_id, "Second drawing project")
    foreign = store.create_drawing_artifact(org.org_id, second_project.project_id)
    with pytest.raises(ValueError, match="drawing artifact not found"):
        store.create_drawing_version(org.org_id, first_project.project_id,
                                     drawing_id=foreign.drawing_id)
