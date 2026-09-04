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
