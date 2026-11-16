# X07 offline evidence portability

This recipe belongs to the Studio X-bench X07 synthetic customer-profile workflow. Run it from the source checkout. It creates fresh receipts, copies their declared source bytes into a disposable root, verifies all seven synthetic steps, and proves that a changed byte refuses verification. Existing receipt files stay untouched.

The fixed inventory covers the journey, report and four product modules. Every journey and local proof must include that inventory. Extra declared files are also verified. A matching commit field alone cannot satisfy the report.

```powershell
@'
from pathlib import Path
import hashlib, importlib.util, json, shutil, tempfile

source = Path.cwd()
def load(name):
    spec = importlib.util.spec_from_file_location(name, source / ('tools/xbench/' + name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

producer = load('x07_synthetic_journey')
report = load('x07_offline_acceptance_report')
with tempfile.TemporaryDirectory(prefix='x07-portability-') as directory:
    temporary = Path(directory)
    journey = producer.run(temporary / 'synthetic.sqlite')
    proof = {
        'schema': 'leaf.x07.offline-proof.v1',
        'source_sha': journey['source_sha'],
        'source_dirty': journey['source_dirty'],
        'files': {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                  for name in report.REQUIRED_SOURCE_FILES},
    }
    original = temporary / 'original'
    original.mkdir()
    for name, value in [('journey.json', journey), ('proof.json', proof)]:
        (original / name).write_text(json.dumps(value), encoding='utf-8')
    before = {path.name: path.read_bytes() for path in original.iterdir()}
    report.aggregate(original / 'journey.json', [original / 'proof.json'],
                     journey['source_sha'], source)
    relocated = temporary / 'relocated'
    relocated.mkdir()
    for name in set(journey['files']) | set(proof['files']):
        target = relocated / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    for name in before:
        shutil.copyfile(original / name, relocated / name)
    result = report.aggregate(relocated / 'journey.json', [relocated / 'proof.json'],
                              journey['source_sha'], relocated)
    assert all(step['state'] == 'SYNTHETIC_PASS' for step in result['steps'])
    target = relocated / 'server/x07_role_selection.py'
    target.write_bytes(target.read_bytes() + b'\n# disposable tamper fixture\n')
    try:
        report.aggregate(relocated / 'journey.json', [relocated / 'proof.json'],
                         journey['source_sha'], relocated)
        raise AssertionError('tampered source was accepted')
    except report.EvidenceError as error:
        assert str(error) == 'SOURCE_DIGEST_MISMATCH'
    assert all((original / name).read_bytes() == value for name, value in before.items())
    assert all((relocated / name).read_bytes() == value for name, value in before.items())
    print('PASS: relocated source verified, tamper refused, original receipts retained')
'@ | python -P -
```

The temporary directory and its databases close on exit. A missing inventory entry, changed file, missing source file, path escape, duplicate step or contradictory assertion produces an unreadable report. Record the fresh receipt's commit and dirty marker when keeping a durable copy. These results describe the synthetic workflow.

For bounded timing evidence:

```powershell
python tools/xbench/x07_acceptance_report_perf.py --smoke
```
