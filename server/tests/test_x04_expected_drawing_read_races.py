"""Identity and recomputation consume the single actual replaced artifact buffer."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('matches', [False, True])
@pytest.mark.parametrize('pattern', ['one-byte', 'irregular'])
def test_expected_identity_uses_actual_short_read_buffer(stdin, matches, pattern, tmp_path, monkeypatch):
    old = artifact(0); new = artifact(1)
    old['drawing']['id'] = old['request']['drawing_id'] = 'synthetic-old'
    new['drawing']['id'] = new['request']['drawing_id'] = 'synthetic-new'
    original = json.dumps(old).encode(); actual = b'\xef\xbb\xbf' + json.dumps(new).encode() + b' '
    source = tmp_path / 'artifact.json'; source.write_bytes(original)
    output = tmp_path / 'checked.json'; output.write_bytes(b'previous')
    opens = []; requested = []; consumed = []; calls = []; export = checker.export_evidence
    class Short(io.BytesIO):
        def read(self, size=-1):
            requested.append(size); count = 1 if pattern == 'one-byte' else (1, 7, 19, 3)[len(requested) % 4]
            chunk = super().read(min(size, count)); consumed.append(len(chunk)); return chunk
    stream = Short(actual); original_open = Path.open
    def opened(path, mode='r', *args, **kwargs):
        if path == source and mode == 'rb': opens.append(path); return stream
        return original_open(path, mode, *args, **kwargs)
    def counted(payload): calls.append(payload); return export(payload)
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-drawing-id',
            'synthetic-new' if matches else 'synthetic-old']
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'export_evidence', counted)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=stream))
        else: patch.setattr(Path, 'open', opened)
        assert checker.main(args) == (0 if matches else 2)
    assert opens == ([] if stdin else [source]) and stream.closed is (not stdin)
    assert sum(consumed) == len(actual) and all(0 < size <= 65536 for size in requested)
    assert calls == ([new] if matches else []) and source.read_bytes() == original
    if matches: assert json.loads(output.read_bytes()) == checker.validate_evidence(new)
    else: assert output.read_bytes() == b'previous'
    source.write_bytes(actual); args[-1] = 'synthetic-new'
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(actual)))
    assert checker.main(args) == 0 and json.loads(output.read_bytes()) == checker.validate_evidence(new)
    assert not list(tmp_path.glob('.x04-*')); stream.close()
