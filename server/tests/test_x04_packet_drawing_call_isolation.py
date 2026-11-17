"""Concurrent CLI drawing bindings remain call-local before trusted validation."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import pytest
from test_x04_packet_drawing_arguments import two_drawing_packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('identity', ['drawing=one', 'caf\u00e9'])
def test_drawing_binding_call_isolation(identity, tmp_path, monkeypatch, capsys):
    value = two_drawing_packet(first=identity); raw = json.dumps(value).encode(); digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / 'packet.json'; source.write_bytes(raw)
    outputs = {name: tmp_path / (name + '.json') for name in ('match', 'wrong')}
    for path in outputs.values(): path.write_bytes(b'previous')
    barrier = threading.Barrier(2); local = threading.local(); calls = {'match': 0, 'wrong': 0}
    read, validate = checker.read_json, checker.validate_evidence
    def interleaved(*args):
        value = read(*args); barrier.wait(timeout=20); return value
    def counted(item): calls[local.name] += 1; return validate(item)
    def arguments(name, ids):
        args = [str(source), '--out', str(outputs[name]), '--expected-packet-sha256', digest]
        for identity in ids: args += ['--expected-artifact-drawing', 'a=' + identity]
        return args
    def run(name):
        local.name = name
        return checker.main(arguments(name, [identity] if name == 'match' else ['wrong']))
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'read_json', interleaved); patch.setattr(checker, 'validate_evidence', counted)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {name: pool.submit(run, name) for name in outputs}
            assert {name: job.result(timeout=30) for name, job in jobs.items()} == {'match': 0, 'wrong': 2}
    assert calls == {'match': 2, 'wrong': 0} and source.read_bytes() == raw
    assert outputs['wrong'].read_bytes() == b'previous'
    assert json.loads(outputs['match'].read_bytes()) == checker.validate_packet(value)
    assert checker.main(arguments('wrong', [identity])) == 0
    assert outputs['wrong'].read_bytes() == outputs['match'].read_bytes()
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
