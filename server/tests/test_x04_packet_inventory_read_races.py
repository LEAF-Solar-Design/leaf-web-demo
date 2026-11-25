"""Packet pin, inventory and trusted operands share the actual short-read buffer."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('pin_matches', [False, True])
@pytest.mark.parametrize('inventory_matches', [False, True])
def test_one_packet_buffer_binds_pin_inventory_and_validation(stdin, pin_matches, inventory_matches, tmp_path, monkeypatch):
    original_value = packet(); changed = deepcopy(original_value)
    changed['artifacts'][1]['id'] = 'renamed'; changed['comparisons'][0]['after'] = 'renamed'
    changed['batches'][0]['artifact_ids'] = ['renamed' if item == 'b' else item for item in changed['batches'][0]['artifact_ids']]
    expected = checker.validate_packet(changed)
    original = json.dumps(original_value).encode(); actual = b'\xef\xbb\xbf' + json.dumps(changed).encode() + b' '
    source = tmp_path / 'packet.json'; source.write_bytes(original); output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    requested = []; consumed = []; opens = []; calls = []; validate = checker.validate_evidence
    class Short(io.BytesIO):
        def read(self, size=-1):
            requested.append(size); data = super().read(min(size, (1, 7, 19, 3)[len(requested) % 4]))
            consumed.append(len(data)); return data
    stream = Short(actual); original_open = Path.open
    def opened(path, mode='r', *args, **kwargs):
        if path == source and mode == 'rb': opens.append(path); return stream
        return original_open(path, mode, *args, **kwargs)
    def counted(item): calls.append(item); return validate(item)
    def args(pin, ids):
        result = ['-' if stdin else str(source), '--out', str(output), '--expected-packet-sha256', pin]
        for identity in ids: result += ['--expected-artifact-id', identity]
        return result
    pin = hashlib.sha256(actual if pin_matches else original).hexdigest()
    ids = ['a', 'renamed'] if inventory_matches else ['a', 'b']; success = pin_matches and inventory_matches
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_evidence', counted)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=stream))
        else: patch.setattr(Path, 'open', opened)
        assert checker.main(args(pin, ids)) == (0 if success else 2)
    assert opens == ([] if stdin else [source]) and stream.closed is (not stdin)
    assert sum(consumed) == len(actual) and all(0 < size <= 65536 for size in requested)
    assert len(calls) == (2 if success else 0) and source.read_bytes() == original
    if success: assert json.loads(output.read_bytes()) == expected
    else: assert output.read_bytes() == b'previous'
    source.write_bytes(actual)
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(actual)))
    assert checker.main(args(hashlib.sha256(actual).hexdigest(), ['a', 'renamed'])) == 0
    assert json.loads(output.read_bytes()) == expected and not list(tmp_path.glob('.x04-*')); stream.close()
