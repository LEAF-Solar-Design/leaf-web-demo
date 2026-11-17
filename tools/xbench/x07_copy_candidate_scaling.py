"""X07 bounded synthetic copy-candidate initialization metrics and exact oracles."""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter
from x07_role_selection import select_modules


def benchmark(count):
    points = [(Fraction(x, 4), Fraction(y, 4)) for x in range(-4, 4) for y in range(-4, 4)]
    intake = {'inserts': [dict(name='Panel', layer='M', handle=f'h{i}', x=float(x), y=float(y))
                          for i, (x, y) in enumerate(points)]}
    candidates = []
    expected = []
    for index in range(count):
        radius = Fraction(1 + index % 8, 8)
        offset = Fraction(index % 3 - 1, 8)
        bounds = [offset - radius, -radius, offset + radius, radius]
        identities = {f'h{i}' for i, (x, y) in enumerate(points)
                      if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]}
        candidates.append(dict(block='Panel', layer='M', design_window=list(map(float, bounds)), oracle_count=len(identities)))
        expected.append(identities)
    before = hashlib.sha256(json.dumps([intake, candidates], sort_keys=True).encode()).hexdigest()
    with tempfile.TemporaryDirectory() as directory:
        workflow = ProfileWorkflow(CustomerProfileStore(Path(directory) / 'profiles.sqlite'))
        tracemalloc.start()
        try:
            start = time.perf_counter()
            adapter = WorkflowAdapter(workflow, context=dict(customer_key='synthetic', project_key='p', drawing_key='d:v1'),
                                      intake=intake, copies=candidates)
            elapsed = time.perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        for candidate, oracle in zip(adapter._copies, expected):
            result = select_modules(intake, block=candidate['block'], layer=candidate['layer'], window=candidate['design_window'])
            if set(result['handles']) != oracle or result['count'] != len(oracle):
                raise RuntimeError('copy count or identity mismatch')
    if before != hashlib.sha256(json.dumps([intake, candidates], sort_keys=True).encode()).hexdigest():
        raise RuntimeError('caller input changed')
    return {'copies': count, 'inserts': len(points), 'elapsed_s': round(elapsed, 6), 'peak_python_bytes': peak,
            'oracle_matches': True, 'input_sha256': before}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--counts', type=int, nargs='+', default=[1, 8, 32, 128, 256])
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    counts = [1, 4, 16] if args.smoke else args.counts
    if len(counts) > 16 or any(not 1 <= count <= 256 for count in counts):
        parser.error('at most16 copy counts between1 and256')
    result = {'schema': 'leaf.x07.copy-candidate-scaling.v1',
              'measurement': 'Adapter initialization and Python allocation peak excluding drawing creation and schema initialization',
              'rows': [benchmark(count) for count in counts]}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
