# Solar document lifecycle replay

Parent: Studio18 Solar layout production hardening. The real-engine suite in
`web/src/cad/solarLifecycleReplay.test.jsx` protects the editor session used
after a generated layout becomes a saved drawing.

The suite builds two synthetic frame outlines in code. It uses the actual
React session and isolated worker with the compiled Node WASM package. It
does not need a service, browser, saved project, credentials or customer data.

## Replay

Install the repository's locked web dependencies. Prepare the worker's Node
package using the same Rust source revision as the checkout. For a local
development build from the repository root:

```powershell
$env:RUSTFLAGS = '--cfg getrandom_backend="wasm_js"'
wasm-pack build vendor/acadrust-worker --target nodejs --dev --no-opt --out-name engine --no-typescript --out-dir pkg-node -- -j 1
Push-Location web
npx vitest run src/cad/solarLifecycleReplay.test.jsx
Pop-Location
```

The Rust toolchain needs the `wasm32-unknown-unknown` target. Generated package
files are build artifacts; do not commit them. An already built package is
usable only when its source matches the worker crate in this checkout.

Expected executed result: four passing tests. When the compiled package is
absent, the suite is explicitly skipped, following the existing real-engine
suite convention. A skipped result does not establish real-engine proof.

## Protected behavior

- Two edits followed by Undo and a divergent edit clear Redo. Unrelated
  geometry remains unchanged, and final Undo posts the original DXF bytes.
- A delayed reply for an old document cannot overwrite a newer document's
  geometry or history, including after a new edit.
- Worker `error` and `messageerror` events during an edit clear pending state.
  A fresh open creates another worker, ignores an old queued reply, permits
  editing and restores the new document's bytes on Undo.

The transport starts a Node subprocess per worker turn and reloads its last
written bytes. This tests actual worker results and session state, not
persistent-process timing. Error events and reply order are injected. The
suite does not establish browser rendering, service save admission, staging
source identity, 144-frame performance or scored acceptance. Preserve those
separate LIVE receipts when reusing these regression tests for another lane.
