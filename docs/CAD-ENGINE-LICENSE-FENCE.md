# CAD engine license fence (card C1-1)

CI job `license-fence` (workflow `.github/workflows/license-fence.yml`, script
`scripts/check_license_fence.py`) blocks two licensing failure modes from
landing in the web bundle: copied OpenCADStudio (GPL) source, and a
MPL-2.0 acadrust import that escapes its isolated worker boundary. This doc
and the script must stay in step — a reviewer diffs them against each other.

## Scope

Scans exactly two roots, relative to the repo root: `web/` (the web bundle's
inputs and, after `npm run build` runs first in the workflow, its built
output `web/dist/`) and `vendor/` (vendored sources). Nothing else in the
repo is in scope for this fence. In particular `server/dwg_convert.py` and
`deploy/Dockerfile.app` carry a documented, load-bearing GPL-3.0+ dependency
(libredwg, consumed strictly across a subprocess boundary and never shipped
to end users) that is server-side, not web-bundle — deliberately out of
scope, not an oversight.

Within scope, every file is read as bytes (not decoded/skipped as "binary"),
so a stripped-header, minified, or `.wasm` blob is scanned the same as
source. Directories named `.git`, `node_modules`, or `__pycache__` are
pruned at any depth; `dist`/`build` are **not** pruned, because a built
bundle is exactly where a license header comment gets stripped by
minification.

Exactly 3 paths are excluded from the scan (`SELF_EXCLUDED_PATHS` in the
script) because they are the fence's own tooling/doc and necessarily spell
out these identifiers in prose or as pattern data:

- `scripts/check_license_fence.py`
- `.github/workflows/license-fence.yml`
- `docs/CAD-ENGINE-LICENSE-FENCE.md`

This set must stay exactly these 3 paths. Widening it to cover a real
source or vendor directory would be the standing evasion hole this fence
exists to prevent — the script's own
`test_self_exclusion_is_exactly_the_three_fence_files` self-test locks it.

## Deny rule 1: OpenCADStudio identifiers/paths

Pattern (case-insensitive, `-`/`_`/space-tolerant between tokens):
`open[-_ ]{0,2}cad[-_ ]{0,2}studio` — matches `OpenCADStudio`,
`opencadstudio`, `Open-CAD-Studio`, `open_cad_studio`, etc.

Applied two ways:

- **Path**: if any component of a scanned file's path matches, it is always
  a violation (no exemption) — a renamed file/directory can't launder a
  copied identifier out of scope.
- **Content**: a match is a violation unless it is a standalone English word
  — both the byte immediately before and immediately after it are
  whitespace or ordinary sentence punctuation (`.,;:!?()`). Anything else
  adjacent (a letter, digit, `_`, `-`, `/`, `\`, a quote) means it is glued
  to an identifier, an import specifier, or a path, which is exactly what
  this rule denies. This exemption is real and load-bearing: it is what
  keeps the fence GREEN on this repo's own
  `web/src/cad/EditSurface.jsx`/`editSurface.test.jsx` (card C1-6), which
  legitimately mention the bare product name in a comment/test-description
  disclaiming that no OpenCADStudio source is used. It does not exempt a
  quoted string, an import specifier, or a compound identifier — see
  `test_opencadstudio_identifier_glued_to_other_tokens_is_a_violation` and
  `test_opencadstudio_bare_prose_mention_is_not_a_violation`.

## Deny rule 2: GPL-3.0 license header

Two patterns, both required because either alone loses one direction:

- SPDX-style: `GPL-3.0`, `GPL-3.0-only`, `GPL-3.0-or-later`, matched with a
  negative lookbehind for a preceding letter so `LGPL-3.0`/`AGPL-3.0` never
  match, and never matches `GPL-2.0`.
- Full boilerplate: the phrase `GNU GENERAL PUBLIC LICENSE` followed within
  300 bytes by `Version 3` — catches the un-stripped header text; the SPDX
  pattern above catches the case where minification stripped everything but
  a one-line SPDX comment.

## Deny rule 3: acadrust outside its isolated-worker prefix

`ALLOWED_ACADRUST_PREFIX = "vendor/acadrust-worker/"` (trailing slash is
load-bearing: without it, a lookalike directory like
`vendor/acadrust-worker-staging/` would also pass a naive `startswith`
check). Inside that prefix, any reference to `acadrust` is expected and
always allowed — that is where the MPL-2.0 engine source lives.

Outside that prefix, a reference to `acadrust` is a violation **unless** it
is inside the string-literal argument of the one legal shape this codebase
loads a worker with:

```js
new Worker(new URL('<...acadrust...>', import.meta.url))
```

(matching the pattern already established by `web/src/cad/engineWorker.js`
for its own worker instantiation). There is deliberately no fixed
"entry-point file" list to check this against — every file outside the
prefix is checked independently, so a new entry point added later, or a
wrapper module that re-exports a legal spawn from elsewhere in the tree, is
still covered without this doc or the script needing to enumerate entry
points by name.

### Where the one legal spawn lives, after the W1 engine-session extraction

Convergence W1 (docs/convergence/ACCEPTANCE.md, "Engine-session ownership")
moved the engine SESSION — `EngineBoundary` construction, worker lifetime,
document bytes, entity state, selection, edit dispatch, the save flow — out of
`web/src/cadedit/CadEditSurface.jsx` and into
`web/src/cadedit/engineSession.js`. The worker PATH deliberately did **not**
move with it.

`engineSession.js` takes its worker factory as a REQUIRED injected argument
and never names the path, so the fence-legal spawn shape stays at exactly one
site — `CadEditSurface.jsx` at W1, `EngineSessionProvider.jsx` since W4d (next
section) — and this extraction creates no second legal site to bless. That is why **`scripts/check_license_fence.py` is unchanged by W1**:
its rules are shape-based and enumerate no entry points (deny rule 3 above),
so there is nothing in them that the extraction could invalidate. This
paragraph is the record of that deliberate no-op, so a reviewer diffing doc
against script sees the decision rather than an omission.

### Where the one legal spawn lives, after the W4d provider (Slice A)

W4d moved the ONE `useEngineSession` call out of `CadEditSurface.jsx` into
`web/src/cadedit/EngineSessionProvider.jsx`, because the ribbon's Modify
group needs the same session and a second call of the store is a second
worker. The one legal spawn shape moved WITH that call: the provider is now
the only non-test module under `web/src` that names the worker path, and
`CadEditSurface.jsx` (like the ribbon's `EngineRibbonClusters.jsx`) is a
pure consumer that names no path and constructs no boundary.

`scripts/check_license_fence.py` is again unchanged: its rules are
shape-based and enumerate no entry points, so a spawn that moves from one
module to another is still exactly one legal spawn to it. The count itself
is held by `web/src/cadedit/engineOwnership.test.js`, which detects the
spawn shape and the boundary construction BY SHAPE (it spells no engine
identifier, so the move cannot make it stale). This paragraph records the
move so a reviewer diffing doc against script sees the decision rather than
a stale site name.

Two boundaries, kept distinct on purpose:

- **Engine MESSAGES** cross `EngineBoundary` only. It is unmodified, and it
  remains the sole schema-validating channel in both directions.
- **Worker LIFETIME** is the session store's, by the ownership contract, and
  lifetime includes death: the store attaches an `error` / `messageerror`
  death watch to the worker handle it constructs, so a crashed worker becomes
  a recoverable state instead of a silent hang. No engine payload is read
  there — a Worker-API lifecycle event carries none.

Companion control, NOT part of this script: `web/src/cadedit/engineOwnership.test.js`
asserts that exactly one non-test module under `web/src` carries the legal
spawn, which is what makes "ONE engine session owner" enforceable rather than
merely documented. It detects by SHAPE (a legal spawn whose URL literal leaves
`web/` for the repo's vendored sources) and names neither the engine nor the
owning file — so it needs no exclusion here, cannot become the standing
evasion hole the "exactly 3 paths" rule guards, and does not rot on a rename.

## Self-tests (negative + positive controls)

`python scripts/check_license_fence.py --self-test` runs an embedded
`unittest` suite. Every fixture is built under a `tempfile.TemporaryDirectory`
at test-run time and is never committed to the tree — a fixture committed
under a scanned path would either red every clean PR forever, or (if
carved out via an exclusion) become exactly the evasion hole this doc's
"exactly 3 paths" rule above guards against. Coverage includes, per side:

- A seeded violation under `web/...` (bundle inputs) goes RED, and a
  separate one under `vendor/...` (vendored sources) goes RED — both scan
  roots are proven RED-capable independently.
- A clean tree goes GREEN (positive control — a fence that always exits
  non-zero would still pass a RED-only test suite).
- acadrust inside the prefix, in any shape, is GREEN.
- acadrust outside the prefix via the legal `new Worker(new URL(...,
  import.meta.url))` spawn is GREEN; the same reference via a plain
  `import`/`require` is RED — both directions of the allow rule are
  exercised, not just one.
- The trailing-slash prefix boundary (`vendor/acadrust-worker-staging/`) is
  treated as outside the real prefix.
- `LGPL-3.0`/`AGPL-3.0`/`GPL-2.0-only` never match; a bare
  `SPDX-License-Identifier: GPL-3.0-or-later` comment (simulating a
  stripped/minified header) does match.
- A binary `.wasm`-shaped blob with an embedded identifier is scanned, not
  skipped.
- `.git/` content is excluded; an untracked sibling file with the same
  content is still scanned (proves the walk isn't relying on `git grep`,
  which would silently skip untracked/ignored vendored dirs).
- Both exit-code directions are asserted explicitly (`scan_tree()` returns
  `[]` on a clean tree, non-empty on a violating one) rather than trusting
  any shell exit-code convention.

The workflow runs `--self-test` as its own step, before the real scan, on
every PR — so a change to the script that breaks any of the above fails the
job before the scan step even runs.

## CI wiring

Workflow: `.github/workflows/license-fence.yml`, job id and `name:` both
`license-fence`, triggered on `pull_request` (no `paths:`/`paths-ignore:`
filter — a vendored-only or docs-only PR still runs the fence) using the
default `pull_request` checkout (the PR's own merge ref, not
`pull_request_target`'s base-ref checkout), with `submodules: recursive` so
a vendored dependency pulled in as a git submodule is real files on disk,
not a single gitlink line. No `actions/cache` (a stale cached vendor tree
can never be served back unscanned) and no `concurrency:`/
`cancel-in-progress` (a cancelled run must never leave this check pending on
a PR).

**Required-check wiring is documented here, not assumed done.** The exact
status-check context to require in branch protection is the job name,
**`license-fence`**. A human with repo admin access must confirm/enable it:

```
gh api repos/LEAF-Solar-Design/leaf-web-demo/branches/main/protection \
  --jq '.required_status_checks'
```

Note this repo's existing `test-gate.yml` records, in its own header
comment, that branch protection is unavailable on this repo's current
GitHub plan — which is exactly why that workflow's `run-all-gates` job is
instead consumed via a workflow-level `needs:` dependency from
`build-platform-images.yml`, not branch protection. `license-fence` has no
such downstream consumer today: until either branch protection becomes
available on this plan, or another workflow adds a `needs: license-fence`
dependency, a failing fence blocks the check going green but does not, by
itself, block the merge button. That gap is the reason this line of the
card's acceptance oracle exists — closing it is a repo-admin/workflow-wiring
action, not a file this fence's script or workflow can perform on its own.
