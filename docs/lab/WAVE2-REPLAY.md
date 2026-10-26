# Wave 2 canvas performance replay

Run from the repository root with Node 20 or later, installed `web` dependencies
and Playwright Chromium. Port 5195 must be free. No server stack, credentials,
cookies or external data are needed. The harness uses embedded registry defaults
and deterministic compatible pins: exactly 500 blocks and 1000 acyclic pipes,
with each input fed at most once. Keep measured receipts and runner logs outside
git. Use the Mac mini for the hardware-dependent release proof.

```powershell
$env:LAB_PERF_SHA = git rev-parse HEAD
Push-Location web
npx playwright test e2e/lab-canvas-perf.spec.mjs --config playwright.lab-perf.config.mjs --reporter=line
$a = $LASTEXITCODE
Pop-Location
exit $a
```

For the Mac mini, the shared tooling provides `scripts/mini_e2e.py` outside this
repository. Its installed Windows invocation from the repository root is:

```powershell
python ~/.claude/scripts/mini_e2e.py run --worktree <worktree> --config playwright.lab-perf.config.mjs --host mac-mini --log-dir <dir>
```

The config runs `npx vite build --config vite.lab-perf.config.mjs` followed by
`npx vite preview --config vite.lab-perf.config.mjs --port 5195 --strictPort`
from `web/`. It opens `/e2e/lab-perf/harness.html` in one Chromium worker at
1920 by 1080, without video or trace. The app build, its output and bundle
baseline are unchanged. Do not replace this with the Vite development server:
the proof measures the production React build without StrictMode replay.

The browser waits for the 500 real canvas node cards, records mount time, waits
for initial dependency layout, pans for at least two seconds while sampling
requestAnimationFrame intervals, and clicks Relayout. It checks completion and
button readiness, p50/p99 pipe marks in both budget states, then selects a real
node and opens Setup, Configure, Test and Code. Rendered edge counts are recorded
separately from the fixture's 1000 pipes; offscreen edge virtualization must not
be confused with missing document pipes. Mount measures rendering before the
asynchronous initial layout completes; relayout duration is a separate metric.

## Operator receipt

The test writes `web/test-results/lab-canvas-perf-receipt.json` and prints the
same object as `LAB_PERF_RECEIPT=<compact JSON>`. It records numbers before timing
assertions, including failed budgets. Functional failures also produce a receipt
with `pass: false`, a failure description and nulls for unavailable measurements.
For a routed run, the receipt is the `LAB_PERF_RECEIPT=` line in the run stdout
under the selected log directory; snapshot files do not return.
A hard process kill or browser timeout at the overall test deadline may prevent
receipt writing; a missing receipt never counts as a pass.

Expected fields:

- `schema`: `leaf.lab-canvas-perf.v1`; `sha`: `LAB_PERF_SHA`, otherwise repository
  HEAD from `git rev-parse HEAD` (five-second timeout), or `unknown` on error.
- `blocks`, `pipes`: 500 and 1000; `renderedNodes`, `renderedEdges`: actual DOM
  counts from the canvas, not synthetic fixture labels.
- `mountMs`, `panFrames`, `panP50Ms`, `panP95Ms`, `relayoutMs`.
- `budgets`: `{mountMs: 500, panP50Ms: 16.7, panP95Ms: 33}`; `pass`: boolean.
- `hardware`: OS `platform` and `release`, `cpuModel`, `cpuCount`,
  `totalMemoryGB` (GiB), `browserName`, `browserVersion`, browser
  `hardwareConcurrency`, `deviceMemory` (null if unavailable) and `userAgent`.

Frame percentiles use nearest rank and are recorded to three decimal places in
milliseconds. Preserve the full receipt line from a routed run: files created in
the remote snapshot may not return. Record the runner job, source snapshot SHA,
run time and full log alongside it in the program receipt folder. A routed
receipt with `sha: unknown` needs the runner's source SHA binding; never replace
it with an unverified branch tip. Record release-candidate results alongside the
reference measurement in OPERATOR.md.

Reference measurement: Mac mini, mini_e2e job `147a73d9`, 2026-10-26; Apple M4
Pro, 12 cores, 48 GB, macOS (darwin 27.0.0), headless Chromium 148.0.7778.96.
The planner's first measured run passed: mount 134.9 ms, pan p50 12.8 ms,
pan p95 27 ms, relayout 2429.6 ms, 500 nodes and 1000 edges rendered,
154 pan frames. A release candidate still needs its own SHA-bound passing receipt.

## Pass and current gaps

A pass requires Playwright exit 0, `pass: true`, 500 rendered nodes, rendered
edges, successful relayout, both pipe budget states and all four tabs, plus mount
<= 500 ms, pan p50 <= 16.7 ms and pan p95 <= 33 ms. Preserve failed runs too; do
not weaken budgets or raise the graph cap to obtain a passing result. The
reference run above records measured hardware; it does not replace the
release-candidate proof.

The admitted graph cap remains 200 blocks / 400 pipes. The harness does not call
the contract validator or admission API; the real canvas's internal validation
still reports that this rendering fixture exceeds admission limits. A later cap
change needs a passing receipt on the release candidate. This hardware-dependent
config is not registered with `scripts/run-all-gates.py` or added to CI.
