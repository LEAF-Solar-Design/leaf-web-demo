# LeafCutListTools AppBundle — the timber cut-list tool (`timber-cutlist`)

The first `kind: "appbundle"` tool in the registry: compiled C# loaded into the headless
AutoCAD engine on APS Design Automation. One command, `LEAFCUTLIST`, reads the open
drawing natively (Line, Polyline, Text, MText, block references), runs the
`CutLists.Core` engine, and writes `result.json` in the §3 envelope:

- `result.table {columns, rows}`: the cut list (members, then connections)
- `result.running_metres {key: m}`, `result.views [{kind, label, members}]`, `result.warnings []`
- `result.files [{name, mime, base64}]`: `cutlist.csv` and `cutlist.pdf`
- `overlay.markers` (view labels) and `overlay.polylines` (merged members, colour by material)

`params.json` (optional): `wall_end_spacing_mm` 300, `wall_mid_spacing_mm` 600,
`joist_spacing_mm` 600, `merge_tolerance_mm` 2, `reconcile_views` true.

## Source and build

The source lives with the engine it hosts (the client deliverable keeps one engine for
desktop and platform, no fork): `CutLists.AppBundle/` in the CutLists solution. Build and
pack there with `Pack.ps1` (net8.0-windows, x64, AutoCAD 2026 managed refs `Private=false`,
so `acdbmgd`/`accoremgd` are never in the zip). Copy `dist/LeafCutListTools.zip` and
`PackageContents.xml` here. Record the zip's sha256 in the PR.

Local oracle before provisioning: `Oracle.ps1 -Dwg <file> -ExpectedCsv <desktop csv>` runs the
bundle in local `accoreconsole.exe` (PowerShell `Start-Process`, CRLF `.scr`, mark-saved
QUIT) and byte-compares the CSV with the desktop app's. Both shipped fixtures are identical.

## Wire into Design Automation (ROOT runs the live calls)

1. `python da/provision_live.py --appbundle engine/appbundle-cutlist/LeafCutListTools.zip`
   creates AppBundle `LeafCutListTools` on `Autodesk.AutoCAD+26_0` (or a new version on
   409), uploads the zip, aliases `prod`.
2. `python da/provision_live.py --tools engine/registry.json` provisions
   `LeafTool_leaf_cutlist` from `da/client.tool_activity_spec` (appbundle branch:
   `/al` loads the bundle, the script runs `LEAFCUTLIST` then a mark-saved QUIT).
3. `POST /api/run {"tool": "timber-cutlist"}` on a stored drawing; the broker routes
   `kind: "appbundle"` to APS (`tool_loader.run_tool_dynamic`, local file is None).

Dry run of both specs without any network: `python da/provision_live.py --dry-run --tools engine/registry.json`.
