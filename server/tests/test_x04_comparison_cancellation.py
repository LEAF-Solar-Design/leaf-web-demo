"""Comparison cancellation retains exception identity and prior publication."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
import compare_x04_offline_evidence as comparison


@pytest.mark.parametrize('stage', ['second-validation', 'encoding'])
def test_comparison_cancel_and_retry(stage, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes(); output = tmp_path / 'out'; output.write_bytes(b'previous')
    cancellation = KeyboardInterrupt('synthetic comparison cancellation')
    validate, write = comparison.validate_evidence, comparison.write_json
    calls = []
    def interrupted_validation(payload):
        calls.append(payload)
        if len(calls) == 2: raise cancellation
        return validate(payload)
    def interrupted_encoding(*args, **kwargs):
        original = json.JSONEncoder.iterencode
        def interrupted(self, value, *rest, **options):
            for chunk in original(self, value, *rest, **options):
                yield chunk
                raise cancellation
        with monkeypatch.context() as encoding_patch:
            encoding_patch.setattr(json.JSONEncoder, 'iterencode', interrupted)
            return write(*args, **kwargs)
    args = [str(source), str(source), '--out', str(output)]
    with monkeypatch.context() as patch:
        patch.setattr(comparison, 'validate_evidence' if stage == 'second-validation' else 'write_json',
                      interrupted_validation if stage == 'second-validation' else interrupted_encoding)
        with pytest.raises(KeyboardInterrupt) as caught: comparison.main(args)
    assert caught.value is cancellation and output.read_bytes() == b'previous'
    assert source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
    streams = capsys.readouterr(); assert streams.out == streams.err == ''
    assert comparison.main(args) == 0 and json.loads(output.read_bytes())['count']['delta'] == 0
