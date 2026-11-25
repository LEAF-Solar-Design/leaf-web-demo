"""Batch permutations move indexed outcomes without changing totals or values."""
from itertools import permutations
import builtins
import io
import json
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from batch_validate_x04_evidence import validate_paths


@pytest.mark.parametrize('order', list(permutations(range(4))))
def test_order_changes_only_occurrence_order(order, tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ('full', 'insert-only', 'forged', 'missing')]
    value = worked_example(); paths[0].write_text(json.dumps(value), encoding='utf-8')
    value['request']['set_ids'] = ['["INSERT","East","Panel"]']
    paths[1].write_text(json.dumps(export_evidence(value)), encoding='utf-8')
    value = worked_example(); value['result']['count'] = 999
    paths[2].write_text(json.dumps(value), encoding='utf-8')
    before = {path: path.read_bytes() for path in paths[:3]}
    def guard(opener):
        def open_file(path, *args, **kwargs):
            assert not str(path).endswith('notes.shx')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os): monkeypatch.setattr(module, 'open', guard(module.open))
    baseline = validate_paths(paths)
    result = validate_paths([paths[i] for i in order])
    assert (result['total'], result['passed'], result['failed']) == (4, 2, 2)
    for index, row in enumerate(result['results']):
        expected = {**baseline['results'][order[index]], 'index': index}
        assert row == expected
        if row['valid']: assert row['recomputed_result']['count'] == (7 if order[index] == 0 else 6)
    assert validate_paths([paths[0]])['passed'] == 1
    assert {path: path.read_bytes() for path in before} == before
