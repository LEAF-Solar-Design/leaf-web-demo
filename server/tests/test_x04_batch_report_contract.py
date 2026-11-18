"""X04 batch report consistency preserves literal repeated path observations."""
from copy import deepcopy
import json
import pytest
from test_x04_comparison_delta_algebra import artifact
from batch_validate_x04_evidence import validate_paths
from validate_x04_offline_evidence import validate_evidence
from validate_x04_review_report import BATCH, validate_report


def batch():
    checked = validate_evidence(artifact(0))
    return {'schema': BATCH, 'total': 3, 'passed': 2, 'failed': 1,
            'original_bytes_integrity': 'unverified', 'results': [
                {'index': 0, 'path': '../../unopened.json', **deepcopy(checked)},
                {'index': 1, 'path': '../../unopened.json', 'valid': False, 'error': ''},
                {'index': 2, 'path': '../../unopened.json', **deepcopy(checked)}]}


def test_genuine_batch_repeated_paths_and_all_failed(tmp_path, monkeypatch):
    source = tmp_path / 'artifact.json'
    source.write_text(json.dumps(artifact(0)), encoding='utf-8')
    reports = [validate_paths([source, source, tmp_path / 'missing']),
               validate_paths([tmp_path / 'missing', tmp_path / 'missing'])]
    def denied(*args, **kwargs):
        raise AssertionError('Report validation must not open embedded paths')
    monkeypatch.setattr('builtins.open', denied)
    monkeypatch.setattr('pathlib.Path.open', denied)
    for report in reports:
        before = deepcopy(report)
        assert validate_report(report)['valid'] is True
        assert report == before


@pytest.mark.parametrize('change', [
    lambda r: r.update(total=4),
    lambda r: r.update(passed=1, failed=2),
    lambda r: r.update(passed=True),
    lambda r: r.update(original_bytes_integrity='authenticated'),
    lambda r: r['results'][1].update(index=2),
    lambda r: r['results'][0].update(index=False),
    lambda r: r['results'][0].update(valid=1),
    lambda r: r['results'][0].update(error=None),
    lambda r: r['results'][1].update(drawing_id='forged'),
    lambda r: r['results'][1].update(recomputed_result={}),
    lambda r: r['results'][1].update(error='x' * 132),
    lambda r: r['results'][0]['recomputed_result'].update(count=True),
    lambda r: r['results'][0]['recomputed_result'].update(count=-1),
    lambda r: r['results'][0]['recomputed_result'].update(highlight_handles=['A', 'A']),
    lambda r: r['results'][0]['recomputed_result'].update(original_bytes_integrity='verified'),
])
def test_batch_contradictions_refuse_atomically(change):
    report = batch()
    change(report)
    before = deepcopy(report)
    with pytest.raises(ValueError):
        validate_report(report)
    assert report == before


def test_literal_empty_paths_and_empty_report_are_structurally_valid():
    report = batch()
    for row in report['results']:
        row['path'] = ''
    assert validate_report(report)['valid'] is True
    report.update(results=[], total=0, passed=0, failed=0)
    assert validate_report(report)['valid'] is True
