"""Barrier-interleaved calls retain separate byte ceilings and destinations."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import pytest
from test_x04_untrusted_derived_counts import worked_example
import compare_x04_offline_evidence as comparison
import batch_validate_x04_evidence as batch


@pytest.mark.parametrize('rotation', range(3))
def test_concurrent_call_local_budgets(rotation, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'input'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    modes = [(kind, budget) for kind in ('comparison', 'batch') for budget in ('exact', 'input-under', 'output-under')]
    modes = modes[rotation:] + modes[:rotation]
    barrier = threading.Barrier(6); local = threading.local()
    originals = {comparison: comparison.read_json, batch: batch.read_json}
    def synchronized(module):
        def read(path, limit):
            if not getattr(local, 'entered', False):
                local.entered = True; barrier.wait(timeout=20)
            return originals[module](path, limit)
        return read
    jobs = []
    for index, (kind, budget) in enumerate(modes):
        module = comparison if kind == 'comparison' else batch
        output = tmp_path / f'out-{index}'; output.write_bytes(b'previous')
        expected = comparison.compare_evidence(worked_example(), worked_example()) if kind == 'comparison' else batch.validate_paths([source])
        output_size = len((json.dumps(expected, ensure_ascii=True, indent=2) + '\n').encode())
        args = [str(source)] * (2 if kind == 'comparison' else 1) + ['--out', str(output),
            '--max-input-bytes', str(len(before) - (budget == 'input-under')),
            '--max-output-bytes', str(output_size - (budget == 'output-under'))]
        jobs.append((module, budget, output, args))
    with monkeypatch.context() as patch:
        for module in originals: patch.setattr(module, 'read_json', synchronized(module))
        with ThreadPoolExecutor(max_workers=6) as executor:
            # Each job gets a fresh thread-local barrier marker.
            futures = [executor.submit(module.main, args) for module, _, _, args in jobs]
            statuses = [future.result(timeout=30) for future in futures]
    for status, (module, budget, output, _) in zip(statuses, jobs):
        expected_status = 0 if budget == 'exact' else 1 if module is batch and budget == 'input-under' else 2
        assert status == expected_status
        if status == 2: assert output.read_bytes() == b'previous'
        else:
            result = json.loads(output.read_bytes())
            if module is comparison: assert result['count']['delta'] == 0
            else: assert (result['passed'], result['failed']) == ((1, 0) if status == 0 else (0, 1))
    streams = capsys.readouterr(); assert streams.out == ''
    assert len(streams.err.splitlines()) == 3 and 'Traceback' not in streams.err
    assert source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
