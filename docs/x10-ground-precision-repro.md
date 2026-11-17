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

Conversion now compares every generated corner axis in common metres using
exact Number equality, including reconstructed corners after converting the
site values back. There is no epsilon or coordinate quantization. A changed
represented corner adds this accessible status: "Precision loss: unit conversion
changed represented corners. Review the boundary coordinates." The source
boundary table remains available next to the current target boundary. When
unrelated invalid module inputs prevent a generated payload, the conversion
review still shows source and converted site tables. Site or unit edits and
Reset clear the conversion review. This warning describes Number representation
loss only, with no terrain or engineering conclusion.

For conversions within bounds, try origins `300000000` metres or `999999999`
feet and positive width and height `0.000001`. Compare the actual source and
target table cells in metres, then convert back. Ordinary origin zero and
`10` feet to `3.048` metres are exact controls without the warning. The
`compares actual source and converted` cases inspect real table readouts and
captured boundaries in both directions, without approximate equality.

Preset name composition invalidates the publishable snapshot once, hides
generated outputs and Copy, and disables workspace Generate until composition
ends. Intermediate draft names remain editable but emit no preset payload.
Completion validates Unicode and controls normally; cancellation to the
original name emits its valid snapshot again. Reset cancels composition and
ignores a late compositionend. Synthetic composition tests cover callbacks and
the mounted workspace gate, not native IME behavior in a browser.

Copy generated JSON is inside the explicit generated JSON disclosure. A user
click writes only that form's valid private payload, exactly matching its
pretty-printed pre text, through navigator.clipboard.writeText. There is no
automatic write, fallback clipboard path or network request. Pending copies
block duplicate clicks. Friendly status text handles success, unavailable API
and rejection. Edits, composition, Reset, disable and unmount retire pending
feedback. They cannot undo a clipboard write already initiated by the user.
Pending or unknown publication locks also cover Copy and all nested inputs.

Site inputs, Module inputs and Frame inputs have nested fieldset legends under
Ground layout inputs. Error IDs, links and the shared site help remain scoped
to each mounted form. Remaining per-frame modules uses exact individually
valid row and column counts, independently of other invalid inputs: default
9952, `1000` by `10` leaves zero, and `73` by `137` exceeds the 10,000 limit by
one while publication parameters remain null. Malformed or individually out of
range counts hide the count guidance. This does not claim site capacity.

`x10GroundKeyboardSafety.test.jsx` mounts the real SolarWorkspaceTools with
mocked canonical publication and checkout boundaries. Enter in structured or
advanced text inputs has no implicit form submission route. All action buttons
use type button. Focused Generate supports the semantic native button path:
the tests send keydown and keyup, then explicitly model the native activation
click to exercise real onClick capture, publication and callback. jsdom does
not synthesize all browser default keyboard actions; these checks make no live
browser or native keyboard activation claim.

Run the frozen focused command from the planner's Windows environment:

```text
python C:/Users/ehaug/.claude/scripts/offhost.py run --host leaf-svc --scope verify --cwd D:/agent-wt/xbench-20261115-x10-ground-input-form/web --worktree D:/agent-wt/xbench-20261115-x10-ground-input-form --fallback none --no-local --timeout 240 -- npx vitest run src/solar/SolarWorkspaceTools.test.jsx src/solar/x10GroundFormIsolation.test.jsx src/solar/x10GroundNumericEquivalence.test.jsx src/solar/x10GroundKeyboardSafety.test.jsx --maxWorkers 1
```

For narrower reproduction, append `-t "measures represented edges"`,
`-t "ground form instance isolation"`, or
`-t "ground numeric semantic snapshots"`. New focused filters include
`-t "compares actual source and converted"`, `-t "IME snapshots"`,
`-t "preset name composition"`, `-t "ground clipboard scope"`,
`-t "exact per-frame count capacity"`, `-t "new Copy action"` and
`-t "ground keyboard safety"`. The new keyboard file is explicitly named in
this operator command; the shared CI registry is outside this task's ownership.
These are reproduction instructions, not a verification result.
