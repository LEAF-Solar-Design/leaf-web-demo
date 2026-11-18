"""X04 five-tool replay checks saved reports after deleting their source artifacts."""
import json
from test_x04_untrusted_derived_counts import worked_example
from test_x04_review_subprocess_chain import run_tool


def test_five_tool_chain_without_underlying_artifacts(tmp_path):
    cwd = tmp_path / 'unrelated'; cwd.mkdir()
    source, first, second, comparison, batch, output = [tmp_path / name for name in
        ('source.json', 'first.json', 'second.json', 'comparison.json', 'batch.json', 'checked.json')]
    original = worked_example()
    source.write_text(json.dumps(original), encoding='utf-8')
    assert run_tool('export_x04_offline_evidence.py', [source, '--out', first], cwd).returncode == 0
    result = run_tool('validate_x04_offline_evidence.py', [first], cwd)
    assert result.returncode == 0
    assert json.loads(result.stdout)['recomputed_result'] == {
        'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
    original['request']['set_ids'] = ['["INSERT","East","Panel"]']
    source.write_text(json.dumps(original), encoding='utf-8')
    assert run_tool('export_x04_offline_evidence.py', [source, '--out', second], cwd).returncode == 0
    assert run_tool('compare_x04_offline_evidence.py', [first, second, '--out', comparison], cwd).returncode == 0
    delta = json.loads(comparison.read_bytes())
    assert delta['count'] == {'before': 7, 'after': 6, 'delta': -1}
    assert delta['highlight_handles'] == {'removed': ['B'], 'added': []}
    assert delta['selected_ids'] == {'removed': ['["LWPOLYLINE","East",""]'], 'added': []}
    assert delta['literal_dependencies'] == {'removed': [], 'added': []}
    assert run_tool('batch_validate_x04_evidence.py', [first, second, first, '--out', batch], cwd).returncode == 0
    report = json.loads(batch.read_bytes())
    assert (report['total'], report['passed'], report['failed']) == (3, 3, 0)
    assert [r['recomputed_result']['count'] for r in report['results']] == [7, 6, 7]
    assert [r['path'] for r in report['results']] == [str(first), str(second), str(first)]
    saved = {p: p.read_bytes() for p in (comparison, batch)}
    for path in (source, first, second):
        path.unlink()
    for path in (comparison, batch):
        result = run_tool('validate_x04_review_report.py', [path, '--out', output], cwd)
        assert result.returncode == 0 and result.stdout == result.stderr == b''
        checked = json.loads(output.read_bytes())
        assert checked['valid'] and checked['original_bytes_integrity'] == 'unverified'
        assert checked['underlying_artifacts_revalidated'] is False
        assert checked['embedded_paths_opened'] is False
        assert path.read_bytes() == saved[path]
    delta['count']['delta'] = 0
    comparison.write_text(json.dumps(delta), encoding='utf-8')
    previous = output.read_bytes()
    refused = run_tool('validate_x04_review_report.py', [comparison, '--out', output], cwd)
    assert refused.returncode == 2 and refused.stdout == b'' and b'refused' in refused.stderr
    assert b'Traceback' not in refused.stderr and output.read_bytes() == previous
    comparison.write_bytes(saved[comparison])
    assert run_tool('validate_x04_review_report.py', [comparison, '--out', output], cwd).returncode == 0
    assert not list(tmp_path.glob('.x04-*'))


def test_documented_synthetic_payload_and_saved_report_replay(tmp_path, monkeypatch):
    from test_x04_untrusted_derived_counts import X04_ROOT
    doc = (X04_ROOT / 'docs/x04-five-tool-report-replay.md').read_text(encoding='utf-8')
    code = doc.split('```python', 1)[1].split('```', 1)[0]
    # The guide runs from repo root; move only its server import base for this scratch replay.
    code = code.replace('str(Path("server").resolve())', repr(str(X04_ROOT / 'server')))
    monkeypatch.chdir(tmp_path)
    exec(compile(code, '<documented synthetic input>', 'exec'), {})
    for side in ('before', 'after'):
        assert run_tool('export_x04_offline_evidence.py',
            [f'{side}-input.json', '--out', f'{side}-artifact.json'], tmp_path).returncode == 0
    assert json.loads((tmp_path / 'before-artifact.json').read_bytes())['result']['count'] == 2
    assert run_tool('validate_x04_offline_evidence.py', ['before-artifact.json'], tmp_path).returncode == 0
    assert run_tool('compare_x04_offline_evidence.py',
        ['before-artifact.json', 'after-artifact.json', '--out', 'comparison.json'], tmp_path).returncode == 0
    assert run_tool('batch_validate_x04_evidence.py',
        ['before-artifact.json', 'after-artifact.json', 'before-artifact.json', '--out', 'batch.json'], tmp_path).returncode == 0
    delta = json.loads((tmp_path / 'comparison.json').read_bytes())
    assert delta['count'] == {'before': 2, 'after': 1, 'delta': -1}
    for side in ('before', 'after'):
        (tmp_path / f'{side}-input.json').unlink()
        (tmp_path / f'{side}-artifact.json').unlink()
    for report in ('comparison.json', 'batch.json'):
        checked = run_tool('validate_x04_review_report.py', [report], tmp_path)
        assert checked.returncode == 0
        assert json.loads(checked.stdout)['underlying_artifacts_revalidated'] is False
