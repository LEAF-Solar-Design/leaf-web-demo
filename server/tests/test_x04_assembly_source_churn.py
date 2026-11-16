"""Each explicit occurrence observes the source's current bytes, not a cached value."""
from copy import deepcopy
import json
import pytest
from test_x04_packet_assembly import manifest_files
from test_x04_comparison_delta_algebra import artifact
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('variant', ['valid', 'malformed', 'missing', 'stale-report'])
def test_repeated_source_changes_are_not_cached(tmp_path, monkeypatch, capsys, variant):
    source, manifest, expected = manifest_files(tmp_path)
    target = tmp_path / 'a.json'; original = target.read_bytes()
    manifest['artifacts'][1]['path'] = 'a.json'
    if variant != 'stale-report':
        manifest['comparisons'] = []; manifest['batches'] = []
    source.write_text(json.dumps(manifest), encoding='utf-8')
    changed = artifact(1); encoded = json.dumps(changed).encode()
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    read = assembler.read_json; observed = []
    def current(path, limit):
        if path == target:
            if observed:
                if variant == 'missing': target.unlink()
                else: target.write_bytes(b'{broken' if variant == 'malformed' else encoded)
            result = read(path, limit); observed.append(deepcopy(result)); return result
        return read(path, limit)
    with monkeypatch.context() as patch:
        patch.setattr(assembler, 'read_json', current)
        # For the stale case, second occurrence is the original a payload instead
        # of b, so the saved a->b report must refuse.
        if variant == 'stale-report': encoded = original
        code = assembler.main([str(source), '--out', str(output)])
    assert code == (0 if variant == 'valid' else 2)
    if variant == 'valid':
        packet = json.loads(output.read_bytes())
        assert [r['artifact']['result']['count'] for r in packet['artifacts']] == [6, 1]
        assert [r['result']['count'] for r in observed] == [6, 1]
    else: assert output.read_bytes() == b'previous'
    assert capsys.readouterr().out == '' and not list(tmp_path.glob('.x04-*'))
    target.write_bytes(original)
    manifest['comparisons'] = []; manifest['batches'] = []
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert [r['artifact']['result']['count'] for r in json.loads(output.read_bytes())['artifacts']] == [6, 6]
