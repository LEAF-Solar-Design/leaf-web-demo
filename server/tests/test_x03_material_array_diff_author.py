"""Reordered stable-ID rows are matched independently of array position."""
import pytest
from test_integration_tool_service import rig
import tools_fallback
import tool_loader
from authored_material_revision import bind_material_revisions
from test_x03_material_revision_author import local, pin_selection


CASES = [
    ("Which assets moved versus changed between the JSON revisions?", ["C", "A", "B"], {}),
    ("Compare the reordered panel rows from the two imports by stable ID.", ["B", "A", "C"], {"A": 9}),
    ("Diff moved and edited items in the inspection material versions.", ["A", "C", "B"], {"A": 4, "B": 7}),
]


@pytest.mark.parametrize("ask,order,edits", CASES)
def test_array_moves_and_content_edits(local, ask, order, edits):
    before = [{"id": key, "value": i} for i, key in enumerate("ABC")]
    prior = {r["id"]: (i, r) for i, r in enumerate(before)}
    after = [{**prior[k][1], "value": edits.get(k, prior[k][1]["value"])} for k in order]
    current = {r["id"]: (i, r) for i, r in enumerate(after)}
    scope, ledger, materials = local
    previous, _ = pin_selection(scope, 1, "rows", before, ledger, materials)
    latest, _ = pin_selection(scope, 2, "rows", after, ledger, materials)
    intake = bind_material_revisions({}, ledger, *scope, "inspection", previous, latest)
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_revision_diff"
    status, payload = tool_loader._run_source_in_sandbox(source, "material_rows.py", intake, {})
    assert status == "ok", payload
    expected = {k: (i != current[k][0], row != current[k][1]) for k, (i, row) in prior.items()
                if (i, row) != current[k]}
    by_id = {r["id"]: r for r in payload[0]["changes"]}
    assert {k: (r["moved"], r["edited"]) for k, r in by_id.items()} == expected
    for k, row in by_id.items():
        assert row["before"]["pointer"] == "/rows/" + str(prior[k][0])
        assert row["after"]["pointer"] == "/rows/" + str(current[k][0])


def test_duplicate_stable_ids_refused(local):
    scope, ledger, materials = local
    previous, _ = pin_selection(scope, 1, "rows", [{"id": "A"}], ledger, materials)
    latest, _ = pin_selection(scope, 2, "rows", [{"id": "A"}, {"id": "A"}], ledger, materials)
    with pytest.raises(ValueError, match="unique IDs"):
        bind_material_revisions({}, ledger, *scope, "inspection", previous, latest)
