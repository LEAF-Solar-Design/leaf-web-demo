from copy import deepcopy
import json
import sqlite3

import pytest
from pydantic import ValidationError

from routers.x07_profiles import Edit
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_compatibility import check_compatibility


MODULE = {"block": "Panel", "layer": "Modules"}
ROW = {"block": "T", "layer": "Posts", "tracker_type": "east_1", "modules_per_instance": 18}


def edit(rows):
    return Edit.model_validate({"expected_version": 1, "roles": {"module": MODULE, "trackers": rows}})


def test_tracker_round_trip_preserves_legacy_pin(tmp_path):
    store = CustomerProfileStore(tmp_path / "profiles.db")
    old = store.save_version("acme", {"module": MODULE})
    pin = store.apply_to_project("project", "acme", expected_version=old["version"])
    with sqlite3.connect(tmp_path / "profiles.db") as db:
        original_bytes = db.execute("SELECT roles_json FROM profile_versions WHERE version=1").fetchone()[0]
    rows = [ROW, {**ROW, "block": "West", "tracker_type": "west-2", "modules_per_instance": 24}]
    roles = edit(rows).model_dump(exclude_none=True)["roles"]
    assert store.save_version("acme", roles, expected_version=1)["roles"] == roles
    with sqlite3.connect(tmp_path / "profiles.db") as db:
        documents = [row[0] for row in db.execute("SELECT roles_json FROM profile_versions ORDER BY version")]
    assert documents[0] == original_bytes
    assert json.loads(documents[1]) == {"schema_version": 2, "roles": roles}
    reopened = CustomerProfileStore(tmp_path / "profiles.db")
    assert reopened.latest("acme")["roles"]["trackers"] == rows
    assert reopened.application_record("project", "acme") == pin
    assert "trackers" not in pin["roles"]


@pytest.mark.parametrize("rows", [
    [ROW, ROW], [ROW, {**ROW, "block": "B", "modules_per_instance": 2}],
    [{k: v for k, v in ROW.items() if k != "tracker_type"}],
    [{**ROW, "tracker_type": "non ascii ö"}], [{**ROW, "extra": "x"}],
] + [[{**ROW, "modules_per_instance": v}] for v in [True, 0, -1, 1000001, 2.5, "2"]])
def test_invalid_tracker_edits_refuse(rows):
    with pytest.raises(ValidationError):
        edit(rows)


def test_tracker_compatibility_requires_exact_pair():
    intake = {"inserts": [{"name": "Panel", "layer": "Modules", "x": 0, "y": 0},
                          {"name": "T", "layer": "Posts", "x": 1, "y": 1}]}
    profile = {"customer_key": "acme", "profile_id": "p", "version": 2,
               "roles": {"module": MODULE, "trackers": [deepcopy(ROW)]}}
    def inspect():
        return check_compatibility(intake, profile, customer_key="acme", propose=lambda _: {})
    assert inspect()["roles"] == profile["roles"]
    profile["roles"]["trackers"][0]["block"] = "t"
    assert inspect()["decision"] == "propose_fresh"
