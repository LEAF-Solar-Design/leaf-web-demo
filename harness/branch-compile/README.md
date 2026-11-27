# Branch package compile gate

Every Branch author submission in staging passes through the pinned Branch2025 ScriptHost compiler and API scanner before the harness stages the package. The model receives V18 diagnostics and can submit a correction in the same session. An unavailable checker also produces V18. The boot probe reports the decision, duration and allowlist hash after the server starts listening.

## Distribution scope (staging harness image only)

Two locked reference sets ship. The 2025 entry of `acad-refs.lock.json` supplies three Autodesk reference assemblies, each verified by its `dllSha256`:
`autocad.net.core 25.0.0 lib/net8.0/AcCoreMgd.dll`, `autocad.net.model 25.0.0 lib/net8.0/AcDbMgd.dll`,
`autocad.net 25.0.1 lib/net8.0/AcMgd.dll`. The net48 check adds the 2023 entry's `autocad.net.core`, `autocad.net.model`
and `autocad.net` 24.2.0 `lib/net47` DLLs with the same three filenames, plus `mscorlib.dll`, `System.dll` and
`System.Core.dll` from `Microsoft.NETFramework.ReferenceAssemblies.net48` 1.0.3 and `Newtonsoft.Json` 13.0.3
`lib/net45/Newtonsoft.Json.dll`. All ten files are checked against pinned SHA-256 hashes during the image build.
They are compile-time metadata references for Roslyn only (never loaded for
execution, never executed), placed in the staging harness image only, and not redistributed anywhere else
(no S3 upload, no artifact upload, no other image). The NuGet packages themselves are restored only inside the Docker build
stage and are not copied into the runtime image; only the listed DLL files are.

## AutoCAD 2023 and 2024 check

The harness-owned `net48/` checker runs on .NET 8 and compiles against AutoCAD 2023 and .NET Framework 4.8 metadata.
Its globals project links the vendored Abstractions and ToolBudget sources. ToolBudget supplies the guards inserted
by the compiler's budget rewrite. MetadataLoadContext resolves target types without executing the reference assemblies.
The checker scans the original source, rewrites its budgets and emits to memory. It never loads or runs the emitted tool.
All vendored files and `BRANCH-PIN.json` are unchanged.

| AutoCAD 2025 net8 gate | AutoCAD 2023 net48 check | `hosts.min_release` written |
| --- | --- | --- |
| accept | accept | max(declared, 2023) |
| accept | refused | max(declared, 2025) |
| accept | unavailable, except not_configured | max(declared, 2025) |
| accept | not_configured | declared value, while the label checker is not deployed |
| refused or unavailable | not run | submission fails with V18 |

The net48 result never fails a submission or adds compile attempts or V18 diagnostics. A floor of 2023 covers
AutoCAD 2023, 2024, 2025 and 2026; a floor of 2025 covers 2025 and 2026. Higher declared floors are preserved.
AutoCAD 2024-only API members are conservatively labelled 2025. This proves compile compatibility, not CAD execution.
Both checks share the two-process limit, and each has its own 30-second deadline including queue time.

## Image build and parity

`BRANCH-PIN.json` identifies the upstream commit and SHA-256 of each vendored file. The build restores the locked dependencies, publishes a self-contained Linux x64 checker and compares the exit code and JSON output for each of the four parity packages with the Windows CLI captures. Only the framework string varies between platforms.

The staging task has 1 vCPU and 2048 MB of memory. One reference-package check took 4.5–5.3 seconds and peaked at 130 MB RSS on Ubuntu. The harness limits checks to two at a time per process. The 30-second deadline includes queue time.
