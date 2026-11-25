"""Usage handling precedes reads, trusted validation and output creation."""
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import assemble_x04_evidence_packet as assembler


CASES = [([], 2), (['--help'], 0), (['--unknown'], 2),
         (['manifest', '--max-in', '1'], 2), (['manifest', '--max-total', '1'], 2),
         (['manifest', '--max-out', '1'], 2), (['manifest', '--out'], 2),
         (['manifest', 'extra'], 2)]
for flag in ('--max-input-bytes', '--max-total-input-bytes', '--max-output-bytes'):
    CASES += [(['manifest', flag, value], 2) for value in ('0', '-1', '1.5', 'nan', 'abc')]
    CASES.append((['manifest', flag], 2))


@pytest.mark.parametrize('arguments,code', CASES)
def test_usage_does_not_reach_io_or_validation(arguments, code, tmp_path, monkeypatch, capsys):
    output = tmp_path / 'never-created.json'
    def forbidden(*args, **kwargs): raise AssertionError('Usage must precede I/O and validation')
    for name in ('read_json', 'check_output_path', 'assemble_manifest', 'validate_packet', 'write_json'):
        monkeypatch.setattr(assembler, name, forbidden)
    monkeypatch.setattr(assembler.InputBudget, 'read_json', forbidden)
    monkeypatch.setattr(Path, 'resolve', forbidden)
    with pytest.raises(SystemExit) as caught:
        assembler.main([*arguments, '--out', str(output)] if '--out' not in arguments else arguments)
    assert caught.value.code == code and not output.exists()
    text = capsys.readouterr()
    assert 'Traceback' not in text.err


def test_documented_unicode_repeated_read_total(tmp_path, capsys):
    from test_x04_packet_assembly import manifest_files
    source, manifest, expected = manifest_files(tmp_path)
    manifest['artifacts'][1]['path'] = 'a.json'
    manifest['comparisons'] = []; manifest['batches'] = []
    payload = expected['artifacts'][0]['artifact']; payload['drawing']['note'] = 'módulo 模块'
    (tmp_path / 'a.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    source.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    total = len(source.read_bytes()) + 2 * len((tmp_path / 'a.json').read_bytes())
    assert len((tmp_path / 'a.json').read_bytes()) > len((tmp_path / 'a.json').read_text(encoding='utf-8'))
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    args = [str(source), '--out', str(output), '--max-total-input-bytes']
    assert assembler.main(args + [str(total - 1)]) == 2 and output.read_bytes() == b'previous'
    assert assembler.main(args + [str(total)]) == 0
    value = json.loads(output.read_bytes())
    assert [r['artifact']['result']['count'] for r in value['artifacts']] == [6, 6]
    assert all(r['artifact']['drawing']['note'] == 'módulo 模块' for r in value['artifacts'])
    assert capsys.readouterr().out == ''
