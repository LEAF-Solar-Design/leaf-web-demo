"""Concurrent artifact calls keep independent expected digests and trusted validation."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker
import export_x04_offline_evidence as shared


def test_artifact_pin_call_isolation_and_fresh_retry(tmp_path, monkeypatch, capsys):
    value = artifact(0); raw = json.dumps(value).encode(); digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / 'artifact.json'; source.write_bytes(raw)
    outputs = {name: tmp_path / (name + '.json') for name in ('match', 'stale')}
    for path in outputs.values(): path.write_bytes(b'previous')
    barrier = threading.Barrier(2); local = threading.local(); calls = {'match': 0, 'stale': 0}; decisions = {}
    decode, validate = shared.decode_json, checker.export_evidence
    def interleaved(encoded, pin=None):
        if encoded == raw and pin is not None:
            decisions[local.name] = pin; barrier.wait(timeout=20)
        return decode(encoded, pin)
    def trusted(item): calls[local.name] += 1; return validate(item)
    def run(name):
        local.name = name
        return checker.main([str(source), '--out', str(outputs[name]), '--expected-artifact-sha256', digest if name == 'match' else '0' * 64])
    with monkeypatch.context() as patch:
        patch.setattr(shared, 'decode_json', interleaved); patch.setattr(checker, 'export_evidence', trusted)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {name: pool.submit(run, name) for name in outputs}
            assert {name: job.result(timeout=30) for name, job in jobs.items()} == {'match': 0, 'stale': 2}
    assert calls == {'match': 1, 'stale': 0} and decisions == {'match': digest, 'stale': '0' * 64}
    assert json.loads(outputs['match'].read_bytes()) == checker.validate_evidence(value) and outputs['stale'].read_bytes() == b'previous'
    assert source.read_bytes() == raw and not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    assert checker.main([str(source), '--out', str(outputs['stale']), '--expected-artifact-sha256', digest]) == 0
    assert outputs['stale'].read_bytes() == outputs['match'].read_bytes()
