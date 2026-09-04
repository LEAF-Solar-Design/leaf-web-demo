// @vitest-environment jsdom
//
// Slice 4b: DemoTour spotlights by `data-tour` anchor FIRST, className chain
// SECOND, with the step -> anchor map coming from the Surface Contract. The
// first describe block pins the resolution order and its fail-closed edges in
// a real DOM, without rendering the tour (resolveTourTarget is the one
// function both the measure and the scroll-into-view path call).
//
// The second and third blocks are THE PAIRING PIN check_tour_anchors.mjs's
// own docstring points to: static source scanning (that oracle) can prove an
// anchor id exists in a file and is referenced by a step, but never that it
// sits on the SAME element the step's className chain would have picked
// instead — that is a DOM fact, not a source fact. "the contract maps
// resolve the SAME elements..." mounts each shell's real markup shape and
// asserts, for every mapped step, that resolving with the contract's anchors
// and resolving with anchors withheld (className chain only) land on the
// identical node. "positive control: the pairing above is not vacuous" then
// proves that comparison is a real tripwire by moving one `data-tour`
// attribute onto an unrelated sibling and showing the two resolutions
// diverge — exactly the drift class an anchor silently landing on the wrong
// element would be.
import { afterEach, describe, expect, it } from 'vitest'

import { resolveTourTarget } from './DemoTour.jsx'
import { TOUR_STEPS } from './tourScript.js'
import { PRODUCT_SURFACES, surfaceContract } from '../site/productSurfaces.js'

afterEach(() => { document.body.innerHTML = '' })

function mount(html) {
  document.body.innerHTML = html
}

const step = (id, target) => ({ id, title: id, body: id, target })

describe('resolveTourTarget: anchor first, className chain second', () => {
  it('an anchored step resolves to the data-tour element even when the className target also exists', () => {
    mount('<div class="app" id="by-class"></div><section data-tour="shell" id="by-anchor"></section>')
    const el = resolveTourTarget(step('welcome', '.app'), { welcome: 'shell' })
    expect(el.id).toBe('by-anchor')
  })

  it('falls back to the className chain when the anchor element is absent', () => {
    mount('<div class="bar-dock" id="dock"></div><div class="workspace-card" id="card"></div>')
    const el = resolveTourTarget(step('count', '.bar, .bar-dock, .workspace-card'), { count: 'command-bar' })
    expect(el.id).toBe('dock')
  })

  it('a step the map does not name keeps its className resolution exactly', () => {
    mount('<div class="strip-decision" id="strip"></div><div class="bar-dock" id="dock"></div>')
    expect(resolveTourTarget(step('version', '.strip-decision, .bar-dock'), { welcome: 'shell' }).id).toBe('strip')
    expect(resolveTourTarget(step('version', '.strip-decision, .bar-dock'), null).id).toBe('strip')
  })

  it('a null map (a shell with no tour on this surface) is the className path', () => {
    mount('<main class="stage-root" id="root" data-tour="shell"></main>')
    expect(resolveTourTarget(step('welcome', '.stage-root'), null).id).toBe('root')
    expect(resolveTourTarget(step('welcome', '.stage-root'), undefined).id).toBe('root')
  })

  it('an invalid anchor id never becomes a selector: it falls back, and cannot throw', () => {
    mount('<div class="app" id="by-class"></div>')
    for (const bad of ['"] , *', 'Shell', 'a b', '', 42, null, {}, '[data-tour="shell"]']) {
      expect(() => resolveTourTarget(step('welcome', '.app'), { welcome: bad })).not.toThrow()
      expect(resolveTourTarget(step('welcome', '.app'), { welcome: bad }).id).toBe('by-class')
    }
  })

  it('tree order: an outer shell element wins over a shared inner component carrying the same id', () => {
    // The stage's .stage-viewer wraps the shared Viewer (.viewer-canvas), and
    // both carry data-tour="viewer" in source; the outer one is what the
    // stage's className target (.stage-viewer) chose before the anchors.
    mount('<div class="stage-viewer" data-tour="viewer" id="outer"><div class="viewer-canvas" data-tour="viewer" id="inner"></div></div>')
    expect(resolveTourTarget(step('viewer', '.stage-viewer'), { viewer: 'viewer' }).id).toBe('outer')
    // And the console, where only the canvas exists, gets the canvas.
    mount('<div class="viewer-wrap"><div class="viewer-canvas" data-tour="viewer" id="canvas"></div></div>')
    expect(resolveTourTarget(step('viewer', '.viewer-canvas, .workspace-card'), { viewer: 'viewer' }).id).toBe('canvas')
  })

  it('a null step or a step with no target and no anchor resolves to nothing', () => {
    mount('<div data-tour="shell"></div>')
    expect(resolveTourTarget(null, { welcome: 'shell' })).toBeNull()
    expect(resolveTourTarget(step('exit', null), { welcome: 'shell' })).toBeNull()
  })
})

describe('the contract maps resolve the SAME elements the className chains chose', () => {
  // A minimal DOM carrying both shells' anchors on the elements the className
  // targets name, so anchor-first and className-only must agree on every
  // mapped step. Any disagreement is a spotlight that moved.
  // Matches the real, current markup: NavRail's `aside.nav` and JobRail's own
  // `aside.rail` carry no `data-tour` (removed as orphaned vocabulary, see
  // check_tour_anchors.mjs's orphan check); `.tc-rail-r` is the stage's OWN
  // `right-rail` anchor and wins over the shared JobRail nested inside it by
  // tree order, so only the outer element is tagged here, as in source.
  const CONSOLE_DOM = `
    <div class="app" data-tour="shell">
      <div class="viewer-wrap"><div class="viewer-canvas" data-tour="viewer"></div></div>
      <div class="bar-dock"><div class="bar" data-tour="command-bar"></div></div>
      <aside class="nav"></aside>
      <aside class="rail"></aside>
    </div>`
  const STAGE_DOM = `
    <main class="stage-root" data-tour="shell">
      <div class="stage-viewer" data-tour="viewer"><div class="viewer-canvas" data-tour="viewer"></div></div>
      <div class="tc-bar-wrap"><div class="tc-bar" data-tour="command-bar"></div></div>
      <aside class="tc-rail tc-rail-l tc-operator-rail"></aside>
      <aside class="tc-rail tc-rail-r" data-tour="right-rail"><aside class="rail"></aside></aside>
    </main>`
  const STAGE_TARGETS = { welcome: '.stage-root', viewer: '.stage-viewer', request: '.tc-bar', versions: '.tc-rail-r', trust: '.tc-rail-r' }

  it('console: every mapped TOUR_STEPS step lands where its className chain lands', () => {
    mount(CONSOLE_DOM)
    for (const surface of PRODUCT_SURFACES.filter((s) => s.contract.scene === 'app')) {
      const anchors = surfaceContract(surface.id).tourAnchors.console
      for (const s of TOUR_STEPS) {
        if (!(s.id in anchors)) continue
        expect(resolveTourTarget(s, anchors), `${surface.id}:${s.id}`).toBe(resolveTourTarget(s, null))
      }
    }
  })

  it('stage: every mapped UNIFIED step lands where its className target lands', () => {
    mount(STAGE_DOM)
    const anchors = surfaceContract('cad').tourAnchors.stage
    for (const [id, target] of Object.entries(STAGE_TARGETS)) {
      expect(id in anchors, id).toBe(true)
      expect(resolveTourTarget(step(id, target), anchors), id).toBe(resolveTourTarget(step(id, target), null))
    }
  })
})

describe('positive control: the pairing above is not vacuous', () => {
  // The two suites just above assert that anchor-first and className-only
  // resolution AGREE on the real markup. That assertion is only worth
  // anything if it is ABLE to disagree — this proves it is, by reproducing
  // exactly the drift class 2 of the tour-anchor review found: a `data-tour`
  // id that sits on the wrong element. Moving `viewer` off `.viewer-canvas`
  // and onto an unrelated sibling must make the two resolutions diverge; if
  // this test could not be made to fail, the equality checks above could
  // pass on a build where an anchor had silently moved.
  it('moving data-tour="viewer" off .viewer-canvas onto a sibling makes anchor-first and className-only disagree', () => {
    mount(`
      <div class="app" data-tour="shell">
        <div data-tour="viewer" class="drifted-anchor"></div>
        <div class="viewer-wrap"><div class="viewer-canvas"></div></div>
        <div class="bar-dock"><div class="bar" data-tour="command-bar"></div></div>
      </div>`)
    const viewerStep = TOUR_STEPS.find((s) => s.id === 'viewer')
    const anchors = surfaceContract('cad').tourAnchors.console
    const byAnchor = resolveTourTarget(viewerStep, anchors)
    const byClassName = resolveTourTarget(viewerStep, null)
    expect(byAnchor).not.toBeNull()
    expect(byClassName).not.toBeNull()
    expect(byAnchor.className).toBe('drifted-anchor')
    expect(byClassName.className).toBe('viewer-canvas')
    expect(byAnchor).not.toBe(byClassName)
  })
})
