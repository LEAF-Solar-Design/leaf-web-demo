"""X04 evidence output survives failed encoding, writes and replacement."""
from contextlib import contextmanager
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import worked_example
import export_x04_offline_evidence as exporter


@pytest.mark.parametrize('failure', ['serialization', 'temporary-write', 'replace'])
def test_export_failure_preserves_existing_output(failure, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    output = tmp_path / 'output.json'
    previous = b'previous complete evidence\x00\xff'
    output.write_bytes(previous)
    if failure == 'serialization':
        original = exporter.json.dumps
        def fail_encoding(value, **kwargs):
            if 'indent' in kwargs:
                raise ValueError('injected serialization failure')
            return original(value, **kwargs)
        monkeypatch.setattr(exporter.json, 'dumps', fail_encoding)
    elif failure == 'temporary-write':
        original = exporter.tempfile.NamedTemporaryFile
        @contextmanager
        def failed_stream(**kwargs):
            with original(**kwargs) as stream:
                class PartialWrite:
                    name = stream.name
                    def write(self, value):
                        stream.write(value[:10])
                        raise OSError('injected temporary-write failure')
                yield PartialWrite()
        monkeypatch.setattr(exporter.tempfile, 'NamedTemporaryFile', failed_stream)
    else:
        def failed_replace(source, target):
            assert Path(source).read_bytes()
            raise OSError('injected replace failure')
        monkeypatch.setattr(exporter.os, 'replace', failed_replace)
    assert exporter.main([str(source), '--out', str(output)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'injected' in streams.err
    assert output.read_bytes() == previous
    assert list(tmp_path.glob('.x04-*')) == []
