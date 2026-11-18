"""Review CLI entry points preserve atomic publication under writer failures."""
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import worked_example
from compare_x04_offline_evidence import main as compare_main
from batch_validate_x04_evidence import main as batch_main
import export_x04_offline_evidence as exporter


@pytest.mark.parametrize('command', ['comparison', 'batch'])
@pytest.mark.parametrize('fault', ['short', 'zero', 'write', 'flush', 'replace', 'cleanup'])
def test_review_publication_faults_and_retry(command, fault, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input.json'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    target = tmp_path / 'output.json'; target.write_bytes(b'previous')
    main = compare_main if command == 'comparison' else batch_main
    args = [str(source)] * (2 if command == 'comparison' else 1) + ['--out', str(target)]
    original_temp = exporter.tempfile.NamedTemporaryFile
    original_unlink = Path.unlink
    class Temporary:
        def __init__(self, **kwargs):
            self.stream = original_temp(**kwargs); self.name = self.stream.name
        def __enter__(self): return self
        def write(self, value):
            if fault in ('write', 'cleanup'): raise OSError('primary write failure')
            if fault == 'zero': return 0
            return self.stream.write(value[:3] if fault == 'short' else value)
        def flush(self):
            if fault == 'flush': raise OSError('primary flush failure')
            self.stream.flush()
        def __exit__(self, *args): self.stream.close()
    def fail_replace(*args): raise OSError('primary replace failure')
    def fail_unlink(path, *args, **kwargs):
        if path.name.startswith('.x04-'): raise OSError('injected cleanup denial')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(exporter.tempfile, 'NamedTemporaryFile', Temporary)
        if fault == 'replace': patch.setattr(exporter.os, 'replace', fail_replace)
        if fault == 'cleanup': patch.setattr(Path, 'unlink', fail_unlink)
        status = main(args)
    streams = capsys.readouterr()
    leftovers = list(tmp_path.glob('.x04-*'))
    assert streams.out == '' and source.read_bytes() == before
    if fault == 'short':
        assert status == 0 and streams.err == ''
    else:
        assert status == 2 and 'refused' in streams.err and 'Traceback' not in streams.err
        assert target.read_bytes() == b'previous'
    if fault == 'cleanup':
        assert len(leftovers) == 1 and leftovers[0].name in streams.err
        assert 'primary write failure' in streams.err.splitlines()[0]
        assert 'remains after cleanup failed' in streams.err
        original_unlink(leftovers[0])
    else: assert leftovers == []
    assert main(args) == 0 and capsys.readouterr().err == ''
    result = json.loads(target.read_bytes())
    if command == 'comparison': assert result['count']['delta'] == 0
    else: assert (result['total'], result['passed'], result['failed']) == (1, 1, 0)
    assert result['original_bytes_integrity'] == 'unverified'
    assert not list(tmp_path.glob('.x04-*')) and source.read_bytes() == before
