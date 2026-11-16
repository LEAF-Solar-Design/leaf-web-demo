"""X04 input budgets bound binary reads for files and stdin."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


class ReadSpy(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.requests = []
        self.consumed = 0

    def read(self, size=-1):
        assert 0 < size <= 65536, 'Input reads must have a bounded positive size'
        self.requests.append(size)
        result = super().read(size)
        self.consumed += len(result)
        return result


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('transport', ['file', 'stdin'])
@pytest.mark.parametrize('oversized', [False, True])
def test_exact_boundary_and_oversized_input_reads(main, transport, oversized, tmp_path, monkeypatch, capsys):
    encoded = json.dumps(worked_example()).encode('utf-8')
    limit = len(encoded)
    supplied = encoded + (b' ' * 10000 if oversized else b'')
    source = tmp_path / 'input.json'
    source.write_bytes(supplied)
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    spy = ReadSpy(supplied)
    opened = []
    if transport == 'stdin':
        monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=spy))
        input_path = '-'
    else:
        original = Path.open
        @contextmanager
        def open_file(path, *args, **kwargs):
            if path == source:
                opened.append(path)
                assert args == ('rb',)
                yield spy
            else:
                with original(path, *args, **kwargs) as stream:
                    yield stream
        monkeypatch.setattr(Path, 'open', open_file)
        input_path = str(source)
    status = main([input_path, '--max-input-bytes', str(limit), '--out', str(target)])
    streams = capsys.readouterr()
    assert streams.out == '' and not list(tmp_path.glob('.x04-*'))
    if oversized:
        assert status == 2 and 'byte ceiling' in streams.err
        assert target.read_bytes() == b'previous'
        if transport == 'file':
            assert opened == [] and spy.requests == []
        else:
            assert spy.consumed == limit + 1
    else:
        assert status == 0 and streams.err == ''
        assert spy.consumed == limit and spy.requests[-1] == 1
        output = json.loads(target.read_bytes())
        result = output.get('result', output.get('recomputed_result'))
        assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']
