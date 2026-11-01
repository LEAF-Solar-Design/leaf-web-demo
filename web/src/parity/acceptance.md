# Studio parity acceptance matrix

`coverage.json` is the registration manifest for `programAcceptance.test.jsx`.
Each verified row registers exactly one test, named by `assertion`; deferred
rows register no tests and are printed with their gate. The initial matrix has
35 rows: 25 verified and ten deferred. Suite hooks check all DP-A1 through
DP-A25 identifiers, unique `(rule, slice, assertion)` triples, row shape,
contract rule-to-slice citations from amendment 2, implemented verified
assertions, and the actual started and completed counts.
There are no skipped tests. A failed assertion also prevents a completed count.

Rule meanings and contract citations come from root amendment 2, which supplies
the `leaf-design-system/docs/design-parity-v2.json` catalog at
`leaf-standard-v1.3.0`. Amendment 3 assigns DP-A21 and DP-A22 to the website
matrix (DP-W3-02) with the `website-only` gate. A verified row establishes its named assertion,
not every requirement that could fall under that rule's title.

Contract rows retain the supplied associations: SchemaForm is DP-A11 under
DP-W1-03, CodeTab is DP-A20 under DP-W1-06, and Sheets contrast is DP-A25
under DP-W1-09. The remaining direct acceptance checks belong to DP-W3-01.
This preserves assertions against merged W1 components without claiming that
a W1 contract whose citation list is empty cited a rule. The test enforces the
amendment's complete citation table for rows assigned to W1 or W2 slices,
with amendment 3's explicit DP-W2-06 copy-voice association.

The coverage scopes are:

| Rules | Evidence or deferral |
| --- | --- |
| DP-A1 | Lab Studio ground color adapter |
| DP-A2 | Root token count and default, Stage and Paper control line colors |
| DP-A3 | Ops text font recipe |
| DP-A4, DP-A5, DP-A6, DP-A7 | Computed spacing, container, radius and elevation tokens |
| DP-A8 | SheetFrame reduced-motion scrolling |
| DP-A9 | Decorative CockpitIcon accessibility |
| DP-A10 | Ghost button background |
| DP-A11 | SchemaForm descriptions and decorative unit suffix |
| DP-A12 | Deferred until shell navigation production assertions are available (F-SHELL) |
| DP-A13 | Computed Stage panel color roles |
| DP-A14 | ResultPanel header scopes and numeric cell classification |
| DP-A15 | DP-W2-03 deferred (F-SHELL+C2) |
| DP-A16 | ShortcutSheet groups; DP-W2-04 deferred (F-SHELL) |
| DP-A17 | DP-W2-05 deferred (C3) |
| DP-A18 | DP-W2-01 and DP-W2-02 deferred (F-SHELL) |
| DP-A19 | Button focus outline and independent single-key preference |
| DP-A20 | CodeTab clipboard resolution and code typography recipe |
| DP-A21 | DP-W3-02 deferred (website-only) for marketing rhythm |
| DP-A22 | DP-W3-02 deferred (website-only) for imagery parity |
| DP-A23 | Branding and dash scans; DP-W2-02 deferred (F-SHELL); DP-W2-06 deferred (F-COMM) |
| DP-A24 | Computed fine/coarse hit target, touch control and text tokens |
| DP-A25 | EscCap button semantics and Sheets hint contrast token |

Token rows check the production CSS contract in jsdom. They do not establish
rendered spacing, panel composition, responsive layouts or actual hit areas.
DP-A21 and DP-A22 are assigned to the website matrix by amendment 3;
these deferred rows do not claim Studio rendering evidence.

W1 production components supply EscCap, SchemaForm, ResultPanel, CodeTab,
ShortcutSheet, CockpitIcon and SheetFrame assertions. Shortcut registry entries
are deterministic fixtures. Lab adapter and button recipes use small DOM
fixtures with production CSS; they do not mount the entire application. Ops
and code typography assertions inspect their production recipes. These checks
do not claim full-shell layout, browser rendering or focus traversal coverage.

The suite injects `leaf-standard.css` and `parity-foundation.css` for each row.
It does not import `styles.css`, `landing.css` or `panels.css`. jsdom 25 cannot
resolve CSS variables or expand outline shorthand: the compatibility helper
resolves the injected production token cascade and materializes matching
declarations in their original selector order. The focus test toggles a class
substituted for `:focus-visible`. Expected values are asserted separately;
they are never inputs to that helper. Token theme assertions use computed
custom properties directly. The Sheets hint fixture uses the production
SheetFrame and Sheets stylesheet, following the W1 contrast test's setup.

## Promoting deferred rows

Only the root promotes rows after the corresponding merge and gate decision.
The initial gates are:

| Slice | Gate |
| --- | --- |
| DP-W2-01 | F-SHELL |
| DP-W2-02 | F-SHELL |
| DP-W2-03 | F-SHELL+C2 |
| DP-W2-04 | F-SHELL |
| DP-W2-05 | C3 |
| DP-W2-06 | F-COMM |

DP-W2-02 has separate DP-A18 and DP-A23 rows. DP-W2-06 has a DP-A23
commercial-copy row under amendment 3, since amendment 2 names no different
rule for it. Its F-COMM gate remains deferred.

DP-W2-11 and DP-W2-12 are website slices and have no Studio rows under
amendment 3. DP-A21 and DP-A22 instead have DP-W3-02 rows deferred with
`fork: "website-only"`. Their evidence belongs in the website matrix; these
rows are not promoted by merging those website slices into this checkout.

Replace the placeholder assertion name with a concrete production assertion,
implement that exact name in the test's `assertions` object, change `status`
to `verified`, and set `fork` to null in the same edit. Add separate rows for
independent checks as needed, keeping triples and verified test names unique.
The implementation lookup fails closed if a root flips status without adding
an assertion. Keep an unmerged row deferred with its named gate, never as a
skipped or empty test. If a specified value disagrees with production, leave
production alone, defer the affected row with `fork: "value-mismatch"`, and
record the observed source and value here for the root's decision.

## Known pre-existing copy hits

Amendment 3 corrects the earlier zero-dash baseline and permits these four
pre-existing JSX text nodes, each containing only U+2014:

| File | Line | Text node |
| --- | --- | --- |
| `web/src/components/operator/AuditPanel.jsx` | 55 | Session placeholder |
| `web/src/site/DrawingCockpit.jsx` | 615 | X coordinate placeholder |
| `web/src/site/DrawingCockpit.jsx` | 616 | Y coordinate placeholder |
| `web/src/site/DrawingCockpit.jsx` | 617 | Scale placeholder |

The DP-A23 dash-scan row is verified. The test excludes exactly these four
file-and-line hits, each with the exact text `—`; no other hits are excluded.
Paths are normalized to workdir-relative forward-slash form on every platform.
Any additional dash text, changed line or new location fails the assertion.
There are no known obsolete branding hits.

The branding scan reads every `.jsx` file
recursively under `src/components` and `src/site`, including test files. The
dash scan parses those files with the existing Babel parser and inspects JSX
text nodes, reporting exact file and line for U+2013 or U+2014. Comments and
JavaScript expressions are not JSX text nodes. Parse or read errors fail the
row rather than narrowing the scan.

## Verification owned by the planner

Run the task record's mini_node Vitest command targeting
`src/parity/programAcceptance.test.jsx`. Its executed test count must match
the verified row count with zero skips. Registration alone is not execution
evidence; retain the runner's counts. Run the record's verify-floor commands
separately.

The rendered check remains a separate root obligation at
`https://staging.leafautomation.ai/app?demo=1`, at 1440x1000 and 390x844:
no horizontal document scroll, controls at least 44px at the narrow viewport,
and Tab reaching the command bar, rail, Details and back. A green jsdom matrix
does not establish those browser measurements.
