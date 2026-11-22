"""Each repeated path occurrence observes its current bytes, without a cache."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
import batch_validate_x04_evidence as batch


@pytest.mark.parametrize('states', [('valid', 'broken', 'valid'), ('valid', 'changed', 'valid'), ('missing', 'valid', 'broken')])
def test_repeated_path_current_bytes(states, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input'; output = tmp_path / 'out'
    valid = worked_example(); changed = worked_example()
    changed['request']['set_ids'] = ['["INSERT","East","Panel"]']
    changed = export_evidence(changed)
    encoded = {'valid': json.dumps(valid).encode(), 'changed': json.dumps(changed).encode(), 'broken': b'{broken'}
    source.write_bytes(encoded['valid']); output.write_bytes(b'previous')
    read = batch.read_json; calls = []
    def current(path, limit):
        state = states[len(calls)]; calls.append(state)
        if state == 'missing': source.unlink(missing_ok=True)
        else: source.write_bytes(encoded[state])
        return read(path, limit)
    with monkeypatch.context() as patch:
        patch.setattr(batch, 'read_json', current)
        assert batch.main([str(source)] * 3 + ['--out', str(output)]) == (0 if states[1] == 'changed' else 1)
    report = json.loads(output.read_bytes())
    expected = [7 if state == 'valid' else 6 if state == 'changed' else None for state in states]
    actual_counts = [row['recomputed_result']['count'] if row['valid'] else None for row in report['results']]
    assert actual_counts == expected
    assert calls == list(states) and [row['index'] for row in report['results']] == [0, 1, 2]
    assert (report['total'], report['passed'], report['failed']) == (3, sum(x is not None for x in expected), sum(x is None for x in expected))
    assert capsys.readouterr().out == ''
    source.write_bytes(encoded['valid'])
    assert batch.main([str(source)] * 3 + ['--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['passed'] == 3
