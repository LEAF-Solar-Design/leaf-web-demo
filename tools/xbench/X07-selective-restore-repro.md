# Selective loaded-role restore replay

Parent: Studio X-bench X07 reusable customer profiles and draft previews.
The loaded editor version is the restore baseline. A project's older pinned
version remains immutable and is displayed separately.

With existing web dependencies, run each offline fixture from the repository root:

```powershell
node tools/xbench/tests/x07_selective_restore_keyboard_dom.cjs --deps web/node_modules
node tools/xbench/tests/x07_selective_restore_races_dom.cjs --deps web/node_modules
node tools/xbench/tests/x07_selective_restore_adapter_dom.cjs --deps web/node_modules
node tools/xbench/x07_selective_restore_soak.cjs --deps web/node_modules --smoke
```

Change Module layer, remove Roof and add Obstruction. Restore loaded roof value
brings back the loaded roof without changing the module edit. Restore loaded
obstruction value removes that draft-only role. Restore loaded module value
copies the exact loaded label, including its Unicode representation. Each action
disappears when that role matches the baseline and focuses a mounted role input,
or Module layer when a draft-only role is removed. No restore calls Save.

The DOM keyboard helper dispatches Enter events and explicitly supplies jsdom's
missing default button activation. The browser receipt independently uses native
Enter. The races fixture restores while an earlier save is pending. Its late
acknowledgment updates the loaded version but retains the newer restored draft
and reports that it is unsaved. Replacing the customer/drawing aborts the old
operation and hides its restore state and success.

The actual JSX/Python adapter fixture pins version 1, explicitly saves ROOF into
version 2, restores optional roles while keeping an unrelated module edit, and
proves version 2 remains stored until explicit Save. That save creates version 3.
Independent SQLite readback checks every version and the original project pin.
The smoke run repeats sixteen optional-role and Unicode edit/restore cycles,
requires exact loaded maps, zero implicit writes, bounded nodes and settled DOMs.

All fixtures use synthetic callbacks or disposable stores and close owned
workers and DOMs in finally. They do not establish authenticated or LIVE acceptance.
