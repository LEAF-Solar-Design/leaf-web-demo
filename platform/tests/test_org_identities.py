"""w4h-b4: the lifecycle picker reads only existing identities in its org."""
import uuid
from datetime import datetime

from leaf_platform import store


def _binding(org):
    return store.create_identity_binding(
        org.org_id, "auth0", f"private-subject-{uuid.uuid4()}", role="editor",
    )


def _read(client, org, target=None):
    return client.get(
        f"/api/orgs/{target or org.org_id}/identities",
        headers={"X-Org-Id": str(org.org_id)},
    )


def test_own_org_identities_shape(client, make_org):
    org = make_org(name="Identity picker")
    binding = _binding(org)
    other = make_org(name="Other picker")
    _binding(other)
    response = _read(client, org)
    assert response.status_code == 200, response.text
    rows = response.json()["identities"]
    assert len(rows) == 1
    assert set(rows[0]) == {"binding_id", "label", "role", "created_at"}
    assert rows[0]["binding_id"] == str(binding.binding_id)
    assert isinstance(rows[0]["label"], str)
    assert rows[0]["role"] == "editor"  # The role passed to create_identity_binding.
    assert datetime.fromisoformat(rows[0]["created_at"]) == binding.created_at
    assert "private-subject" not in response.text


def test_another_org_identities_are_404(client, make_org):
    org = make_org(name="Caller")
    other = make_org(name="Hidden")
    _binding(other)
    response = _read(client, org, other.org_id)
    assert response.status_code == 404, response.text


def test_org_identities_cap_and_label_order(client, make_org):
    org = make_org(name="Bounded picker")
    bindings = [_binding(org) for _ in range(205)]
    response = _read(client, org)
    assert response.status_code == 200, response.text
    rows = response.json()["identities"]
    expected = sorted(
        ({"binding_id": str(b.binding_id), "label": f"Member {str(b.binding_id)[:8]}",
          "role": "editor", "created_at": b.created_at.isoformat()}
         for b in bindings),
        key=lambda row: (row["label"], row["binding_id"]),
    )[:200]
    assert len(rows) == 200
    assert rows == expected


def test_binding_without_name_uses_short_form(client, make_org):
    org = make_org(name="Unnamed binding")
    binding = _binding(org)
    response = _read(client, org)
    assert response.status_code == 200, response.text
    assert response.json() == {"identities": [{
        "binding_id": str(binding.binding_id),
        "label": f"Member {str(binding.binding_id)[:8]}",
        "role": "editor",
        "created_at": binding.created_at.isoformat(),
    }]}
