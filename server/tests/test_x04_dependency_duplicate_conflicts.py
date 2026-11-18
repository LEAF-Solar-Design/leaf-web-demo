"""Duplicate decoded records have deterministic local dispositions, without I/O."""
from itertools import permutations
import builtins
import io
import os
from drawing_dependency_report import from_pairs


def test_conflicting_flags_and_ignored_remap_metadata_are_order_independent(monkeypatch):
    records = [
        [(0, 'STYLE'), (3, 'shared.shx'), (70, '0'), (340, 'first-remap')],
        [(0, 'STYLE'), (3, 'shared.shx'), (70, '2'), (340, 'other-remap')],
        [(0, 'STYLE'), (3, 'shared.shx'), (70, '1'), (340, 'shape-remap')],
        [(0, 'BLOCK'), (1, 'shared.dwg'), (70, '4')],
        [(0, 'BLOCK'), (1, 'shared.dwg'), (70, '8')],
        [(0, 'BLOCK'), (1, 'shared.dwg'), (70, '0')],
    ]
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError('Decoded report must never follow any target')
    for module in (builtins, io, os):
        monkeypatch.setattr(module, 'open', forbidden)
    expected = None
    for order in permutations(records):
        pairs = [pair for record in order for pair in record]
        before = list(pairs)
        report = from_pairs(pairs)
        if expected is None:
            expected = report
        assert report == expected and pairs == before
    rows = expected['dependencies']
    assert {(row['kind'], row['name'], row['disposition']) for row in rows} == {
        ('font', 'shared.shx', 'font remapped'),
        ('shape_font', 'shared.shx', 'font remapped'), ('xref', 'shared.dwg', 'detached')}
    assert all(row['remapped_to'] == 'sans-serif' for row in rows if row['kind'] != 'xref')
    assert calls == []
