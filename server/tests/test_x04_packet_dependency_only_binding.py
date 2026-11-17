"""Literal dependency-only edits bind whole rows and never open their targets."""
from copy import deepcopy
from pathlib import Path
import pytest
import tool_loader
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence
from validate_x04_review_report import validate_report
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('kind', ['name', 'disposition'])
def test_dependency_only_cross_binding(kind, monkeypatch):
    raw = worked_example(); a = export_evidence(raw)
    raw['drawing']['dependency_pairs'] = [[0, 'STYLE'], [3, '../../unopened/new.shx']] if kind == 'name' else [[0, 'IMAGEDEF'], [1, 'notes.shx']]
    raw['observed_dependencies'] = from_pairs(raw['drawing']['dependency_pairs'])
    b = export_evidence(raw); matching = compare_evidence(a, b)
    assert a['result'] == b['result']
    assert matching['count']['delta'] == 0 and matching['highlight_handles'] == {'removed': [], 'added': []}
    assert matching['literal_dependencies']['removed'] and matching['literal_dependencies']['added']
    value = {'schema': checker.SCHEMA, 'artifacts': [{'id': 'a', 'artifact': a}, {'id': 'b', 'artifact': b}],
             'comparisons': [{'id': 'c', 'before': 'a', 'after': 'b', 'report': compare_evidence(a, a)}], 'batches': []}
    assert validate_report(value['comparisons'][0]['report'])['valid']
    real_open = Path.open
    def guarded(path, *args, **kwargs):
        assert path.resolve() == tool_loader.PLATFORM_PACKAGE_REGISTRY.resolve(), 'Dependency target opened'
        return real_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded)
        before = deepcopy(value)
        with pytest.raises(ValueError, match='contradict'):
            checker.validate_packet(value)
        assert value == before
        value['comparisons'][0]['report'] = matching
        before = deepcopy(value)
        assert checker.validate_packet(value)['comparisons'][0]['report'] == matching
        assert value == before
