# CAD engine license review: acadrust (MPL-2.0)

Review performed 2026-08-24 by the root completion session under explicit operator delegation (EXECUTION-PLAN completion program; the delegating mandate is recorded verbatim in the program plan doc). This artifact satisfies the "license review" precondition of `envelopes/cad_edit.yaml` in the leaf-plan repo.

## Component under review

- **acadrust**, upstream https://github.com/hakanaktt/acadrust, license **MPL-2.0**.
- Consumed rev-pinned at `18500466e7e4392ef830fdc59cede75fa3794f2b`, **unmodified**: zero local patches to crate source. Every adaptation lives in this repo's own wrapper (`vendor/acadrust-worker/src/lib.rs` and the JS worker files), which is Leaf Automation code.
- Distribution form: compiled to a WebAssembly artifact, loaded exclusively as an isolated Web Worker through the fence's one allowed spawn shape; never imported into the proprietary web bundle.

## MPL-2.0 obligations as they apply here

1. **File-level copyleft.** MPL-2.0 (sec. 1.7, 3.3) permits combining Covered Software with proprietary code in a Larger Work provided the Covered Software's own files stay under MPL. We neither relicense nor modify any covered file.
2. **Source availability (sec. 3.2).** Distributing Executable Form (the `.wasm`) obliges us to inform recipients how to obtain the Covered Software's source. Satisfied by the public upstream repository plus the exact pinned rev; the NOTICE below must ship with, or be reachable from, the product.
3. **Notices (sec. 3.4).** Upstream license notices must not be removed. No crate source is vendored into the bundle: cargo fetches the pinned rev at build time with notices intact.
4. **Modification duty (sec. 3.2 / 1.10).** If Leaf Automation ever patches crate files locally, those files' source must be published under MPL-2.0. **Tripwire:** any change of `vendor/acadrust-worker/Cargo.toml` from the bare `rev =` git pin to a `path`/vendored dependency re-opens this review.

## Technical controls in force

- **License fence CI** (`scripts/check_license_fence.py` + `.github/workflows/license-fence.yml`): `acadrust` may be referenced only under `ALLOWED_ACADRUST_PREFIX = "vendor/acadrust-worker/"`, and from outside that prefix only through the single legal `new Worker(new URL(...))` spawn shape. Self-test: **17/17**. Live scan: clean as of this review.
- This review lands together with the move of the crate directory **into** the fence's scan scope. It previously lived at `engine/acadrust-worker/`, which `SCAN_ROOTS = ("web", "vendor")` never scanned at all — the OQ-4 gap, closed by that move. Worth stating plainly: for the period the crate sat in `engine/`, the fence was reporting clean over a tree that did not contain the crate, so its green was not evidence about acadrust.
- **Known residual gap, accepted.** On the current GitHub plan the fence check is visible in CI but is not a branch-protection-required context, so a failing fence blocks the check going green without blocking the merge button. Compensating controls: fence visibility on every PR touching these paths, plus the tripwire clause above.

## Unscanned-window release audit (OQ-4)

Audit run by the executor on 2026-09-26 in the task worktree. The window began at `80ca662b` (2026-08-19), when the crate was added at `engine/acadrust-worker/`, and ended at `29051a82` (2026-08-24), when it moved to `vendor/acadrust-worker/`, inside `SCAN_ROOTS = ("web", "vendor")`.

The initial audit recorded the three command reruns below; checks 4 and 5 add the planner's source-checked image-packaging and release-build evidence at `29051a82^`:

1. Path-reference history:

   ```text
   git log --format="%h %ad %s" --date=short -S engine/acadrust-worker -- deploy web/package.json web/vite.config.js web/src .github
   ```

   Returned exactly `29051a82` (2026-08-24), `634e4857` (2026-08-23), and `80ca662b` (2026-08-19). Rerunning with `--name-only` matched only `web/src/cad/engineWasmHarness.test.js` and `web/src/cad/engineWasmHarness.realwasm.test.js`: both at `29051a82`, only the real-WASM test at `634e4857`, and only the other test at `80ca662b`. Both are test files that the production bundle never imports.

2. References immediately before the move:

   ```text
   git grep -n acadrust 29051a82^ -- deploy .github web/package.json web/vite.config.js web/src
   ```

   Hit only `web/src/cad/engineWasmHarness.test.js:48`, `web/src/cad/engineWasmHarness.realwasm.test.js:40`, and the header comment at `.github/workflows/license-fence.yml:5-6`. The test references use `new URL('../../../engine/acadrust-worker/worker-entry.mjs', import.meta.url)`; the workflow references are comments, not crate consumers.

3. Build, workflow, and deploy changes during the window:

   ```text
   git log --format="%h %ad %s" --date=short 80ca662b^..29051a82 -- deploy .github web/package.json web/vite.config.js
   ```

   Listed exactly `00390c1a` (2026-08-21, Bake lifecycle flag into web container), `78b9e904` (2026-08-20, bake `VITE_LIFECYCLE_UI=1` into both web artifact builds), `64831460` (2026-08-19, CAD engine license fence), and `d7846444` (2026-08-19, contract check producer). Inspection of their changes found no build, workflow, or deploy reference that consumed the crate; the license-fence header comment is the non-executable mention recorded above.

4. Image packaging:

   ```text
   git show 29051a82^:deploy/Dockerfile.app
   git show 29051a82^:deploy/Dockerfile.broker
   git show 29051a82^:.github/workflows/build-platform-images.yml
   git show 29051a82^:.dockerignore
   git ls-tree -r --name-only 29051a82^ engine/acadrust-worker/
   ```

   `deploy/Dockerfile.app:92` contains `COPY engine/    /app/engine/`, and `deploy/Dockerfile.broker:83` contains `COPY engine/  /app/engine/`, both present since `f34908c2` (2026-07-18). The workflow uses the repository root as its build context (`context: .`). At `29051a82^`, the `.dockerignore` entries for `engine/` exclude only `engine/appbundle-write/bin`, `engine/appbundle-write/obj`, and `engine/appbundle-write/build`, not `engine/acadrust-worker/`. Thus images built from main during the window carried that directory as files.

   The `ls-tree` command lists exactly six Leaf Automation-authored files under `engine/acadrust-worker/`: `.gitignore`, `Cargo.toml`, `bindings.mjs`, `fixtures/one_line.dxf`, `src/lib.rs`, and `worker-entry.mjs`. None contains acadrust source. `Cargo.toml` declares only the git dependency `acadrust = { git = "https://github.com/hakanaktt/acadrust.git", rev = "18500466e7e4392ef830fdc59cede75fa3794f2b" }`, and the directory's `.gitignore` excludes `target/` and `Cargo.lock`. Neither Dockerfile contains `cargo`, `rustc`, or `wasm-pack`, so neither image fetched or compiled acadrust. At `29051a82^`, nothing in `server/`, `da/`, `platform/`, `harness/`, or `deploy/` imports the directory; `git grep -n acadrust 29051a82^ -- server da platform harness deploy` hits only a test asserting that the word is absent.

   The local-build path differs: `docs/ACADRUST-SPIKE-DAY3.md:155-164` records `wasm-pack build --release --target web --out-dir pkg`, which wrote `engine/acadrust-worker/pkg/acadrust_worker_bg.wasm` (2,004,823 bytes), a compiled acadrust binary. The `target/` and `Cargo.lock` lines in `engine/acadrust-worker/.gitignore` do not cover `pkg/`; no `pkg/` file appears in `git ls-tree` at the window commits, but that output existed on a machine that ran the day-3 build. Docker does not read `.gitignore`. The `.dockerignore` entries `engine/appbundle-write/bin/`, `engine/appbundle-write/obj/`, and `engine/appbundle-write/build/` exclude neither `engine/acadrust-worker/pkg/`, `engine/acadrust-worker/pkg-node/`, nor `engine/acadrust-worker/target/`. `deploy/README.md:58` documents a local `docker compose build`. Because both Dockerfiles copy `engine/` wholesale, an image built locally after that wasm build would have carried compiled acadrust.

5. Release build provenance:

   At `29051a82^`, `.github/workflows/build-platform-images.yml` uses `actions/checkout@v4` in every job (lines 197, 683, 749, 942, 1291, 1614, 1743, 1859, 2106, 2300, and 2741), builds with `context: .` (lines 862, 1401, and 1933), and contains no `cargo`, `rustc`, `wasm-pack`, or wasm build step. A fresh checkout holds only tracked files, so a workflow-built image carried only the six tracked Leaf Automation-authored files under `engine/acadrust-worker/` listed above, none of them acadrust source or a compiled acadrust binary.

Images built by the build-platform-images workflow from a clean checkout during the window carried no acadrust code; an image built locally with `docker compose` after the day-3 wasm build could have carried the compiled acadrust wasm from `engine/acadrust-worker/pkg/`, and this audit does not establish that no such image was deployed; OQ-4 is therefore closed for workflow-built releases, with the local-build residual named.

Recommendation: exclude `engine/acadrust-worker/pkg/`, `engine/acadrust-worker/pkg-node/`, and `engine/acadrust-worker/target/` in `.dockerignore` so a local build can no longer carry compiled output (a follow-up change, not made here).

## Verdict

**ACCEPTABLE.** MPL-2.0, unmodified and rev-pinned, isolated behind the worker boundary with source availability satisfied upstream, is compatible with Leaf Automation's proprietary distribution model.

Conditions binding on future work:

1. The NOTICE line below ships in the product's third-party attributions surface before any `cad_edit` general-availability release.
2. The license fence stays green on every change under `web/` and `vendor/`.
3. Local modification of any crate file re-opens this review (see the tripwire).

## Scope limit of this review

This is a review of **license compatibility for the current consumption shape**, performed by an engineering agent under operator delegation. It is not a legal opinion and does not bind any third party. It deliberately does not opine on trademark, patent-grant interactions with any future Leaf Automation patent position, or the terms under which Leaf Automation's own product is licensed to tenants.

## NOTICE line (for the attributions surface)

> This product includes acadrust (https://github.com/hakanaktt/acadrust), licensed under the Mozilla Public License 2.0. Source for the exact revision used is available at the upstream repository.
