"""Report kind and expected digest see the same replaced, short-read byte buffer."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('matches', [False, True])
@pytest.mark.parametrize('expected_kind', ['comparison', 'batch'])
def test_one_decoded_buffer_controls_hash_and_kind(stdin, matches, expected_kind, tmp_path, monkeypatch):
    original = json.dumps(comparison()).encode(); value = batch()
    actual = b'\xef\xbb\xbf' + json.dumps(value).encode() + b' '
    source = tmp_path / 'report.json'; source.write_bytes(original)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    requested = []; consumed = []; opens = []; calls = []
    class Short(io.BytesIO):
        def read(self, size=-1):
            requested.append(size); chunk = super().read(min(size, (1, 7, 19, 3)[len(requested) % 4]))
            consumed.append(len(chunk)); return chunk
    stream = Short(actual); original_open = Path.open; validate = checker.validate_report
    def opened(path, mode='r', *args, **kwargs):
        if path == source and mode == 'rb': opens.append(path); return stream
        return original_open(path, mode, *args, **kwargs)
    def checked(payload): calls.append(payload); return validate(payload)
    pin = hashlib.sha256(actual if matches else original).hexdigest()
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-report-sha256', pin,
            '--expected-report-schema', expected_kind]
    success = matches and expected_kind == 'batch'
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_report', checked)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=stream))
        else: patch.setattr(Path, 'open', opened)
        assert checker.main(args) == (0 if success else 2)
    assert sum(consumed) == len(actual) and all(0 < size <= 65536 for size in requested)
    assert opens == ([] if stdin else [source]) and source.read_bytes() == original
    assert calls == ([value] if success else []) and stream.closed is (not stdin)
    if success: assert json.loads(output.read_bytes()) == checker.validate_report(value)
    else: assert output.read_bytes() == b'previous'
    source.write_bytes(actual)
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(actual)))
    args[args.index('--expected-report-sha256') + 1] = hashlib.sha256(actual).hexdigest(); args[-1] = 'batch'
    assert checker.main(args) == 0 and json.loads(output.read_bytes()) == checker.validate_report(value)
    assert not list(tmp_path.glob('.x04-*')); stream.close()
