"""Ignored lookalike fields cannot substitute for recognized row pins."""
import hashlib
import json
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('budgeted', [False, True])
@pytest.mark.parametrize('location', ['top-level', 'row-extension', 'nested-extension'])
def test_misplaced_pins_do_not_enforce_coverage(location, budgeted, tmp_path, capsys):
    source, manifest, expected = pinned_files(tmp_path)
    for key in ('artifacts', 'comparisons', 'batches'):
        for row in manifest[key]: del row['expected_sha256']
    target = tmp_path / 'a.json'; original = target.read_bytes(); stale = hashlib.sha256(original).hexdigest()
    target.write_bytes(original + b' '); changed = target.read_bytes()
    if location == 'top-level': manifest['expected_sha256'] = stale
    elif location == 'row-extension': manifest['artifacts'][0]['extension'] = {'expected_sha256': stale}
    else: manifest['extension'] = {'artifacts': [{'expected_sha256': stale}]}
    source.write_text(json.dumps(manifest), encoding='utf-8')
    output = tmp_path / 'packet.json'
    args = [str(source), '--out', str(output)] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    assert assembler.main(args) == 0 and json.loads(output.read_bytes()) == expected
    previous = output.read_bytes()
    manifest['artifacts'][0]['expected_sha256'] = stale
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main(args) == 2 and output.read_bytes() == previous and target.read_bytes() == changed
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    manifest['artifacts'][0]['expected_sha256'] = hashlib.sha256(changed).hexdigest()
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main(args) == 0 and output.read_bytes() == previous
