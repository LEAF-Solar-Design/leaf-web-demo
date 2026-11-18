"""Observe atomic batch output refusal after explicit occurrence validation."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
import batch_validate_x04_evidence as batch
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs


def main():
    observations = []
    with tempfile.TemporaryDirectory() as directory:
        source, output = Path(directory) / 'source', Path(directory) / 'output'
        pairs = [[0, 'STYLE'], [3, '../synthetic.shx']]
        source.write_text(json.dumps(export_evidence({'schema': SCHEMA, 'synthetic': True,
            'drawing': {'id': 'synthetic-output', 'dependency_pairs': pairs,
                'intake': {'inserts': [{'layer': 'A', 'name': 'Module', 'handle': 'A'}]}},
            'observed_dependencies': from_pairs(pairs),
            'request': {'drawing_id': 'synthetic-output', 'set_ids': ['["INSERT","A","Module"]']}})), encoding='utf-8')
        before = source.read_bytes()
        for count in (10, 100, 1000):
            calls = []
            validate = batch.validate_evidence
            def counted(value): calls.append(1); return validate(value)
            batch.validate_evidence = counted
            try:
                output.write_bytes(b'previous')
                stdout, stderr = io.StringIO(), io.StringIO()
                tracemalloc.start(); started = time.perf_counter()
                try:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        status = batch.main([str(source)] * count + ['--out', str(output), '--max-output-bytes', '64'])
                    seconds = time.perf_counter() - started; peak = tracemalloc.get_traced_memory()[1]
                finally: tracemalloc.stop()
                assert status == 2 and stdout.getvalue() == '' and 'refused' in stderr.getvalue()
                assert len(calls) == count and output.read_bytes() == b'previous'
                assert not list(Path(directory).glob('.x04-*')) and source.read_bytes() == before
                calls.clear()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    retry = batch.main([str(source)] * count + ['--out', str(output)])
                report = json.loads(output.read_bytes())
                assert retry == 0 and len(calls) == count
                assert (report['total'], report['passed'], report['failed']) == (count, count, 0)
                assert [row['index'] for row in report['results']] == list(range(count))
                observations.append({'occurrences': count, 'ceiling_bytes': 64, 'validation_calls': count,
                    'seconds': seconds, 'peak_bytes': peak, 'retry_output_bytes': output.stat().st_size})
            finally: batch.validate_evidence = validate
    print(json.dumps({'schema': 'leaf.x04.batch-output-refusal-observations.v1', 'observations': observations}))


if __name__ == '__main__': main()
