"""Destination refusal and symlink replacement never alter unrelated sentinel bytes."""
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('kind', ['directory', 'missing-parent', 'unrelated-symlink'])
def test_destination_contract_and_recovery(kind, tmp_path, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    sentinel = tmp_path / 'sentinel'; sentinel.write_bytes(b'unrelated sentinel')
    output = tmp_path / 'packet.json'
    if kind == 'directory': output.mkdir()
    elif kind == 'missing-parent': output = tmp_path / 'absent' / 'packet.json'
    else:
        try: output.symlink_to(sentinel)
        except OSError as error: pytest.skip(str(error))
    assert assembler.main([str(source), '--out', str(output)]) == (0 if kind == 'unrelated-symlink' else 2)
    assert sentinel.read_bytes() == b'unrelated sentinel'
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    assert not list(tmp_path.rglob('.x04-*')) and capsys.readouterr().out == ''
    if kind == 'unrelated-symlink':
        assert not output.is_symlink() and json.loads(output.read_bytes()) == expected
    elif kind == 'directory': assert output.is_dir() and not list(output.iterdir())
    else: assert not output.parent.exists()
    recovered = tmp_path / 'recovered.json'
    assert assembler.main([str(source), '--out', str(recovered)]) == 0
    assert json.loads(recovered.read_bytes()) == expected
