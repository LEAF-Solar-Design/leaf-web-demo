"""Concurrent X04 output replacement always publishes a complete artifact."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Barrier, Lock
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence, main
from validate_x04_offline_evidence import validate_evidence


def test_two_interleaved_exports_publish_one_whole_artifact(tmp_path, monkeypatch):
    inputs = []
    expected = []
    for index in range(2):
        payload = worked_example()
        payload['drawing']['id'] = f'synthetic-writer-{index}'
        payload['request']['drawing_id'] = payload['drawing']['id']
        payload['drawing']['intake']['inserts'][0]['rows'] = index + 2
        expected.append(export_evidence(payload))
        source = tmp_path / f'source-{index}.json'
        source.write_text(json.dumps(payload), encoding='utf-8')
        inputs.append(source)
    before = [source.read_bytes() for source in inputs]
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    barrier = Barrier(2)
    lock = Lock()
    temporary_paths = []
    replace = os.replace
    def controlled_replace(source, destination):
        assert Path(destination) == target
        assert validate_evidence(json.loads(Path(source).read_text()))['valid']
        with lock:
            temporary_paths.append(Path(source))
        barrier.wait(timeout=15)
        replace(source, destination)
    monkeypatch.setattr(os, 'replace', controlled_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(main, [str(source), '--out', str(target)]) for source in inputs]
        assert [job.result(timeout=30) for job in jobs] == [0, 0]
    result = json.loads(target.read_text())
    assert result in expected
    assert validate_evidence(result)['valid'] is True
    assert len(set(temporary_paths)) == 2
    assert not any(path.exists() for path in temporary_paths)
    assert not list(tmp_path.glob('.x04-*'))
    assert [source.read_bytes() for source in inputs] == before
