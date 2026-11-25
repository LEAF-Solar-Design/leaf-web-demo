# Undo last restore replay

Parent: Studio X-bench X07 loaded-role restoration and reusable customer profiles.
Undo restores a draft snapshot. It does not roll back a stored version or project pin.

Run from the repository root with existing web dependencies:

```powershell
node tools/xbench/tests/x07_restore_undo_keyboard_dom.cjs --deps web/node_modules
node tools/xbench/tests/x07_restore_undo_races_dom.cjs --deps web/node_modules
node tools/xbench/tests/x07_restore_undo_adapter_dom.cjs --deps web/node_modules
node tools/xbench/x07_restore_undo_soak.cjs --deps web/node_modules --smoke
```

Remove Roof, restore its loaded value, then activate Undo last restore. Roof
becomes absent again and Module layer receives focus. Add a draft-only Obstruction,
restore it away and undo: its exact Unicode label returns and its input receives
focus. Two restores retain only the snapshot before the last restore. One undo
consumes it, and no second undo action remains. Save is never called by either action.

Any subsequent field edit, role addition/removal, reset or explicit reload clears
undo. Changing customer or drawing retires it with the old editor. The keyboard
fixture sends Enter events with jsdom's default activation supplied explicitly;
the source-bound browser receipt separately verifies native Enter.

For a pending-save race, edit a role, restore it, start an explicit save and then
undo. Undo advances the draft revision. The earlier save acknowledgment may update
the loaded version but cannot clear the newer draft. A separate customer/drawing
A-B-A fixture checks abort and retirement of old acknowledgments.

The actual JSX/Python adapter fixture repeats this for two customers. It creates
loaded version 2 explicitly, restores and undoes unsaved removed/draft-only roles,
proves version 2 remains stored until Save, and checks explicit version 3 and both
original version 1 project pins independently in SQLite. Smoke mode compares every
draft against a separate model over sixteen optional-role and Unicode sequences,
with at most one snapshot, bounded mounted nodes, zero writes and settled DOMs.

All inputs and stores are disposable. Owned children and DOMs close in finally.
These fixtures do not establish authenticated integration or LIVE acceptance.
