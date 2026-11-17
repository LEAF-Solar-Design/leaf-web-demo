# Ground input reproduction (source and mocked publication)

For a selected canonical project with no drawing version, open Ground Physical's
project publication controls. In **Ground layout inputs**, enter origin X/Y and
positive rectangle width/height. Choose **Site drawing units** Metres or Feet.
For example, origin (10, -20), width 10, height 10 produces corners
`[[10,-20],[20,-20],[20,-10],[10,-10]]` in the selected drawing units.
Feet shows a separate converted size: 10 ft is 3.048 metres, rounded to at most
six decimal places. That display never converts published coordinates or
`drawing_units`; module dimensions and gaps always use metres.

Edit **Module power (watts)** (default 715), **Horizontal module gap (metres)**
and **Vertical module gap (metres)** (both default 0.02), and **Module orientation**
(Portrait by default, or Landscape). The native preset receives `ModulePowerWp`,
`HorizontalGapM`, `VerticalGapM`, and `Orientation` from these controls.
Read the current layout, review it, then use **Generate editable project drawing**.

Numeric tokens must be finite decimal numbers of at most 64 characters; blank,
NaN, Infinity and overflowing exponents refuse publication. Watts must be a safe
integer from 1 through 1,000,000. Gaps accept 0 through 100 inclusive. Module
length/width must be greater than 0 and at most 100 metres. Rows/columns are
integers 1 to 1000 with product at most 10,000. Tilt is 0 to 90 degrees and azimuth
0 to 360. Site sizes are greater than 0 and at most 1,000,000 drawing units; origins
and all corners must stay within ±1,000,000,000. Trimmed preset names have 1 to 256
characters. Orientation accepts only Portrait/Landscape. Revealed errors attach
to the implicated controls; correcting a field removes its error association.

**Advanced JSON** switches to **Site boundary (JSON)**, **Complete preset (JSON)**,
and **Drawing units for publication**; **Structured inputs** returns to the retained
rectangle draft. Selecting another project resets drafts. The separate **Original
layout intake (JSON)** file path preserves the original intake and its preset
loading choice (historical collection reuse by default, or current native loading).
Use **Clear original intake** to return to the manual input modes.

While publication is pending or its outcome is unknown, manual inputs, new module
controls, mode switching and intake selection stay locked. An unknown outcome
offers **Reconcile layout publication**: retry the same captured request, without
editing or rereading it. Existing native publication/history and project/profile
continuity paths remain the responsibility of the composed workspace publisher.

The registered mounted suite uses the real workspace and capture/composition path
with mocked publication services. It is source proof, not LIVE publication or a
full terrain/engineering assessment. The original advanced intake assertion remains
alongside the structured capture checks. Run the existing offhost command from
the planner's Windows environment:

```text
python C:/Users/ehaug/.claude/scripts/offhost.py run --host leaf-svc --scope verify --cwd D:/agent-wt/xbench-20261115-x10-ground-input-form/web --worktree D:/agent-wt/xbench-20261115-x10-ground-input-form --fallback none --no-local --timeout 240 -- npx vitest run src/solar/SolarWorkspaceTools.test.jsx --maxWorkers 1
```

For focused reproduction, append `-t "refuses blank|associates stable field errors"`
for invalid controls and their DOM descriptors; append
`-t "locks .* drafts while pending and unknown"` for both locked modes and exact
request reconciliation. Capture/preview filters are
`-t "publishes ordinary|previews feet in metres"`; draft/reset coverage is
`-t "preserves separate mode drafts"`. The full command is the acceptance check.
