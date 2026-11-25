"""Saved-report concurrent calls retain separate ceilings and destinations."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as validator


@pytest.mark.parametrize('rotation', range(3))
def test_barrier_interleaved_limits(rotation, tmp_path, monkeypatch, capsys):
    sources, summaries = {}, {}
    for factory in (comparison, batch):
        source = tmp_path / f'{factory.__name__}.json'
        report = factory()
        source.write_text(json.dumps(report), encoding='utf-8')
        sources[factory] = (source, source.read_bytes())
        summaries[factory] = validator.validate_report(report)
    jobs = [(factory, mode) for factory in sources for mode in ('exact', 'input-under', 'output-under')]
    jobs = jobs[rotation:] + jobs[:rotation]
    barrier = threading.Barrier(len(jobs))
    original_read, original_open = validator.read_json, Path.open
    def synchronized(source, limit):
        barrier.wait(timeout=20)
        return original_read(source, limit)
    def guarded(path, *args, **kwargs):
        assert path in {value[0] for value in sources.values()}, 'Embedded path read'
        return original_open(path, *args, **kwargs)
    calls = []
    for index, (factory, mode) in enumerate(jobs):
        source, raw = sources[factory]
        output = tmp_path / f'output-{index}.json'; output.write_bytes(b'previous')
        encoded_size = len((json.dumps(summaries[factory], ensure_ascii=True, indent=2) + '\n').encode())
        args = [str(source), '--out', str(output), '--max-input-bytes',
                str(len(raw) - (mode == 'input-under')), '--max-output-bytes',
                str(encoded_size - (mode == 'output-under'))]
        calls.append((factory, mode, output, args))
    with monkeypatch.context() as patch:
        patch.setattr(validator, 'read_json', synchronized)
        patch.setattr(Path, 'open', guarded)
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(validator.main, args) for _, _, _, args in calls]
            statuses = [future.result(timeout=30) for future in futures]
    for status, (factory, mode, output, _) in zip(statuses, calls):
        assert status == (0 if mode == 'exact' else 2)
        if mode == 'exact':
            assert json.loads(output.read_bytes()) == summaries[factory]
        else:
            assert output.read_bytes() == b'previous'
    captured = capsys.readouterr()
    assert captured.out == '' and len(captured.err.splitlines()) == 4
    assert 'Traceback' not in captured.err
    for source, raw in sources.values():
        assert source.read_bytes() == raw
    assert not list(tmp_path.glob('.x04-*'))
