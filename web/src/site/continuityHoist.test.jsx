// @vitest-environment jsdom
//
// SLICE 4b's CONTINUITY HOIST, proven two ways.
//
// 1. BYTE-IDENTICAL MARKUP. `continuityHoist.before-fixture.json` is the
//    outerHTML of the continuity rail and the sign-out control, plus the
//    nav's child class list, captured on the UNTOUCHED tree at origin/main
//    8ff0c601 by rendering the pre-4b <ProductSurfaceTabs> with the props each
//    scene passed (console: no signedIn; stage: signedIn + onSignOut), for
//    every studio surface and three data cases (honest-empty + catalog,
//    drawing-only + catalog, open project + no catalog). The same elements
//    rendered through the 4b path (SiteRoot's ContinuityStore -> SurfaceFrame
//    publish -> the tabs adopt the host) must match it byte for byte, and the
//    nav's visible children must still be tablist, rail, sign-out. The one
//    new node is the host wrapper, whose class landing.css gives
//    `display: contents`, so it has no box; that rule is pinned here too.
//
//    The capture transcription (deleted after the capture, recorded here so
//    the fixture's provenance is readable):
//      render(<ProductSurfaceTabs activeSurface={surface} states={STATES}
//        onSelect={noop} workspaceProject={data.workspaceProject}
//        catalog={data.catalog} {...(scene === 'stage'
//          ? { signedIn: true, onSignOut: noop } : {})} />)
//      -> [data-testid="continuity-rail"].outerHTML,
//         .tc-account-signout?.outerHTML,
//         [...nav.children].map((c) => c.className)
//
// 2. THE CONTRACT THE HOIST BUYS. The rail is the SAME node across a surface
//    switch (F-8, as before) AND across a scene swap (new): scene A's frame
//    and tabs unmount, scene B's mount, and the element identity holds. Scene
//    B sees scene A's last published state until it publishes its own, the
//    first paint before any publish is the honest-empty fallback, and the
//    store holds a snapshot only (no controller). Outside a store everything
//    fails closed: the tabs render only their tablist, the frame's publish is
//    a no-op, nothing throws.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import ProductSurfaceTabs from '../components/ProductSurfaceTabs.jsx'

import ContinuityStore from './ContinuityStore.jsx'
import {
  CONTINUITY_HOST_CLASS,
  createContinuityHost,
  initialContinuitySnapshot,
  normalizeContinuitySnapshot,
  sameContinuitySnapshot,
} from './continuityStore.js'
import SurfaceFrame from './SurfaceFrame.jsx'
import { EMPTY_WORKSPACE_PROJECT, deriveWorkspaceProjectState } from './workspaceProjectState.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const FIXTURE = JSON.parse(readFileSync(join(HERE, 'continuityHoist.before-fixture.json'), 'utf8'))

afterEach(cleanup)

const noop = () => {}
const STATES = Object.freeze({
  browser: { state: 'available', label: 'Ready' },
  cad: { state: 'available', label: 'Ready' },
  solar: { state: 'beta', label: 'Beta' },
  ios: { state: 'setup', label: 'Setup required' },
})
const CATALOG = Object.freeze({
  source: 'server',
  families: [
    { family_id: 'measurement', label: 'Measurement', capabilities: [{ name: 'count_panels', label: 'Count panels' }] },
    { family_id: 'stringing', label: 'Stringing', capabilities: [{ name: 'solve_strings' }, { name: 'route_home_runs' }] },
  ],
})
// The same three data cases the fixture was captured with, by the same names.
const DATA = {
  'empty+catalog': { workspaceProject: EMPTY_WORKSPACE_PROJECT, catalog: CATALOG },
  'drawing-only+catalog': { workspaceProject: deriveWorkspaceProjectState({ drawingName: 'rooftop_demo', orgId: 'org-1' }), catalog: CATALOG },
  'project+nocatalog': { workspaceProject: deriveWorkspaceProjectState({ openProjectId: 'p-1', projectName: 'rooftop_demo', orgId: 'org-1' }), catalog: null },
}
const SURFACES = ['browser', 'cad', 'solar', 'ios']

/** A scene, the way SiteRoot mounts one: the frame around the tabs. */
function Scene({ scene, surface, data, signedIn = false, onSignOut = null }) {
  const props = {
    scene,
    activeSurface: surface,
    states: STATES,
    onSelect: noop,
    workspaceProject: data.workspaceProject,
    catalog: data.catalog,
    signedIn,
    onSignOut,
  }
  return (
    <SurfaceFrame {...props}>
      <SurfaceFrame.Tabs />
    </SurfaceFrame>
  )
}

const rail = (root) => root.querySelector('[data-testid="continuity-rail"]')
const signOut = (root) => root.querySelector('.tc-account-signout')
const nav = (root) => root.querySelector('.tc-product-nav')
const host = (root) => root.querySelector(`.${CONTINUITY_HOST_CLASS}`)
// The nav's children as the fixture recorded them, with the host (display:
// contents, no box) flattened into its own children. That flattening is the
// ONE named difference between the two trees, and it is a difference the
// browser's box tree does not have.
const visibleChildren = (navEl) => Array.from(navEl.children).flatMap((c) => (
  c.classList.contains(CONTINUITY_HOST_CLASS) ? Array.from(c.children).map((g) => g.className) : [c.className]
))

describe('1. the rail and the sign-out render byte-identical markup, in the same place', () => {
  it('the fixture covers every scene x surface x data case', () => {
    const keys = Object.keys(FIXTURE).sort()
    expect(keys).toEqual([...SURFACES.map((s) => `console:${s}`), ...SURFACES.map((s) => `stage:${s}`)].sort())
    for (const key of keys) expect(Object.keys(FIXTURE[key]).sort()).toEqual(Object.keys(DATA).sort())
  })

  for (const scene of ['console', 'stage']) {
    for (const surface of SURFACES) {
      for (const [name, data] of Object.entries(DATA)) {
        it(`${scene}:${surface} · ${name}`, () => {
          const expected = FIXTURE[`${scene}:${surface}`][name]
          const { container } = render(
            <ContinuityStore search={`?surface=${surface}`}>
              <Scene scene={scene} surface={surface} data={data} signedIn={scene === 'stage'} onSignOut={scene === 'stage' ? noop : null} />
            </ContinuityStore>,
          )
          const navEl = nav(container)
          expect(rail(container)?.outerHTML ?? null).toBe(expected.rail)
          expect(signOut(container)?.outerHTML ?? null).toBe(expected.signOut)
          expect(visibleChildren(navEl)).toEqual(expected.navChildren)
          // The host is the nav's LAST child, right after the tablist, and it
          // holds exactly the rail (and the sign-out when signed in).
          const h = host(container)
          expect(h.parentNode).toBe(navEl)
          expect(navEl.lastElementChild).toBe(h)
          expect(navEl.firstElementChild.className).toBe('tc-product-tabs')
        })
      }
    }
  }

  it('the host has no box: landing.css gives its class display: contents, and it carries no inline style', () => {
    const css = readFileSync(join(HERE, 'landing.css'), 'utf8')
    expect(css).toMatch(new RegExp(`\\.${CONTINUITY_HOST_CLASS} \\{ display: contents; \\}`))
    const h = createContinuityHost(document)
    expect(h.className).toBe(CONTINUITY_HOST_CLASS)
    expect(h.getAttribute('style')).toBeNull()
    expect(h.attributes.length).toBe(1)
  })

  it('the fixture probe would catch a changed attribute (positive control)', () => {
    // A serializer that dropped attributes would make every row above vacuous.
    const { container } = render(
      <ContinuityStore search="?surface=cad">
        <Scene scene="stage" surface="cad" data={DATA['empty+catalog']} signedIn onSignOut={noop} />
      </ContinuityStore>,
    )
    expect(rail(container).outerHTML).toContain('data-project-state="empty"')
    expect(rail(container).outerHTML).not.toBe(FIXTURE['stage:cad']['drawing-only+catalog'].rail)
  })
})

describe('2. the never-remounts contract, across a surface switch and across the scene crossing', () => {
  it('the rail is the SAME node across a profile switch and pulses instead of remounting (F-8, as before)', () => {
    const data = DATA['project+nocatalog']
    const { container, rerender } = render(
      <ContinuityStore search="?surface=browser"><Scene scene="stage" surface="browser" data={DATA['empty+catalog']} /></ContinuityStore>,
    )
    const before = rail(container)
    expect(before.dataset.pulse).toBe('false')
    rerender(<ContinuityStore search="?surface=browser"><Scene scene="stage" surface="solar" data={data} /></ContinuityStore>)
    expect(rail(container)).toBe(before)
    expect(before.dataset.pulse).toBe('true')
    expect(before.textContent).toContain('rooftop_demo')
  })

  it('the rail is the SAME node across the /try -> /app crossing, adopted into the new nav', () => {
    const { container, rerender } = render(
      <ContinuityStore search="?surface=cad"><Scene key="try" scene="stage" surface="cad" data={DATA['drawing-only+catalog']} /></ContinuityStore>,
    )
    const before = rail(container)
    const tryNav = nav(container)
    expect(before.closest('.tc-product-nav')).toBe(tryNav)
    // Scene swap: a DIFFERENT element type at the same slot, exactly like
    // SiteRoot's ternary replacing StageScene with the console arm.
    rerender(<ContinuityStore search="?surface=cad"><Scene key="app" scene="console" surface="cad" data={DATA['drawing-only+catalog']} /></ContinuityStore>)
    const appNav = nav(container)
    expect(appNav).not.toBe(tryNav)
    expect(tryNav.isConnected).toBe(false)
    expect(rail(container)).toBe(before)
    expect(before.closest('.tc-product-nav')).toBe(appNav)
    expect(before.isConnected).toBe(true)
    // Same surface on both sides of the crossing: no pulse (a crossing is not
    // a profile switch).
    expect(before.dataset.pulse).toBe('false')
  })

  it('a crossing that lands on a DIFFERENT surface than the previous scene published DOES pulse (intended, pinned: the pre-4b remount hid this, it never excluded it)', () => {
    // Before the hoist, ContinuityRail was rendered fresh by whichever scene
    // was mounted, so a /try <-> /app crossing always REMOUNTED it; the
    // pulse effect's own first-mount guard (see ProductSurfaceTabs.jsx
    // ContinuityRail) then skipped firing no matter what activeSurface
    // changed to. Slice 4b's rail never remounts across a crossing, so that
    // guard no longer applies here: the effect's [activeSurface] dependency
    // sees the new scene's value and fires, exactly as it does for an
    // in-scene profile switch. That is the F-8 contract as documented
    // ("A surface change pulses it") behaving correctly on a case the old
    // code path never truly decided about, it only happened to hide it.
    const { container, rerender } = render(
      <ContinuityStore search="?surface=browser"><Scene key="try" scene="stage" surface="browser" data={DATA['empty+catalog']} /></ContinuityStore>,
    )
    const before = rail(container)
    expect(before.dataset.pulse).toBe('false')
    rerender(<ContinuityStore search="?surface=browser"><Scene key="app" scene="console" surface="cad" data={DATA['drawing-only+catalog']} /></ContinuityStore>)
    expect(rail(container)).toBe(before)
    expect(before.dataset.pulse).toBe('true')
  })

  it('the new scene shows the LAST published state until it publishes its own', () => {
    const { container, rerender } = render(
      <ContinuityStore search="?surface=cad"><Scene key="try" scene="stage" surface="cad" data={DATA['project+nocatalog']} /></ContinuityStore>,
    )
    expect(rail(container).textContent).toContain('project · rooftop_demo')
    // The crossing window: the stage is gone and the console's frame has not
    // mounted yet (App is lazy; SiteRoot's Suspense fallback is null). A nav
    // with no frame above it publishes nothing, so the rail keeps the
    // stage's derivation rather than flashing the empty fallback.
    rerender(<ContinuityStore search="?surface=cad"><ProductSurfaceTabs key="window" activeSurface="cad" states={STATES} onSelect={noop} /></ContinuityStore>)
    expect(rail(container).textContent).toContain('project · rooftop_demo')
    // Then it publishes, and the rail follows.
    rerender(<ContinuityStore search="?surface=cad"><Scene key="app" scene="console" surface="cad" data={DATA['empty+catalog']} /></ContinuityStore>)
    expect(rail(container).dataset.projectState).toBe('empty')
    expect(rail(container).textContent).toContain('2 families / 3 tools')
  })

  it('first paint before any publish is the honest-empty fallback, with the surface from the search string', () => {
    const snap = initialContinuitySnapshot('?surface=solar&demo=1')
    expect(snap).toEqual({ activeSurface: 'solar', workspaceProject: null, catalog: null, signedIn: false, onSignOut: null })
    expect(initialContinuitySnapshot('?surface=nonsense').activeSurface).toBe('cad')
    expect(initialContinuitySnapshot('').activeSurface).toBe('cad')
    // Rendered: the store alone, its host adopted by a bare nav that never publishes.
    const { container } = render(
      <ContinuityStore search="?surface=ios"><ProductSurfaceTabs activeSurface="ios" states={STATES} onSelect={noop} /></ContinuityStore>,
    )
    expect(rail(container).dataset.projectState).toBe('empty')
    expect(rail(container).textContent).toContain('no project open')
    expect(signOut(container)).toBeNull()
  })

  it('the sign-out is the same node across a surface switch and invokes the published handler', () => {
    const calls = []
    const onSignOut = () => calls.push(true)
    const { container, rerender } = render(
      <ContinuityStore search="?surface=browser"><Scene scene="stage" surface="browser" data={DATA['empty+catalog']} signedIn onSignOut={onSignOut} /></ContinuityStore>,
    )
    const btn = signOut(container)
    expect(btn).toBeTruthy()
    rerender(<ContinuityStore search="?surface=browser"><Scene scene="stage" surface="solar" data={DATA['empty+catalog']} signedIn onSignOut={onSignOut} /></ContinuityStore>)
    expect(signOut(container)).toBe(btn)
    fireEvent.click(btn)
    expect(calls).toEqual([true])
    // The console publishes no sign-out (App's header owns its own control),
    // so after the crossing the control is gone, as it was before the hoist.
    rerender(<ContinuityStore search="?surface=browser"><Scene key="app" scene="console" surface="solar" data={DATA['empty+catalog']} /></ContinuityStore>)
    expect(signOut(container)).toBeNull()
  })

  it('an unchanged publish does not replace the snapshot, and a malformed one is normalized, never thrown', () => {
    const a = normalizeContinuitySnapshot({ activeSurface: 'cad', workspaceProject: EMPTY_WORKSPACE_PROJECT, catalog: CATALOG, signedIn: true, onSignOut: noop })
    const b = normalizeContinuitySnapshot({ activeSurface: 'cad', workspaceProject: EMPTY_WORKSPACE_PROJECT, catalog: CATALOG, signedIn: true, onSignOut: noop })
    expect(sameContinuitySnapshot(a, b)).toBe(true)
    expect(sameContinuitySnapshot(a, { ...b, catalog: { families: [] } })).toBe(false)
    expect(sameContinuitySnapshot(a, null)).toBe(false)
    expect(normalizeContinuitySnapshot(null)).toEqual({ activeSurface: 'cad', workspaceProject: null, catalog: null, signedIn: false, onSignOut: null })
    expect(normalizeContinuitySnapshot({ activeSurface: 'sheets', workspaceProject: 'x', catalog: 7, signedIn: 'yes', onSignOut: 'nope' }))
      .toEqual({ activeSurface: 'cad', workspaceProject: null, catalog: null, signedIn: false, onSignOut: null })
    expect(Object.isFrozen(a)).toBe(true)
  })
})

describe('3. fails closed outside a store', () => {
  it('a tabs band rendered alone renders its tablist and nothing else', () => {
    const { container } = render(<ProductSurfaceTabs activeSurface="browser" states={STATES} onSelect={noop} />)
    expect(nav(container).children).toHaveLength(1)
    expect(nav(container).firstElementChild.className).toBe('tc-product-tabs')
    expect(rail(container)).toBeNull()
    expect(host(container)).toBeNull()
  })

  it('a frame rendered alone publishes into nothing and never throws', () => {
    expect(() => render(<Scene scene="console" surface="cad" data={DATA['empty+catalog']} />)).not.toThrow()
  })

  it('the host factory returns null with no document, and the store then renders children alone', () => {
    expect(createContinuityHost(null)).toBeNull()
    expect(createContinuityHost({})).toBeNull()
  })

  it('the store keeps a snapshot, never a controller (no session / catalog / engine import)', () => {
    for (const file of ['ContinuityStore.jsx', 'continuityStore.js']) {
      const imports = readFileSync(join(HERE, file), 'utf8').split('\n').filter((line) => line.startsWith('import '))
      expect(imports.length).toBeGreaterThan(0)
      for (const line of imports) {
        expect(line).not.toMatch(/useSessionController|createSessionController|useWorkspaceControllers|EngineSessionProvider|useCapabilityCatalog|controllers\//)
      }
    }
    // And SiteRoot mounts exactly ONE store, inside the identity provider,
    // above the scene ternary.
    const siteRoot = readFileSync(join(HERE, 'SiteRoot.jsx'), 'utf8')
    expect(siteRoot.match(/<ContinuityStore /g)).toHaveLength(1)
    const provider = siteRoot.indexOf('<DrawingIdentityProvider')
    const store = siteRoot.indexOf('<ContinuityStore ')
    const ternary = siteRoot.indexOf("{scene === 'app' ? (")
    expect(provider).toBeGreaterThan(-1)
    expect(store).toBeGreaterThan(provider)
    expect(ternary).toBeGreaterThan(store)
  })

  it('a publish after the store is gone is inert (the hook drops its handle with the provider)', () => {
    const { container, unmount } = render(
      <ContinuityStore search="?surface=cad"><Scene scene="stage" surface="cad" data={DATA['empty+catalog']} /></ContinuityStore>,
    )
    const before = rail(container)
    act(() => unmount())
    expect(before.isConnected).toBe(false)
  })
})
