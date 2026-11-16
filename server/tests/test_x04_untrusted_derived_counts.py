"""Saved result claims cannot replace trusted counting from physical entities."""
from copy import deepcopy
import json
import pytest
import tool_loader
from test_x04_minsert_arrays import TOOL, selected
from pathlib import Path
import sys

X04_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(X04_ROOT / 'scripts'))
from export_x04_offline_evidence import export_evidence, main as export_main
from validate_x04_offline_evidence import validate_evidence, main as validate_main


def worked_example():
    text = (X04_ROOT / 'docs/x04-offline-report-export-contract.md').read_text(encoding='utf-8')
    return json.loads(text.split('```json\n', 1)[1].split('```', 1)[0])


def test_portable_worked_example_exports_and_validates_without_target_opens(monkeypatch):
    import builtins
    import io
    import os
    source = worked_example()
    before = deepcopy(source)
    opened = []
    def guard(opener):
        def open_file(path, *args, **kwargs):
            if str(path).endswith('notes.shx'):
                opened.append(str(path))
                raise AssertionError('Never open observed dependency targets')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os):
        monkeypatch.setattr(module, 'open', guard(module.open))
    artifact = export_evidence(source)
    assert artifact['result'] == {'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
    assert artifact['observed_dependencies'] == source['observed_dependencies']
    assert validate_evidence(artifact)['valid'] is True
    assert source == before and opened == []


@pytest.mark.parametrize('field,value', [('count', 700), ('count', True),
    ('highlight_handles', ['A', 'BAD']), ('highlight_handles', ['A', 'A', 'B']),
    ('original_bytes_integrity', 'verified')])
def test_validator_refuses_forged_claims_but_export_recomputes(field, value):
    source = worked_example(); source['result'][field] = value
    before = deepcopy(source)
    with pytest.raises(ValueError):
        validate_evidence(source)
    assert export_evidence(source)['result']['count'] == 7 and source == before


@pytest.mark.parametrize('mutation', ['schema', 'identity', 'selector', 'report'])
def test_invalid_portable_inputs_refuse_before_emission(mutation, tmp_path, capsys):
    source = worked_example()
    if mutation == 'schema': source['schema'] = 'unknown'
    if mutation == 'identity': source['request']['drawing_id'] = 'another-drawing'
    if mutation == 'selector': source['request']['set_ids'] = ['unknown-set']
    if mutation == 'report': source['observed_dependencies']['dependencies'] = [None]
    path = tmp_path / 'input.json'; path.write_text(json.dumps(source))
    assert export_main([str(path)]) == 2
    assert capsys.readouterr().out == ''
    assert validate_main([str(path)]) == 2
    assert capsys.readouterr().out == ''


def test_portable_cli_replaces_output_only_after_complete_valid_result(tmp_path, capsys):
    source = worked_example(); path = tmp_path / 'input.json'; output = tmp_path / 'evidence.json'
    path.write_text(json.dumps(source)); output.write_text('previous')
    assert export_main([str(path), '--out', str(output)]) == 0
    assert validate_main([str(output)]) == 0
    assert json.loads(capsys.readouterr().out)['valid'] is True
    source['schema'] = 'invalid'; path.write_text(json.dumps(source))
    before = output.read_bytes()
    assert export_main([str(path), '--out', str(output)]) == 2
    assert output.read_bytes() == before and not list(tmp_path.glob('.x04-*'))


@pytest.mark.parametrize('claims', [
    {'count': 999999, 'selected_sets': [{'set_id': 'forged', 'count': 999999}]},
    {'count': -1, 'selected_set': {'layer': 'Wrong'}, 'overlay': {'highlight_handles': ['BAD']}},
    {'count': None, 'selected_sets': None, 'highlight_handles': ['DEAD']},
    {'count': {'bad': True}, 'selected_sets': 'all', 'overlay': [False]},
])
def test_forged_saved_claims_are_opaque_and_recomputed(claims):
    intake = {'inserts': [{'name': 'Panel', 'layer': 'A', 'handle': '0x0a', 'rows': 2, 'columns': 3},
                          {'name': 'Panel', 'layer': 'B', 'handle': 'A', 'rows': 2, 'columns': 3},
                          {'name': 'Panel', 'layer': 'B', 'handle': 'B'}], **claims}
    saved = json.loads(json.dumps(intake))
    before = deepcopy(saved)
    outcome = selected(saved)
    assert outcome['ok'] and outcome['result']['count'] == 7
    assert set(outcome['overlay']['highlight_handles']) == {'A', 'B'}
    assert outcome['result']['original_bytes_integrity'] == 'unverified'
    listed = tool_loader.run_tool_dynamic(TOOL, saved, {}, aps_live=False)
    assert listed['ok'] and listed['result']['selection_required'] and listed['result']['count'] is None
    refused = tool_loader.run_tool_dynamic(TOOL, saved, {'set_ids': ['forged']}, aps_live=False)
    assert not refused['ok'] and not refused.get('result') and not refused.get('overlay')
    assert saved == before
