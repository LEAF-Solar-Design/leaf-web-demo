# Branch package compile gate

Every Branch author submission in staging passes through the pinned Branch2025 ScriptHost compiler and API scanner before the harness stages the package. The model receives V18 diagnostics and can submit a correction in the same session. An unavailable checker also produces V18. The boot probe reports the decision, duration and allowlist hash after the server starts listening.

## Distribution scope (staging harness image only)

Exactly three Autodesk reference assemblies, from the 2025 entry of `acad-refs.lock.json`, each verified by its `dllSha256`:
`autocad.net.core 25.0.0 lib/net8.0/AcCoreMgd.dll`, `autocad.net.model 25.0.0 lib/net8.0/AcDbMgd.dll`,
`autocad.net 25.0.1 lib/net8.0/AcMgd.dll`. They are compile-time metadata references for Roslyn only (never loaded for
execution, never executed), placed only in the leaf-platform harness container image, and not redistributed anywhere else
(no S3 upload, no artifact upload, no other image). The NuGet packages themselves are restored only inside the Docker build
stage and are not copied into the runtime image; only the three DLL files are.

## Image build and parity

`BRANCH-PIN.json` identifies the upstream commit and SHA-256 of each vendored file. The build restores the locked dependencies, publishes a self-contained Linux x64 checker and compares the exit code and JSON output for each of the four parity packages with the Windows CLI captures. Only the framework string varies between platforms.

The staging task has 1 vCPU and 2048 MB of memory. One reference-package check took 4.5–5.3 seconds and peaked at 130 MB RSS on Ubuntu. The harness limits checks to two at a time per process. The 30-second deadline includes queue time.
