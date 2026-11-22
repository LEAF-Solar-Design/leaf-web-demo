# Role difference preview replay

Parent: Studio X-bench X07 reusable customer drawing profiles.
These are offline injected UI and disposable SQLite checks, not LIVE acceptance.

Run from the repository root with existing web dependencies:

```powershell
python -P -m pytest --disable-plugin-autoload -q tools/xbench/tests/test_x07_role_product_journeys.py
node tools/xbench/tests/x07_role_diff_adapter_dom.cjs --deps web/node_modules
node tools/xbench/x07_profile_diff_render_perf.cjs --deps web/node_modules --smoke
```

The X07-only Python wrapper runs the real adapter review, keyboard focus,
client A-B-A, role difference and render smoke fixtures. Web unit tests stay
independent of these fixtures. The CI web-family audit has no exemption.

In the editor, changing Module layer and removing Roof previews exact before
and after values against the displayed loaded version. Previewing writes
nothing. Reset draft cancels those edits and shows No role changes. A refused
save retains both the draft and its differences. Only an explicit save writes.

The profile review also compares its immutable pinned roles with the loaded
current roles. Equal roles on different versions show No role changes. Saving
changed roles updates this display while preserving the original pin and its
separate role summary. Draft edits alone do not change current-role drift.

The real adapter fixture uses two customers. It adds ROOF into version 2,
removes it and changes the module layer in version 3, then compares the rendered
values with independent maps and the read-only Python version diff tool. The
layers Mo\u0301D and M\u00f3D remain distinct; no Unicode normalization or trimming
is performed. Python diff and foreign-scope refusal preserve database bytes.
Both old pins still reference version 1 with its original roles.

The smoke tool renders five label lengths up to 4096 characters with eight
draft updates each, verifies exact counts and values, and reports bounded
mounted nodes and measured timings. It closes its DOM after each case.
Every child and disposable store settles in finally. No network or real
customer input is required. Browser captures use synthetic callbacks and have
their own source-bound replay and cleanup receipts.
