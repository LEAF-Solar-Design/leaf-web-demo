import json

import pytest

from leaf_platform import snapshots
from leaf_platform.db import cursor


def test_canonical_snapshot_hash_and_diff_are_deterministic():
    first = {"z": [2, 1], "a": {"enabled": True}}
    second = {"a": {"enabled": True}, "z": [2, 1]}
    assert snapshots.canonical_bytes(first) == snapshots.canonical_bytes(second)
    item = snapshots.draft("catalog", first, b"source", {"uri": "fixture://catalog"})
    assert item.content_sha256 == snapshots.sha256(snapshots.canonical_bytes(second))
    assert snapshots.diff(first, {"z": [2, 3], "a": {"enabled": True}}) == [
        {"path": "/z/1", "before": 1, "after": 3}
    ]


def test_import_is_content_addressed_and_immutable():
    content = {"items": [{"sku": "INV-1", "watts": 10000}]}
    item = snapshots.draft("catalog", content, json.dumps(content).encode(),
                           {"uri": "fixture://catalog-v1", "reader": "test"})
    first = snapshots.import_snapshot(item)
    repeated = snapshots.import_snapshot(item)
    assert repeated["snapshot_id"] == first["snapshot_id"]
    with cursor() as cur:
        with pytest.raises(Exception, match="immutable canonical ledger"):
            cur.execute("UPDATE platform_snapshots SET content = '{}' WHERE snapshot_id = %(id)s",
                        {"id": first["snapshot_id"]})


def test_channel_requires_all_three_kinds_and_bootstrap_is_explicitly_degraded():
    pins = snapshots.channel_pins()
    assert set(pins) == {"catalog", "standards", "ahj"}
    assert pins["catalog"]["review_state"] == "candidate"
    assert pins["standards"]["review_state"] == "candidate"
    assert pins["ahj"]["review_state"] == "advisory"
