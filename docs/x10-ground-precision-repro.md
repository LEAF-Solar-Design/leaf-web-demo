# Ground represented geometry and snapshot reproduction

This is offline source proof using the real form and mocked workspace services.
It does not establish LIVE publication or a full terrain or engineering PASS.

Enter origin X and Y `900000000`, width `0.000001` and height `0.000002`.
The generated X corner is `900000000.000001`, but subtracting its represented
origin gives `9.5367431640625e-7`, not the entered width. Inspect the boundary
table and generated source JSON. Site size and the schematic description use
these represented edge differences. Area, perimeter and converted metre size
use the same edges, with the existing six decimal display rule and "below
display precision" for tiny measurements. Schematic ratios use those edges
with a minimum visible dimension of 2, and remain labeled not to scale.
Repeat with negative origins and feet. The controls retain their entered tokens.

`SolarWorkspaceTools.test.jsx` cases named `measures represented edges` compare
measurements and SVG attributes with emitted polygon edges, including ordinary
inputs. `describes unit reinterpretation` checks visible help and stable control
descriptors. The selector reinterprets unchanged tokens in selected drawing
units. Convert site units changes origins, sizes and units to preserve geometry
within Number arithmetic; module metres stay unchanged. Existing pending and
unknown outcome locks still apply.

`x10GroundFormIsolation.test.jsx` mutates callback snapshots synchronously and
later, including nested boundary and preset values, added fields and deleted
fields. Rerender and subsequent edits keep private UI JSON, table and measurements
clean. Replacement callbacks receive fresh independent snapshots of current
private state. Two-form checks also cover local IDs and cross-form isolation.

`x10GroundNumericEquivalence.test.jsx` uses bounded real control edits. Try width
`10`, ` +001.000e1 ` and `10.0`: equal producer payloads do not emit again to the
same callback. Name trimming follows the same rule. A malformed edit emits null
once, further invalid edits stay silent, and correction emits a valid snapshot.
A replacement callback receives the current valid snapshot or null even if the
value is unchanged. Unit reinterpretation emits because drawing units change;
conversion feedback alone does not. Decimal and exponent forms, exact integer
controls, zero versus nonzero underflow, signs, whitespace and finite field
bounds are covered without a second validator or test-only geometry producer.

Run the frozen focused command from the planner's Windows environment:

```text
python C:/Users/ehaug/.claude/scripts/offhost.py run --host leaf-svc --scope verify --cwd D:/agent-wt/xbench-20261115-x10-ground-input-form/web --worktree D:/agent-wt/xbench-20261115-x10-ground-input-form --fallback none --no-local --timeout 240 -- npx vitest run src/solar/SolarWorkspaceTools.test.jsx src/solar/x10GroundFormIsolation.test.jsx src/solar/x10GroundNumericEquivalence.test.jsx --maxWorkers 1
```

For narrower reproduction, append `-t "measures represented edges"`,
`-t "ground form instance isolation"`, or
`-t "ground numeric semantic snapshots"`. These are reproduction instructions,
not a verification result.
