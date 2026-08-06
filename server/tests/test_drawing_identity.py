"""Drawing namespace compatibility contract."""
from __future__ import annotations

import pytest

import drawing_identity


@pytest.mark.parametrize("alias", ["rooftop_demo", "rooftop-demo", "demo"])
def test_curated_aliases_share_source_and_store_ids(alias):
    assert drawing_identity.source_id(alias) == "rooftop_demo"
    assert drawing_identity.store_id(alias) == "demo"


def test_unknown_tenant_drawing_passes_through_for_scoped_resolution():
    drawing_id = "u-0123456789"
    assert drawing_identity.source_id(drawing_id) == drawing_id
    assert drawing_identity.store_id(drawing_id) == drawing_id
    assert drawing_identity.curated_identity(drawing_id) is None

