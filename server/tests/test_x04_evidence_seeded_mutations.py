"""Seeded X04 mutations have independently specified semantic outcomes."""
from copy import deepcopy
import json
import random
import pytest
from test_x04_untrusted_derived_counts import worked_example, validate_main


@pytest.mark.parametrize('seed', [17, 83, 211])
def test_seeded_single_field_mutations_with_valid_controls(seed, tmp_path, capsys):
    baseline = worked_example()
    cases = [
        (('schema',), 'unknown', False),
        (('drawing', 'id'), 'other-drawing', False),
        (('request', 'drawing_id'), 'other-drawing', False),
        (('request', 'set_ids'), ['unknown'], False),
        (('result', 'count'), 8, False),
        (('result', 'count'), True, False),
        (('result', 'count'), 7.0, False),
        (('result', 'highlight_handles'), ['A'], False),
        (('result', 'highlight_handles'), ['A', 'A', 'B'], False),
        (('result', 'original_bytes_integrity'), 'verified', False),
        (('observed_dependencies', 'scope'), 'raw_dwg', False),
        (('observed_dependencies', 'coverage_note'), 'complete raw inventory', False),
        (('observed_dependencies', 'external_files_opened'), True, False),
        (('observed_dependencies', 'dependencies', 0, 'name'), 'forged.shx', False),
        (('observed_dependencies', 'dependencies', 0, 'disposition'), 'loaded', False),
        (('observed_dependencies', 'dependencies', 0, 'remapped_to'), 'forged-font', False),
        (('opaque',), {'seed': seed, 'claim': 999}, True),
        (('request', 'set_ids'), baseline['request']['set_ids'] * 2, True),
        (('request', 'set_ids'), list(reversed(baseline['request']['set_ids'])), True),
        (('result', 'highlight_handles'), ['B', 'A'], True),
    ]
    random.Random(seed).shuffle(cases)
    control = tmp_path / 'control.json'
    control.write_text(json.dumps(baseline), encoding='utf-8')
    target = tmp_path / 'output.json'
    for path, value, accepted in cases:
        payload = deepcopy(baseline)
        owner = payload
        for key in path[:-1]:
            owner = owner[key]
        owner[path[-1]] = deepcopy(value)
        before = deepcopy(payload)
        source = tmp_path / 'mutation.json'
        source.write_text(json.dumps(payload), encoding='utf-8')
        source_bytes = source.read_bytes()
        target.write_bytes(b'previous')
        status = validate_main([str(source), '--out', str(target)])
        streams = capsys.readouterr()
        assert streams.out == '' and source.read_bytes() == source_bytes and payload == before
        assert not list(tmp_path.glob('.x04-*'))
        if accepted:
            assert status == 0 and streams.err == ''
        else:
            assert status == 2 and 'refused' in streams.err and 'Traceback' not in streams.err
            assert target.read_bytes() == b'previous'
            assert validate_main([str(control), '--out', str(target)]) == 0
            streams = capsys.readouterr()
            assert streams.out == streams.err == ''
        assert json.loads(target.read_bytes())['recomputed_result'] == {
            'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
