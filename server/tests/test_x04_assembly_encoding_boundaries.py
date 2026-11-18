"""Raw BOM/UTF-8 and escaped Unicode handling is deterministic for each role."""
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('name', ['manifest.json', 'a.json', 'c.json', 'batch.json'])
@pytest.mark.parametrize('encoding', ['bom', 'invalid-utf8', 'escaped-surrogate'])
def test_file_encoding_refusal_or_exact_literal_and_recovery(tmp_path, capsys, name, encoding):
    source, manifest, expected = manifest_files(tmp_path)
    target = tmp_path / name; original = target.read_bytes()
    value = json.loads(original)
    if encoding == 'bom': changed = b'\xef\xbb\xbf' + original
    elif encoding == 'invalid-utf8': changed = original + b'\xff'
    else:
        value['opaque'] = '\ud800'
        changed = json.dumps(value, ensure_ascii=True).encode('ascii')
    target.write_bytes(changed)
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    code = assembler.main([str(source), '--out', str(output)])
    accepted = encoding == 'bom' or (encoding == 'escaped-surrogate' and name == 'manifest.json')
    assert code == (0 if accepted else 2)
    if accepted:
        packet = json.loads(output.read_bytes()); assert packet == expected
    else: assert output.read_bytes() == b'previous'
    assert target.read_bytes() == changed and not list(tmp_path.glob('.x04-*'))
    captured = capsys.readouterr(); assert captured.out == '' and 'Traceback' not in captured.err
    target.write_bytes(original)
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
