# X04 report inventory handoff

Studio X-bench X04's offline packet validator accepts repeated `--expected-comparison-id` and `--expected-batch-id` options. Each supplied set must exactly match its decoded packet section before trusted artifact checks. Repeating the same expected ID refuses during argument parsing. Options may appear in any order.

Use a synthetic packet with local artifact IDs `a` and `b`, comparison ID `c`, and batch ID `batch`. The existing `packet()` fixture in `server/tests/test_x04_packet_cross_binding.py` creates this packet. Commands below run from the checkout root and use only a temporary synthetic workspace.

```powershell
$checkout = (Get-Location).Path
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('x04-report-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $scratch | Out-Null
$packet = Join-Path $scratch 'packet.json'
$output = Join-Path $scratch 'validated.json'
@'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path[:0] = [str(root / 'server/tests'), str(root / 'scripts'), str(root / 'server')]
from test_x04_packet_cross_binding import packet
pathlib.Path(sys.argv[2]).write_bytes(json.dumps(packet()).encode('utf-8'))
'@ | python - $checkout $packet
$validator = Join-Path $checkout 'scripts/validate_x04_evidence_packet.py'
python $validator $packet --out $output --expected-comparison-id c --expected-batch-id batch
$prior = [IO.File]::ReadAllBytes($output)
python $validator $packet --out $output --expected-comparison-id omitted
if ($LASTEXITCODE -ne 2) { throw 'Expected report inventory refusal' }
if ([Convert]::ToBase64String($prior) -cne [Convert]::ToBase64String([IO.File]::ReadAllBytes($output))) {
    throw 'Prior output changed on refusal'
}
python $validator $packet --out $output --expected-comparison-id c --expected-batch-id batch
if ($LASTEXITCODE -ne 0) { throw 'Corrected inventory did not recover' }
python -c "import subprocess,sys; raise SystemExit(subprocess.run([sys.executable,sys.argv[1],'-','--expected-comparison-id','c','--expected-batch-id','batch'],input=open(sys.argv[2],'rb').read()).returncode)" $validator $packet
```

The binary stdin command prints a validated summary. The guide's PowerShell commands are a replay recipe. Focused automated file/stdin cases execute on leaf-svc.

Omitting both report options leaves the existing report checks in place. To allow a packet that deliberately omits a comparison or batch report, omit that section's expected-ID options and supply the internally valid packet. Expecting `c` while the `comparisons` array is empty refuses before trusted checks. Supplying only comparison expectations leaves batch inventory unconstrained while all batch semantics still run.

Expected IDs constrain labels and section membership. They do not constrain comparison deltas or batch counts by themselves. Existing validation checks those claims against embedded artifacts after the inventories match. `--expected-packet-sha256` can additionally constrain exact input bytes. Original-byte integrity remains `unverified`, and these checks establish no producer signature or customer DWG provenance.
