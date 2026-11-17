# X04 embedded drawing bindings

Studio X-bench X04's offline packet validator accepts repeated `--expected-artifact-drawing ID=DRAWING` options. The key names an embedded artifact. The value must equal that artifact's decoded `drawing.id` before trusted artifact validation. Split occurs at the first equals sign, so `a=drawing=one` binds artifact `a` to drawing label `drawing=one`.

Duplicate keys and malformed mappings refuse during argument parsing. Unknown artifact keys and mismatched drawing labels refuse after byte limits, optional packet pin, strict JSON decoding and inventory checks. Existing semantic checks follow successful bindings.

Run this synthetic replay from the checkout root with the repository's Python environment in safe-path mode (`python -P`). It creates two drawings with labels `drawing=one` and `drawing-two`. The script uses a temporary directory and the existing synthetic fixture helper.

```python
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path.cwd()
sys.path[:0] = [str(root / 'server/tests'), str(root / 'scripts'), str(root / 'server')]
from test_x04_packet_drawing_arguments import two_drawing_packet

validator = root / 'scripts/validate_x04_evidence_packet.py'
with tempfile.TemporaryDirectory(prefix='x04-drawing-bindings-') as directory:
    source = Path(directory) / 'packet.json'
    output = Path(directory) / 'summary.json'
    packet = two_drawing_packet()
    source.write_bytes(json.dumps(packet).encode())
    bindings = ['--expected-artifact-drawing', 'a=drawing=one',
                '--expected-artifact-drawing', 'b=drawing-two']
    command = [sys.executable, '-P', str(validator)]
    subprocess.run([*command, str(source), '--out', str(output), *bindings], check=True)
    previous = output.read_bytes()
    streamed = subprocess.run([*command, '-', *bindings], input=source.read_bytes(), capture_output=True)
    assert streamed.returncode == 0 and streamed.stdout == previous

    # A valid replacement retains artifact label a and changes its drawing label.
    packet['artifacts'][0]['artifact']['drawing']['id'] = 'replacement'
    packet['artifacts'][0]['artifact']['request']['drawing_id'] = 'replacement'
    source.write_bytes(json.dumps(packet).encode())
    refused = subprocess.run([*command, str(source), '--out', str(output), *bindings], capture_output=True)
    assert refused.returncode == 2 and refused.stdout == b''
    assert output.read_bytes() == previous
    corrected = ['--expected-artifact-drawing', 'a=replacement',
                 '--expected-artifact-drawing', 'b=drawing-two']
    subprocess.run([*command, str(source), '--out', str(output), *corrected], check=True)

    # A partial map constrains its named artifact while validating the full packet.
    subprocess.run([*command, str(source), '--expected-artifact-drawing', 'b=drawing-two'], check=True)
```

A partial map binds only its named artifacts. Omission retains existing packet validation. Drawing IDs compare literally, including case, Unicode normalization form, spaces and equals signs. Inventory options constrain packet-local membership. Drawing bindings constrain labels within named artifacts. Packet digest pins constrain raw input bytes. Trusted validation recomputes derived claims. Original-byte integrity remains `unverified`, and producer authenticity or original DWG custody needs its own receipt.

Focused file/stdin tests and growth observations run through managed leaf-svc verification. The benchmark grows 2, 10 and 100 embedded artifacts at 131072 packet bytes and records trusted calls, elapsed time and peak memory across matching, wrong and omitted bindings. A wrong drawing binding reaches zero trusted calls. Timing observations carry no thresholds.
