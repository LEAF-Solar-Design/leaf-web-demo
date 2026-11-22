"""X04 output flush and close failures preserve the existing artifact."""
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main
import export_x04_offline_evidence as exporter


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('failure', ['flush', 'close'])
def test_file_finalization_failure_cleanup_and_retry(main, failure, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    original = exporter.tempfile.NamedTemporaryFile
    temporaries = []
    class FailedFinalization:
        def __init__(self, **kwargs):
            self.stream = original(**kwargs)
            self.name = self.stream.name
            temporaries.append(Path(self.name))
        def __enter__(self):
            return self
        def write(self, value):
            return self.stream.write(value)
        def flush(self):
            if failure == 'flush':
                raise OSError('injected file flush failure')
            self.stream.flush()
        def __exit__(self, *args):
            self.stream.close()
            if failure == 'close':
                raise OSError('injected file close failure')
    with monkeypatch.context() as patch:
        patch.setattr(exporter.tempfile, 'NamedTemporaryFile', FailedFinalization)
        assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and f'injected file {failure} failure' in streams.err
    assert 'Traceback' not in streams.err and target.read_bytes() == b'previous'
    assert temporaries and not any(path.exists() for path in temporaries)
    assert not list(tmp_path.glob('.x04-*'))
    assert main([str(source), '--out', str(target)]) == 0
    streams = capsys.readouterr()
    assert streams.out == streams.err == ''
    output = json.loads(target.read_bytes())
    result = output.get('result', output.get('recomputed_result'))
    assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']
