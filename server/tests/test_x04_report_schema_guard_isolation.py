"""Concurrent expected report kinds remain independent across calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as checker


@pytest.mark.parametrize('factory,kind', [(comparison, 'comparison'), (batch, 'batch')])
def test_call_local_schema_decisions_and_fresh_retry(factory, kind, tmp_path, monkeypatch, capsys):
    value = factory(); raw = json.dumps(value).encode(); digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / 'report.json'; source.write_bytes(raw)
    outputs = {name: tmp_path / (name + '.json') for name in ('match', 'wrong')}
    for path in outputs.values(): path.write_bytes(b'previous')
    barrier = threading.Barrier(2); local = threading.local(); calls = {'match': 0, 'wrong': 0}
    read, validate = checker.read_json, checker.validate_report
    def interleaved(*args):
        value = read(*args); barrier.wait(timeout=20); return value
    def checked(item): calls[local.name] += 1; return validate(item)
    def run(name):
        local.name = name
        selected = kind if name == 'match' else ('batch' if kind == 'comparison' else 'comparison')
        return checker.main([str(source), '--out', str(outputs[name]), '--expected-report-sha256', digest,
                             '--expected-report-schema', selected])
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'read_json', interleaved); patch.setattr(checker, 'validate_report', checked)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {name: pool.submit(run, name) for name in outputs}
            assert {name: job.result(timeout=30) for name, job in jobs.items()} == {'match': 0, 'wrong': 2}
    assert calls == {'match': 1, 'wrong': 0}
    assert outputs['wrong'].read_bytes() == b'previous' and source.read_bytes() == raw
    assert json.loads(outputs['match'].read_bytes()) == checker.validate_report(value)
    assert checker.main([str(source), '--out', str(outputs['wrong']), '--expected-report-schema', kind]) == 0
    assert outputs['wrong'].read_bytes() == outputs['match'].read_bytes()
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
