"""Four CLI entry points replay offline from unrelated safe-path subprocesses."""
import json
from pathlib import Path
import subprocess
import sys
from test_x04_untrusted_derived_counts import worked_example, X04_ROOT

BOOTSTRAP = '''import runpy, socket, sys
def denied(*args, **kwargs):
    raise AssertionError('Network is forbidden in the offline replay')
class OfflineSocket(socket.socket):
    def __init__(self, *args, **kwargs): denied()
socket.socket = OfflineSocket
socket.create_connection = socket.getaddrinfo = denied
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name='__main__')
'''


def run_tool(script, arguments, cwd, seed=0):
    # Deliberately supply only a synthetic hash seed, without reading host env.
    return subprocess.run([sys.executable, '-P', '-c', BOOTSTRAP,
        str(X04_ROOT / 'scripts' / script), *map(str, arguments)],
        cwd=cwd, env={'PYTHONHASHSEED': str(seed)}, capture_output=True, timeout=30)


def test_four_tool_safe_path_chain_and_forged_middle(tmp_path):
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    source, exported, after, forged, output = [tmp_path / name for name in ('source.json', 'exported.json', 'after.json', 'forged.json', 'output.json')]
    source.write_text(json.dumps(worked_example()), encoding='utf-8'); before = source.read_bytes()
    result = run_tool('export_x04_offline_evidence.py', [source, '--out', exported], unrelated)
    assert result.returncode == 0 and result.stdout == result.stderr == b''
    artifact = json.loads(exported.read_bytes())
    assert artifact['result'] == {'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
    result = run_tool('validate_x04_offline_evidence.py', [exported], unrelated)
    assert result.returncode == 0 and json.loads(result.stdout)['recomputed_result'] == artifact['result']
    changed = worked_example(); changed['request']['set_ids'] = ['["INSERT","East","Panel"]']
    source.write_text(json.dumps(changed), encoding='utf-8')
    assert run_tool('export_x04_offline_evidence.py', [source, '--out', after], unrelated).returncode == 0
    result = run_tool('compare_x04_offline_evidence.py', [exported, after], unrelated)
    delta = json.loads(result.stdout)
    assert result.returncode == 0 and result.stderr == b''
    assert delta['count'] == {'before': 7, 'after': 6, 'delta': -1}
    assert delta['highlight_handles'] == {'removed': ['B'], 'added': []}
    artifact['result']['count'] = 999
    forged.write_text(json.dumps(artifact), encoding='utf-8')
    output.write_bytes(b'previous')
    result = run_tool('compare_x04_offline_evidence.py', [exported, forged, '--out', output], unrelated)
    assert result.returncode == 2 and result.stdout == b'' and b'refused' in result.stderr
    assert output.read_bytes() == b'previous'
    result = run_tool('batch_validate_x04_evidence.py', [exported, forged, after], unrelated)
    report = json.loads(result.stdout)
    assert result.returncode == 1 and result.stderr == b''
    assert (report['total'], report['passed'], report['failed']) == (3, 2, 1)
    assert [row['valid'] for row in report['results']] == [True, False, True]
    forged.write_bytes(exported.read_bytes())
    result = run_tool('batch_validate_x04_evidence.py', [exported, forged, after], unrelated)
    assert result.returncode == 0 and json.loads(result.stdout)['passed'] == 3
    assert before == json.dumps(worked_example()).encode('utf-8')
    assert not list(tmp_path.glob('.x04-*'))
