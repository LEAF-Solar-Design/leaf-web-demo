"""ASCII standard streams preserve X04 Unicode JSON and inert diagnostics."""
import json
import subprocess
import sys
import pytest
from test_x04_untrusted_derived_counts import X04_ROOT, worked_example
from drawing_dependency_report import from_pairs


@pytest.mark.parametrize('script', ['export_x04_offline_evidence.py', 'validate_x04_offline_evidence.py'])
@pytest.mark.parametrize('mode', ['stdout-success', 'file-success', 'missing-input', 'duplicate-key'])
def test_ascii_streams_with_unicode_paths_and_literals(script, mode, tmp_path):
    folder = tmp_path / '目录-東'
    folder.mkdir()
    source = folder / '输入-東.json'
    target = folder / '输出-東.json'
    payload = worked_example()
    payload['drawing']['id'] = 'synthetic-東'
    payload['request']['drawing_id'] = payload['drawing']['id']
    reference = 'C:/synthetic/字体-東.shx'
    payload['drawing']['dependency_pairs'] = [[0, 'STYLE'], [3, reference]]
    payload['observed_dependencies'] = from_pairs(payload['drawing']['dependency_pairs'])
    encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if mode == 'duplicate-key':
        key = json.dumps('東\r\n\x1b[31m', ensure_ascii=False)
        encoded = ('{' + key + ':0,' + key + ':1}').encode('utf-8')
    source.write_bytes(encoded)
    target.write_bytes(b'previous')
    input_path = folder / 'missing-東.json' if mode == 'missing-input' else source
    arguments = [str(X04_ROOT / 'scripts' / script), str(input_path)]
    if mode != 'stdout-success':
        arguments += ['--out', str(target)]
    wrapper = '''
import runpy,sys
sys.stdout.reconfigure(encoding='ascii', errors='strict')
sys.stderr.reconfigure(encoding='ascii', errors='strict')
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-P', '-c', wrapper, *arguments],
                            capture_output=True, cwd=folder, timeout=30)
    assert source.read_bytes() == encoded
    if mode.endswith('success'):
        assert result.returncode == 0 and result.stderr == b''
        output = json.loads(result.stdout if mode == 'stdout-success' else target.read_bytes())
        identity = output.get('drawing_id', output.get('drawing', {}).get('id'))
        assert identity == 'synthetic-東'
        claims = output.get('result', output.get('recomputed_result'))
        assert claims['count'] == 7 and claims['highlight_handles'] == ['A', 'B']
        if script.startswith('export_'):
            assert output['observed_dependencies']['dependencies'][0]['name'] == reference
        else:
            assert json.loads(source.read_bytes())['observed_dependencies']['dependencies'][0]['name'] == reference
    else:
        assert result.returncode == 2 and result.stdout == b''
        diagnostic = result.stderr.decode('ascii')
        assert 'refused' in diagnostic and 'Traceback' not in diagnostic
        assert len(result.stderr) <= 2048 and '\x1b' not in diagnostic and '\r' not in diagnostic
        assert diagnostic.count('\n') == 1 and target.read_bytes() == b'previous'
    assert not list(folder.glob('.x04-*'))
