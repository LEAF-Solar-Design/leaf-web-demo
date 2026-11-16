"""Opaque saved-report extensions never upgrade the checker proof scope."""
from copy import deepcopy
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
from validate_x04_review_report import validate_report


@pytest.mark.parametrize('factory', [comparison, batch])
@pytest.mark.parametrize('extension', [
    {'opaque': {'nested': [False, None, 42, '../../unopened.json']}},
    {'provenance': {'authenticated': True, 'source_sha256': 'a' * 64,
                    'artifact_path': '//unopened/share/customer.dwg'}},
    {'underlying_artifacts_revalidated': True, 'embedded_paths_opened': True,
     'validation_scope': 'authenticated_source_bytes', 'valid': False},
])
def test_extensions_are_compatible_but_never_repeated_as_proof(factory, extension, monkeypatch):
    report = factory()
    report.update(deepcopy(extension))
    if 'results' in report:
        report['results'][0]['provenance'] = deepcopy(extension)
        report['results'][0]['recomputed_result']['opaque_path'] = '../../never-open.dwg'
    else:
        report['literal_dependencies']['added'] = [{'kind': 'font', 'name': '../../never-open.shx',
            'disposition': 'font remapped', 'provenance': deepcopy(extension)}]
    before = deepcopy(report)
    def denied(*args, **kwargs):
        raise AssertionError('No embedded target may be opened')
    monkeypatch.setattr('builtins.open', denied)
    monkeypatch.setattr('pathlib.Path.open', denied)
    checked = validate_report(report)
    assert checked == {'schema': 'leaf.x04.review-report-validation.v1', 'valid': True,
        'report_schema': report['schema'], 'validation_scope': 'saved_report_internal_consistency',
        'underlying_artifacts_revalidated': False, 'embedded_paths_opened': False,
        'original_bytes_integrity': 'unverified'}
    assert report == before


@pytest.mark.parametrize('factory', [comparison, batch])
def test_integrity_claim_is_reserved_even_with_opaque_provenance(factory):
    report = factory()
    report.update(original_bytes_integrity='verified', provenance={'trusted': True})
    with pytest.raises(ValueError, match='unverified'):
        validate_report(report)
