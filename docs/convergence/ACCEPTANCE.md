# One-shell convergence — frozen acceptance criteria (W0A)

Ratified 2026-09-01 by the operator (plan: the perma-workspace convergence;
sol-critic run 20260901-133318-1036fa1b, verdict PROCEED-WITH-CHANGES,
integrated). This file is the durable copy of the contracts every convergence
slice is accepted against. Version-append on change; chat and plan files never
override it.

## The end state

One shell (the stage architecture, `.stage-root`) serves every product
surface. The drawing is the permanent ground on all product-host pages;
rails, panes, cockpit furniture, and console content float over it. `/try`
and `/app` become MODES of that shell, not shells. The old `.app` grid shell
is deleted at the end (W7), only after contract receipts, not before.

## Wave order (sol-resequenced; supersedes any earlier ordering)

- W0A freeze truth (this file, matrices, baselines) — DONE when merged.
- W0B bounded shell-independent polish; staging by default, production only
  when a slice carries an independently valuable user outcome.
- W1 engine + identity foundations: `DrawingIdentityProvider` and ONE named
  engine-session boundary land BEFORE any controller or Viewer deletion.
- W2 controller convergence while both shells stand.
- W3 shared shell behind the rollout control: mount the SHARED Viewer first,
  prove every row of the route matrix below plus rollback, and only then
  remove App's Viewer.
- W4 overlay rails + basic cockpit furniture.
- W5 showcases in risk order: usage → version chain → agent trace →
  receipts → editable properties.
- W6 production flip on contract receipts (see "Flip criteria").
- W7 old-shell deletion + token cleanup (only irreversible wave).
- Split viewports: SEPARATE initiative, not part of convergence.

## Review policy (operator-ratified 2026-09-01)

Adversarial multi-agent panel ONLY on risk-bearing slices: engine ownership,
checkout/single-writer, tenant/project scope, auth/boot routing, the
production flip, and any slice touching redaction. Mechanical slices
(locators, docs, token value moves, extractions with pins repointed) get
changed-path tests + focused review.

## Route and boot-state matrix (source of truth: authBoot.js, routeScene.js, SiteRoot.jsx)

Every row must behave identically before and after each wave, and under the
rollout control OFF after every wave until W7. "Console" = today's /app
surface; "stage" = today's /try surface.

| Input | Today | Converged |
|---|---|---|
| `/` on APP_ONLY_HOSTS (platform, platform-staging, vercel) | scene `site` redirects to www.leafautomation.ai | unchanged — the marketing origin is OUT OF SCOPE (front-door contract) |
| `/` elsewhere (localhost, previews) | LandingCast | unchanged |
| `/try` | stage | studio, mode `operator` |
| `/app`, `/app/*`, `/ty`, `/ty/*` | console | studio, mode `console` |
| `/sheets`, `/sheets/*` | sheets page (redirects off app-only hosts) | unchanged |
| unknown path | scene `site` | unchanged |
| `?fixture=` / `?dev=` / `?drawing=` on ANY path | boots console regardless of path | boots studio mode `console`; `?drawing=` also seeds DrawingIdentityProvider |
| `?demo=` off `/try` | boots console | boots mode `console`; on `/try` stays operator mode. NOTE: `?demo` ALSO selects the stage's initial drawing id today (SiteRoot) — converged, one reading must serve both consumers; add a test the first time this wiring is touched |
| `?ops=` off `/try` | boots console (no-op surface in mock) | same, mode `console` |
| Auth0 callback (`?code=&state=`) on any path except `/try` | DEFERS all /api traffic until the code exchange stores leaf.jwt, then boots console | identical deferral, then mode `console`. The redirect_uri is the bare ORIGIN — the callback lands on `/`; never regress the deferral |
| Reload during checkout handoff | Web-Lock-gated 30s handoff preserves the writer id | identical; covered by check:checkout-identity + ownership specs |
| Esc at top level, mode `console`, on APP_ONLY_HOSTS | (new hazard) | must NEVER navigate('/') — that redirects to the marketing origin and discards work. Esc ladder ends inside the studio |

## Flag and rollout matrix

| Flag | Kind | Contract |
|---|---|---|
| `VITE_CAD_EDIT` | structural bundle fence + license fence | literal `import.meta.env.VITE_CAD_EDIT === '1'`, first `&&` operand at call sites; bundleFence test builds twice and greps emitted JS; the engine worker path is named ONLY in cadedit/CadEditSurface.jsx (license fence doc + script stay in step) |
| `VITE_LIFECYCLE_UI` | structural bundle fence | same literal shape; projects/bundleFence test |
| `VITE_IOS_SURFACE` | NOT a bundle fence | deliberately rendered-dormant when off (ios/flag.js) — the envelope requires a visible dormant placeholder; server route 404-refuses while LEAF_IOS_SURFACE_ENABLED is off. Never "fix" it into a fence |
| One-shell rollout | RUNTIME server rail (decision R-1, ratified) | name: `LEAF_ONE_SHELL_ENABLED`, precedent LEAF_IOS_SURFACE_ENABLED. One shared web image serves staging AND production, so a build-time flag cannot flip staging first. Any build-arg change touches FOUR files: both env blocks in build-platform-images.yml, deploy/Dockerfile.web ARG, check_web_container_config.mjs. The rail needs its own build-twice bundle-shape test before W3 (a literal first operand alone proves nothing about the emitted bundle) and a ROLLBACK test: rail off restores the old shell with no stale storage, URL state, or provider duplication |

All three VITE flags are `=1` in every deployed artifact. A fence is a
licensing/reachability control, not a feature-hide rail; incomplete UI ships
behind the runtime rail or does not ship.

## Engine-session ownership (binding for W1+)

ONE engine session owner. `CadEditSurface` (or its W1 extraction, the
engine-session store) owns: EngineBoundary construction, worker lifetime,
document load/dispose, entity state, selection identity, edit dispatch, save
truth (re-parse of written bytes), undo/redo interaction with versions, and
recovery from worker crash or WebGL loss. The PropertiesDock and every other
cockpit surface CONSUME that store; none of them may construct a second
EngineBoundary or name the worker path (license fence). Engine-truth
readouts (entity/byte counts from re-parse) render ONLY for documents that
actually passed through the engine — a server-loaded intake never touched
the WASM kernel and gets no fabricated readout. Defined states required
before any dock work: selection identity across edits, optimistic vs
reparsed geometry, save completion, version reseating, drawing switch
mid-edit.

## Scope-reset contract (binding for every persistent surface)

Any surface that persists across interactions (agent trace tab, receipts,
version history, checkout state, document identity) MUST reset on tenant
switch and on project switch, with a test per surface asserting no stale
data survives the switch. Added the first time each surface becomes
persistent, not later.

## Fetch-budget contract

Startup-critical fetches go through fetchWithBudget. Known violation to fix
before the ship-ladder showcase: ios/iosSurfaceStatus.js calls the supplied
fetch directly.

## Bundle accounting

check_bundle_budget.mjs gates the DEPLOYED flag combination (all VITE flags
=1) against its committed baseline; the off-combinations are structurally
covered by the two bundleFence build-twice tests. If a new deployable flag
combination appears (the one-shell rail does not change VITE flags), it gets
its own baseline entry. Known measurement noise: ~1.2-1.5 KB gz between
identical builds — allowances account for it; never tighten an allowance
below observed noise.

## Reduced-motion acceptance (per new animated surface)

Not just "duration 0": the reduced run must preserve information (state
distinguishable without motion), keep focus (no focus loss on suppressed
transitions), and keep inert/aria-hidden semantics identical to the
unreduced run.

## Flip criteria (W6)

Production flips on receipts, not cadences: every route-matrix row exercised
on staging with the rail ON; auth-callback deep link lands signed in;
checkout take/release/handoff proven; upload reseats the drawing identity;
rail readback confirmed in the deployed task definition; documented one-step
fallback (rail off) verified once on staging. Screenshots are evidence of
LOOK, never of routing or ownership.

## Deletion criteria (W7)

The old shell is deleted only when: two clean staging cadences with the rail
ON in production; every source-string pin that referenced App.jsx or old
selectors has been rewritten as a behavior-based check (a deleted file must
not leave a permanently-named implementation contract); bundle receipt at or
below the W0 baseline; the rail and its flag module removed in the same PR.

---

## Version 2 — 2026-09-01 (W3 mount PR): two contract resolutions, appended

1. **The rail's "build-twice bundle-shape test" (flag matrix, one-shell row)
   resolves to a single-build shape receipt.** That sentence predates the
   R-1 rail decision: the ratified rail is RUNTIME, so both shells ship in
   ONE bundle and no build input varies. The receipt carrying the same
   intent is `web/src/site/oneShellBundleShape.test.js`: one real vite
   build, then proof on the emitted JS that the studio shell ships, the old
   shell ships beside it, the `__LEAF_FLAGS`/`oneShell` runtime read
   survives un-folded, and no `VITE_ONE_SHELL` ever appears. The rollback
   test the same row requires is `web/e2e/local/one-shell-mount.spec.mjs`
   (on → off restores the old shell; storage/URL/provider assertions).

2. **W3's "mount the SHARED Viewer" lands as the portal-ground.** The
   shared component (`web/src/components/Viewer.jsx`) is already the only
   canvas both shells use; what W3 moves is WHERE the console's instance
   renders: under the rail, SiteRoot's studio shell provides the z0 ground
   node and App portals its own element into it (`site/studioGround.js`).
   The console keeps full ownership of the drawing dataflow — props, ref,
   version seat/undo/redo, pendingEdit — in both rail states, so the route
   matrix and rollback are provable without a dataflow migration.
   "Remove App's Viewer" (the W3 tail) remains: it means retiring the
   console's INLINE render path and migrating drawing state onto the shared
   controller, only after these receipts hold. The studio shell is
   deliberately NOT `.stage-root` yet: landing.css re-pins the full dark
   token set on that class; adoption happens with W4's console-mode token
   re-pin, gated by check_token_repin key parity.

The `?demo` dual-read row's "add a test the first time this wiring is
touched" clause is satisfied by the mount spec's rail-ON rows (`?demo=1`
boots console mode off `/try`, stays operator on `/try`).
