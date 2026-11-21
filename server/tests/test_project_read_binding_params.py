"""Regression for transport drawing selectors on schema-closed project read tools."""
import pytest
from test_project_scoped_read_tool import (
    D1, D2, lane, memory, install_tool_body, request, tool_override,
)
from envelopes import ok_envelope


@pytest.mark.parametrize("name,field,value", [
    ("layer-bounds", "exact_match", False),
    ("count-symbols", "block_name", "PV"),
    ("inspect-curves", "limit", 3),
])
def test_bound_selector_stays_out_of_closed_read_inputs(monkeypatch, lane, name, field, value):
    tool = tool_override(monkeypatch, name=name, params={
        "type": "object", "properties": {field: {}}, "additionalProperties": False,
    })
    seen = []
    def read(intake, params):
        seen.append(dict(params))
        return ok_envelope(name, "1.0.0", {"count": 1}, None, 1)
    install_tool_body(monkeypatch, read)
    supplied = {field: value, "drawing_id": str(D1)}
    result = request(lane, tool=tool, params=supplied)
    assert result.status_code == 200, result.text
    assert seen == [{field: value}]
    assert result.json()["project_context"]["drawing_id"] == str(D1)
    assert request(lane, tool=tool, params={**supplied, "extra": 1}).status_code == 400
    assert request(lane, tool=tool, params={**supplied, "drawing_id": str(D2)}).status_code != 200
    assert len(seen) == 1
