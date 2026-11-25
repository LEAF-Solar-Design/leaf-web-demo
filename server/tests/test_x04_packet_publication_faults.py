"""Packet publication faults retain prior bytes and clean only owned temporary files."""
import json
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker
import export_x04_offline_evidence as shared


@pytest.mark.parametrize('stage', ['write', 'flush', 'replace'])
@pytest.mark.parametrize('cancel', [False, True])
def test_packet_fault_and_retry(stage, cancel, tmp_path, monkeypatch, capsys):
    source, output = tmp_path / 'packet.json', tmp_path / 'checked.json'
    source.write_text(json.dumps(packet()), encoding='utf-8'); before = source.read_bytes()
    output.write_bytes(b'prior')
    other = tmp_path / '.x04-other-writer'; other.write_bytes(b'other owner')
    original = shared.tempfile.NamedTemporaryFile
    paths = []
    failure = KeyboardInterrupt('Synthetic packet cancellation') if cancel else OSError('Synthetic packet write fault')
    class Temporary:
        def __init__(self, **kwargs):
            self.stream = original(**kwargs); self.name = self.stream.name; paths.append(shared.Path(self.name))
        def __enter__(self):
            return self
        def write(self, value):
            if stage == 'write':
                self.stream.write(value[:3]); raise failure
            return self.stream.write(value)
        def flush(self):
            self.stream.flush()
            if stage == 'flush':
                raise failure
        def __exit__(self, *args):
            self.stream.close()
    def fail_replace(*args):
        raise failure
    with monkeypatch.context() as patch:
        patch.setattr(shared.tempfile, 'NamedTemporaryFile', Temporary)
        if stage == 'replace':
            patch.setattr(shared.os, 'replace', fail_replace)
        if cancel:
            with pytest.raises(KeyboardInterrupt) as caught:
                checker.main([str(source), '--out', str(output)])
            assert caught.value is failure
        else:
            assert checker.main([str(source), '--out', str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and ('refused' in captured.err) == (not cancel)
    assert 'Traceback' not in captured.err
    assert source.read_bytes() == before and output.read_bytes() == b'prior'
    assert paths and all(not path.exists() for path in paths)
    assert list(tmp_path.glob('.x04-*')) == [other] and other.read_bytes() == b'other owner'
    assert checker.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['original_bytes_integrity'] == 'unverified'
