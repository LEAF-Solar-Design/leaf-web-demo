// @vitest-environment jsdom
//
// SLICE 4a's ZERO-VISUAL-CHANGE PROOF.
//
// The claim: every shell element SurfaceFrame's slots render is byte-identical
// — same tag, same class names, same testids, same depth-first order — to what
// App.jsx (the console) and ToolCast.jsx (the stage) hand-rolled before this
// slice. The frame moved the GATES; it moved no DOM.
//
// How this test avoids grading its own homework:
//
//   1. `todayConsole()` / `todayStage()` below are a LITERAL transcription of
//      the pre-slice-4a mount sites, prop for prop, each cited to the line it
//      came from on origin/main 1a6d6630. They import the same leaf components
//      the scenes import; they are not a paraphrase of SurfaceFrame.
//   2. `surfaceFrame.today-fixture.json` was captured by rendering THOSE
//      transcriptions on the untouched worktree, BEFORE App.jsx or
//      ToolCast.jsx were edited, and committed. It is the frozen artifact.
//   3. Both the transcription AND SurfaceFrame are asserted equal to that
//      fixture. If the transcription drifts, test 1 fails and the fixture is
//      no longer vouched for; if the frame drifts, test 2 fails. Agreement
//      between the two alone can never carry the claim.
//
// What it deliberately does NOT cover, because a jsdom render cannot: the
// PLACEMENT of each slot inside the scene's tree. That is the e2e one-shell
// byte-identity rows' job (web/e2e/local/one-shell-mount.spec.mjs), and it is
// why the slot mounts stayed exactly where the elements already stood.
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import EntitlementGate from '../components/EntitlementGate.jsx'
import JobRail from '../components/JobRail.jsx'
import ProductSurfaceTabs, { ProductSurfaceFrame } from '../components/ProductSurfaceTabs.jsx'
import Toast from '../components/Toast.jsx'

import ContinuityStore from './ContinuityStore.jsx'
import { CONTINUITY_HOST_CLASS, useContinuityPublish } from './continuityStore.js'
import { StatusToggles } from './DrawingCockpit.jsx'
import SurfaceFrame from './SurfaceFrame.jsx'
import { surfaceContract } from './productSurfaces.js'
import { EMPTY_WORKSPACE_PROJECT } from './workspaceProjectState.js'

const FIXTURE_PATH = join(dirname(fileURLToPath(import.meta.url)), 'surfaceFrame.today-fixture.json')
// Regeneration is a deliberate, out-of-band act, never something a failing run
// can do for itself: SURFACE_FRAME_CAPTURE=1 npx vitest run src/site/surfaceFrame.render.test.jsx
// rewrites the fixture from the transcription and asserts nothing. The
// committed fixture was captured that way on the UNTOUCHED worktree at
// origin/main 1a6d6630, before App.jsx or ToolCast.jsx were edited, which is
// what makes it evidence rather than a restatement of the new code.
const CAPTURE = process.env.SURFACE_FRAME_CAPTURE === '1'
const FIXTURE = CAPTURE ? null : JSON.parse(readFileSync(FIXTURE_PATH, 'utf8'))

afterEach(cleanup)

// --- the shared scene data both renderings receive ------------------------
// Fixed, small and deterministic: mock job data (JobRail's live branch arms a
// 4 s settle timer; mock skips it), a two-family catalog so the frame's
// per-surface familyIds filter actually filters, and the honest EMPTY project
// state every surface falls back to.
const CATALOG = Object.freeze({
  source: 'server',
  families: [
    {
      family_id: 'measurement',
      label: 'Measurement',
      description: 'Measure the drawing',
      capabilities: [{ name: 'count_panels', label: 'Count panels' }],
    },
    {
      family_id: 'stringing',
      label: 'Stringing',
      description: 'Solve strings',
      capabilities: [{ name: 'solve_strings' }, { name: 'route_home_runs' }],
    },
  ],
})

const STATES = Object.freeze({
  browser: { state: 'available', label: 'Ready' },
  cad: { state: 'available', label: 'Ready' },
  solar: { state: 'beta', label: 'Beta' },
  ios: { state: 'setup', label: 'Setup required' },
})

const ENTITLEMENTS = Object.freeze({
  tier: 'pro',
  entitlements: { tier: 'pro', entitlements: { run_read: true, run_write: true, build: false, converse: true }, source: 'auth0' },
  loading: false,
  mock: false,
})

const JOBS = Object.freeze({
  mock: true, jobs: [], currentJob: null, inflight: null, reattaching: false, onSelectJob: noop,
})

const TOAST = Object.freeze({ toast: { id: 't1', text: 'Version 4 saved' }, onDone: noop })

// The command bar is a RENDER PROP: the frame's only contract is that it emits
// whatever the scene hands it, untouched, so a sentinel proves the pass-through
// exactly where the real PromptBox / .tc-bar block would prove nothing extra.
const COMMAND_BAR = () => <div className="cb-sentinel" data-testid="command-bar-sentinel">bar</div>
const PROJECT_SLOT = <div className="ios-slot-sentinel" data-testid="ios-project-slot">ship lane</div>

function noop() {}

// Console posture. `studio` is App's useStudioGround() truthiness; the rest are
// App.jsx:2257/2264/2284 postures. wide + collapsed is the shipped default.
const POSTURE = Object.freeze({
  studio: true,
  navExpanded: false,
  onNavExpand: noop,
  wideViewport: true,
  jobRailExpanded: false,
  onJobRailExpand: noop,
  onJobRailCollapse: noop,
})

// --- depth-first element sequence -----------------------------------------
// tag + className + testid, in document order. Text is deliberately excluded:
// this pin is about STRUCTURE and identity hooks, which is what a CSS rule, a
// screenshot row and a Playwright locator all key off.
//
// Slice 4b, the ONE named exclusion: the continuity HOST. SiteRoot's
// ContinuityStore renders the rail and the sign-out into a host <div> the
// nav adopts right after the tablist, and landing.css gives that host
// `display: contents`, so it generates no box: the rail and the button are
// the nav's flex items exactly as when the tabs rendered them inline. The
// walker flattens it (its children are walked in place; the host itself is
// not emitted), which is the same view the browser's box tree has, and the
// fixture captured before 4a stays the oracle. site/continuityHoist.test.jsx
// pins the host's class, its CSS rule, and the byte-identical markup inside.
function sequence(node) {
  const out = []
  const walk = (el) => {
    for (const child of el.children) {
      if (child.classList.contains(CONTINUITY_HOST_CLASS)) { walk(child); continue }
      out.push([
        child.tagName.toLowerCase(),
        child.getAttribute('class') || '',
        child.getAttribute('data-testid') || '',
      ].join('|'))
      walk(child)
    }
  }
  walk(node)
  return out
}

// The scene's publish, transcribed: SurfaceFrame calls useContinuityPublish
// with these five fields (SurfaceFrame.jsx); this stand-in does the same so
// the transcription of today's TABS mount carries the same data through the
// same store path without mounting a frame (which would be testing the
// subject with itself).
function Published({ activeSurface, workspaceProject = null, catalog = null, signedIn = false, onSignOut = null, children }) {
  useContinuityPublish({ activeSurface, workspaceProject, catalog, signedIn, onSignOut })
  return children
}

// Every slot renders inside ONE store, the way SiteRoot mounts every scene
// (slice 4b). The store adds no element to the slot's subtree; the rail and
// the sign-out appear where the tabs adopt the host and nowhere else.
function slotSequence(element) {
  const { container, unmount } = render(<div><ContinuityStore>{element}</ContinuityStore></div>)
  const seq = sequence(container.firstChild)
  unmount()
  return seq
}

// ---------------------------------------------------------------------------
// TODAY, transcribed. Every branch below is the pre-slice-4a source; the
// citation is the line on origin/main 1a6d6630.
// ---------------------------------------------------------------------------

/** App.jsx's console mounts. `studioGround`/`drafting`/`wideViewport` are the
 *  scene locals the gates read; `drafting` is contract.chrome.cockpit
 *  (App.jsx:2309) and `jobSpine` is rails.right === 'job-spine' (App.jsx:2314). */
function todayConsole(activeSurface) {
  const slots = surfaceContract(activeSurface)
  const studioGround = POSTURE.studio
  const drafting = slots.chrome.cockpit
  const jobSpine = slots.rails.right === 'job-spine'
  const { wideViewport, jobRailExpanded } = POSTURE
  return {
    // App.jsx:2868-2873. Slice 4b: the rail's data and the sign-out are
    // PUBLISHED (App passes no signedIn, so no sign-out), and the tabs adopt
    // the store's host; the element sequence is the pre-4b one.
    tabs: (
      <Published activeSurface={activeSurface} workspaceProject={EMPTY_WORKSPACE_PROJECT} catalog={CATALOG}>
        <ProductSurfaceTabs
          activeSurface={activeSurface}
          states={STATES}
          onSelect={noop}
        />
      </Published>
    ),
    // App.jsx:2898-2910
    frame: slots.chrome.productFrame ? (
      <ProductSurfaceFrame
        activeSurface={activeSurface}
        states={STATES}
        catalog={CATALOG}
        catalogError={null}
        workspaceProject={EMPTY_WORKSPACE_PROJECT}
        onCreateProject={noop}
        projectSlot={slots.chrome.projectSlot === 'ios-surface' ? PROJECT_SLOT : null}
      />
    ) : null,
    // App.jsx:3376-3388 — the inline mount, null on a wide drafting surface.
    entitlementInline: studioGround && drafting && wideViewport ? null : (
      <EntitlementGate {...ENTITLEMENTS} />
    ),
    // App.jsx:3263-3268 — the properties dock's Plan section, reached only
    // under studioGround && dockSections && wideViewport (App.jsx:3251).
    entitlementDocked: studioGround && slots.rails.dock && wideViewport ? (
      <EntitlementGate {...ENTITLEMENTS} />
    ) : null,
    // App.jsx:3457 — the PromptBox stands here (sentinel; see COMMAND_BAR).
    commandBar: COMMAND_BAR(),
    // App.jsx:3505-3521
    jobRail: (
      <JobRail
        mock={JOBS.mock}
        jobs={JOBS.jobs}
        currentJob={JOBS.currentJob}
        inflight={JOBS.inflight}
        reattaching={JOBS.reattaching}
        onSelectJob={JOBS.onSelectJob}
        spine={!!studioGround && jobSpine && wideViewport && !jobRailExpanded}
        onExpand={studioGround && jobSpine ? noop : undefined}
        onCollapse={studioGround && jobSpine && jobRailExpanded ? noop : undefined}
      />
    ),
    // App.jsx:3502
    toast: <Toast toast={TOAST.toast} onDone={TOAST.onDone} />,
    // App.jsx:3578
    cockpit: studioGround && drafting ? <StatusToggles /> : null,
  }
}

/** ToolCast.jsx's stage mounts. No posture, no cockpit band; the frame arm is
 *  contract.chrome.stageBranch === 'frame' (ToolCast.jsx:1434/2077/2138). */
function todayStage(activeSurface) {
  const slots = surfaceContract(activeSurface)
  return {
    // ToolCast.jsx:1422-1430 — the stage passes signedIn/onSignOut (slice
    // 4b: published to the store, adopted by the tabs; see todayConsole).
    tabs: (
      <Published activeSurface={activeSurface} workspaceProject={EMPTY_WORKSPACE_PROJECT} catalog={CATALOG} signedIn onSignOut={noop}>
        <ProductSurfaceTabs
          activeSurface={activeSurface}
          states={STATES}
          onSelect={noop}
        />
      </Published>
    ),
    // ToolCast.jsx:2138-2147
    frame: slots.chrome.stageBranch === 'frame' ? (
      <ProductSurfaceFrame
        activeSurface={activeSurface}
        states={STATES}
        catalog={CATALOG}
        catalogError={null}
        workspaceProject={EMPTY_WORKSPACE_PROJECT}
        onCreateProject={noop}
        projectSlot={null}
      />
    ) : null,
    // ToolCast.jsx:1893-1898 — always inline, inside the trust panel.
    entitlementInline: <EntitlementGate {...ENTITLEMENTS} />,
    entitlementDocked: null,
    // ToolCast.jsx:1954 — the .tc-bar block (sentinel; see COMMAND_BAR).
    commandBar: COMMAND_BAR(),
    // ToolCast.jsx:1759-1766 — no spine props at all.
    jobRail: (
      <JobRail
        mock={JOBS.mock}
        jobs={JOBS.jobs}
        currentJob={JOBS.currentJob}
        inflight={JOBS.inflight}
        reattaching={JOBS.reattaching}
        onSelectJob={JOBS.onSelectJob}
      />
    ),
    // ToolCast.jsx:2040
    toast: <Toast toast={TOAST.toast} onDone={TOAST.onDone} />,
    // The stage has no drafting status band.
    cockpit: null,
  }
}

// --- the same six elements, through the frame ------------------------------

function framed(activeSurface, { console: isConsole }) {
  const common = {
    scene: isConsole ? 'console' : 'stage',
    activeSurface,
    states: STATES,
    catalog: CATALOG,
    catalogError: null,
    workspaceProject: EMPTY_WORKSPACE_PROJECT,
    onSelect: noop,
    onCreateProject: noop,
    session: null,
    entitlement: null,
    commandBar: COMMAND_BAR,
    jobRail: JOBS,
    toast: TOAST,
  }
  const slots = surfaceContract(activeSurface)
  const props = isConsole
    ? {
      ...common,
      posture: POSTURE,
      projectSlot: slots.chrome.projectSlot === 'ios-surface' ? PROJECT_SLOT : null,
      // App declares the ONE placement its two mounts used to spell twice.
      entitlement: {
        ...ENTITLEMENTS,
        placement: POSTURE.studio && slots.chrome.cockpit && POSTURE.wideViewport ? 'docked' : 'inline',
      },
    }
    : {
      ...common,
      posture: null,
      projectSlot: null,
      signedIn: true,
      onSignOut: noop,
      entitlement: { ...ENTITLEMENTS, placement: 'inline' },
    }
  const wrap = (child) => <SurfaceFrame {...props}>{child}</SurfaceFrame>
  return {
    tabs: wrap(<SurfaceFrame.Tabs />),
    frame: wrap(<SurfaceFrame.Frame />),
    entitlementInline: wrap(<SurfaceFrame.Entitlement at="inline" />),
    entitlementDocked: wrap(<SurfaceFrame.Entitlement at="docked" />),
    commandBar: wrap(<SurfaceFrame.CommandBar />),
    jobRail: wrap(<SurfaceFrame.JobRail />),
    toast: wrap(<SurfaceFrame.Toast />),
    cockpit: wrap(<SurfaceFrame.Cockpit />),
  }
}

const SLOTS = [
  'tabs', 'frame', 'entitlementInline', 'entitlementDocked',
  'commandBar', 'jobRail', 'toast', 'cockpit',
]
const SURFACES = ['browser', 'cad', 'solar', 'ios']
const CASES = [
  ...SURFACES.map((id) => ({ key: `console:${id}`, surface: id, console: true })),
  ...SURFACES.map((id) => ({ key: `stage:${id}`, surface: id, console: false })),
]

function capture(built) {
  const out = {}
  for (const slot of SLOTS) out[slot] = slotSequence(built[slot])
  return out
}

// A plain `if`, not describe.runIf: runIf registers a SKIPPED suite when the
// env var is unset, and the web-vitest gate fails closed on any skip outside
// its allowlist. With `if`, nothing is registered on an ordinary run.
if (CAPTURE) {
  describe('fixture capture (SURFACE_FRAME_CAPTURE=1)', () => {
    it('writes the transcription of today to disk', () => {
      const out = {}
      for (const testCase of CASES) {
        out[testCase.key] = capture(
          testCase.console ? todayConsole(testCase.surface) : todayStage(testCase.surface),
        )
      }
      writeFileSync(FIXTURE_PATH, `${JSON.stringify(out, null, 2)}\n`)
    })
  })
}

// ---------------------------------------------------------------------------
// DELIBERATE DIVERGENCES from the frozen fixture.
//
// The fixture is EVIDENCE: bytes captured on the untouched worktree at
// origin/main 1a6d6630, before slice 4a edited anything. Regenerating it to
// clear a failure would destroy exactly the property that makes it evidence,
// and would re-capture every OTHER row from today's code at the same time.
//
// So a slice that changes the shell ON PURPOSE records the change here, one
// scene:surface:slot at a time, with the reason. Every row not listed is
// still compared against the frozen bytes, unchanged.
const DIVERGENCES = {
  // P1 studio-shell pass: solar declared chrome.productFrame TRUE, which put
  // a 450px opaque marketing card in the console's flow over solar's OWN
  // ribbon, document band and canvas (measured y=28..478 at 1512x950; ground
  // top y=155; document band pushed from y~123 to y=585). Solar IS the CAD
  // workspace on the solar tool set (operator directive 2026-09-01), so the
  // contract now says false and this slot renders nothing — the same empty
  // sequence console:cad has always had. See productSurfaces.js's solar
  // record and surfaceGates.test.js's own DIVERGENCES table.
  'console:solar': { frame: [] },
}

/** The expectation for one slot: the frozen fixture, unless this slot is a
 *  recorded divergence. Never falls back silently — a key that names a slot
 *  the fixture does not have would be a typo that quietly disabled a row. */
function expected(key, slot) {
  const override = DIVERGENCES[key]
  if (override && slot in override) return override[slot]
  return FIXTURE[key][slot]
}

/** Every slot of one case, divergences applied. */
function expectedCase(key) {
  const out = {}
  for (const slot of SLOTS) out[slot] = expected(key, slot)
  return out
}

describe.skipIf(CAPTURE)('SurfaceFrame — zero visual change against the captured shell', () => {
  it('the fixture covers every scene x surface case', () => {
    expect(Object.keys(FIXTURE).sort()).toEqual(CASES.map((c) => c.key).sort())
  })

  describe('1. the transcription still matches the frozen fixture', () => {
    for (const testCase of CASES) {
      it(`${testCase.key} renders today's captured element sequence`, () => {
        const built = testCase.console ? todayConsole(testCase.surface) : todayStage(testCase.surface)
        expect(capture(built)).toEqual(expectedCase(testCase.key))
      })
    }
  })

  describe('2. SurfaceFrame reproduces it slot for slot', () => {
    for (const testCase of CASES) {
      const built = () => framed(testCase.surface, { console: testCase.console })
      for (const slot of SLOTS) {
        it(`${testCase.key} · ${slot}`, () => {
          expect(slotSequence(built()[slot])).toEqual(expected(testCase.key, slot))
        })
      }
    }
  })

  it('every recorded divergence names a real case and slot, and actually diverges', () => {
    // A divergence table is dangerous in exactly two ways: a typo'd key that
    // silently overrides nothing, and an entry left behind after the shell
    // moved back, which would then be pinning the fixture's own value while
    // claiming to be an exception. Both are caught here.
    for (const [key, slots] of Object.entries(DIVERGENCES)) {
      expect(Object.keys(FIXTURE)).toContain(key)
      for (const [slot, value] of Object.entries(slots)) {
        expect(SLOTS).toContain(slot)
        expect(value).not.toEqual(FIXTURE[key][slot])
      }
    }
    // And the one we expect today, by name, so silently emptying the table
    // (which would make every row above compare to the frozen bytes again,
    // and fail) cannot be mistaken for "the divergence was reverted".
    expect(DIVERGENCES['console:solar'].frame).toEqual([])
    expect(FIXTURE['console:solar'].frame.length).toBeGreaterThan(0)
  })

  it('the sequence probe would catch a changed class name (positive control)', () => {
    // Without this a serializer that returned [] for everything would report
    // GREEN for every row above.
    const a = slotSequence(<div className="x"><span className="y" /></div>)
    const b = slotSequence(<div className="x"><span className="z" /></div>)
    expect(a).not.toEqual(b)
    expect(a).toEqual(['div|x|', 'span|y|'])
  })
})

describe.skipIf(CAPTURE)('SurfaceFrame — the gates it now owns', () => {
  it('a slot mounted outside a frame renders nothing instead of throwing', () => {
    // Fail closed. The alternative is a ReferenceError inside a scene's tree.
    for (const Slot of [
      SurfaceFrame.Tabs, SurfaceFrame.Frame, SurfaceFrame.Entitlement,
      SurfaceFrame.CommandBar, SurfaceFrame.JobRail, SurfaceFrame.Toast,
      SurfaceFrame.Cockpit,
    ]) {
      expect(slotSequence(<Slot />)).toEqual([])
    }
  })

  it('the stage never renders the cockpit toggles, on any surface', () => {
    for (const id of SURFACES) {
      expect(slotSequence(framed(id, { console: false }).cockpit)).toEqual([])
    }
  })

  it('the console renders the cockpit toggles exactly where the contract declares one', () => {
    for (const id of SURFACES) {
      const rendered = slotSequence(framed(id, { console: true }).cockpit).length > 0
      expect(rendered).toBe(surfaceContract(id).chrome.cockpit)
    }
  })

  it('the job rail is spined exactly where rails.right declares a spine', () => {
    for (const id of SURFACES) {
      const { container, unmount } = render(
        <div>{framed(id, { console: true }).jobRail}</div>,
      )
      const spined = !!container.querySelector('[data-spine="true"], .job-spine')
        || container.innerHTML.includes('job-rail-spine')
      expect(spined).toBe(surfaceContract(id).rails.right === 'job-spine')
      unmount()
    }
  })

  it('the entitlement gate renders at exactly one declared placement', () => {
    for (const id of SURFACES) {
      const built = framed(id, { console: true })
      const inline = slotSequence(built.entitlementInline).length > 0
      const docked = slotSequence(built.entitlementDocked).length > 0
      expect(inline !== docked).toBe(true)
    }
  })

  it('the command bar slot emits the scene node untouched', () => {
    const node = <section className="scene-owned" data-testid="scene-bar"><b className="inner" /></section>
    const asNode = slotSequence(
      <SurfaceFrame scene="console" activeSurface="cad" states={STATES} commandBar={node}>
        <SurfaceFrame.CommandBar />
      </SurfaceFrame>,
    )
    const asRenderProp = slotSequence(
      <SurfaceFrame scene="console" activeSurface="cad" states={STATES} commandBar={() => node}>
        <SurfaceFrame.CommandBar />
      </SurfaceFrame>,
    )
    expect(asNode).toEqual(['section|scene-owned|scene-bar', 'b|inner|'])
    expect(asRenderProp).toEqual(asNode)
  })

  // isConsole() used to infer console-vs-stage from whether `posture` was
  // null, which falls OPEN: a caller that forgot posture, or typo'd a surface
  // id into the wrong prop, silently resolved to "stage" and every
  // console-only gate below (Cockpit, the docked entitlement placement, the
  // job-rail spine) would go quiet instead of the mount failing. `scene` is
  // now a required, explicit two-value contract, validated once at the
  // provider, so a wrong scene is loud on the FIRST render, not a silently
  // wrong gate three components deep.
  describe('scene is a required, explicit contract — a wrong one throws, never falls open', () => {
    it('throws when scene is not passed at all', () => {
      expect(() => render(
        <SurfaceFrame activeSurface="cad" states={STATES}>
          <SurfaceFrame.Tabs />
        </SurfaceFrame>,
      )).toThrow(/scene must be 'console' or 'stage'/)
    })

    it('throws when scene is a garbage value (e.g. a surface id typoed into it)', () => {
      expect(() => render(
        <SurfaceFrame scene="cad" activeSurface="cad" states={STATES}>
          <SurfaceFrame.Tabs />
        </SurfaceFrame>,
      )).toThrow(/scene must be 'console' or 'stage'/)
    })

    it('accepts exactly "console" and "stage", nothing else', () => {
      for (const scene of ['console', 'stage']) {
        expect(() => render(
          <SurfaceFrame scene={scene} activeSurface="cad" states={STATES}>
            <SurfaceFrame.Tabs />
          </SurfaceFrame>,
        )).not.toThrow()
        cleanup()
      }
    })
  })
})
