"""Discover source fields by ID and keep their labels and values distinct."""
import pytest

from test_integration_tool_service import FIELD, REF, read, rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List the field definitions for my project datasets.", ("Load", "load"), ("Load", "load")),
    ("Show the column definitions for the connected datasets.", ("east", "west"), ("Caf\u00e9", "Cafe\u0301")),
    ("Which datasets are available? Include their field definitions.", ("net_kw", "gross_kw"), ("Power", "Power")),
]


@pytest.mark.parametrize("ask,ids,labels", CASES)
def test_field_identity_through_discovery(rig, ask, ids, labels):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    fields = [dict(FIELD, id=identity, label=label) for identity, label in zip(ids, labels)]
    dataset = dict(record(REF["id"], "ready"), fields=fields)
    rig.response = {"records": [dataset], "nextCursor": None}
    report = rig.svc.run_authored_dataset_report(ask, *scope)
    discovered, = report["datasets"]
    assert discovered["dataset_ref"] == REF
    assert [(f["id"], f["label"].encode("utf-8")) for f in discovered["fields"]] == [
        (ids[0], labels[0].encode("utf-8")), (ids[1], labels[1].encode("utf-8"))]

    # A real query binds both row values to the returned IDs, regardless of display label.
    values = {ids[0]: 12, ids[1]: 97}
    rig.response = dict(read(), fields=fields, rows=[{"key": "sample", "values": values}])
    result = rig.call()["result"]
    assert [result["rows"][0]["values"][f["id"]] for f in discovered["fields"]] == [12, 97]

    # The wire contract requires unique ASCII identifiers, but imposes no label uniqueness.
    for invalid_ids in ((ids[0], ids[0]), ("Caf\u00e9", "Cafe\u0301")):
        dataset["fields"] = [dict(f, id=i) for f, i in zip(fields, invalid_ids)]
        rig.response = {"records": [dataset], "nextCursor": None}
        with pytest.raises(service.ReadFailure, match="^invalid_response$"):
            rig.svc.run_authored_dataset_report(ask, *scope)
