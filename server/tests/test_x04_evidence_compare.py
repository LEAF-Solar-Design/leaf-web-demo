"""X04 comparison uses independently checked claims and literal deltas."""
from copy import deepcopy
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence, main


def changed_example():
    payload = worked_example()
    payload['request']['set_ids'] = ['["INSERT","East","Panel"]']
    payload['drawing']['dependency_pairs'] = [[0, 'STYLE'], [3, '../literal-font.shx']]
    payload['observed_dependencies'] = from_pairs(payload['drawing']['dependency_pairs'])
    return export_evidence(payload)


def test_exact_deltas_are_immutable_and_unverified():
    before, after = worked_example(), changed_example()
    originals = deepcopy((before, after))
    result = compare_evidence(before, after)
    assert result['count'] == {'before': 7, 'after': 6, 'delta': -1}
    assert result['selected_ids'] == {'removed': ['["LWPOLYLINE","East",""]'], 'added': []}
    assert result['highlight_handles'] == {'removed': ['B'], 'added': []}
    assert result['literal_dependencies']['removed'] == before['observed_dependencies']['dependencies']
    assert result['literal_dependencies']['added'] == after['observed_dependencies']['dependencies']
    assert result['original_bytes_integrity'] == 'unverified'
    assert (before, after) == originals
    reverse = compare_evidence(after, before)
    assert reverse['count']['delta'] == 1 and reverse['highlight_handles']['added'] == ['B']


@pytest.mark.parametrize('side', ['before', 'after'])
@pytest.mark.parametrize('mutation', ['count', 'identity', 'dependency', 'highlight'])
def test_refusals_preserve_existing_output_and_inputs(side, mutation, tmp_path, capsys):
    pair = {'before': worked_example(), 'after': changed_example()}
    item = pair[side]
    if mutation == 'count': item['result']['count'] = 100
    if mutation == 'identity':
        item['drawing']['id'] = item['request']['drawing_id'] = 'different'
    if mutation == 'dependency': item['observed_dependencies']['dependencies'] = []
    if mutation == 'highlight': item['result']['highlight_handles'] = ['BAD']
    paths = [tmp_path / name for name in ('before', 'after')]
    for path, payload in zip(paths, pair.values()): path.write_text(json.dumps(payload), encoding='utf-8')
    original = [path.read_bytes() for path in paths]
    target = tmp_path / 'out'; target.write_bytes(b'previous')
    assert main([*map(str, paths), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'Traceback' not in streams.err and 'refused' in streams.err
    assert target.read_bytes() == b'previous' and [path.read_bytes() for path in paths] == original


@pytest.mark.parametrize('alias', ['before', 'after'])
def test_output_alias_refused(alias, tmp_path, capsys):
    paths = {name: tmp_path / name for name in ('before', 'after')}
    for path in paths.values(): path.write_text(json.dumps(worked_example()), encoding='utf-8')
    original = paths[alias].read_bytes()
    assert main([str(paths['before']), str(paths['after']), '--out', str(paths[alias])]) == 2
    assert paths[alias].read_bytes() == original and capsys.readouterr().out == ''


def test_success_and_budget_refusal(tmp_path, capsys):
    paths = [tmp_path / name for name in ('a', 'b')]
    for path, payload in zip(paths, [worked_example(), changed_example()]):
        path.write_text(json.dumps(payload), encoding='utf-8')
    assert main(list(map(str, paths))) == 0
    assert json.loads(capsys.readouterr().out)['count']['delta'] == -1
    assert main([*map(str, paths), '--max-output-bytes', '1']) == 2
    assert capsys.readouterr().out == ''


def test_identical_and_reordered_memberships():
    a = worked_example(); b = deepcopy(a)
    b['request']['set_ids'].reverse(); b['result']['highlight_handles'].reverse()
    result = compare_evidence(a, b)
    for key in ('selected_ids', 'highlight_handles', 'literal_dependencies'):
        assert result[key] == {'removed': [], 'added': []}
    assert result['count']['delta'] == 0
