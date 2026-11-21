"""Manifest shape defects refuse before any explicit payload file is read."""
from copy import deepcopy
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


CASES = [('root', None, value) for value in (None, [], True, 3, 'schema')]
CASES += [('schema', None, value) for value in (None, [], {}, False, 4, 'wrong')]
CASES += [('section', section, value) for section in ('artifacts', 'comparisons', 'batches')
          for value in (None, {}, True, 4, 'rows')]
CASES += [('row', section, value) for section in ('artifacts', 'comparisons', 'batches')
          for value in (None, [], True, 4, 'row')]
CASES += [('path', section, value) for section in ('artifacts', 'comparisons', 'batches')
          for value in (None, [], {}, True, 4, '', ' \t\n')]
CASES += [('duplicate', section, None) for section in ('artifacts', 'comparisons', 'batches')]


@pytest.mark.parametrize('kind,section,bad', CASES)
def test_invalid_manifest_refuses_before_payload_reads_and_recovers(tmp_path, monkeypatch, capsys, kind, section, bad):
    source, valid, expected = manifest_files(tmp_path)
    changed = deepcopy(valid)
    if kind == 'root': changed = bad
    elif kind == 'schema': changed['schema'] = bad
    elif kind == 'section': changed[section] = bad
    elif kind == 'row': changed[section][-1] = bad
    elif kind == 'path': changed[section][-1]['path'] = bad
    else: changed[section].append(deepcopy(changed['artifacts'][0]))
    source.write_text(json.dumps(changed), encoding='utf-8')
    snapshot = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    opened = []; read = assembler.read_json
    def guarded(path, limit):
        opened.append(path)
        if path != source.resolve(): raise AssertionError('Payload read before manifest refusal')
        return read(path, limit)
    with monkeypatch.context() as patch:
        patch.setattr(assembler, 'read_json', guarded)
        assert assembler.main([str(source), '--out', str(output)]) == 2
    assert opened == [source.resolve()]
    assert output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    captured = capsys.readouterr()
    assert captured.out == '' and 'refused' in captured.err and 'Traceback' not in captured.err
    assert all(p.read_bytes() == data for p, data in snapshot.items())
    source.write_text(json.dumps(valid), encoding='utf-8')
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
