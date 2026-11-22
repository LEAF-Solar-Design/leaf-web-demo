"""Assembler writer faults retain source/destination bytes and clean only owned temps."""
import json
from pathlib import Path
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler
import export_x04_offline_evidence as shared


@pytest.mark.parametrize('stage', ['write', 'flush', 'close', 'replace'])
@pytest.mark.parametrize('cancel', [False, True])
def test_assembler_publication_fault_and_retry(stage, cancel, tmp_path, monkeypatch, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    other = tmp_path / '.x04-other-owner'; other.write_bytes(b'other owner')
    original = shared.tempfile.NamedTemporaryFile; paths = []
    failure = KeyboardInterrupt('synthetic assembly cancellation') if cancel else OSError('synthetic writer fault')
    class Temporary:
        def __init__(self, **kwargs):
            self.stream = original(**kwargs); self.name = self.stream.name; paths.append(Path(self.name))
        def __enter__(self): return self
        def write(self, value):
            if stage == 'write': self.stream.write(value[:3]); raise failure
            return self.stream.write(value)
        def flush(self):
            self.stream.flush()
            if stage == 'flush': raise failure
        def __exit__(self, *args):
            self.stream.close()
            if stage == 'close': raise failure
    def replace(*args): raise failure
    with monkeypatch.context() as patch:
        patch.setattr(shared.tempfile, 'NamedTemporaryFile', Temporary)
        if stage == 'replace': patch.setattr(shared.os, 'replace', replace)
        if cancel:
            with pytest.raises(KeyboardInterrupt) as caught: assembler.main([str(source), '--out', str(output)])
            assert caught.value is failure
        else: assert assembler.main([str(source), '--out', str(output)]) == 2
    assert output.read_bytes() == b'previous' and all(p.read_bytes() == data for p, data in snapshots.items())
    assert paths and all(not path.exists() for path in paths)
    assert list(tmp_path.glob('.x04-*')) == [other] and other.read_bytes() == b'other owner'
    assert capsys.readouterr().out == ''
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
