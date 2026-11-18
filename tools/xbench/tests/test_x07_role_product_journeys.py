"""X07-only registration surface for offline JSX product fixtures."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('name', [
    'x07_role_review_adapter_journey.cjs',
    'x07_profile_review_focus_dom.cjs',
    'x07_profile_review_client_aba_dom.cjs',
    'x07_role_diff_adapter_dom.cjs',
    '../x07_profile_diff_render_perf.cjs',
    'x07_selective_restore_keyboard_dom.cjs',
    'x07_selective_restore_races_dom.cjs',
    'x07_selective_restore_adapter_dom.cjs',
    '../x07_selective_restore_soak.cjs',
    'x07_restore_undo_keyboard_dom.cjs',
    'x07_restore_undo_races_dom.cjs',
    'x07_restore_undo_adapter_dom.cjs',
    '../x07_restore_undo_soak.cjs',
])
def test_offline_role_product_fixture(name):
    dependencies = ROOT / 'web/node_modules'
    node = shutil.which('node')
    if node is None or not all((dependencies / package).is_dir() for package in (
        'jsdom', 'react', 'esbuild', '@testing-library/react',
    )):
        pytest.skip('X07 actual JSX dependency set unavailable on this host; Node-prepared replay required')
    script = Path(__file__).parent / name
    args = [node, str(script), '--deps', str(dependencies)]
    if 'perf' in name or 'soak' in name:
        args.append('--smoke')
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    if 'perf' in name:
        report = json.loads(result.stdout)
        assert report['schema'] == 'leaf.x07.role-diff-render-perf.v1'
        assert report['cleanup'] is True
        assert len(report['timings']) == 5
        assert all(row['max_nodes'] <= 60 for row in report['timings'])
    elif 'soak' in name:
        report = json.loads(result.stdout)
        undo = 'restore_undo' in name
        assert report['schema'] == ('leaf.x07.restore-undo-soak.v1' if undo else 'leaf.x07.selective-restore-soak.v1')
        assert report['cycles'] == 16 and report['writes'] == 0
        assert report['max_nodes'] <= (200 if undo else 180) and report['cleanup'] is True
    else:
        assert 'PASS:' in result.stdout
