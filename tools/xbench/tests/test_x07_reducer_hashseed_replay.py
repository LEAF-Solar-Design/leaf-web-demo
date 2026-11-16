from pathlib import Path
import json
import subprocess
import sys
import unittest

DIRECTORY = Path(__file__).resolve().parents[1]
WORKER = r'''
import json,sys
sys.path.insert(0,sys.argv[1])
from x07_history_reducer import reduce_history
history=json.loads(sys.argv[2]);before=json.dumps(history,ensure_ascii=False)
def oracle(rows):
    opens=0
    for row in rows:
        if row=='open': opens+=1
        elif row=='commit' and opens>=2: return 'ORDERED_DUPLICATE'
    return None
result=reduce_history(history,oracle)
assert result['complete'] and result['signature']==oracle(result['commands'])=='ORDERED_DUPLICATE'
for index in range(len(result['commands'])):
    assert oracle(result['commands'][:index]+result['commands'][index+1:])!=result['signature']
assert json.dumps(history,ensure_ascii=False)==before
print(json.dumps(result,sort_keys=True,separators=(',',':')))
'''


class ReducerHashseedReplay(unittest.TestCase):
    def test_fresh_seeded_processes_emit_identical_canonical_replays(self):
        history = ['noise', 'open', 'é', 'open', 'noise', 'commit', 'commit']
        original = json.dumps(history)
        outputs = []
        for seed in ['0', '1', '42', '4294967295']:
            with self.subTest(seed=seed):
                # Explicit child-only configuration; never read the host environment.
                child = subprocess.run([sys.executable, '-c', WORKER, str(DIRECTORY), original],
                                       env={'PYTHONHASHSEED': seed, 'PYTHONUTF8': '1'}, capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertEqual(child.stderr, b'')
                outputs.append(child.stdout)
        self.assertTrue(all(value == outputs[0] for value in outputs))
        replay = json.loads(outputs[0])
        self.assertEqual(replay['commands'], ['open', 'open', 'commit'])
        self.assertEqual(replay['signature'], 'ORDERED_DUPLICATE')
        self.assertLessEqual(replay['oracle_calls'], 128)
        self.assertEqual(json.dumps(history), original)
