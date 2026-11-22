"""Cleanup errors preserve primary X04 refusals and cancellation identity."""
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main
import export_x04_offline_evidence as exporter


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('failure', ['write', 'replace', 'cancel'])
def test_cleanup_failure_keeps_primary_and_reports_leftover(main, failure, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    original_factory = exporter.tempfile.NamedTemporaryFile
    original_unlink = Path.unlink
    label = 'PRIMARY_' + failure.upper()
    class FailedWrite:
        def __init__(self, **kwargs):
            self.stream = original_factory(**kwargs)
            self.name = self.stream.name
        def __enter__(self):
            return self
        def write(self, value):
            self.stream.write(value[:10])
            raise OSError(label)
        def __exit__(self, *args):
            self.stream.close()
    def failed_replace(*args):
        if failure == 'cancel':
            raise KeyboardInterrupt(label)
        raise OSError(label)
    def failed_unlink(path, *args, **kwargs):
        if path.parent == tmp_path and path.name.startswith('.x04-'):
            raise PermissionError('SECONDARY_UNLINK_FAILURE')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', failed_unlink)
        if failure == 'write':
            patch.setattr(exporter.tempfile, 'NamedTemporaryFile', FailedWrite)
        else:
            patch.setattr(exporter.os, 'replace', failed_replace)
        if failure == 'cancel':
            with pytest.raises(KeyboardInterrupt, match=label) as cancelled:
                main([str(source), '--out', str(target)])
            assert cancelled.value.__notes__
        else:
            assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    leftovers = list(tmp_path.glob('.x04-*'))
    assert streams.out == '' and len(leftovers) == 1
    assert leftovers[0].name in streams.err and 'remains after cleanup failed' in streams.err
    if failure != 'cancel':
        assert label in streams.err.splitlines()[0]
    assert target.read_bytes() == b'previous' and source.read_bytes() == before
    for path in leftovers:
        original_unlink(path)
    assert main([str(source), '--out', str(target)]) == 0
    streams = capsys.readouterr()
    assert streams.out == streams.err == '' and not list(tmp_path.glob('.x04-*'))
    result = json.loads(target.read_bytes())
    assert result.get('result', result.get('recomputed_result'))['count'] == 7
