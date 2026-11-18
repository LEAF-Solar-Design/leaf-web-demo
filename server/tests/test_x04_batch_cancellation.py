"""Cancellation stops the batch before any partial report publication."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
import batch_validate_x04_evidence as batch


@pytest.mark.parametrize('cancel_at', [1, 2, 3])
def test_cancellation_preserves_output_and_full_retry(cancel_at, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    target = tmp_path / 'output'; target.write_bytes(b'previous')
    original = batch.validate_evidence
    calls = []
    cancellation = KeyboardInterrupt('synthetic cancellation')
    def interrupted(payload):
        calls.append(payload['drawing']['id'])
        if len(calls) == cancel_at: raise cancellation
        return original(payload)
    args = [str(source)] * 4 + ['--out', str(target)]
    with monkeypatch.context() as patch:
        patch.setattr(batch, 'validate_evidence', interrupted)
        with pytest.raises(KeyboardInterrupt) as caught: batch.main(args)
    assert caught.value is cancellation and len(calls) == cancel_at
    streams = capsys.readouterr()
    assert streams.out == streams.err == ''
    assert target.read_bytes() == b'previous' and source.read_bytes() == before
    assert not list(tmp_path.glob('.x04-*'))
    assert batch.main(args) == 0
    report = json.loads(target.read_bytes())
    assert (report['total'], report['passed'], report['failed']) == (4, 4, 0)
    assert [row['index'] for row in report['results']] == list(range(4))
    assert capsys.readouterr().err == '' and source.read_bytes() == before
