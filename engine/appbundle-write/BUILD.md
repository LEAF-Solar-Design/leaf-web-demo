# LeafWriteTools AppBundle — build & package notes (drawing.write productization)

The **compiled productization path** for `drawing.write`. The live, proven path is
the LISP recipe in `da/write_lisp.py` driven by `da/write_spike.py` against the
`LeafWriteProbe` Activity (`kind: "script"`, no build). This bundle is the skeleton
for when a write tool wants real C# / full ObjectARX access (transactions,
geometry libraries, structured error handling) instead of a headless `.scr`.

## What this is
One `CommandMethod` in `Contents/LeafWriteTools.cs`, mirroring the LISP mutate
order and writing `output.dwg` (the Activity's Result localName) to the DA
working dir:

| Command          | engine_op     | LISP equivalent                    |
|------------------|---------------|------------------------------------|
| `LEAFWRITEPROBE` | `write_probe` | `da/write_lisp.build_write_scr()`  |

Mutation (load-bearing order, identical to the LISP spike):
1. `Erase()` one pre-existing model-space `Polyline` (the DELETE).
2. Ensure `LayerTableRecord` `LEAF_WRITE_PROBE` exists (the layer Make).
3. Append a closed 4-vertex `Polyline` on `LEAF_WRITE_PROBE` (the ADD).
4. `Database.SaveAs("output.dwg", DwgVersion.Current)`.

Verification is identical to the LISP spike: re-extract `output.dwg` and assert
`LEAF_WRITE_PROBE` is present with a polyline on it (ADD) and exactly one original
polyline handle is gone (DELETE).

## Build (skeleton — refs must exist locally)
```
dotnet build LeafWriteTools.csproj -c Release
```
Requires AutoCAD managed refs (`acdbmgd.dll`, `accoremgd.dll`) at the HintPath in
the `.csproj`. Pick `TargetFramework` by target engine:

| DA engine               | TargetFramework    | AutoCAD refs             |
|-------------------------|--------------------|--------------------------|
| `Autodesk.AutoCAD+24_3` | `net48`            | 2024                     |
| `Autodesk.AutoCAD+25_1` | `net8.0-windows`   | 2025                     |
| `Autodesk.AutoCAD+26_0` | `net8.0-windows`   | 2026 (default in csproj) |

> The refs are `Private=false` — the engine supplies them at runtime; the bundle
> must not ship them, or the load fails on a version clash (same discipline as the
> read-tool bundle in `../appbundle/` and the Branch2025 two-DLL deploy).

## Package as an AppBundle (.zip that DA ingests)
```
LeafWriteTools.bundle/
  PackageContents.xml            <- this dir's PackageContents.xml
  Contents/
    LeafWriteTools.dll           <- dotnet build output
```
```powershell
# from engine/appbundle-write after a successful build:
$stage = "LeafWriteTools.bundle"
New-Item -ItemType Directory -Force "$stage/Contents" | Out-Null
Copy-Item PackageContents.xml "$stage/"
Copy-Item bin/Release/LeafWriteTools.dll "$stage/Contents/"
Compress-Archive -Path "$stage" -DestinationPath LeafWriteTools.zip -Force
```

## Wire into Design Automation (root owns the live calls)
1. `POST appbundles` with id `LeafWriteTools`, engine `Autodesk.AutoCAD+26_0`;
   upload `LeafWriteTools.zip` to the returned signed URL; create alias `prod`.
2. Create an **Activity** whose `commandLine` NETLOADs the bundle and runs the
   command, e.g.
   `"$(engine.path)\\accoreconsole.exe /i $(args[HostDwg].path) /al $(appbundles[LeafWriteTools].path) /s $(settings[script].path)"`
   where a tiny bootstrap `.scr` is just `LEAFWRITEPROBE\n`.
   - parameters: input `HostDwg` (`input.dwg`), output `Result` (`output.dwg`).
3. `POST workitems` with the drawing GET url + an output PUT url; poll; download
   `output.dwg` and re-extract to verify.

## Status
Skeleton, deliberately **not built here** (no guarantee the AutoCAD 2026 refs
resolve on this host). The LISP spike (`da/write_spike.py`) is the demonstrated
`drawing.write` capability; this bundle is the compiled path to productize it.
