"""Identical forged claims on both sides still require independent validation."""
import builtins
import io
import json
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from compare_x04_offline_evidence import main


@pytest.mark.parametrize('side', ['left', 'right', 'both'])
@pytest.mark.parametrize('claim', ['count', 'highlight', 'dependency'])
def test_forged_sides_refuse_and_retry(side, claim, tmp_path, monkeypatch, capsys):
    payloads = [worked_example(), worked_example()]
    for index in ([0, 1] if side == 'both' else [0 if side == 'left' else 1]):
        if claim == 'count': payloads[index]['result']['count'] = 999
        elif claim == 'highlight': payloads[index]['result']['highlight_handles'] = ['BAD']
        else: payloads[index]['observed_dependencies']['dependencies'][0]['remapped_to'] = 'forged'
    paths = [tmp_path / name for name in ('left.json', 'right.json')]
    for path, value in zip(paths, payloads): path.write_text(json.dumps(value), encoding='utf-8')
    originals = [path.read_bytes() for path in paths]
    opened = []
    def guard(opener):
        def open_file(path, *args, **kwargs):
            if str(path).endswith('notes.shx'):
                opened.append(str(path)); raise AssertionError('Dependency target opened')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os): monkeypatch.setattr(module, 'open', guard(module.open))
    output = tmp_path / 'delta.json'; output.write_bytes(b'previous')
    assert main([*map(str, paths), '--out', str(output)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err and 'Traceback' not in streams.err
    assert output.read_bytes() == b'previous' and [path.read_bytes() for path in paths] == originals
    assert not list(tmp_path.glob('.x04-*'))
    for path in paths: path.write_text(json.dumps(worked_example()), encoding='utf-8')
    assert main([*map(str, paths), '--out', str(output)]) == 0
    result = json.loads(output.read_bytes())
    assert result['count'] == {'before': 7, 'after': 7, 'delta': 0}
    assert result['highlight_handles'] == {'removed': [], 'added': []}
    assert result['original_bytes_integrity'] == 'unverified'
    assert not opened and capsys.readouterr().err == ''
