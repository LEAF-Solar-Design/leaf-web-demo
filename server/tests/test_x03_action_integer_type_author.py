"""Compare integer-valued JSON numbers against independent decimal arithmetic."""
from decimal import Decimal
import json

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_extra_fields_author import closed
from test_x03_action_string_length_author import pinned_dispatch


CASES = [
    ("Run the selected action to set the number of panels.", "panel-count", [4.0, 4.25, True, 0]),
    ("Execute the discovered action to record the signed meter offset.", "meter-offset", [-3.0, -3.5, False, -9]),
    ("Submit my chosen action to file the beam inspection index.", "beam-index", [1e5, 1e-4, True, -0.0]),
]


@pytest.mark.parametrize("ask,operation,samples", CASES)
def test_mathematical_integer(catalog, ask, operation, samples):
    dispatch, calls = pinned_dispatch(catalog, ask, operation, closed(quantity={"type": "integer"}))
    expected = []
    for sample in samples:
        literal = json.dumps(sample)
        number = None if isinstance(sample, bool) else Decimal(literal)
        payload = {"quantity": sample}
        if number is not None and number == number.to_integral_value():
            dispatch(payload)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
