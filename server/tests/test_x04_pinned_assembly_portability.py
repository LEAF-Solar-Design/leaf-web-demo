"""Pinned assembly remains portable evidence after its explicitly read files disappear."""
import hashlib
import json
from test_x04_assembly_source_pins import pinned_files
from test_x04_review_subprocess_chain import run_tool


def test_pinned_assembly_then_source_free_packet_validation(tmp_path):
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    source, manifest, expected = pinned_files(bundle); source.write_text(json.dumps(manifest), encoding='utf-8')
    pin = hashlib.sha256(source.read_bytes()).hexdigest()
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    output = tmp_path / 'packet.json'
    result = run_tool('assemble_x04_evidence_packet.py', [source, '--expected-manifest-sha256', pin, '--out', output], unrelated)
    assert result.returncode == 0 and result.stdout == result.stderr == b''
    assert json.loads(output.read_bytes()) == expected and b'expected_sha256' not in output.read_bytes()
    for path in bundle.iterdir(): path.unlink()
    bundle.rmdir(); assert not bundle.exists()
    checked = run_tool('validate_x04_evidence_packet.py', [output], unrelated)
    assert checked.returncode == 0 and checked.stderr == b''
    summary = json.loads(checked.stdout)
    assert [r['recomputed_result']['count'] for r in summary['validated_artifacts']] == [6, 1]
    assert summary['comparisons'][0]['report']['count'] == {'before': 6, 'after': 1, 'delta': -5}
    assert [r['recomputed_result']['count'] for r in summary['batches'][0]['report']['results']] == [6, 1, 6]
    assert summary['original_bytes_integrity'] == 'unverified' and summary['external_paths_opened'] is False
