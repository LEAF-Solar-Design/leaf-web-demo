"""Read errors and cancellations preserve outputs and every explicit source."""
import io
import json
from pathlib import Path
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('position', [0, 1, 3], ids=['first', 'middle', 'last'])
@pytest.mark.parametrize('kind', ['permission', 'mid-read', 'cancel'])
def test_source_read_fault_is_atomic_and_recovers(tmp_path, monkeypatch, capsys, position, kind):
    source, manifest, expected = manifest_files(tmp_path)
    ordered = [tmp_path / row['path'] for section in ('artifacts', 'comparisons', 'batches')
               for row in manifest[section]]
    target = ordered[position]
    snapshot = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    cancellation = KeyboardInterrupt('synthetic read cancellation')
    fault = cancellation if kind == 'cancel' else OSError('synthetic mid-read failure')
    original_open = Path.open; seen = []
    class FailingRead(io.BytesIO):
        def __init__(self, data): super().__init__(data); self.reads = 0
        def read(self, amount=-1):
            self.reads += 1
            if self.reads > 1: raise fault
            return super().read(min(amount, 7))
    def opened(path, mode='r', *args, **kwargs):
        if mode == 'rb' and path in ordered:
            seen.append(path)
            if path == target:
                if kind == 'permission': raise PermissionError('synthetic denied')
                return FailingRead(snapshot[path])
        return original_open(path, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', opened)
        if kind == 'cancel':
            with pytest.raises(KeyboardInterrupt) as caught:
                assembler.main([str(source), '--out', str(output)])
            assert caught.value is cancellation
        else:
            assert assembler.main([str(source), '--out', str(output)]) == 2
    assert seen == ordered[:position + 1]
    assert output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    assert all(p.read_bytes() == data for p, data in snapshot.items())
    assert capsys.readouterr().out == ''
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
