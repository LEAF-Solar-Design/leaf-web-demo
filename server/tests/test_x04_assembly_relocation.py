"""Explicit relative sources remain portable after their original home is removed."""
import json
from test_x04_packet_assembly import manifest_files
from test_x04_review_subprocess_chain import run_tool


def test_relocated_manifest_replays_offline_without_original_directory(tmp_path):
    original = tmp_path / 'original'; original.mkdir()
    source, manifest, expected = manifest_files(original)
    snapshot = {p.name: p.read_bytes() for p in original.iterdir()}
    relocated = tmp_path / 'relocated'
    original.rename(relocated)
    assert not original.exists()
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    # Network-denied safe-path child; no synthetic dependency targets exist.
    output = tmp_path / 'packet.json'
    result = run_tool('assemble_x04_evidence_packet.py', [relocated / source.name, '--out', output], unrelated)
    assert result.returncode == 0 and result.stdout == result.stderr == b''
    assert json.loads(output.read_bytes()) == expected
    checked = run_tool('validate_x04_evidence_packet.py', [output], unrelated)
    assert checked.returncode == 0 and checked.stderr == b''
    summary = json.loads(checked.stdout)
    assert [r['recomputed_result']['count'] for r in summary['validated_artifacts']] == [6, 1]
    assert summary['comparisons'][0]['report']['count'] == {'before': 6, 'after': 1, 'delta': -5}
    assert [r['recomputed_result']['highlight_handles'] for r in summary['batches'][0]['report']['results']] == [['A'], ['B'], ['A']]
    assert summary['external_paths_opened'] is False and summary['original_bytes_integrity'] == 'unverified'
    assert {p.name: p.read_bytes() for p in relocated.iterdir()} == snapshot
