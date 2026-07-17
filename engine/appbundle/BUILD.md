# LeafDaTools AppBundle — build & package notes

The **compiled-tool** option for Lane B (`kind: "appbundle"`). The live demo path
is the LISP scripts in `../tools/` (`kind: "script"`), which need no build. This
bundle is the skeleton for when a tool wants real C# / full ObjectARX access.

## What this is
Three `CommandMethod`s in `Contents/LeafDaTools.cs`, one per engine_op, each
writing `result.json` (Result envelope, CONTRACT.md §3) to the DA working dir:

| Command        | engine_op                    | registry tool                |
|----------------|------------------------------|------------------------------|
| `LEAFCOUNT`     | `count_by_layer`             | count-by-layer               |
| `LEAFAREA`      | `measure_panel_area`         | measure-panel-area           |
| `LEAFHIGHLIGHT` | `highlight_panels_near_edge` | highlight-panels-near-edge   |

Oracle for all three: `python ../selfcheck.py` (pure-python mirror).

## Build (skeleton — refs must exist locally)
```
dotnet build LeafDaTools.csproj -c Release
```
Requires AutoCAD managed refs (`acdbmgd.dll`, `accoremgd.dll`) at the HintPath in
the `.csproj`. Pick TargetFramework by target engine:

| DA engine                 | TargetFramework      | AutoCAD refs |
|---------------------------|----------------------|--------------|
| `Autodesk.AutoCAD+24_3`   | `net48`              | 2024         |
| `Autodesk.AutoCAD+25_1`   | `net8.0-windows`     | 2025         |
| `Autodesk.AutoCAD+26_0`   | `net8.0-windows`     | 2026 (default in csproj) |

> The refs are `Private=false` — the engine supplies them at runtime; the bundle
> must not ship them, or the load fails on a version clash (same discipline as the
> Branch2025 two-DLL deploy).

## Package as an AppBundle (.zip that DA ingests)
The AppBundle is a folder named `LeafDaTools.bundle/` zipped:
```
LeafDaTools.bundle/
  PackageContents.xml            <- this dir's PackageContents.xml
  Contents/
    LeafDaTools.dll              <- dotnet build output
```
```powershell
# from engine/appbundle after a successful build:
$stage = "LeafDaTools.bundle"
New-Item -ItemType Directory -Force "$stage/Contents" | Out-Null
Copy-Item PackageContents.xml "$stage/"
Copy-Item bin/Release/LeafDaTools.dll "$stage/Contents/"
Compress-Archive -Path "$stage" -DestinationPath LeafDaTools.zip -Force
```

## Wire into Design Automation (Lane A / root own the live calls)
1. `POST appbundles` with id `LeafDaTools`, engine `Autodesk.AutoCAD+26_0`; upload
   `LeafDaTools.zip` to the returned signed URL; create alias `prod`.
2. Create one **Activity** per command, e.g. `count_by_layer`:
   - `commandLine`: `"$(engine.path)\\accoreconsole.exe /i $(args[dwg].path) /al $(appbundles[LeafDaTools].path) /s $(args[script].path)"`
     where a tiny bootstrap `.scr` (`LEAFCOUNT\n`) invokes the command, **or** use
     `/al` + a script that just runs the command name.
   - parameters: input `dwg` (the DWG), optional input `params` (`params.json` for
     highlight), output `result` (`result.json`).
3. `POST workitems` with the drawing + optional params URLs and an output upload
   URL; poll; download `result.json` → return as the Result envelope.

The Activity/AppBundle/WorkItem POSTs are **root's** live APS calls — this bundle
is only the payload they run. `da/client.run_tool(...)` (Lane A) dispatches by the
tool's `engine_op`; before APS is live it calls `selfcheck.run_mock(engine_op, …)`.

## Status
Skeleton, deliberately not built here (no guarantee the AutoCAD refs resolve on
this host). The logic matches `../selfcheck.py` line-for-line so the compiled
output can be diffed against the oracle once built.
