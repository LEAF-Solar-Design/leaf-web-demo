"""Encoded packet bytes, including ASCII escapes and newline, govern output budgets."""
import json
from test_x04_packet_assembly import manifest_files
import pytest
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('literal', ['ASCII', '\U0001f680', '\r\n\t\x1b'])
def test_exact_output_budget_refusal_and_recovery(literal, tmp_path, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    path = tmp_path / 'a.json'; value = json.loads(path.read_bytes()); value['drawing']['note'] = literal
    path.write_text(json.dumps(value, ensure_ascii=True), encoding='utf-8')
    expected['artifacts'][0]['artifact'] = value
    encoded = (json.dumps(expected, ensure_ascii=True, allow_nan=False, indent=2) + '\n').encode()
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    args = [str(source), '--out', str(output), '--max-output-bytes']
    assert assembler.main(args + [str(len(encoded) - 1)]) == 2
    assert output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == '' and all(p.read_bytes() == data for p, data in snapshots.items())
    assert assembler.main(args + [str(len(encoded))]) == 0 and output.read_bytes() == encoded
