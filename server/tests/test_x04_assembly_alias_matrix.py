"""Output aliases to every explicit read position refuse and preserve source bytes."""
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('name', ['manifest.json', 'a.json', 'c.json', 'batch.json'])
@pytest.mark.parametrize('kind', ['normalized', 'hardlink', 'symlink'])
def test_source_position_aliases_and_corrected_output(name, kind, tmp_path, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    target = tmp_path / name; output = tmp_path / 'alias.json'
    if kind == 'normalized':
        directory = tmp_path / 'nested'; directory.mkdir(); output = directory / '..' / name
    else:
        try: output.hardlink_to(target) if kind == 'hardlink' else output.symlink_to(target)
        except OSError as error: pytest.skip(str(error))
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert assembler.main([str(source), '--out', str(output)]) == 2
    assert all(p.read_bytes() == data for p, data in snapshots.items()) and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == ''
    recovered = tmp_path / 'recovered.json'
    assert assembler.main([str(source), '--out', str(recovered)]) == 0
    assert json.loads(recovered.read_bytes()) == expected
