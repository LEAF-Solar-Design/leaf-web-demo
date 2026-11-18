"""Portable claims must agree with embedded X04 synthetic inputs."""
from copy import deepcopy
import builtins
import io
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from validate_x04_offline_evidence import validate_evidence


@pytest.mark.parametrize('field', ['name', 'disposition', 'remapped_to', 'coverage', 'scope', 'opened', 'selection'])
def test_changed_claim_refuses_without_input_mutation_or_target_opens(field, monkeypatch):
    artifact = export_evidence(worked_example())
    report = artifact['observed_dependencies']
    if field in ('name', 'disposition', 'remapped_to'):
        report['dependencies'][0][field] = 'forged'
    elif field == 'coverage':
        report['coverage_note'] = 'Complete raw DWG inventory verified'
    elif field == 'scope':
        report['scope'] = 'raw_dwg'
    elif field == 'opened':
        report['external_files_opened'] = True
    else:
        artifact['request']['set_ids'] = artifact['request']['set_ids'][:1]
    before = deepcopy(artifact)
    opened = []
    def guarded(opener):
        def open_file(path, *args, **kwargs):
            if str(path).endswith('notes.shx'):
                opened.append(str(path))
                raise AssertionError('Dependency target opened')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os):
        monkeypatch.setattr(module, 'open', guarded(module.open))
    with pytest.raises(ValueError):
        validate_evidence(artifact)
    assert artifact == before and opened == []
