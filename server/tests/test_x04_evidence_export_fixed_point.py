"""X04 export is byte-stable after normalization and explicit stream budgets recover."""
from copy import deepcopy
import json
import subprocess
import sys
from test_x04_untrusted_derived_counts import X04_ROOT, worked_example
from export_x04_offline_evidence import export_evidence, write_json
from validate_x04_offline_evidence import validate_evidence


def test_repeated_export_validation_has_stable_serialized_bytes(tmp_path):
    source = worked_example()
    source['drawing']['future_extension'] = {'literal': '東\nC:/synthetic/fixture', 'values': [None, True, 1.5]}
    source['drawing']['intake']['source_path'] = 'C:/synthetic/input.dwg'
    source['request']['set_ids'] *= 2
    source['result']['count'] = 999
    before = deepcopy(source)
    current = source
    first_bytes = None
    target = tmp_path / 'export.json'
    for _ in range(10):
        current_before = deepcopy(current)
        artifact = export_evidence(current)
        assert current == current_before
        assert validate_evidence(artifact)['recomputed_result'] == artifact['result']
        assert artifact['result']['count'] == 7 and artifact['result']['highlight_handles'] == ['A', 'B']
        assert artifact['drawing'] == before['drawing']
        write_json(artifact, target)
        encoded = target.read_bytes()
        if first_bytes is None:
            first_bytes = encoded
        assert encoded == first_bytes
        assert not list(tmp_path.glob('.x04-*'))
        current = json.loads(encoded)
    assert source == before


def test_escape_amplification_refuses_small_budget_then_exact_budget_recovers(tmp_path):
    source = worked_example()
    source['drawing']['future_extension'] = {'literal': '東' * 1000}
    input_bytes = json.dumps(source, ensure_ascii=False).encode('utf-8')
    exported = subprocess.run([sys.executable, '-P', str(X04_ROOT / 'scripts/export_x04_offline_evidence.py'), '-'],
                              input=input_bytes, capture_output=True, cwd=tmp_path, timeout=30)
    assert exported.returncode == 0 and exported.stderr == b''
    assert len(exported.stdout) > len(input_bytes)
    target = tmp_path / 'validation.json'
    target.write_bytes(b'previous')
    command = [sys.executable, '-P', str(X04_ROOT / 'scripts/validate_x04_offline_evidence.py'), '-',
               '--out', str(target), '--max-input-bytes']
    refused = subprocess.run(command + [str(len(input_bytes))], input=exported.stdout,
                             capture_output=True, cwd=tmp_path, timeout=30)
    assert refused.returncode == 2 and refused.stdout == b'' and b'byte ceiling' in refused.stderr
    assert target.read_bytes() == b'previous'
    recovered = subprocess.run(command + [str(len(exported.stdout))], input=exported.stdout,
                               capture_output=True, cwd=tmp_path, timeout=30)
    assert recovered.returncode == 0 and recovered.stdout == recovered.stderr == b''
    result = json.loads(target.read_bytes())['recomputed_result']
    assert result == {'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
