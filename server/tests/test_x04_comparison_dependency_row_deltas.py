"""Whole dependency rows retain literal identity and producer dispositions."""
import builtins
from copy import deepcopy
import io
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence

NAME = '../same-target'
PAIRS = {'font': [[0, 'STYLE'], [3, NAME]],
         'shape': [[0, 'STYLE'], [70, '1'], [3, NAME]],
         'image': [[0, 'IMAGEDEF'], [1, NAME]],
         'underlay': [[0, 'PDFDEFINITION'], [1, NAME]],
         'xref': [[0, 'BLOCK'], [70, '4'], [1, NAME]]}


def artifact(kind):
    value = worked_example()
    value['drawing']['dependency_pairs'] = deepcopy(PAIRS[kind])
    value['observed_dependencies'] = from_pairs(value['drawing']['dependency_pairs'])
    return export_evidence(value)


@pytest.mark.parametrize('left', list(PAIRS))
@pytest.mark.parametrize('right', list(PAIRS))
def test_whole_row_deltas_and_no_target_opens(left, right, monkeypatch):
    opened = []
    def guard(opener):
        def open_file(path, *args, **kwargs):
            if str(path) == NAME:
                opened.append(str(path)); raise AssertionError('Dependency target opened')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os): monkeypatch.setattr(module, 'open', guard(module.open))
    a, b = artifact(left), artifact(right)
    originals = deepcopy((a, b))
    result = compare_evidence(a, b)
    expected = {'removed': [], 'added': []} if left == right else {
        'removed': a['observed_dependencies']['dependencies'], 'added': b['observed_dependencies']['dependencies']}
    assert result['literal_dependencies'] == expected
    assert result['count']['delta'] == 0 and (a, b) == originals and not opened
    # Object-key order cannot create a literal-row membership change.
    for row in b['observed_dependencies']['dependencies']:
        entries = list(row.items()); row.clear(); row.update(reversed(entries))
    assert compare_evidence(a, b)['literal_dependencies'] == expected


@pytest.mark.parametrize('field', ['disposition', 'remapped_to'])
def test_forged_same_kind_disposition_or_remap_is_not_independently_valid(field):
    a = artifact('font'); b = deepcopy(a)
    b['observed_dependencies']['dependencies'][0][field] = 'forged'
    with pytest.raises(ValueError, match='embedded decoded records'): compare_evidence(a, b)
