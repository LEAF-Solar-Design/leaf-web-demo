"""X04 registry failures refuse atomically and recover after the registry returns."""
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import X04_ROOT, worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('failure', ['missing', 'invalid-json', 'missing-tools', 'wrong-root',
    'wrong-tools', 'nameless-row', 'nonobject-row', 'no-count-tool'])
def test_registry_failure_and_valid_recovery(main, failure, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    registry_path = X04_ROOT / 'engine/registry.json'
    original = Path.read_text
    registry_before = original(registry_path, encoding='utf-8')
    values = {'invalid-json': '{', 'missing-tools': '{}', 'wrong-root': '[]',
              'wrong-tools': '{"tools":{}}', 'nameless-row': '{"tools":[{}]}',
              'nonobject-row': '{"tools":[null]}', 'no-count-tool': '{"tools":[{"name":"other"}]}'}
    def failed_registry(path, *args, **kwargs):
        if path == registry_path:
            if failure == 'missing':
                raise FileNotFoundError('injected missing registry')
            return values[failure]
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', failed_registry)
        assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'registry' in streams.err.lower() and 'Traceback' not in streams.err
    assert target.read_bytes() == b'previous' and source.read_bytes() == before
    assert not list(tmp_path.glob('.x04-*'))
    assert main([str(source), '--out', str(target)]) == 0
    streams = capsys.readouterr()
    assert streams.out == streams.err == ''
    output = json.loads(target.read_bytes())
    result = output.get('result', output.get('recomputed_result'))
    assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']
    assert original(registry_path, encoding='utf-8') == registry_before
