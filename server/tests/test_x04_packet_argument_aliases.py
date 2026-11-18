"""X04 packet usage and input aliases refuse before publication."""
import json
import os
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('args,code', [(['--help'], 0), ([], 2),
    (['input', '--unknown'], 2), (['input', '--max', '1'], 2),
    (['--out', 'output'], 2), (['input', '--out'], 2),
    *[(['input', option, value], 2) for option in ('--max-input-bytes', '--max-output-bytes')
      for value in ('0', '-1', 'bad')]])
def test_usage_before_io(args, code, tmp_path, monkeypatch, capsys):
    def denied(*args, **kwargs):
        raise AssertionError('Usage must precede IO')
    for name in ('read_json', 'write_json', 'check_output_path'):
        monkeypatch.setattr(checker, name, denied)
    with pytest.raises(SystemExit) as exited:
        checker.main(args)
    assert exited.value.code == code
    captured = capsys.readouterr()
    assert 'usage:' in (captured.out if code == 0 else captured.err)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('mode', ['normalized', 'hardlink', 'output-symlink', 'input-symlink'])
def test_alias_refusal_and_retry(mode, tmp_path, capsys):
    source = tmp_path / 'packet.json'; source.write_text(json.dumps(packet()), encoding='utf-8')
    raw = source.read_bytes(); alias = tmp_path / 'alias.json'
    if mode == 'normalized':
        (tmp_path / 'sub').mkdir(); target = tmp_path / 'sub/../packet.json'
    elif mode == 'hardlink':
        os.link(source, alias); target = alias
    elif mode == 'output-symlink':
        alias.symlink_to(source); target = alias
    else:
        alias.symlink_to(source); target, source = source, alias
    assert checker.main([str(source), '--out', str(target)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'refused' in captured.err
    assert source.read_bytes() == target.read_bytes() == raw
    assert not list(tmp_path.glob('.x04-*'))
    output = tmp_path / 'recovered.json'
    assert checker.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['valid'] and source.read_bytes() == raw
