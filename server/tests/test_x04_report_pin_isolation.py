"""Concurrent report calls keep independent expected digests and trusted validation."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as checker
import export_x04_offline_evidence as shared


@pytest.mark.parametrize('factory', [comparison, batch])
def test_report_pin_call_isolation_and_fresh_retry(factory, tmp_path, monkeypatch, capsys):
    value = factory(); raw = json.dumps(value).encode(); digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / 'report.json'; source.write_bytes(raw)
    outputs = {name: tmp_path / (name + '.json') for name in ('match', 'stale')}
    for path in outputs.values(): path.write_bytes(b'previous')
    barrier = threading.Barrier(2); local = threading.local(); calls = {'match': 0, 'stale': 0}; decisions = {}
    decode, validate = shared.decode_json, checker.validate_report
    def interleaved(encoded, pin=None):
        if encoded == raw and pin is not None:
            decisions[local.name] = pin; barrier.wait(timeout=20)
        return decode(encoded, pin)
    def trusted(item): calls[local.name] += 1; return validate(item)
    def run(name):
        local.name = name
        return checker.main([str(source), '--out', str(outputs[name]), '--expected-report-sha256', digest if name == 'match' else '0' * 64])
    with monkeypatch.context() as patch:
        patch.setattr(shared, 'decode_json', interleaved); patch.setattr(checker, 'validate_report', trusted)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {name: pool.submit(run, name) for name in outputs}
            assert {name: job.result(timeout=30) for name, job in jobs.items()} == {'match': 0, 'stale': 2}
    assert calls == {'match': 1, 'stale': 0} and decisions == {'match': digest, 'stale': '0' * 64}
    assert json.loads(outputs['match'].read_bytes()) == checker.validate_report(value) and outputs['stale'].read_bytes() == b'previous'
    assert source.read_bytes() == raw and not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    assert checker.main([str(source), '--out', str(outputs['stale']), '--expected-report-sha256', digest]) == 0
    assert outputs['stale'].read_bytes() == outputs['match'].read_bytes()
