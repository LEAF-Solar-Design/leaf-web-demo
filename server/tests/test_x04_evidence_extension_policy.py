"""Opaque X04 extensions preserve compatibility without replacing known claims."""
from copy import deepcopy
import builtins
import io
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from validate_x04_offline_evidence import validate_evidence


@pytest.mark.parametrize('location', ['envelope', 'drawing', 'intake', 'request', 'result'])
@pytest.mark.parametrize('forged', [None, 'count', 'highlight_handles'])
def test_opaque_extensions_cannot_replace_known_claims(location, forged, monkeypatch):
    payload = worked_example()
    owners = {'envelope': payload, 'drawing': payload['drawing'],
              'intake': payload['drawing']['intake'], 'request': payload['request'],
              'result': payload['result']}
    extension = {'count': 999, 'target': 'notes.shx', 'nested': [None, True, {'future': '東'}]}
    owners[location]['future_extension'] = deepcopy(extension)
    if forged == 'count':
        payload['result']['count'] = 999
    elif forged == 'highlight_handles':
        payload['result']['highlight_handles'] = ['BAD']
    before = deepcopy(payload)
    def guard(opener):
        def open_file(path, *args, **kwargs):
            assert not str(path).endswith('notes.shx'), 'Dependency targets stay unopened'
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os):
        monkeypatch.setattr(module, 'open', guard(module.open))
    if forged is None:
        assert validate_evidence(payload)['valid']
    else:
        with pytest.raises(ValueError, match='claim'):
            validate_evidence(payload)
    exported = export_evidence(payload)
    assert exported['result']['count'] == 7
    assert exported['result']['highlight_handles'] == ['A', 'B']
    assert exported['drawing'] == payload['drawing']
    assert 'future_extension' not in exported
    assert 'future_extension' not in exported['request']
    assert 'future_extension' not in exported['result']
    assert payload == before


@pytest.mark.parametrize('location', ['report', 'dependency'])
def test_dependency_extensions_refuse_exact_report_contract(location):
    payload = worked_example()
    owner = payload['observed_dependencies']
    if location == 'dependency':
        owner = owner['dependencies'][0]
    owner['future_extension'] = 'unknown'
    before = deepcopy(payload)
    with pytest.raises(ValueError, match='match embedded decoded records'):
        validate_evidence(payload)
    assert payload == before
