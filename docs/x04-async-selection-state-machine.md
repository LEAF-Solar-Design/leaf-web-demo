# X04 module selection callback states

Parent: Studio X-bench X04 raw drawing intake, track 2. These synthetic checks
prove component and saved-intake behavior offline. They do not prove the real
drawing oracle, live job recording, browser layout or assistive technology use.

| Trigger | Visible state and permitted next action |
|---|---|
| Current listing with no explicit choice | Count button disabled. Choose a candidate. |
| Explicit choice | Count button enabled when a current callback exists and no run is active. |
| Submit returns a pending promise or thenable | Count controls disabled; polite status says Counting modules. No duplicate callback. |
| Current callback resolves | Controls become usable. Completion alone supplies no Counted summary. |
| Current callback throws or rejects | Generic actionable alert, choices retained, one retry per activation. Private errors are hidden. |
| Then getter throws | Same generic failure and usable retry. |
| Thenable resolves or rejects repeatedly | Native promise assimilation accepts its first terminal result only. Later calls cannot replace it. |
| Drawing A changes to B, then returns to A | Old request epoch is invalid. Choices must be explicit again, even if IDs match. |
| New A request starts, then old A callback settles | Old success or failure cannot unlock the new request or change its alert. |
| Selected candidate disappears while pending | Choice is removed permanently. Completion does not restore it. |
| A matching recorded result arrives | Counted text requires current IDs, labels and counts, plus a valid physical-union bound. |
| User changes choices after recorded result | Old Counted text is hidden; request a fresh count. |

The callback starts work. The result envelope records the chosen sets and count.
Those are separate events: resolving a callback does not invent accepted count
evidence. The containing App must withhold the selection callback for results
whose drawing origin differs from the current drawing.

Saved candidate `space` and `layout` fields must be strings when supplied.
Missing fields retain model-space defaults. Incompatible values refuse the
whole request with re-import guidance before any partial listing or count.
Valid paper-space or named sheet-layout records stay excluded.

Run from the X04 worktree using existing installed dependencies:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_space_layout_types.py server/tests/test_x04_selection_envelope_consistency.py -q
npm --prefix web run test:unit -- src/components/ModuleSetPicker.test.jsx
node scripts/bench_x04_picker_id_payload.mjs --out D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-picker-id-payload.json
```

The picker tests cover A/B/A return and both old-promise terminal outcomes,
throwing then getters, repeated thenable terminals and retry. The Python checks
pin listing, single-set and overlapping multi-set envelopes through JSON, with
repeated selector IDs, exact counts and unique highlights. The benchmark fixes
32 candidates while varying opaque ID length, including a lone surrogate. It
asserts label associations and exact callback IDs, measures DOM ID size and
render duration without a timing gate, then unmounts and closes each DOM.
