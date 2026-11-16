"""X04 count replay cannot authenticate optional saved original-byte metadata."""
from copy import deepcopy
import base64
import hashlib
import json

import pytest
import tool_loader
from dxf_intake import parse_dxf_bytes
from test_x04_original_bytes_replay import RAW, guard_source
from test_x04_minsert_arrays import TOOL, selected


@pytest.mark.parametrize('tamper', ['none', 'payload', 'encoding', 'length', 'digest', 'types', 'missing'])
def test_saved_original_metadata_never_claims_verified_bytes(tamper, tmp_path, monkeypatch):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    source = tmp_path / 'original.dxf'
    attempts = guard_source(monkeypatch, source)
    intake = parse_dxf_bytes(RAW, source_name=str(source))
    # These optional saved fields are synthetic unknown extensions, not a claim
    # that the real intake serializes raw bytes using this schema.
    metadata = {'original_bytes_base64': base64.b64encode(RAW).decode('ascii'),
                'original_byte_length': len(RAW), 'original_sha256': hashlib.sha256(RAW).hexdigest()}
    if tamper == 'payload':
        metadata['original_bytes_base64'] = base64.b64encode(RAW + b'tampered').decode('ascii')
    elif tamper == 'encoding':
        metadata['original_bytes_base64'] = '%%% invalid base64'
    elif tamper == 'length':
        metadata['original_byte_length'] += 1
    elif tamper == 'digest':
        metadata['original_sha256'] = '0' * 64
    elif tamper == 'types':
        metadata = {'original_bytes_base64': [], 'original_byte_length': False, 'original_sha256': {}}
    elif tamper == 'missing':
        metadata = {}
    intake.update(metadata)
    saved = json.loads(json.dumps(intake, allow_nan=False))
    snapshot = deepcopy(saved)
    outcomes = []
    for params in ({}, None):
        outcome = (tool_loader.run_tool_dynamic(TOOL, saved, params, aps_live=False)
                   if params is not None else selected(saved))
        assert outcome['ok']
        assert outcome['result']['original_bytes_integrity'] == 'unverified'
        assert not outcome['result'].get('verified_sha256')
        assert saved == snapshot and attempts == []
        outcomes.append(outcome)
    assert outcomes[0]['result']['count'] is None
    assert outcomes[1]['result']['count'] == 7
    assert outcomes[1]['overlay']['highlight_handles'] == ['A', 'B']
    assert selected(saved)['result'] == outcomes[1]['result']


def test_independent_hash_comparison_needs_the_actual_original_bytes():
    # Test oracle only: the count tool intentionally makes no such comparison.
    recorded = hashlib.sha256(RAW).hexdigest()
    assert hashlib.sha256(RAW).hexdigest() == recorded
    assert hashlib.sha256(RAW + b'tampered').hexdigest() != recorded
