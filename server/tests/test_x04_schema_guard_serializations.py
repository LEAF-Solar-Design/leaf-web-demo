"""Expected report kinds survive equivalent encodings with their own byte pins."""
import hashlib
import json
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as checker


@pytest.mark.parametrize('factory,kind', [(comparison, 'comparison'), (batch, 'batch')])
@pytest.mark.parametrize('encoding', ['compact', 'reordered', 'escaped-unicode', 'bom-crlf'])
def test_encoding_correct_kind_and_wrong_kind_refusal(factory, kind, encoding, tmp_path, monkeypatch, capsys):
    value = factory(); value['ignored_note'] = 'm\u00f3dulo \u6a21\u5757 \U0001f680'
    encodings = {
        'compact': json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(),
        'reordered': json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode(),
        'escaped-unicode': json.dumps(value, ensure_ascii=True, indent=2).encode(),
        'bom-crlf': b'\xef\xbb\xbf' + json.dumps(value, ensure_ascii=False, indent=2).replace('\n', '\r\n').encode()}
    assert len(set(encodings.values())) == 4
    raw = encodings[encoding]; source = tmp_path / 'report.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; digest = hashlib.sha256(raw).hexdigest()
    args = [str(source), '--out', str(output), '--expected-report-sha256', digest, '--expected-report-schema']
    assert checker.main(args + [kind]) == 0
    previous = output.read_bytes(); assert json.loads(previous) == checker.validate_report(value)
    calls = []
    def forbidden(item): calls.append(item); raise AssertionError('Wrong kind precedes report semantics')
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_report', forbidden)
        assert checker.main(args + ['batch' if kind == 'comparison' else 'comparison']) == 2
    assert calls == [] and source.read_bytes() == raw and output.read_bytes() == previous
    assert checker.main(args + [kind]) == 0 and output.read_bytes() == previous
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
