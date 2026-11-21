"""X04 user cancellation propagates while temporary output is cleaned up."""
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main
import export_x04_offline_evidence as exporter


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('stage', ['temporary-write', 'before-replace'])
def test_cancellation_cleanup_and_subsequent_valid_export(main, stage, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    original = exporter.tempfile.NamedTemporaryFile
    class CancelledWrite:
        def __init__(self, **kwargs):
            self.stream = original(**kwargs)
            self.name = self.stream.name
        def __enter__(self):
            return self
        def write(self, value):
            self.stream.write(value[:10])
            raise KeyboardInterrupt('injected user cancellation')
        def __exit__(self, *args):
            self.stream.close()
    def cancelled_replace(temporary, destination):
        assert Path(temporary).read_bytes()
        raise KeyboardInterrupt('injected user cancellation')
    with monkeypatch.context() as patch:
        if stage == 'temporary-write':
            patch.setattr(exporter.tempfile, 'NamedTemporaryFile', CancelledWrite)
        else:
            patch.setattr(exporter.os, 'replace', cancelled_replace)
        with pytest.raises(KeyboardInterrupt, match='injected user cancellation'):
            main([str(source), '--out', str(target)])
    streams = capsys.readouterr()
    assert streams.out == streams.err == ''
    assert target.read_bytes() == b'previous' and source.read_bytes() == before
    assert not list(tmp_path.glob('.x04-*'))
    assert main([str(source), '--out', str(target)]) == 0
    streams = capsys.readouterr()
    assert streams.out == streams.err == ''
    output = json.loads(target.read_bytes())
    result = output.get('result', output.get('recomputed_result'))
    assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']
