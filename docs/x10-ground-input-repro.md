# Ground input reproduction (source and mocked publication)

For a selected canonical project with no drawing version, open Ground Physical's
project publication controls. In **Ground layout inputs**, enter origin X/Y and
positive rectangle width/height. Choose **Site drawing units** Metres or Feet.
The selector interprets the current numbers in the chosen units; it does not
convert them. To preserve physical site geometry, use **Convert site units to
feet** or **Convert site units to metres**. Conversion divides by the exact
declared factor 0.3048 for metres to feet, or multiplies by 0.3048 for feet to
metres, then switches the selected units. Origin X/Y, width and height change
together. Converted input tokens use JavaScript `String(number)` without decimal
truncation. Both the current and target site must satisfy the numeric, bounds and
distinct-corner checks; target tokens must also fit 64 characters. A refusal
keeps all input values, selected units and publication parameters unchanged and
announces the reason. Conversion itself never publishes anything.
For example, origin (10, -20), width 10, height 10 produces corners
`[[10,-20],[20,-20],[20,-10],[10,-10]]` in the selected drawing units.
Feet shows a separate converted size: 10 ft is 3.048 metres, rounded to at most
six decimal places. That display never converts published coordinates or
`drawing_units`; module dimensions, thickness and gaps always use metres.

Edit **Module thickness (metres)** (default 0.033), **Module power (watts)**
(default 715), **Horizontal module gap (metres)**
and **Vertical module gap (metres)** (both default 0.02), and **Module orientation**
(Portrait by default, or Landscape). The native preset receives `ModulePowerWp`,
`HorizontalGapM`, `VerticalGapM`, and `Orientation` from these controls.
Thickness maps to `ModuleThicknessM`; conversion leaves module metres and power
unchanged. The **Per-frame DC watts** summary is rows times columns times module
power, an integer bounded by 10,000,000,000 watts. It is one frame's module power
sum, with no site total or engineering inference, and disappears when inputs are
invalid. The existing site summary remains visible for valid inputs.

Review the accessible **Site rectangle schematic (not to scale)**. Its title and
description identify units, dimensions and origin, including negative origins.
It shows only the site rectangle, with normalized ratios and a visible minimum
for narrow dimensions. It changes no publication coordinates, renders no actual
module geometry and makes no terrain claim. Invalid inputs hide the schematic.
Read the current layout, review it, then use **Generate editable project drawing**.

Numeric tokens must be finite decimal numbers of at most 64 characters; blank,
NaN, Infinity and overflowing exponents refuse publication. Watts must be a safe
integer from 1 through 1,000,000. Gaps accept 0 through 100 inclusive. Module
length/width must be greater than 0 and at most 100 metres; thickness must be
greater than 0 and at most 1 metre. Rows/columns are
integers 1 to 1000 with product at most 10,000. Tilt is 0 to 90 degrees and azimuth
0 to 360. Site sizes are greater than 0 and at most 1,000,000 drawing units; origins
and all corners must stay within -1,000,000,000 to 1,000,000,000. Each positive
dimension must produce a distinct corresponding corner: `x + width > x` and
`y + height > y`. Tiny positive sizes that collapse at large positive or negative
origins are refused without approximating geometry. Errors identify both the
origin and size. Small representable dimensions remain legal.
Trimmed preset names have 1 to 256
characters. Orientation accepts only Portrait/Landscape. Revealed errors attach
to the implicated controls; correcting a field removes its error association.

**Advanced JSON** switches to **Site boundary (JSON)**, **Complete preset (JSON)**,
and **Drawing units for publication**; **Structured inputs** returns to the retained
rectangle draft. Selecting another project resets drafts. The separate **Original
layout intake (JSON)** file path preserves the original intake and its preset
loading choice (historical collection reuse by default, or current native loading).
Use **Clear original intake** to return to the manual input modes.

**Reset structured inputs** restores every structured field default, blank site
width/height, origin (0, 0), and metres, and clears revealed errors and conversion
feedback. Field IDs remain stable. Output parameters become null until a valid
site is entered again. This action preserves the separate Advanced JSON draft.

While publication is pending or its outcome is unknown, manual inputs, new module
controls, reset, conversion, mode switching and intake selection stay locked. An unknown outcome
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

For focused reproduction, append
`-t "refuses blank|collapsed|representable tiny|associates stable field errors"`
for numeric limits, collapse and DOM descriptors; append
`-t "locks .* drafts while pending and unknown"` for both locked modes and exact
request reconciliation. Capture/preview filters are
`-t "publishes ordinary|previews feet in metres|per-frame DC watts|accessible finite schematic|unique preview"`.
Conversion coverage is
`-t "converts site units|refuses site conversion|exact parameters on conversion refusal"`;
draft/reset coverage is
`-t "preserves separate mode drafts|resets structured inputs|reset structured inputs preserves"`.
The full command is the acceptance check. These notes record reproduction steps,
not a LIVE or bench PASS result.
