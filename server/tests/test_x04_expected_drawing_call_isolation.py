"""Expected drawing IDs remain local to concurrent validator invocations."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker


def test_expected_identity_call_isolation_and_fresh_retry(tmp_path, monkeypatch, capsys):
    value = artifact(0); identity = value['drawing']['id']; raw = json.dumps(value).encode()
    source = tmp_path / 'artifact.json'; source.write_bytes(raw)
    outputs = {name: tmp_path / (name + '.json') for name in ('match', 'wrong')}
    for output in outputs.values(): output.write_bytes(b'previous')
    barrier = threading.Barrier(2); local = threading.local(); calls = {'match': 0, 'wrong': 0}
    read, export = checker.read_json, checker.export_evidence
    def interleaved(*args):
        payload = read(*args); barrier.wait(timeout=20); return payload
    def counted(payload): calls[local.name] += 1; return export(payload)
    def run(name):
        local.name = name
        return checker.main([str(source), '--out', str(outputs[name]), '--expected-drawing-id',
                             identity if name == 'match' else identity + '-wrong'])
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'read_json', interleaved); patch.setattr(checker, 'export_evidence', counted)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {name: pool.submit(run, name) for name in outputs}
            assert {name: job.result(timeout=30) for name, job in jobs.items()} == {'match': 0, 'wrong': 2}
    assert calls == {'match': 1, 'wrong': 0} and source.read_bytes() == raw
    assert outputs['wrong'].read_bytes() == b'previous'
    assert json.loads(outputs['match'].read_bytes()) == checker.validate_evidence(value)
    assert checker.main([str(source), '--out', str(outputs['wrong']), '--expected-drawing-id', identity]) == 0
    assert outputs['wrong'].read_bytes() == outputs['match'].read_bytes()
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
