"""Missing/closed/nonbinary stdin refuses with preserved output and recovers."""
import io
import json
import sys
from types import SimpleNamespace
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as validator


@pytest.mark.parametrize('factory', [comparison, batch])
@pytest.mark.parametrize('mode', ['absent', 'closed', 'text-only'])
def test_stdin_unavailable_and_restored(factory, mode, tmp_path, monkeypatch, capsys):
    raw = json.dumps(factory()).encode('utf-8')
    closed = io.BytesIO(raw); closed.close()
    stdin = {'absent': None, 'closed': SimpleNamespace(buffer=closed), 'text-only': io.StringIO('')}[mode]
    output = tmp_path / 'checked.json'; output.write_bytes(b'previous')
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdin', stdin)
        assert validator.main(['-', '--out', str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'open binary input buffer' in captured.err
    assert 'Traceback' not in captured.err and output.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*'))
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert validator.main(['-', '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == validator.validate_report(factory())
