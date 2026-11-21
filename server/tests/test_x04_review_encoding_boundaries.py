"""BOM, invalid UTF-8 and escaped surrogate inputs have explicit CLI outcomes."""
import builtins
import io
import json
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from compare_x04_offline_evidence import main as compare_main
from batch_validate_x04_evidence import main as batch_main


@pytest.mark.parametrize('kind', ['bom', 'invalid-utf8', 'surrogate'])
@pytest.mark.parametrize('command', ['comparison', 'batch'])
def test_encoding_boundary_and_recovery(kind, command, tmp_path, monkeypatch, capsys):
    encoded = json.dumps(worked_example()).encode('utf-8')
    if kind == 'bom': variant = b'\xef\xbb\xbf' + encoded
    elif kind == 'invalid-utf8': variant = b'\xff' + encoded
    else: variant = encoded[:-1] + b',"opaque":"\\ud800"}'
    valid, source, output = [tmp_path / name for name in ('valid', 'variant', 'out')]
    valid.write_bytes(encoded); source.write_bytes(variant); output.write_bytes(b'previous')
    def guard(opener):
        def open_file(path, *args, **kwargs):
            assert not str(path).endswith('notes.shx')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os): monkeypatch.setattr(module, 'open', guard(module.open))
    main = compare_main if command == 'comparison' else batch_main
    paths = [source, valid] if command == 'comparison' else [valid, source, valid]
    assert main([*map(str, paths), '--out', str(output)]) == (0 if kind == 'bom' else 2 if command == 'comparison' else 1)
    streams = capsys.readouterr(); assert streams.out == '' and 'Traceback' not in streams.err
    if kind != 'bom' and command == 'comparison':
        assert 'refused' in streams.err and output.read_bytes() == b'previous'
    else:
        report = json.loads(output.read_bytes())
        if command == 'comparison': assert report['count']['delta'] == 0
        else:
            assert [row['index'] for row in report['results']] == [0, 1, 2]
            assert [row['valid'] for row in report['results']] == [True, kind == 'bom', True]
    assert source.read_bytes() == variant and valid.read_bytes() == encoded
    source.write_bytes(encoded)
    assert main([*map(str, paths), '--out', str(output)]) == 0
    assert not list(tmp_path.glob('.x04-*'))
