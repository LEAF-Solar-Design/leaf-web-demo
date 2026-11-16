"""X04 command entry points have deterministic Unicode and error streams."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
from test_x04_untrusted_derived_counts import X04_ROOT, worked_example, export_main, validate_main


@pytest.mark.parametrize('script', ['export_x04_offline_evidence.py', 'validate_x04_offline_evidence.py'])
def test_subprocess_unicode_success_is_one_json_document(script, tmp_path):
    payload = worked_example()
    payload['drawing']['id'] = 'synthetic-東-é-☀'
    payload['request']['drawing_id'] = payload['drawing']['id']
    source = tmp_path / 'unicode.json'
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    result = subprocess.run([sys.executable, '-P', str(X04_ROOT / 'scripts' / script), str(source)],
                            capture_output=True, encoding='utf-8', timeout=30, cwd=tmp_path)
    assert result.returncode == 0 and result.stderr == ''
    value = json.loads(result.stdout)
    identity = value.get('drawing_id', value.get('drawing', {}).get('id'))
    assert identity == payload['drawing']['id']


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('failure', ['missing', 'unreadable'])
def test_input_io_failure_emits_only_diagnostic(main, failure, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input.json'
    if failure == 'unreadable':
        source.write_text('{}')
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == source:
                raise PermissionError('injected unreadable input')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', denied)
    target = tmp_path / 'result.json'
    assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err
    assert not target.exists() and not list(tmp_path.glob('.x04-*'))
