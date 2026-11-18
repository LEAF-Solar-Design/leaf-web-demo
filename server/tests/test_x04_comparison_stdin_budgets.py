"""Either comparison side can stream within exact input and output byte ceilings."""
import io
import json
import sys
from types import SimpleNamespace
import pytest
from test_x04_evidence_compare import changed_example
from test_x04_untrusted_derived_counts import worked_example
from compare_x04_offline_evidence import compare_evidence, main


@pytest.mark.parametrize('side', [0, 1])
@pytest.mark.parametrize('budget', ['exact', 'input-under', 'output-under'])
def test_stdin_side_and_exact_encoded_budgets(side, budget, tmp_path, monkeypatch, capsys):
    payloads = [worked_example(), changed_example()]
    encoded = [json.dumps(value, ensure_ascii=False).encode('utf-8') for value in payloads]
    streamed = encoded[side] + b' ' * (max(map(len, encoded)) + 10 - len(encoded[side]))
    source = tmp_path / 'other.json'
    source.write_bytes(encoded[1 - side])
    before = source.read_bytes()
    target = tmp_path / 'delta.json'
    target.write_bytes(b'previous')
    expected = compare_evidence(*payloads)
    output = (json.dumps(expected, ensure_ascii=True, allow_nan=False, indent=2) + '\n').encode('utf-8')
    arguments = ['-', str(source)] if side == 0 else [str(source), '-']
    arguments += ['--out', str(target), '--max-input-bytes', str(len(streamed) - (budget == 'input-under')),
                  '--max-output-bytes', str(len(output) - (budget == 'output-under'))]
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(streamed)))
    assert main(arguments) == (0 if budget == 'exact' else 2)
    streams = capsys.readouterr()
    assert streams.out == '' and source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
    if budget == 'exact':
        assert streams.err == '' and target.read_bytes() == output
        assert expected['count'] == {'before': 7, 'after': 6, 'delta': -1}
        assert expected['highlight_handles'] == {'removed': ['B'], 'added': []}
    else:
        assert 'refused' in streams.err and 'Traceback' not in streams.err
        assert target.read_bytes() == b'previous'
        monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(streamed)))
        arguments[arguments.index('--max-input-bytes') + 1] = str(len(streamed))
        arguments[arguments.index('--max-output-bytes') + 1] = str(len(output))
        assert main(arguments) == 0 and target.read_bytes() == output
        assert capsys.readouterr().out == ''


def test_dual_stdin_refuses_without_consumption(tmp_path, monkeypatch, capsys):
    stream = io.BytesIO(b'{unread')
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=stream))
    target = tmp_path / 'out'; target.write_bytes(b'previous')
    assert main(['-', '-', '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err and 'Traceback' not in streams.err
    assert stream.tell() == 0 and target.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*'))
