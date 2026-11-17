"""Expected packet inventory arguments refuse before I/O and preserve defaults."""
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('extra', [
    *[['--expected-artifact-id', value] for value in ('', ' ', '../x', '1', '\u00e9', 'a' * 65)],
    ['--expected-artifact-id', 'a', '--expected-artifact-id', 'a'],
    ['--expected-artifact-id'], ['--expected-artifact', 'a'], ['--expected-artifact-i', 'a'],
])
def test_expected_inventory_usage_precedes_io(extra, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs): raise AssertionError('Inventory usage must precede I/O')
    for name in ('check_output_path', 'read_json', 'validate_packet', 'validate_evidence', 'write_json'):
        monkeypatch.setattr(checker, name, forbidden)
    output = tmp_path / 'never.json'
    with pytest.raises(SystemExit) as caught: checker.main(['-', '--out', str(output), *extra])
    assert caught.value.code == 2 and not output.exists()
    captured = capsys.readouterr(); assert 'usage:' in captured.err and captured.out == '' and 'Traceback' not in captured.err


@pytest.mark.parametrize('stdin', [False, True])
def test_exact_inventory_and_omitted_default_compatibility(stdin, tmp_path, monkeypatch, capsys):
    value = packet(); raw = json.dumps(value).encode(); source = tmp_path / 'packet.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; expected = checker.validate_packet(value)
    def run(ids=None):
        if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        args = ['-' if stdin else str(source), '--out', str(output)]
        for identity in ids or []: args += ['--expected-artifact-id', identity]
        return checker.main(args)
    assert run() == 0 and json.loads(output.read_bytes()) == expected
    previous = output.read_bytes()
    assert run(['b', 'a']) == 0 and output.read_bytes() == previous
    with monkeypatch.context() as patch:
        def forbidden(*args): raise AssertionError('Inventory mismatch cannot trust artifacts')
        patch.setattr(checker, 'validate_evidence', forbidden)
        assert run(['a']) == 2 and output.read_bytes() == previous
    assert run(['a', 'b']) == 0 and output.read_bytes() == previous
    assert source.read_bytes() == raw and not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
