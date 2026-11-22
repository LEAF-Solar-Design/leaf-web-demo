"""Saved-report entry point preserves prior publication on faults and cancellation."""
import json
import pytest
import export_x04_offline_evidence as shared
from test_x04_comparison_report_contract import comparison
from validate_x04_review_report import main


@pytest.mark.parametrize('stage', ['write', 'replace'])
@pytest.mark.parametrize('cancel', [False, True])
def test_saved_report_publication_failure_and_retry(stage, cancel, tmp_path, monkeypatch, capsys):
    source, output = tmp_path / 'saved.json', tmp_path / 'checked.json'
    source.write_text(json.dumps(comparison()), encoding='utf-8')
    before = source.read_bytes()
    output.write_bytes(b'prior checked report')
    unrelated = tmp_path / '.x04-other-writer'; unrelated.write_bytes(b'owned by another writer')
    original = shared.tempfile.NamedTemporaryFile
    paths = []
    failure = KeyboardInterrupt('synthetic cancellation') if cancel else OSError('synthetic publication fault')
    class Temporary:
        def __init__(self, **kwargs):
            self.stream = original(**kwargs); self.name = self.stream.name
            paths.append(self.name)
        def __enter__(self):
            return self
        def write(self, value):
            if stage == 'write':
                self.stream.write(value[:3])
                raise failure
            return self.stream.write(value)
        def flush(self):
            self.stream.flush()
        def __exit__(self, *args):
            self.stream.close()
    def fail_replace(*args):
        raise failure
    with monkeypatch.context() as patch:
        patch.setattr(shared.tempfile, 'NamedTemporaryFile', Temporary)
        if stage == 'replace':
            patch.setattr(shared.os, 'replace', fail_replace)
        if cancel:
            with pytest.raises(KeyboardInterrupt) as caught:
                main([str(source), '--out', str(output)])
            assert caught.value is failure
        else:
            assert main([str(source), '--out', str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert ('refused' in captured.err) is (not cancel)
    assert 'Traceback' not in captured.err
    assert output.read_bytes() == b'prior checked report' and source.read_bytes() == before
    assert paths and all(not shared.Path(path).exists() for path in paths)
    assert unrelated.read_bytes() == b'owned by another writer'
    assert main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['underlying_artifacts_revalidated'] is False
    assert list(tmp_path.glob('.x04-*')) == [unrelated]
