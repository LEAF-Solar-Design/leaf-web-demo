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
Before stringifying target numbers, conversion also refuses any nonzero source
coordinate or dimension that becomes numeric zero. For example, feet origins
`5e-324` and `-5e-324` cannot convert to metres. Refusal preserves the entire
draft, units and exact parameter object without another callback. Literal zero,
negative zero and representable tiny converted coordinates remain legal.
For example, origin (10, -20), width 10, height 10 produces corners
`[[10,-20],[20,-20],[20,-10],[10,-10]]` in the selected drawing units.
Feet shows a separate converted size: 10 ft is 3.048 metres, rounded to at most
six decimal places. That display never converts published coordinates or
`drawing_units`; module dimensions, thickness and gaps always use metres.
Each positive feet dimension below display precision, including multiplication
underflow, reads "below display precision" in the converted size. Width and
height are formatted independently, so a tiny width can appear alongside a
normal height such as 3.048 metres without showing a false zero.

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

For valid inputs, **Site boundary coordinates (drawing units: m)** or the feet
equivalent lists the exact four generated boundary vertices in their existing
order. Corner row headers and X/Y column headers identify the selected drawing
units. Coordinates use full roundtrippable Number strings; no closing vertex is
added. **Site rectangle centre (drawing coordinates)** gives X/Y in m or ft,
derived from the generated boundary's opposite corners. Conversion preserves the
centre's physical meaning within Number arithmetic. This is the rectangle centre,
with no module, site DC or terrain interpretation.

Open **Generated source input parameters (JSON)** to inspect read-only source
inputs for generation, not a saved result. The disclosure starts closed. Its
escaped text is JSON from the exact parameter object passed to the callback,
including the current boundary, native preset and drawing units. Edits and unit
conversion update it. Names containing Unicode or HTML-like text remain text.
Invalid inputs and reset remove the disclosure, coordinate table and centre.

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
Nonzero decimal coefficients that convert to numeric zero, such as `1e-999`
and `-1e-999`, are refused in every numeric field, including origins, gaps and
angles. Significand digits determine mathematical zero independently of the
exponent. Literal `0`, `-0`, `0.0` and `0e-999` remain legal where zero is allowed;
`5e-324` remains supported where range and corner checks allow it. Rows, columns
and watts require exact decimal-token integrality before conversion to Number.
`1.00000000000000001` and `0.99999999999999999` refuse publication even if Number
rounds them to an integer. `1e1`, `1000e-2` and `1.00e0` remain legal. The bounded
parser compares decimal places, trailing zeros and an exponent; it never expands
large powers of ten.
Valid inputs show site rectangle area in square metres and perimeter in metres.
Feet use 0.3048 only for this display calculation. A 10 by 20 metre rectangle
and equivalent represented feet show 200 square metres and 60 metres, to six
decimal places. Positive measurements below 0.000001, including calculation
underflow, read "below display precision" instead of a false exact zero. These
rectangle measurements make no buildable area or site DC claim and disappear
with invalid inputs. Publication coordinates remain in the selected units.
Raw preset names reject C0 and C1 controls, including leading, trailing and
embedded newlines or tabs, before trimming. Lone high or low UTF-16 surrogates
are rejected; paired emoji, non-Latin names and composed or decomposed text are
retained without normalization or rewriting. Valid names receive trimming only
and must have 1 to 256 characters after trimming. Name errors attach to the name
control, clear parameters and disable generation.
Orientation accepts only Portrait/Landscape. Revealed errors attach
to the implicated controls; correcting a field removes its error association.
The single revealed error summary links each invalid field by its human label.
Links focus the real control without hash navigation. Correcting one field
removes only its link; reset clears the summary. Locked forms disable these
focus actions. Any site coordinate, size or units edit clears old conversion
success or refusal feedback. Module-only edits retain that feedback; another
conversion action supplies fresh feedback.

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
python C:/Users/ehaug/.claude/scripts/offhost.py run --host leaf-svc --scope verify --cwd D:/agent-wt/xbench-20261115-x10-ground-input-form/web --worktree D:/agent-wt/xbench-20261115-x10-ground-input-form --fallback none --no-local --timeout 240 -- npx vitest run src/solar/SolarWorkspaceTools.test.jsx src/solar/x10GroundFormIsolation.test.jsx --maxWorkers 1
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

Conversion underflow and display filters are
`-t "subnormal|representable origin conversion|tiny feet size"`.
Raw name and Unicode publication filters are
`-t "raw preset name controls|valid Unicode with trim only"`.
Exact disclosure, coordinate table and centre coverage uses
`-t "discloses exact read-only source JSON"`.
These cases use the real form with callback capture or the mounted workspace
with mocked publication. The refusal cases assert no publication call; the
direct form cases compare the full draft, callback count and parameter identity.

New numeric and display filters are
`-t "nonzero decimal underflow|mathematical zero|rectangle area and perimeter"`.
Summary and feedback filters are
`-t "single error summary|stale conversion"`.
The explicitly named isolation suite mounts two real forms with independent
callbacks and scoped fieldset queries. It edits either instance, checks local
IDs and accessible targets, conversion feedback, errors, correction and reset,
then replaces one callback and checks current capture and absence of late cross
instance effects. Its filter is `-t "ground form instance isolation"`.
