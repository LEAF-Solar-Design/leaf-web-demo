"""Each observed primary/bigfont reference keeps its local kind and disposition."""
import builtins
import io
import os
import pytest
from drawing_dependency_report import from_pairs


@pytest.mark.parametrize('groups,expected', [
    ([(3, 'primary.shx'), (4, 'big.shx')], {('font', 'primary.shx'), ('shape_font', 'big.shx')}),
    ([(4, 'big.shx')], {('shape_font', 'big.shx')}),
    ([(3, ''), (4, 'big.shx')], {('shape_font', 'big.shx')}),
    ([(3, 'shared.shx'), (4, 'shared.shx')], {('font', 'shared.shx'), ('shape_font', 'shared.shx')}),
    ([(70, '1'), (3, 'shared.shx'), (4, 'shared.shx')], {('shape_font', 'shared.shx')}),
])
def test_observed_bigfont_references_are_literal_and_never_opened(groups, expected, monkeypatch):
    pairs = [(0, 'SECTION'), (2, 'TABLES'), (0, 'STYLE'), *groups, (0, 'ENDSEC'), (0, 'EOF')]
    before = list(pairs)
    opened = []
    def no_open(*args, **kwargs):
        opened.append(args)
        raise AssertionError('Never open font targets')
    for module in (builtins, io, os):
        monkeypatch.setattr(module, 'open', no_open)
    report = from_pairs(pairs)
    assert {(row['kind'], row['name']) for row in report['dependencies']} == expected
    assert all(row['disposition'] == 'font remapped' and row['remapped_to'] == 'sans-serif'
               for row in report['dependencies'])
    assert report['external_files_opened'] is False and pairs == before and opened == []
