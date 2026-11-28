"""X04 export is byte-stable after normalization and explicit stream budgets recover."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from test_x04_untrusted_derived_counts import X04_ROOT, worked_example
from drawing_dependency_report import from_pairs
from export_x04_offline_evidence import export_evidence, write_json
from validate_x04_offline_evidence import validate_evidence


def test_dependency_report_accepts_complete_historical_and_current_profiles():
    source = worked_example()
    legacy = source['observed_dependencies']
    pairs = source['drawing']['dependency_pairs']
    pairs[:] = [[0, 'SECTION'], [2, 'TABLES'], [0, 'TABLE'], [2, 'STYLE'],
                *pairs, [0, 'ENDTAB'], [0, 'ENDSEC'],
                [0, 'SECTION'], [2, 'ENTITIES'], [0, 'LINE'], [5, 'A9'],
                [6, 'MissingDash'], [0, 'ENDSEC'], [0, 'EOF']]
    assert export_evidence(source)['observed_dependencies'] == legacy

    current = from_pairs(pairs)
    source['observed_dependencies'] = current
    assert any(row['kind'] == 'linetype' for row in current['dependencies'])
    assert export_evidence(source)['observed_dependencies'] == current

    incomplete = deepcopy(current)
    incomplete.pop('note_style_links')
    omitted_rows = {**current, 'dependencies': legacy['dependencies']}
    forged = deepcopy(legacy)
    forged['dependencies'][0]['name'] = 'forged.shx'
    for report in (incomplete, omitted_rows, forged):
        source['observed_dependencies'] = report
        with pytest.raises(ValueError, match='must match embedded decoded records'):
            export_evidence(source)


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
