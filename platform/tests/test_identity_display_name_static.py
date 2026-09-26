"""B5 display-name validation and HTTP boundaries without a database."""
import uuid
from unittest.mock import Mock

import pytest

from leaf_platform import project_lifecycle


def _request_ids(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    org_id, actor_binding_id, binding_id = (uuid.uuid4() for _ in range(3))
    headers = {
        "X-Org-Id": str(org_id),
        "X-Actor-Binding-Id": str(actor_binding_id),
    }
    return org_id, actor_binding_id, binding_id, headers


def test_normalize_display_name_trims_and_clears():
    assert project_lifecycle.normalize_display_name("  Ada Lovelace  ") == "Ada Lovelace"
    assert project_lifecycle.normalize_display_name(None) is None
    assert project_lifecycle.normalize_display_name("") is None
    assert project_lifecycle.normalize_display_name("   ") is None
    assert project_lifecycle.normalize_display_name("a" * 100) == "a" * 100


def test_normalize_display_name_rejects_over_100_characters():
    with pytest.raises(ValueError):
        project_lifecycle.normalize_display_name("a" * 101)


def test_normalize_display_name_rejects_control_and_format_characters():
    for char in ("\x00", "\n", "\u202e"):
        with pytest.raises(ValueError):
            project_lifecycle.normalize_display_name(f"Ada{char}Lovelace")


def test_normalize_display_name_rejects_non_string():
    for value in (42, False, [], {}):
        with pytest.raises(ValueError):
            project_lifecycle.normalize_display_name(value)


def test_put_label_for_another_org_is_404_and_writes_nothing(client, monkeypatch):
    _org_id, _actor_id, binding_id, headers = _request_ids(monkeypatch)
    write = Mock()
    monkeypatch.setattr(project_lifecycle, "set_identity_display_name", write)
    response = client.put(
        f"/api/orgs/{uuid.uuid4()}/identities/{binding_id}/label",
        json={"display_name": "Ada"}, headers=headers,
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "org not found"}
    write.assert_not_called()


def test_put_label_rejects_invalid_body_with_422(client, monkeypatch):
    org_id, _actor_id, binding_id, headers = _request_ids(monkeypatch)
    write = Mock()
    monkeypatch.setattr(project_lifecycle, "set_identity_display_name", write)
    for body in (
        {"display_name": "a" * 101},
        {"display_name": "Ada", "extra": "forbidden"},
        {},
    ):
        response = client.put(
            f"/api/orgs/{org_id}/identities/{binding_id}/label",
            json=body, headers=headers,
        )
        assert response.status_code == 422, response.text
    write.assert_not_called()


def test_put_label_non_owner_is_403(client, monkeypatch):
    org_id, _actor_id, binding_id, headers = _request_ids(monkeypatch)
    write = Mock(side_effect=project_lifecycle.LifecycleForbidden())
    monkeypatch.setattr(project_lifecycle, "set_identity_display_name", write)
    response = client.put(
        f"/api/orgs/{org_id}/identities/{binding_id}/label",
        json={"display_name": "Ada"}, headers=headers,
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "only the organization owner can name members"}


def test_put_label_unknown_binding_is_404(client, monkeypatch):
    org_id, _actor_id, binding_id, headers = _request_ids(monkeypatch)
    write = Mock(side_effect=project_lifecycle.LifecycleUnavailable())
    monkeypatch.setattr(project_lifecycle, "set_identity_display_name", write)
    response = client.put(
        f"/api/orgs/{org_id}/identities/{binding_id}/label",
        json={"display_name": "Ada"}, headers=headers,
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "identity not found"}


def test_put_label_returns_the_resolved_identity_row(client, monkeypatch):
    org_id, actor_binding_id, binding_id, headers = _request_ids(monkeypatch)
    row = {
        "binding_id": str(binding_id), "label": "Ada Lovelace",
        "role": "editor", "created_at": "2026-09-26T00:00:00+00:00",
    }
    write = Mock(return_value=row)
    monkeypatch.setattr(project_lifecycle, "set_identity_display_name", write)
    response = client.put(
        f"/api/orgs/{org_id}/identities/{binding_id}/label",
        json={"display_name": "  Ada Lovelace  "}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"identity": row}
    write.assert_called_once_with(org_id, actor_binding_id, binding_id, "Ada Lovelace")
