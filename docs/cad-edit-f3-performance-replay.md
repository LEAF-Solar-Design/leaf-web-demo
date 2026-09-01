# CAD edit F-3 performance replay

See `~/.claude/MISSION.md`. F-3 is the browser CAD editing slice in the hosted web lane of the natural-language CAD tool-building platform. This replay measures its real client-side engine path.

## Prerequisites

Use the exact source checkout that the receipt will name. Install the locked web dependencies and the matching Chromium binary outside this replay:

```powershell
Set-Location web
npm ci
npx playwright install chromium
Set-Location ..
```

Then build the machine-local engine package if `vendor/acadrust-worker/pkg-web` is absent:

```powershell
$env:RUSTFLAGS='--cfg getrandom_backend="wasm_js"'; Push-Location vendor/acadrust-worker; wasm-pack build --release --target web . --out-dir pkg-web --out-name engine; Pop-Location
```

## Replay

From the repository root:

```powershell
Set-Location web
npm run test:cad-edit-closeout-measurement
npm --silent run measure:cad-edit-closeout | Tee-Object -FilePath ../docs/cad-edit-f3-performance-receipt.json
```

The measurement command writes one JSON record to stdout. Its schema is `leaf.cad-edit-f3-performance.v1`. The command does not create or replace a receipt file itself.

## Frozen budgets

The load budget uses 10 fresh Chromium contexts. Each sample starts immediately before Playwright calls `setInputFiles` with `vendor/acadrust-worker/fixtures/one_line.dxf`. It stops when the real `CadEditSurface`, backed by the real browser worker and compiled engine, displays a nonempty `data-testid="cad-edit-entity-list"`. The nearest-rank p95 must be at most 2,000 ms.

The bundle budget builds the exact checkout twice with `vite build --manifest`. The only intended environment difference is `VITE_CAD_EDIT=1` versus `VITE_CAD_EDIT=0`. Starting at the manifest ENTRY, the replay follows only static imports and counts each reachable JavaScript and CSS artifact once at its gzip size. It excludes dynamic imports, worker output, and wasm from this base graph. The on-minus-off delta must be less than 5,120 gzip bytes.

The engine wasm stays separate from that base graph. The receipt records its raw SHA-256 and gzip size. It also records the source SHA, named fixture SHA-256, each manifest SHA-256, each initial artifact SHA-256, raw timing samples, p50, p95, browser version, OS, CPU, both frozen budgets, and the combined result.

## What the receipt proves

A passing receipt binds one exact source checkout and fixture to two facts on the named machine: the flag-gated initial bundle delta meets the frozen size budget, and 10 clean Chromium contexts load the named DXF through the real F-3 surface and real engine within the frozen p95 budget. It does not measure dynamic features, network deployment latency, other browsers, or other hardware.
