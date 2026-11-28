"""Exercise positional and trailing cells under the action's pinned draft-07 rules."""
import socket
import urllib.request

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_extra_fields_author import closed
from test_x03_action_string_length_author import pinned_dispatch


CASES = [
    ("Run the selected action to save the panel tag and count as a fixed pair.",
     "panel-pair", "closed"),
    ("Submit my chosen action to file the beam count followed by inspection flags.",
     "beam-flags", "flags"),
    ("Execute the discovered action to append operator notes to the meter reading.",
     "meter-notes", "notes"),
]


@pytest.mark.parametrize("ask,operation,tail", CASES)
def test_legacy_tuple_arguments(catalog, monkeypatch, ask, operation, tail):
    connect = socket.socket.connect

    def local_loopback(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return connect(sock, address)  # Windows asyncio socket pair
        pytest.fail("External connection attempted")

    monkeypatch.setattr(socket.socket, "connect", local_loopback)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda *args, **kwargs: pytest.fail("Unexpected URL retrieval"))
    extras = {"closed": False, "flags": {"type": "boolean"}, "notes": {"type": "string"}}
    schema = closed(row={"type": "array", "minItems": 2,
                         "items": [{"type": "string"}, {"type": "integer"}],
                         "additionalItems": extras[tail]})
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    expected = []
    for row in (["P", 2], [2, "P"], ["P"], ["P", True], ["P", 2, True, False],
                ["P", 2, "ok"], ["P", 2, True, "ok"]):
        correct_pair = len(row) >= 2 and type(row[0]) is str and type(row[1]) is int
        extra_type = {"flags": bool, "notes": str}.get(tail)
        if correct_pair and all(type(item) is extra_type for item in row[2:]):
            dispatch({"row": row})
            expected.append({"row": row})
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch({"row": row})
        assert calls == expected
