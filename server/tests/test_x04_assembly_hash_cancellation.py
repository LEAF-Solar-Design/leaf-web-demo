"""Pinned hash/decode cancellations preserve identity and close streams."""
import json
from pathlib import Path
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler
import export_x04_offline_evidence as shared


@pytest.mark.parametrize('budgeted', [False, True])
@pytest.mark.parametrize('stage', ['hash', 'decode'])
def test_pinned_cancellation_preserves_identity_and_retry(stage, budgeted, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path); source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}; raw = snapshots[tmp_path / 'a.json']
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    cancellation = KeyboardInterrupt('synthetic pinned cancellation')
    opened = []; streams = []; original_open = Path.open
    sha, loads = shared.hashlib.sha256, shared.json.loads
    def observed(path, mode='r', *a, **kw):
        stream = original_open(path, mode, *a, **kw)
        if mode == 'rb': opened.append(path); streams.append(stream)
        return stream
    def cancel_hash(data=b'', *a, **kw):
        if data == raw: raise cancellation
        return sha(data, *a, **kw)
    def cancel_decode(value, *a, **kw):
        if value == raw.decode('utf-8'): raise cancellation
        return loads(value, *a, **kw)
    args = [str(source), '--out', str(output)] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', observed)
        if stage == 'hash': patch.setattr(shared.hashlib, 'sha256', cancel_hash)
        else: patch.setattr(shared.json, 'loads', cancel_decode)
        with pytest.raises(KeyboardInterrupt) as caught: assembler.main(args)
        assert caught.value is cancellation
    assert opened == [source, tmp_path / 'a.json'] and all(stream.closed for stream in streams)
    assert output.read_bytes() == b'previous' and all(p.read_bytes() == data for p, data in snapshots.items())
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    assert assembler.main(args) == 0 and json.loads(output.read_bytes()) == expected
