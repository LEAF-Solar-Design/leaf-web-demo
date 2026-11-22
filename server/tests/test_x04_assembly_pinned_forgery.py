"""Matching byte hashes do not bypass trusted entity/report semantics."""
import hashlib
import json
import pytest
from test_x04_assembly_source_pins import pinned_files
from compare_x04_offline_evidence import compare_evidence
from validate_x04_offline_evidence import validate_evidence
from validate_x04_review_report import validate_report
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('budgeted', [False, True])
@pytest.mark.parametrize('kind', ['count', 'comparison', 'batch'])
def test_correctly_pinned_forgery_still_refuses(kind, budgeted, tmp_path, capsys):
    source, manifest, expected = pinned_files(tmp_path)
    target = tmp_path / {'count': 'a.json', 'comparison': 'c.json', 'batch': 'batch.json'}[kind]
    original = target.read_bytes(); forged = json.loads(original)
    a, b = [r['artifact'] for r in expected['artifacts']]
    if kind == 'count': forged['result']['count'] = 999
    elif kind == 'comparison': forged = compare_evidence(a, a); assert validate_report(forged)['valid']
    else:
        forged['results'] = [{'index': i, 'path': '../../unopened.json', **validate_evidence(b)} for i in range(3)]
        assert validate_report(forged)['valid']
    target.write_text(json.dumps(forged), encoding='utf-8')
    for key in ('artifacts', 'comparisons', 'batches'):
        for row in manifest[key]: row['expected_sha256'] = hashlib.sha256((tmp_path / row['path']).read_bytes()).hexdigest()
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    args = [str(source), '--out', str(output)] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    assert assembler.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'expected_sha256' not in captured.err
    assert ('count' if kind == 'count' else 'contradict') in captured.err
    assert output.read_bytes() == b'previous' and all(p.read_bytes() == data for p, data in snapshots.items())
    assert not list(tmp_path.glob('.x04-*'))
    target.write_bytes(original)
    for key in ('artifacts', 'comparisons', 'batches'):
        for row in manifest[key]: row['expected_sha256'] = hashlib.sha256((tmp_path / row['path']).read_bytes()).hexdigest()
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main(args) == 0 and json.loads(output.read_bytes()) == expected
