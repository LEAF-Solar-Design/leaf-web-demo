"""Each explicit file has its own strict JSON byte ceiling, including whitespace."""
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('name', ['manifest.json', 'a.json', 'c.json', 'batch.json'])
def test_exact_and_one_byte_under_per_file_limits(tmp_path, capsys, name):
    source, manifest, expected = manifest_files(tmp_path)
    inputs = list(tmp_path.iterdir())
    target = tmp_path / name
    ceiling = max(p.stat().st_size for p in inputs) + 256
    target.write_bytes(target.read_bytes() + b' ' * (ceiling - target.stat().st_size))
    snapshot = {p: p.read_bytes() for p in inputs}
    output = tmp_path / 'packet.json'
    args = [str(source), '--out', str(output), '--max-input-bytes']
    assert assembler.main(args + [str(ceiling)]) == 0
    previous = output.read_bytes()
    assert json.loads(previous) == expected
    assert sum(len(data) for data in snapshot.values()) > ceiling
    assert assembler.main(args + [str(ceiling - 1)]) == 2
    assert output.read_bytes() == previous and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == ''
    assert all(p.read_bytes() == data for p, data in snapshot.items())
    assert assembler.main(args + [str(ceiling)]) == 0 and output.read_bytes() == previous


@pytest.mark.parametrize('name', ['manifest.json', 'a.json', 'c.json', 'batch.json'])
@pytest.mark.parametrize('kind', ['duplicate-key', 'trailing-json'])
def test_strict_json_per_file_refusal_and_recovery(tmp_path, capsys, name, kind):
    source, manifest, expected = manifest_files(tmp_path)
    target = tmp_path / name; original = target.read_bytes()
    if kind == 'duplicate-key':
        target.write_bytes(original.replace(b'{', b'{"schema":"duplicate",', 1))
    else: target.write_bytes(original + b'\n{}')
    changed = target.read_bytes()
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    assert assembler.main([str(source), '--out', str(output)]) == 2
    assert output.read_bytes() == b'previous' and target.read_bytes() == changed
    assert capsys.readouterr().out == '' and not list(tmp_path.glob('.x04-*'))
    target.write_bytes(original)
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
