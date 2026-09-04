/**
 * THE SURFACE CONTRACT — gate equivalence (standardization slice 2).
 *
 * Slice 2 repointed every surface-literal chrome gate in the shell onto the
 * contract. The claim that makes that safe is "behaviour identical", and this
 * file is what turns that claim into a test instead of an assertion in a PR
 * body: for ALL FOUR surface ids, each derived gate is pinned equal to the
 * literal predicate it replaced.
 *
 * The old predicates are written here as LITERALS on purpose — `id !== 'cad'`,
 * `id === 'cad' || id === 'solar'`, the stage's three-arm ternary — never
 * imported from the module under test. A table that read its expectation out
 * of productSurfaces.js would agree with any future edit to a default, which
 * is exactly the drift this file exists to catch. Editing a contract value
 * without editing the matching literal here fails, loudly, by design.
 *
 * Where a gate is intentionally the same predicate as another (the cockpit,
 * the rail postures, the dock and the command line all followed `drafting`
 * before this slice), each is pinned SEPARATELY. They are separate slots by
 * the operator rule ("everything needs to be ABLE to be there"), so a later
 * slice may legitimately split one from the others — and must then update one
 * row here, not silently pass because a shared helper moved.
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import {
  DEFAULT_PRODUCT_SURFACE,
  PRODUCT_SURFACES,
  surfaceContract,
  surfaceGround,
} from './productSurfaces.js'
import { groundShowsDrawing } from './SurfaceGrounds.jsx'

// The four ids as a literal, so a surface added without a row here fails the
// completeness test below rather than quietly going unpinned.
const SURFACE_IDS = ['browser', 'cad', 'solar', 'ios']

// ---------------------------------------------------------------------------
// The old predicates, verbatim from the pre-slice-2 source. Each cites the
// site it was lifted from at the commit slice 2 branched from (origin/main
// 1a49766). These are the ORACLE; the contract is what is under test.
// ---------------------------------------------------------------------------
const OLD = {
  // App.jsx:2838  {activeSurface !== 'cad' && (<ProductSurfaceFrame .../>)}
  productFrame: (id) => id !== 'cad',
  // App.jsx:2859  display: activeSurface === 'cad' || activeSurface === 'solar'
  workspaceCard: (id) => id === 'cad' || id === 'solar',
  // App.jsx:2846  projectSlot={activeSurface === 'ios' ? <IosSurface/> : null}
  iosProjectSlot: (id) => id === 'ios',
  // App.jsx:2269  const drafting = groundShowsDrawing(activeSurface)
  // Spelled out rather than calling groundShowsDrawing, which slice 2 also
  // repointed: an oracle that shares an implementation with its subject
  // proves nothing.
  drafting: (id) => id === 'cad' || id === 'solar',
  // ToolCast.jsx:1434 / :2077 / :2139 — the stage's three-arm ternary.
  stageBranch: (id) => (id === 'cad' ? 'cad' : id === 'ios' ? 'ios' : 'frame'),
  // App.jsx:2296  studioGround && activeSurface === 'solar' (layer accent)
  // App.jsx:2313  solarStringsEligible's surface term
  solar: (id) => id === 'solar',
  // SurfaceGrounds.jsx:298 / :306  surface === 'browser' / surface === 'ios'
  board: (id) => id === 'browser',
  deviceStage: (id) => id === 'ios',
  // App.jsx:2234  useState('draw') — one console-global literal, every surface.
  ribbonHome: () => 'draw',
}

describe('Surface Contract — every repointed gate equals its old literal', () => {
  for (const id of SURFACE_IDS) {
    const c = surfaceContract(id)

    it(`${id}: chrome.productFrame === (id !== 'cad')`, () => {
      expect(c.chrome.productFrame).toBe(OLD.productFrame(id))
    })

    it(`${id}: chrome.workspaceCard === (id === 'cad' || id === 'solar')`, () => {
      expect(c.chrome.workspaceCard).toBe(OLD.workspaceCard(id))
    })

    it(`${id}: chrome.projectSlot === 'ios-surface' iff the surface is ios`, () => {
      expect(c.chrome.projectSlot === 'ios-surface').toBe(OLD.iosProjectSlot(id))
    })

    it(`${id}: chrome.cockpit === the old drafting predicate`, () => {
      expect(c.chrome.cockpit).toBe(OLD.drafting(id))
    })

    it(`${id}: chrome.stageBranch === the stage's old three-arm ternary`, () => {
      expect(c.chrome.stageBranch).toBe(OLD.stageBranch(id))
    })

    it(`${id}: rails.left === 'spine' matches the old navSpine surface term`, () => {
      expect(c.rails.left === 'spine').toBe(OLD.drafting(id))
    })

    it(`${id}: rails.right === 'job-spine' matches the old JobRail surface term`, () => {
      expect(c.rails.right === 'job-spine').toBe(OLD.drafting(id))
    })

    it(`${id}: rails.dock is truthy exactly where the dock used to mount`, () => {
      // The mount gate reads the TRUTHINESS of rails.dock, so that is what is
      // pinned — not the section list, which the schema suite owns.
      expect(!!c.rails.dock).toBe(OLD.drafting(id))
    })

    it(`${id}: commandLine === the old PromptBox commandLine surface term`, () => {
      expect(c.commandLine).toBe(OLD.drafting(id))
    })

    it(`${id}: groundMaterial.layerAccent === 'solar' iff the surface is solar`, () => {
      expect(c.groundMaterial.layerAccent === 'solar').toBe(OLD.solar(id))
    })

    it(`${id}: groundMaterial.solarStrings === the old solarStringsEligible term`, () => {
      expect(c.groundMaterial.solarStrings).toBe(OLD.solar(id))
    })

    it(`${id}: ground kinds match the old SurfaceGrounds active flags`, () => {
      expect(surfaceGround(id) === 'board').toBe(OLD.board(id))
      expect(surfaceGround(id) === 'device-stage').toBe(OLD.deviceStage(id))
      expect(surfaceGround(id) === 'drawing').toBe(OLD.drafting(id))
    })

    it(`${id}: the ribbon opens on the same tab useState('draw') used to give`, () => {
      // The console's ribbon tab is GLOBAL state, so App reads toolbar.home
      // with a fallback to the default surface's home. Both arms must land on
      // the literal the fallback replaced, or a browser -> cad switch would
      // mount the ribbon with no tab selected.
      const home = surfaceContract(id).toolbar.home
        ?? surfaceContract(DEFAULT_PRODUCT_SURFACE).toolbar.home
      expect(home).toBe(OLD.ribbonHome())
    })
  }

  it('groundShowsDrawing, now derived from the contract, keeps its truth table', () => {
    for (const id of SURFACE_IDS) expect(groundShowsDrawing(id)).toBe(OLD.drafting(id))
    // The signature is unchanged and it still fails closed on a non-surface:
    // a Set miss, never a normalization to the CAD default.
    expect(groundShowsDrawing(undefined)).toBe(false)
    expect(groundShowsDrawing(null)).toBe(false)
    expect(groundShowsDrawing('not-a-surface')).toBe(false)
    expect(groundShowsDrawing('')).toBe(false)
  })

  it('an unknown surface activates neither the board nor the device stage', () => {
    // SurfaceGrounds now asks surfaceGround(), which normalizes an unknown id
    // to the CAD contract. That must still light up NO alternate ground, the
    // same as the old `surface === 'browser'` / `=== 'ios'` literals.
    for (const bogus of ['not-a-surface', '', undefined, null]) {
      expect(surfaceGround(bogus) === 'board').toBe(false)
      expect(surfaceGround(bogus) === 'device-stage').toBe(false)
    }
  })

  it('covers every surface the module ships, so no row can go unpinned', () => {
    expect(PRODUCT_SURFACES.map(({ id }) => id).sort()).toEqual([...SURFACE_IDS].sort())
  })

  it('is not vacuous: the table actually separates the four surfaces', () => {
    // A table where every expectation is the same value would pass against a
    // constant contract. Prove the gates disagree across surfaces.
    expect(new Set(SURFACE_IDS.map((id) => surfaceContract(id).chrome.stageBranch)).size).toBe(3)
    expect(SURFACE_IDS.filter((id) => surfaceContract(id).chrome.cockpit)).toEqual(['cad', 'solar'])
    expect(SURFACE_IDS.filter((id) => surfaceContract(id).chrome.productFrame))
      .toEqual(['browser', 'solar', 'ios'])
    // The solar quirk this slice PRESERVES: the frame renders over a shown
    // workspace card. If a later slice fixes it, this line is the tripwire.
    expect(surfaceContract('solar').chrome.productFrame).toBe(true)
    expect(surfaceContract('solar').chrome.workspaceCard).toBe(true)
    // The documented console/stage divergences, also preserved: the stage
    // gives solar the frame arm while the console gives it a cockpit.
    expect(surfaceContract('solar').chrome.stageBranch).toBe('frame')
    expect(surfaceContract('solar').chrome.cockpit).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// The slice's headline claim, checked against the shipped source rather than
// against a summary of it: no shell component decides chrome by comparing
// activeSurface to a string literal any more.
//
// Deliberately scanned WITH comments, which makes this stricter than a
// compiled-output grep and needs no transform step (esbuild cannot run under
// vitest's jsdom environment). The cost is a house rule the repointed sites
// already follow: a comment recording what a gate replaced names the old
// predicate WITHOUT re-spelling the comparison, so prose can never satisfy or
// defeat this pin.
// ---------------------------------------------------------------------------
// Every file that renders shell chrome from a surface: the two scenes, the
// product frame (its project-slot gate was the one comparison #978 missed) and
// the grounds. A new shell file joins this list the day it reads a surface.
// Slice 4a added SurfaceFrame.jsx (the ONE wrapper both scenes mount, which
// now owns the tabs / product frame / entitlement / job rail / toast / cockpit
// gates for both) and NavRail.jsx (the console rail, which owns its own
// collapse-button gate). Both read declared slots; neither may drift back to a
// literal, which is exactly what this scan is for.
const SHELLS = [
  '../App.jsx',
  './ToolCast.jsx',
  '../components/ProductSurfaceTabs.jsx',
  './SurfaceGrounds.jsx',
  './SurfaceFrame.jsx',
  './NavRail.jsx',
]
const SURFACE_LITERAL = /activeSurface\s*[!=]==\s*['"]/
// The frame and the grounds hold the surface as `surface` / `surface.id`, so a
// literal comparison there hides from the activeSurface probe. Second probe.
const SURFACE_ID_LITERAL = /\bsurface(?:\.id)?\s*[!=]==\s*['"]/

describe('Surface Contract — no surface-literal chrome gate survives', () => {
  for (const relative of SHELLS) {
    it(`${relative} compares no surface id to a string literal`, () => {
      const source = readFileSync(new URL(relative, import.meta.url), 'utf8')
      expect(source).not.toMatch(SURFACE_LITERAL)
      expect(source).not.toMatch(SURFACE_ID_LITERAL)
      // The file must still be the one that renders the surface, or the pins
      // above pass vacuously against an unrelated module.
      expect(source).toMatch(/activeSurface|\bsurface\b/)
    })
  }

  it('the probes would catch a reintroduced literal (positive control)', () => {
    // Without this, a broken regex would report GREEN forever. Built from
    // fragments so the control itself cannot trip the scans above.
    const reintroduced = `const x = activeSurface ${'==='} 'cad' ? 1 : 2`
    expect(reintroduced).toMatch(SURFACE_LITERAL)
    expect(`if (activeSurface ${'!=='} 'ios') return`).toMatch(SURFACE_LITERAL)
    expect(`const showProjectState = surface.id ${'!=='} 'ios'`).toMatch(SURFACE_ID_LITERAL)
    expect(`active={surface ${'==='} 'browser'}`).toMatch(SURFACE_ID_LITERAL)
    // A key/tab id comparison is not a chrome gate and must stay out of reach.
    expect(`const selected = surface.id ${'==='} activeSurface`).not.toMatch(SURFACE_ID_LITERAL)
  })
})
