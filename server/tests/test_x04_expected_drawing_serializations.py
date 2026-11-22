"""Equivalent artifact encodings retain exact decoded drawing identity."""
import json
import pytest
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker


@pytest.mark.parametrize('encoding', ['compact', 'reordered', 'escaped-id', 'bom-crlf'])
def test_decoded_identity_across_encodings(encoding, tmp_path, monkeypatch, capsys):
    value = artifact(0); identity = ' caf\u00e9 \U0001f680 '
    value['drawing']['id'] = value['request']['drawing_id'] = identity
    encodings = {
        'compact': json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(),
        'reordered': json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode(),
        'escaped-id': json.dumps(value, ensure_ascii=True, indent=2).encode(),
        'bom-crlf': b'\xef\xbb\xbf' + json.dumps(value, ensure_ascii=False, indent=2).replace('\n', '\r\n').encode()}
    assert len(set(encodings.values())) == 4
    raw = encodings[encoding]; source = tmp_path / 'artifact.json'; source.write_bytes(raw)
    output = tmp_path / 'checked.json'; expected = checker.validate_evidence(value)
    args = [str(source), '--out', str(output), '--expected-drawing-id']
    assert checker.main(args + [identity]) == 0 and json.loads(output.read_bytes()) == expected
    previous = output.read_bytes(); calls = []
    def forbidden(item): calls.append(item); raise AssertionError('Mismatched identity must precede recomputation')
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'export_evidence', forbidden)
        assert checker.main(args + [identity.strip()]) == 2
    assert calls == [] and source.read_bytes() == raw and output.read_bytes() == previous
    assert checker.main(args + [identity]) == 0 and output.read_bytes() == previous
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
