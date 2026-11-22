"""Equivalent JSON serializations require their own exact report byte digests."""
import hashlib
import json
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as checker


@pytest.mark.parametrize('factory', [comparison, batch])
@pytest.mark.parametrize('kind', ['compact', 'reordered', 'escaped-unicode', 'bom-crlf'])
def test_exact_encoding_pins_and_cross_encoding_refusals(factory, kind, tmp_path, monkeypatch, capsys):
    value = factory(); value['ignored_note'] = 'módulo 模块 \U0001f680'
    encodings = {
        'compact': json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(),
        'reordered': json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode(),
        'escaped-unicode': json.dumps(value, ensure_ascii=True, indent=2).encode(),
        'bom-crlf': b'\xef\xbb\xbf' + json.dumps(value, ensure_ascii=False, indent=2).replace('\n', '\r\n').encode()}
    assert len(set(encodings.values())) == 4
    raw = encodings[kind]; source = tmp_path / 'report.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; expected = checker.validate_report(value)
    args = [str(source), '--out', str(output), '--expected-report-sha256']
    assert checker.main(args + [hashlib.sha256(raw).hexdigest()]) == 0
    previous = output.read_bytes(); assert json.loads(previous) == expected
    calls = []
    def forbidden(item): calls.append(item); raise AssertionError('Cross-encoding digest cannot reach trusted validation')
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_report', forbidden)
        for name, other in encodings.items():
            if name != kind:
                assert checker.main(args + [hashlib.sha256(other).hexdigest()]) == 2
                assert output.read_bytes() == previous and source.read_bytes() == raw
    assert calls == [] and not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    assert checker.main(args + [hashlib.sha256(raw).hexdigest()]) == 0
    assert json.loads(output.read_bytes()) == expected
