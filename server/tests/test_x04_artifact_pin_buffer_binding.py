"""Artifact pins follow the bounded actual decoded buffer, never a reopened file."""
import hashlib
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
def test_artifact_pin_actual_short_read_buffer(stdin, matches, pattern, tmp_path, monkeypatch):
    value = artifact(0); original = json.dumps(value).encode(); actual = b'\xef\xbb\xbf' + original + b' '
    source = tmp_path / 'artifact.json'; source.write_bytes(original)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    requested = []; consumed = []; opens = []; streams = []; calls = []
    export = checker.export_evidence
    def counted(payload): calls.append(payload); return export(payload)
    class Short(io.BytesIO):
        def read(self, amount=-1):
            requested.append(amount)
            count = 1 if pattern == 'one-byte' else (3, 17, 1, 63)[len(requested) % 4]
            data = super().read(min(amount, count)); consumed.append(len(data)); return data
    stream = Short(actual); streams.append(stream); original_open = Path.open
    def observed(path, mode='r', *a, **kw):
        if path == source and mode == 'rb': opens.append(path); return stream
        return original_open(path, mode, *a, **kw)
    digest = hashlib.sha256(actual if matches else original).hexdigest()
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-artifact-sha256', digest]
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'export_evidence', counted)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=stream))
        else: patch.setattr(Path, 'open', observed)
        assert checker.main(args) == (0 if matches else 2)
    assert sum(consumed) == len(actual) and all(0 < size <= 65536 for size in requested)
    assert opens == ([] if stdin else [source]) and source.read_bytes() == original
    assert calls == ([value] if matches else [])
    if not stdin: assert stream.closed
    if matches: assert json.loads(output.read_bytes()) == checker.validate_evidence(value)
    else: assert output.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*'))
    # Retry with ordinary correct bytes, using a fresh stdin buffer if needed.
    pin = hashlib.sha256(original).hexdigest(); args[-1] = pin
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(original)))
    assert checker.main(args) == 0


@pytest.mark.parametrize('stdin', [False, True])
def test_artifact_budget_precedes_pin_and_stops_at_one_excess_byte(stdin, tmp_path, monkeypatch):
    raw = json.dumps(artifact(0)).encode(); source = tmp_path / 'artifact.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    limit = len(raw) - 1; consumed = []
    class Short(io.BytesIO):
        def read(self, amount=-1):
            data = super().read(min(amount, 7)); consumed.append(len(data)); return data
    # File stat sees the old bounded file while open returns the full stream.
    source.write_bytes(raw[:limit]); original_open = Path.open
    def observed(path, mode='r', *a, **kw):
        return Short(raw) if path == source and mode == 'rb' else original_open(path, mode, *a, **kw)
    args = ['-' if stdin else str(source), '--out', str(output), '--max-input-bytes', str(limit), '--expected-artifact-sha256', '0' * 64]
    with monkeypatch.context() as patch:
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=Short(raw)))
        else: patch.setattr(Path, 'open', observed)
        assert checker.main(args) == 2
    assert sum(consumed) == limit + 1 and output.read_bytes() == b'previous'
    source.write_bytes(raw)
    args[args.index('--max-input-bytes') + 1] = str(len(raw)); args[-1] = hashlib.sha256(raw).hexdigest()
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    assert checker.main(args) == 0
