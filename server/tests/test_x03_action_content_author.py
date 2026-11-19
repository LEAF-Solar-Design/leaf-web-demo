"""Pass annotated content strings to pinned actions without interpreting their bytes."""
from copy import deepcopy
import base64
import urllib.request

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_extra_fields_author import closed
from test_x03_action_string_length_author import pinned_dispatch


CASES = [
    ("Run the selected action to attach the panel note as supplied.", "panel-note",
     "base64", "text/plain", ["SGVsbG8=", "not encoded!"]),
    ("Submit my chosen action to preserve the beam inspection media.", "beam-media",
     "base64", "application/json", ["eyJhIjoxfQ==", "{not json"]),
    ("Execute the discovered action to file the meter drawing text.", "meter-image",
     "custom", "image/svg+xml", ["<svg/>", "https://example.invalid/no-fetch"]),
]


@pytest.mark.parametrize("ask,operation,encoding,media,samples", CASES)
def test_content_annotations_preserve_text(catalog, monkeypatch, ask, operation, encoding, media, samples):
    schema = closed(content={"type": "string", "contentEncoding": encoding, "contentMediaType": media})
    def unexpected(*args, **kwargs):
        pytest.fail("Content annotations invoked a decoder or URL retrieval")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", unexpected)
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    decode = base64.b64decode
    submitted = [*samples, *(sample.encode("utf-8") for sample in samples)]
    def guard_content(data, *args, **kwargs):
        if data in submitted:
            unexpected()
        return decode(data, *args, **kwargs)
    monkeypatch.setattr(base64, "b64decode", guard_content)
    expected = []
    for value in [None, *samples]:
        payload = {"content": value}
        snapshot = deepcopy(payload)
        if isinstance(value, str):
            dispatch(payload)
            expected.append(snapshot)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert payload == snapshot and calls == expected
