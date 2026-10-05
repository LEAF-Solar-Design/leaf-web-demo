/**
 * DrawingIdentityProvider acceptance (convergence W1).
 *
 * Three things are proven here, in the order the frozen contract states them:
 *
 *  1. SEEDING reproduces each shell's PREVIOUS behavior exactly. Every
 *     expectation below is the value App.jsx's module constants or
 *     SiteRoot.jsx's `INITIAL_OPERATOR_DRAWING_ID` produced before the
 *     provider existed — the ladders are restated as data, not re-derived.
 *
 *  2. The `?demo` DUAL-CONSUMER note from the route matrix: "one reading must
 *     serve both consumers; add a test the first time this wiring is
 *     touched." Each row below reads its search string ONCE and drives BOTH
 *     the boot decision (the real bootWantsApp) and the drawing selection
 *     (the real seed) from that single reading, so a drift between them is a
 *     failing test rather than a silent split.
 *
 *  3. The SCOPE-RESET contract: no stale drawing id survives a project
 *     switch or close. Driven through the SHIPPED hook (useDrawingScopeReset)
 *     and the SHIPPED provider, not a restatement of them.
 *
 *  4. (panel W1) The provider SURVIVES the scene detours SiteRoot can take,
 *     and serves each MODE its own identity while doing so — driven through a
 *     model of SiteRoot's real three-branch shape around the real provider.
 *
 *  5. (panel W1) The TENANT half of the scope-reset contract: a principal
 *     switch clears EVERY mode's identity, including the boot seed behind a
 *     mode that has not activated yet.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { noteUnauthorized } from '../api.js'
import { bootWantsApp } from '../site/authBoot.js'
import { searchForProductSurface } from '../site/productSurfaces.js'
import { forgetLiveDrawingId, liveDrawingId, WORKBENCH_ID_KEY } from '../site/workbenchId.js'
import {
  DRAWING_MODE_CONSOLE,
  DRAWING_MODE_OPERATOR,
  authTenantScope,
  classifyDemo,
  drawingUrlAfterUpload,
  drawingUrlAfterReset,
  hasDrawingSelection,
  identityFromUploadReceipt,
  isScopeSwitch,
  modeDrawingId,
  readDrawingParam,
  seedDrawingIdentity,
} from './drawingIdentity.js'
import {
  DrawingIdentityProvider,
  useDrawingIdentity,
  useDrawingScopeReset,
} from './DrawingIdentityProvider.jsx'

afterEach(() => { cleanup(); sessionStorage.removeItem(WORKBENCH_ID_KEY) })

describe('accepted upload URL persistence', () => {
  let write, originalUrl, originalState
  const account = (id) => ({ drawing_id: id, tenant_kind: 'account' })
  const guest = (id) => ({ drawing_id: id, tenant_kind: 'guest' })
  const address = () => window.location.pathname + window.location.search + window.location.hash
  const mount = (url, options = {}) => {
    const { mode = DRAWING_MODE_CONSOLE, strict = false } = options
    const scene = Object.prototype.hasOwnProperty.call(options, 'scene') ? options.scene : 'app'
    originalUrl = address()
    originalState = window.history.state
    window.history.replaceState({ marker: 'preserved' }, '', url)
    write = vi.spyOn(window.history, 'replaceState')
    const props = { mode, scene, search: window.location.search, publicDemo: false, liveDemo: false,
      readLiveDrawingId: () => null, rememberDrawingId: vi.fn() }
    const tree = (over = {}) => {
      const provider = <DrawingIdentityProvider {...props} {...over}><ConsoleProbe /></DrawingIdentityProvider>
      return strict ? <React.StrictMode>{provider}</React.StrictMode> : provider
    }
    const view = render(tree())
    return { ...view, props, update: (over) => view.rerender(tree(over)) }
  }
  afterEach(() => {
    cleanup()
    write?.mockRestore()
    if (originalUrl !== undefined) window.history.replaceState(originalState, '', originalUrl)
    write = undefined
    originalUrl = undefined
  })
  const rows = [
    { id: 'URL307A-01', before: '/app?surface=browser', after: '/app?surface=browser', drawing: 'demo', origin: 'mode' },
    { id: 'URL307A-02', before: '/app?surface=browser', after: '/app?surface=browser&drawing=U', uploads: [account('U')], drawing: 'U' },
    { id: 'URL307A-03', before: '/app?surface=browser&drawing=U', after: '/app?surface=browser&drawing=V', uploads: [account('U'), account('V')], drawing: 'V' },
    { id: 'URL307A-04', before: '/app?drawing=V', after: '/app?drawing=U', uploads: [account('U')], drawing: 'U' },
    { id: 'URL307A-05', before: '/app?drawing=U&surface=browser', after: '/app?surface=browser', uploads: [guest('G')], drawing: 'G' },
    { id: 'URL307A-06', before: '/app?surface=browser', after: '/app?surface=browser', uploads: [guest('G')], drawing: 'G', calls: 0 },
    { id: 'URL307A-07', before: '/try', after: '/try', mode: DRAWING_MODE_OPERATOR, scene: 'tool', uploads: [account('U')], drawing: 'U', calls: 0 },
    { id: 'URL307A-08', before: '/app?demo=1', after: '/app?demo=1', drawing: 'demo', origin: 'mode' },
    { id: 'URL307A-09', before: '/app/leaf-platform', after: '/app/leaf-platform', scene: 'leaf-platform', uploads: [account('U')], drawing: 'U', calls: 0 },
    { id: 'URL307A-10', before: '/app?surface=browser&note=keep#viewer', after: '/app?surface=browser&note=keep&drawing=U#viewer', uploads: [account('U')], drawing: 'U' },
    { id: 'URL307A-15', before: '/app?surface=browser&drawing=U', after: '/app?surface=browser&drawing=U', uploads: [account('U'), account('U')], drawing: 'U', calls: 0 },
    { id: 'URL307A-16', before: '/app?drawing=rooftop_demo', after: '/app?drawing=rooftop_demo', drawing: 'demo', source: 'rooftop_demo', origin: 'query' },
    { id: 'URL307A-17', before: '/app?drawing=', after: '/app?drawing=', drawing: 'demo', origin: 'mode' },
    { id: 'URL307A-18', before: '/?drawing=V&surface=browser#viewer', after: '/?drawing=U&surface=browser#viewer', uploads: [account('U')], drawing: 'U' },
    { id: 'URL307A-19', before: '/try?drawing=V', after: '/try?drawing=U', uploads: [account('U')], drawing: 'U' },
    { id: 'URL307A-20', before: '/app?drawing=V&drawing=W&surface=browser', after: '/app?drawing=U&surface=browser', uploads: [account('U')], drawing: 'U' },
  ]
  for (const row of rows) {
    it(`${row.id} preserves the selected drawing and allowed address`, () => {
      const h = mount(row.before, row)
      const historyState = window.history.state
      expect(write).not.toHaveBeenCalled()
      if (!row.uploads) expect(shown('drawing-tenant')).toBe('null')
      if (row.id === 'URL307A-07') expect(shown('drawing-id')).toBe('null')
      for (const receipt of row.uploads || []) {
        let promoted
        act(() => { promoted = controls.setFromUpload(receipt) })
        expect(promoted).toEqual({ drawingId: receipt.drawing_id, source: receipt.drawing_id, origin: 'upload', tenantKind: receipt.tenant_kind })
        expect(Object.isFrozen(promoted)).toBe(true)
      }
      expect(address()).toBe(row.after)
      expect(shown('drawing-id')).toBe(row.drawing)
      expect(shown('drawing-source')).toBe(row.source || (row.drawing === 'demo' ? 'rooftop_demo' : row.drawing))
      expect(shown('drawing-origin')).toBe(row.origin || 'upload')
      expect(write).toHaveBeenCalledTimes(row.calls ?? (row.uploads ? 1 : 0))
      if (write.mock.calls.length) expect(write.mock.calls[0][0]).toBe(historyState)
      if (row.id === 'URL307A-07') expect(h.props.rememberDrawingId).toHaveBeenCalledWith('U')
    })
  }

  it('URL307A-14 query restoration, rerender and mode reactivation never write', () => {
    const h = mount('/app?surface=solar&drawing=U')
    act(() => { controls.setFromUpload(account('U')); controls.setFromQuery() })
    h.update({}); h.update({ mode: DRAWING_MODE_OPERATOR }); h.update({ mode: DRAWING_MODE_CONSOLE })
    expect(shown('drawing-id')).toBe('U')
    expect(shown('drawing-origin')).toBe('query')
    expect(address()).toBe('/app?surface=solar&drawing=U')
    expect(write).not.toHaveBeenCalled()
  })

  it('URL307A-21 rejected ids leave the query identity and address alone', () => {
    mount('/app?drawing=V')
    for (const drawing_id of [undefined, '', null, 42]) {
      act(() => { expect(controls.setFromUpload({ drawing_id, tenant_kind: 'account' })).toBeNull() })
      expect(shown('drawing-id')).toBe('V')
      expect(shown('drawing-origin')).toBe('query')
    }
    expect(address()).toBe('/app?drawing=V')
    expect(write).not.toHaveBeenCalled()
  })

  it('URL307A-22 the surface commit preserves the upload selection', () => {
    mount('/app?surface=browser&drawing=U&note=keep#viewer')
    act(() => { controls.setFromUpload(account('U')) })
    window.history.replaceState(window.history.state, '', window.location.pathname + searchForProductSurface(window.location.search, 'solar') + window.location.hash)
    expect(address()).toBe('/app?surface=solar&drawing=U&note=keep#viewer')
    expect(shown('drawing-id')).toBe('U')
    expect(shown('drawing-tenant')).toBe('account')
  })

  it('URL307A-23 unknown tenant provenance still promotes without a URL write', () => {
    mount('/app?drawing=V')
    for (const tenant_kind of ['enterprise', null, undefined]) {
      act(() => { controls.setFromUpload({ drawing_id: 'U', tenant_kind }) })
      expect(shown('drawing-id')).toBe('U')
      expect(shown('drawing-origin')).toBe('upload')
      expect(shown('drawing-tenant')).toBe('null')
    }
    expect(address()).toBe('/app?drawing=V')
    expect(write).not.toHaveBeenCalled()
  })

  it('URL307A-STRICTMODE mount and rerenders do not replay the synchronous commit', () => {
    const h = mount('/app?surface=browser', { strict: true })
    expect(write).not.toHaveBeenCalled()
    act(() => { controls.setFromUpload(account('U')) })
    expect(write).toHaveBeenCalledTimes(1)
    h.update({}); h.update({})
    expect(write).toHaveBeenCalledTimes(1)
    expect(address()).toBe('/app?surface=browser&drawing=U')
  })

  it('URL307A-SURFACE-COMPOSITION both commit orders read the current address', () => {
    mount('/app?note=keep&note=also#viewer')
    const surface = () => window.history.replaceState(window.history.state, '', window.location.pathname + searchForProductSurface(window.location.search, 'solar') + window.location.hash)
    act(() => { controls.setFromUpload(account('U')) }); surface()
    expect(address()).toBe('/app?note=keep&note=also&drawing=U&surface=solar#viewer')
    window.history.replaceState(window.history.state, '', '/app?note=keep&note=also#viewer')
    surface(); act(() => { controls.setFromUpload(account('U')) })
    expect(address()).toBe('/app?note=keep&note=also&surface=solar&drawing=U#viewer')
    expect(shown('drawing-id')).toBe('U')
  })

  it('URL307A-FROZEN-SEED upload leaves query restoration and its frozen seed unchanged', () => {
    mount('/app?drawing=V')
    let seed, restored
    act(() => { seed = controls.setFromQuery(); controls.setFromUpload(account('U')) })
    expect(write).toHaveBeenCalledTimes(1)
    act(() => { restored = controls.setFromQuery() })
    expect(restored).toBe(seed)
    expect(Object.isFrozen(seed)).toBe(true)
    expect(seed).toEqual({ drawingId: 'V', source: 'V', origin: 'query' })
    expect(shown('drawing-id')).toBe('V')
    expect(address()).toBe('/app?drawing=U')
    expect(write).toHaveBeenCalledTimes(1)
  })

  it('URL307A-HISTORY-THROWS a failed address commit keeps the promoted identity', () => {
    mount('/app?drawing=V')
    write.mockImplementation(() => { throw new Error('history unavailable') })
    act(() => { controls.setFromUpload(account('U')) })
    expect(shown('drawing-id')).toBe('U')
    expect(shown('drawing-tenant')).toBe('account')
    expect(address()).toBe('/app?drawing=V')
  })

  it('URL307A-PURE policy gates eligibility and avoids unrelated canonicalization', () => {
    expect(drawingUrlAfterUpload()).toBeNull()
    expect(drawingUrlAfterUpload(null)).toBeNull()
    const policy = (over) => drawingUrlAfterUpload({ href: 'https://example.test/app?note=a%20b', mode: DRAWING_MODE_CONSOLE, scene: 'app', receipt: account('U'), promoted: true, ...over })
    expect(policy({})).toBe('/app?note=a+b&drawing=U')
    for (const over of [{ promoted: false }, { promoted: 1 }, { mode: DRAWING_MODE_OPERATOR }, { scene: undefined }, { scene: 'leaf-platform' }, { href: 'not a URL' }, { receipt: null }, { receipt: { drawing_id: 3, tenant_kind: 'account' } }, { receipt: { drawing_id: 'U', tenant_kind: 'enterprise' } }]) expect(policy(over)).toBeNull()
    expect(policy({ href: 'https://example.test/app?drawing=U&note=a%20b' })).toBeNull()
    expect(policy({ receipt: guest('G') })).toBeNull()
    expect(policy({ href: 'https://example.test/app?drawing=V&note=a%20b&note=c&drawing=W#viewer', receipt: guest('G') })).toBe('/app?note=a+b&note=c#viewer')
    expect(policy({ receipt: { ...account('U'), status: 'extracting' } })).toBe('/app?note=a+b&drawing=U')
  })

  it('URL307A-OMITTED-SCENE retains the provider consumer default and uses the current scene', () => {
    const h = mount('/app?surface=browser', { scene: undefined })
    act(() => { controls.setFromUpload(account('U')) })
    expect(shown('drawing-id')).toBe('U')
    expect(write).not.toHaveBeenCalled()
    h.update({ scene: 'app' })
    act(() => { controls.setFromUpload(account('V')) })
    expect(address()).toBe('/app?surface=browser&drawing=V')
    expect(write).toHaveBeenCalledTimes(1)
  })
})

// A probe that renders the identity and exposes its mutators, so the tests
// drive the real provider rather than its internals.
const controls = {}
function readout(identity) {
  controls.scopeToken = identity.scopeToken
  controls.isScopeCurrent = identity.isScopeCurrent
  controls.setFromUpload = identity.setFromUpload
  controls.setFromQuery = identity.setFromQuery
  controls.reset = identity.reset
  controls.resetAll = identity.resetAll
  return (
    <>
      <span data-testid="drawing-id">{String(identity.drawingId)}</span>
      <span data-testid="drawing-source">{String(identity.source)}</span>
      <span data-testid="drawing-origin">{identity.origin}</span>
      <span data-testid="drawing-mode">{identity.mode}</span>
      <span data-testid="drawing-tenant">{String(identity.tenantKind)}</span>
    </>
  )
}

function Probe({ projectId = undefined }) {
  const identity = useDrawingIdentity()
  useDrawingScopeReset(projectId)
  return readout(identity)
}

// This probe isolates identity reads from the surfaces' project observation.
function ConsoleProbe() {
  return readout(useDrawingIdentity())
}

const shown = (testId) => screen.getByTestId(testId).textContent

describe('seeding: the console reproduces App.jsx\'s module constants', () => {
  it('defaults to the rooftop intake source addressed as the demo store drawing', () => {
    const identity = seedDrawingIdentity({ mode: DRAWING_MODE_CONSOLE, search: '' })
    expect(identity).toMatchObject({
      source: 'rooftop_demo',   // App's DRAWING_SOURCE
      drawingId: 'demo',        // App's REQUESTED_DRAWING_ID
      origin: 'mode',
    })
  })

  it('honours ?drawing= as the intake source and maps it to its store id', () => {
    expect(seedDrawingIdentity({ mode: DRAWING_MODE_CONSOLE, search: '?drawing=acceptance-7-a' }))
      .toMatchObject({ source: 'acceptance-7-a', drawingId: 'acceptance-7-a', origin: 'query' })
    // The one mapped source: `rooftop_demo` addresses the `demo` store drawing.
    expect(seedDrawingIdentity({ mode: DRAWING_MODE_CONSOLE, search: '?drawing=rooftop_demo' }))
      .toMatchObject({ source: 'rooftop_demo', drawingId: 'demo', origin: 'query' })
  })

  it('treats an EMPTY ?drawing= as no request, exactly as `get(...) || DEFAULT` did', () => {
    expect(readDrawingParam('?drawing=')).toBeNull()
    expect(seedDrawingIdentity({ mode: DRAWING_MODE_CONSOLE, search: '?drawing=' }))
      .toMatchObject({ source: 'rooftop_demo', drawingId: 'demo' })
  })

  it('is total on a malformed search rather than throwing into the boot path', () => {
    expect(() => seedDrawingIdentity({ mode: DRAWING_MODE_CONSOLE, search: '%%%' })).not.toThrow()
    expect(seedDrawingIdentity({ mode: DRAWING_MODE_CONSOLE, search: '%%%' }).drawingId).toBe('demo')
  })
})

describe('seeding: the stage reproduces SiteRoot\'s INITIAL_OPERATOR_DRAWING_ID', () => {
  const stage = (over) => seedDrawingIdentity({ mode: DRAWING_MODE_OPERATOR, search: '', ...over })

  it('selects cat-panels under the proof surface, ahead of every demo arm', () => {
    expect(stage({ proofMode: true, publicDemo: true, liveDemo: true }).drawingId).toBe('cat-panels')
  })

  it('selects demo for the anonymous public demo and rooftop_demo for the live tour', () => {
    expect(stage({ publicDemo: true }).drawingId).toBe('demo')
    expect(stage({ liveDemo: true }).drawingId).toBe('rooftop_demo')
  })

  it('falls back to the drawing a previous upload remembered for this session', () => {
    expect(stage({ liveId: 'acceptance-9-b' })).toMatchObject({
      drawingId: 'acceptance-9-b', source: 'acceptance-9-b', origin: 'stored',
    })
  })

  it('NEVER invents a drawing for an empty session (site/workbenchId.js rule)', () => {
    expect(stage({ liveId: null })).toMatchObject({ drawingId: null, source: null, origin: 'empty' })
  })

  it('applies NO store mapping on the stage — source and id have always matched there', () => {
    expect(stage({ liveDemo: true })).toMatchObject({ drawingId: 'rooftop_demo', source: 'rooftop_demo' })
    expect(modeDrawingId({ mode: DRAWING_MODE_OPERATOR, liveDemo: true })).toBe('rooftop_demo')
  })

  it('applies no mapping on the QUERY arm either (panel W1 NIT: code now says what the comment says)', () => {
    // `?drawing=` boots the CONSOLE on every path, so this pair is not
    // reachable through SiteRoot — which is exactly why the mismatch could
    // sit here unnoticed. The stage's invariant is unconditional: on the
    // operator stage source and drawingId are the same value, always.
    expect(stage({ search: '?drawing=rooftop_demo' }))
      .toMatchObject({ drawingId: 'rooftop_demo', source: 'rooftop_demo', origin: 'query' })
    // The console, unchanged: it still maps the named source to its store id.
    expect(seedDrawingIdentity({ mode: DRAWING_MODE_CONSOLE, search: '?drawing=rooftop_demo' }))
      .toMatchObject({ drawingId: 'demo', source: 'rooftop_demo', origin: 'query' })
  })
})

// ---------------------------------------------------------------------------
// The route matrix's `?demo` dual-consumer note, asserted at the provider seam.
// ---------------------------------------------------------------------------
describe('route matrix: ONE reading of the search serves both consumers', () => {
  // Every row states the search ONCE. `bootWantsApp` (the boot decision) and
  // `classifyDemo` -> `seedDrawingIdentity` (the drawing selection) both take
  // that same string, so a second, drifting reading cannot pass this suite.
  const ROWS = [
    // `/try?demo=1` boots the console, the one studio, which seeds its own
    // identity; `?demo=tour` on /try still stays on the stage.
    { search: '?demo=1', path: '/try', signedIn: false, boot: true, drawingId: 'demo', source: 'rooftop_demo' },
    { search: '?demo=1', path: '/try', signedIn: true, boot: true, drawingId: 'demo', source: 'rooftop_demo' },
    { search: '?demo=tour', path: '/try', signedIn: false, boot: false, drawingId: 'rooftop_demo', source: 'rooftop_demo' },
    // `?demo=` OFF /try boots the console, which seeds its own identity.
    { search: '?demo=1', path: '/', signedIn: false, boot: true, drawingId: 'demo', source: 'rooftop_demo' },
    { search: '?demo=1', path: '/app', signedIn: true, boot: true, drawingId: 'demo', source: 'rooftop_demo' },
    // `?drawing=` boots the console on ANY path and seeds the provider.
    { search: '?drawing=job-42', path: '/try', signedIn: false, boot: true, drawingId: 'job-42', source: 'job-42' },
    { search: '?drawing=job-42', path: '/', signedIn: false, boot: true, drawingId: 'job-42', source: 'job-42' },
    // `?fixture=` / `?dev=` boot the console with no drawing named.
    { search: '?fixture=1', path: '/try', signedIn: false, boot: true, drawingId: 'demo', source: 'rooftop_demo' },
    { search: '?dev=1', path: '/', signedIn: false, boot: true, drawingId: 'demo', source: 'rooftop_demo' },
    // `?ops=` off /try boots the console; on /try it stays operator.
    { search: '?ops=1', path: '/', signedIn: false, boot: true, drawingId: 'demo', source: 'rooftop_demo' },
    { search: '?ops=1', path: '/try', signedIn: false, boot: false, drawingId: null, source: null },
    // Plain /try: no drawing until an upload or a remembered id supplies one.
    { search: '', path: '/try', signedIn: true, boot: false, drawingId: null, source: null },
  ]

  for (const row of ROWS) {
    it(`${row.search || '(no query)'} on ${row.path}${row.signedIn ? ' signed in' : ''} -> ${row.boot ? 'console' : 'operator'} / ${row.drawingId}`, () => {
      const search = row.search // the ONE reading
      const bootsConsole = bootWantsApp(search, row.path)
      expect(bootsConsole).toBe(row.boot)

      const demo = classifyDemo(search, row.signedIn)
      const identity = seedDrawingIdentity({
        mode: bootsConsole ? DRAWING_MODE_CONSOLE : DRAWING_MODE_OPERATOR,
        search,
        publicDemo: demo.publicDemo,
        liveDemo: demo.liveDemo,
        liveId: null,
      })
      expect(identity.drawingId).toBe(row.drawingId)
      expect(identity.source).toBe(row.source)
    })
  }

  it('`?demo` is classified once and both flags follow from that single value', () => {
    expect(classifyDemo('?demo=1', false)).toMatchObject({ value: '1', publicDemo: true, liveDemo: false })
    expect(classifyDemo('?demo=1', true)).toMatchObject({ value: '1', publicDemo: false, liveDemo: true })
    expect(classifyDemo('?demo=tour', false)).toMatchObject({ value: 'tour', publicDemo: false, liveDemo: true })
    expect(classifyDemo('', false)).toMatchObject({ value: null, publicDemo: false, liveDemo: false })
  })
})

describe('the provider owns the identity for its mode', () => {
  it('serves the console seed to its subtree', () => {
    render(
      <DrawingIdentityProvider mode={DRAWING_MODE_CONSOLE} search="?drawing=rooftop_demo">
        <Probe />
      </DrawingIdentityProvider>,
    )
    expect(shown('drawing-id')).toBe('demo')
    expect(shown('drawing-source')).toBe('rooftop_demo')
    expect(shown('drawing-mode')).toBe('console')
  })

  it('promotes an upload receipt and remembers it only for an ACCOUNT tenant', () => {
    const rememberDrawingId = vi.fn()
    render(
      <DrawingIdentityProvider
        mode={DRAWING_MODE_OPERATOR}
        search=""
        publicDemo={false}
        liveDemo={false}
        readLiveDrawingId={() => null}
        rememberDrawingId={rememberDrawingId}
      >
        <Probe />
      </DrawingIdentityProvider>,
    )
    expect(shown('drawing-id')).toBe('null')

    act(() => { controls.setFromUpload({ drawing_id: 'guest-upload-1', tenant_kind: 'guest' }) })
    expect(shown('drawing-id')).toBe('guest-upload-1')
    expect(shown('drawing-origin')).toBe('upload')
    expect(shown('drawing-tenant')).toBe('guest')
    expect(rememberDrawingId).not.toHaveBeenCalled()

    act(() => { controls.setFromUpload({ drawing_id: 'account-upload-1', tenant_kind: 'account' }) })
    expect(shown('drawing-id')).toBe('account-upload-1')
    expect(shown('drawing-tenant')).toBe('account')
    expect(rememberDrawingId).toHaveBeenCalledWith('account-upload-1')
  })

  it('a receipt with no drawing id promotes NOTHING (the old early return)', () => {
    expect(identityFromUploadReceipt({ tenant_kind: 'account' })).toBeNull()
    expect(identityFromUploadReceipt(null)).toBeNull()
    expect(identityFromUploadReceipt({ drawing_id: 'U' }).tenantKind).toBeNull()
    render(
      <DrawingIdentityProvider mode={DRAWING_MODE_OPERATOR} search="" publicDemo liveDemo={false}>
        <Probe />
      </DrawingIdentityProvider>,
    )
    expect(shown('drawing-id')).toBe('demo')
    act(() => { controls.setFromUpload({ drawing_id: '' }) })
    expect(shown('drawing-id')).toBe('demo')
  })

  it('setFromQuery restores the identity this page load BOOTED with, not a later remembered id', () => {
    const readLiveDrawingId = vi.fn(() => 'boot-seeded')
    render(
      <DrawingIdentityProvider
        mode={DRAWING_MODE_OPERATOR}
        search=""
        publicDemo={false}
        liveDemo={false}
        readLiveDrawingId={readLiveDrawingId}
        rememberDrawingId={() => true}
      >
        <Probe />
      </DrawingIdentityProvider>,
    )
    expect(shown('drawing-id')).toBe('boot-seeded')
    act(() => { controls.setFromUpload({ drawing_id: 'uploaded-later', tenant_kind: 'account' }) })
    expect(shown('drawing-id')).toBe('uploaded-later')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('boot-seeded')
    // Read once at mount: re-seeding must not re-consult storage that a later
    // upload has since written, or a scope reset could be silently undone.
    expect(readLiveDrawingId).toHaveBeenCalledTimes(1)
  })

  it('refuses to serve a surface mounted without a provider', () => {
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<Probe />)).toThrow(/DrawingIdentityProvider/)
    quiet.mockRestore()
  })
})

// ---------------------------------------------------------------------------
// Scope-reset contract (binding).
// ---------------------------------------------------------------------------
describe('scope reset: no stale drawing id survives a project switch', () => {
  const mount = (projectId) => render(
    <DrawingIdentityProvider
      mode={DRAWING_MODE_OPERATOR}
      search=""
      publicDemo={false}
      liveDemo={false}
      readLiveDrawingId={() => null}
      rememberDrawingId={() => true}
    >
      <Probe projectId={projectId} />
    </DrawingIdentityProvider>,
  )

  it('clears the uploaded drawing when the open project SWITCHES', () => {
    const view = mount('project-a')
    act(() => { controls.setFromUpload({ drawing_id: 'project-a-drawing', tenant_kind: 'account' }) })
    expect(shown('drawing-id')).toBe('project-a-drawing')

    sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
    const oldUpload = controls.setFromUpload
    const oldQuery = controls.setFromQuery
    const oldCurrent = controls.isScopeCurrent
    view.rerender(
      <DrawingIdentityProvider
        mode={DRAWING_MODE_OPERATOR}
        search=""
        publicDemo={false}
        liveDemo={false}
        readLiveDrawingId={() => null}
        rememberDrawingId={() => true}
      >
        <Probe projectId="project-b" />
      </DrawingIdentityProvider>,
    )
    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-source')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')
    expect(oldCurrent()).toBe(false)
    expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
    act(() => {
      expect(oldUpload({ drawing_id: 'obsolete-upload', tenant_kind: 'account' })).toBeNull()
      expect(oldQuery()).toBeNull()
      expect(controls.setFromQuery().origin).toBe('reset')
    })
  })

  it('clears the uploaded drawing when the open project CLOSES', () => {
    const view = mount('project-a')
    act(() => { controls.setFromUpload({ drawing_id: 'project-a-drawing', tenant_kind: 'account' }) })
    sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
    const oldUpload = controls.setFromUpload
    const oldQuery = controls.setFromQuery
    const oldCurrent = controls.isScopeCurrent
    view.rerender(
      <DrawingIdentityProvider
        mode={DRAWING_MODE_OPERATOR}
        search=""
        publicDemo={false}
        liveDemo={false}
        readLiveDrawingId={() => null}
        rememberDrawingId={() => true}
      >
        <Probe projectId={null} />
      </DrawingIdentityProvider>,
    )
    expect(shown('drawing-id')).toBe('null')
    expect(oldCurrent()).toBe(false)
    expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
    act(() => {
      expect(oldUpload({ drawing_id: 'obsolete-upload', tenant_kind: 'account' })).toBeNull()
      expect(oldQuery()).toBeNull()
      expect(controls.setFromQuery().origin).toBe('reset')
    })
  })

  it('does NOT clear on the first project open — that is not a switch', () => {
    const view = mount(null)
    act(() => { controls.setFromUpload({ drawing_id: 'uploaded-before-open', tenant_kind: 'account' }) })
    sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
    const oldUpload = controls.setFromUpload
    const oldQuery = controls.setFromQuery
    const oldCurrent = controls.isScopeCurrent
    view.rerender(
      <DrawingIdentityProvider
        mode={DRAWING_MODE_OPERATOR}
        search=""
        publicDemo={false}
        liveDemo={false}
        readLiveDrawingId={() => null}
        rememberDrawingId={() => true}
      >
        <Probe projectId="project-a" />
      </DrawingIdentityProvider>,
    )
    expect(shown('drawing-id')).toBe('uploaded-before-open')
    expect(oldCurrent()).toBe(true)
    expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBe('u-upload')
    act(() => { expect(oldQuery()).not.toBeNull() })
  })

  it('the switch predicate itself: only a move AWAY from an open project counts', () => {
    expect(isScopeSwitch(null, 'p1')).toBe(false)
    expect(isScopeSwitch(undefined, 'p1')).toBe(false)
    expect(isScopeSwitch('p1', 'p1')).toBe(false)
    expect(isScopeSwitch('p1', 'p2')).toBe(true)
    expect(isScopeSwitch('p1', null)).toBe(true)
    expect(isScopeSwitch('p1', '')).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Panel W1 finding 1: the provider survives SiteRoot's scene detours, and a
// mode change serves that mode's OWN seed.
//
// SiteRootModel is the shipped shape, not a paraphrase of it: one provider at
// the root of all three arms, `mode` following the scene, the sheets arm
// rendering a sibling that does NOT read the identity (SheetsPage does not).
// Everything asserted comes from the REAL provider mounted underneath.
// ---------------------------------------------------------------------------
const NO_TOKEN = () => null
const NO_AUTH_EVENTS = () => () => {}

function SiteRootModel({ scene, projectId = undefined, search = '' }) {
  return (
    <DrawingIdentityProvider
      mode={scene === 'app' ? DRAWING_MODE_CONSOLE : DRAWING_MODE_OPERATOR}
      search={search}
      publicDemo={false}
      liveDemo={false}
      readLiveDrawingId={() => null}
      rememberDrawingId={() => true}
      readAuthToken={NO_TOKEN}
      subscribeAuthChange={NO_AUTH_EVENTS}
    >
      {scene === 'app' ? (
        <ConsoleProbe />
      ) : scene === 'sheets' ? (
        <span data-testid="sheets">sheets</span>
      ) : (
        <Probe projectId={projectId} />
      )}
    </DrawingIdentityProvider>
  )
}

describe('scene detours: an in-progress upload identity survives the round trip', () => {
  it('/try -> / -> /sheets -> /try keeps the guest upload the stage was holding', () => {
    const view = render(<SiteRootModel scene="tool" />)
    act(() => { controls.setFromUpload({ drawing_id: 'u-guest-in-progress', tenant_kind: 'guest' }) })
    expect(shown('drawing-id')).toBe('u-guest-in-progress')

    // The cover, then the sheets sibling — the arm that used to unmount the
    // provider outright and take the upload identity with it.
    view.rerender(<SiteRootModel scene="site" />)
    expect(shown('drawing-id')).toBe('u-guest-in-progress')
    view.rerender(<SiteRootModel scene="sheets" />)
    expect(screen.getByTestId('sheets')).toBeTruthy()

    view.rerender(<SiteRootModel scene="tool" />)
    expect(shown('drawing-id')).toBe('u-guest-in-progress')
    expect(shown('drawing-origin')).toBe('upload')
  })

  it('a mode switch serves the NEW mode its own fresh seed, never the previous mode\'s identity', () => {
    // The panel measured `console|null|null|empty` here: the seed was frozen
    // to whichever mode mounted first, so the console read the stage's empty
    // identity instead of its own default.
    const view = render(<SiteRootModel scene="tool" />)
    expect(shown('drawing-mode')).toBe('operator')
    expect(shown('drawing-id')).toBe('null')

    view.rerender(<SiteRootModel scene="app" />)
    expect(shown('drawing-mode')).toBe('console')
    expect(shown('drawing-id')).toBe('demo')            // App's REQUESTED_DRAWING_ID
    expect(shown('drawing-source')).toBe('rooftop_demo') // App's DRAWING_SOURCE
    expect(shown('drawing-origin')).toBe('mode')
  })

  it('each mode keeps its OWN identity across a switch and back', () => {
    const view = render(<SiteRootModel scene="tool" />)
    act(() => { controls.setFromUpload({ drawing_id: 'stage-upload', tenant_kind: 'guest' }) })
    expect(shown('drawing-id')).toBe('stage-upload')

    view.rerender(<SiteRootModel scene="app" />)
    expect(shown('drawing-id')).toBe('demo')
    act(() => { controls.setFromUpload({ drawing_id: 'console-upload', tenant_kind: 'guest' }) })
    expect(shown('drawing-id')).toBe('console-upload')

    // Back to the stage: its own upload, not the console's.
    view.rerender(<SiteRootModel scene="tool" />)
    expect(shown('drawing-id')).toBe('stage-upload')
    view.rerender(<SiteRootModel scene="app" />)
    expect(shown('drawing-id')).toBe('console-upload')
  })

  it('a project switch resets the ACTIVE mode only — a project lives inside one tenant', () => {
    const view = render(<SiteRootModel scene="app" />)
    act(() => { controls.setFromUpload({ drawing_id: 'console-upload', tenant_kind: 'guest' }) })
    view.rerender(<SiteRootModel scene="tool" projectId="project-a" />)
    act(() => { controls.setFromUpload({ drawing_id: 'stage-upload', tenant_kind: 'guest' }) })

    sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
    const oldUpload = controls.setFromUpload
    const oldQuery = controls.setFromQuery
    const oldCurrent = controls.isScopeCurrent
    view.rerender(<SiteRootModel scene="tool" projectId="project-b" />)
    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')

    expect(oldCurrent()).toBe(false)
    expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
    act(() => {
      expect(oldUpload({ drawing_id: 'obsolete-upload', tenant_kind: 'account' })).toBeNull()
      expect(oldQuery()).toBeNull()
      expect(controls.setFromQuery().origin).toBe('reset')
    })
    view.rerender(<SiteRootModel scene="app" />)
    expect(shown('drawing-id')).toBe('console-upload')
  })
})

// ---------------------------------------------------------------------------
// Panel W1 finding 2: the TENANT half of the scope-reset contract.
//
// SEAM (documented in DrawingIdentityProvider.jsx): the tenant this client
// speaks for is `X-Tenant-Id` (api.js's TENANT, a build-time constant fixed
// for the life of the document) plus the bearer PRINCIPAL, so the principal is
// the only client-observable tenant scope and a principal change is the tenant
// switch the contract names.
// ---------------------------------------------------------------------------
const jwtFor = (claims) => `h.${btoa(JSON.stringify(claims)).replace(/=+$/, '')}.sig`

describe('scope reset: no drawing identity survives a TENANT switch', () => {
  // A hand-driven principal: the test owns the token and the notification,
  // so the assertions are about the provider, not about localStorage.
  function principalHarness(initialToken) {
    const listeners = new Set()
    const state = { token: initialToken }
    return {
      state,
      read: () => state.token,
      subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener) },
      change(token) {
        state.token = token
        act(() => { listeners.forEach((listener) => listener()) })
      },
    }
  }

  const tree = (harness, scene, search = '') => (
    <DrawingIdentityProvider
      mode={scene === 'app' ? DRAWING_MODE_CONSOLE : DRAWING_MODE_OPERATOR}
      search={search}
      publicDemo={false}
      liveDemo={false}
      readLiveDrawingId={() => null}
      rememberDrawingId={() => true}
      readAuthToken={harness.read}
      subscribeAuthChange={harness.subscribe}
    >
      <Probe />
    </DrawingIdentityProvider>
  )

  it('clears EVERY mode\'s identity when the principal switches', () => {
    const harness = principalHarness(jwtFor({ sub: 'auth0|alice' }))
    const view = render(tree(harness, 'app'))
    act(() => { controls.setFromUpload({ drawing_id: 'alice-console', tenant_kind: 'account' }) })
    view.rerender(tree(harness, 'tool'))
    act(() => { controls.setFromUpload({ drawing_id: 'alice-stage', tenant_kind: 'account' }) })
    expect(shown('drawing-id')).toBe('alice-stage')

    sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
    const oldUpload = controls.setFromUpload
    const oldQuery = controls.setFromQuery
    const oldCurrent = controls.isScopeCurrent
    harness.change(jwtFor({ sub: 'auth0|bob' }))

    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')
    // The other mode too — not just whichever one happened to be active.
    view.rerender(tree(harness, 'app'))
    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')
    expect(oldCurrent()).toBe(false)
    expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
    act(() => {
      expect(oldUpload({ drawing_id: 'obsolete-upload', tenant_kind: 'account' })).toBeNull()
      expect(oldQuery()).toBeNull()
      expect(controls.setFromQuery().origin).toBe('reset')
    })
  })

  it('signing OUT is a scope exit: the account drawing does not outlive the principal', () => {
    const harness = principalHarness(jwtFor({ sub: 'auth0|alice' }))
    render(tree(harness, 'tool'))
    act(() => { controls.setFromUpload({ drawing_id: 'alice-stage', tenant_kind: 'account' }) })
    sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
    const oldUpload = controls.setFromUpload
    const oldQuery = controls.setFromQuery
    const oldCurrent = controls.isScopeCurrent
    harness.change(null)
    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')
    expect(oldCurrent()).toBe(false)
    expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
    act(() => {
      expect(oldUpload({ drawing_id: 'obsolete-upload', tenant_kind: 'account' })).toBeNull()
      expect(oldQuery()).toBeNull()
      expect(controls.setFromQuery().origin).toBe('reset')
    })
  })

  it('VOIDS the boot seed behind a mode that has not activated yet', () => {
    // The hole a per-mode map opens if the reset only touched live entries:
    // the console had never rendered, so its `?drawing=` seed would have been
    // waiting to hand the NEXT principal the previous one's drawing.
    const harness = principalHarness(jwtFor({ sub: 'auth0|alice' }))
    const view = render(tree(harness, 'tool', '?drawing=alice-only'))
    expect(shown('drawing-id')).toBe('alice-only')

    sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
    const oldUpload = controls.setFromUpload
    const oldQuery = controls.setFromQuery
    const oldCurrent = controls.isScopeCurrent
    harness.change(jwtFor({ sub: 'auth0|bob' }))
    expect(shown('drawing-id')).toBe('null')

    view.rerender(tree(harness, 'app', '?drawing=alice-only'))
    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')
    // And setFromQuery cannot smuggle it back either.
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('null')
    expect(oldCurrent()).toBe(false)
    expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
    act(() => {
      expect(oldUpload({ drawing_id: 'obsolete-upload', tenant_kind: 'account' })).toBeNull()
      expect(oldQuery()).toBeNull()
      expect(controls.setFromQuery().origin).toBe('reset')
    })
  })

  it('the FIRST principal observation is not a switch (signing in adopts what the guest built)', () => {
    const harness = principalHarness(null)
    render(tree(harness, 'tool'))
    act(() => { controls.setFromUpload({ drawing_id: 'u-guest-1', tenant_kind: 'guest' }) })
    harness.change(jwtFor({ sub: 'auth0|alice' }))
    expect(shown('drawing-id')).toBe('u-guest-1')
  })

  it('the scope key is the SUBJECT, so a same-subject re-mint is not a switch', () => {
    const harness = principalHarness(jwtFor({ sub: 'auth0|alice', exp: 1 }))
    render(tree(harness, 'tool'))
    act(() => { controls.setFromUpload({ drawing_id: 'alice-stage', tenant_kind: 'account' }) })
    harness.change(jwtFor({ sub: 'auth0|alice', exp: 2 }))
    expect(shown('drawing-id')).toBe('alice-stage')
  })

  // The three cases above drive INJECTED seams. These two drive the SHIPPED
  // defaults, so the wiring itself is proven and not just the state machine.
  const shippedTree = (search = '') => (
    <DrawingIdentityProvider
      mode={DRAWING_MODE_OPERATOR}
      search={search}
      publicDemo={false}
      liveDemo={false}
      readLiveDrawingId={() => null}
      rememberDrawingId={() => true}
    >
      <Probe />
    </DrawingIdentityProvider>
  )

  it('SHIPPED seam: a sign-out in another tab (storage event) resets the identity', () => {
    const token = jwtFor({ sub: 'auth0|alice' })
    try {
      localStorage.setItem('leaf.jwt', token)
      render(shippedTree())
      sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
      const oldUpload = controls.setFromUpload
      const oldQuery = controls.setFromQuery
      const oldCurrent = controls.isScopeCurrent
      act(() => { controls.setFromUpload({ drawing_id: 'alice-stage', tenant_kind: 'account' }) })
      expect(shown('drawing-id')).toBe('alice-stage')

      act(() => {
        localStorage.removeItem('leaf.jwt')
        window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' }))
      })
      expect(shown('drawing-id')).toBe('null')
      expect(shown('drawing-origin')).toBe('reset')
      expect(oldCurrent()).toBe(false)
      expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
      act(() => {
        expect(oldUpload({ drawing_id: 'obsolete-upload', tenant_kind: 'account' })).toBeNull()
        expect(oldQuery()).toBeNull()
        expect(controls.setFromQuery().origin).toBe('reset')
      })
    } finally {
      localStorage.removeItem('leaf.jwt')
    }
  })

  it('SHIPPED seam: the transport proving the token dead (api.js 401 wipe) resets the identity', () => {
    const token = jwtFor({ sub: 'auth0|alice' })
    try {
      localStorage.setItem('leaf.jwt', token)
      render(shippedTree())
      sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
      const oldUpload = controls.setFromUpload
      const oldQuery = controls.setFromQuery
      const oldCurrent = controls.isScopeCurrent
      act(() => { controls.setFromUpload({ drawing_id: 'alice-stage', tenant_kind: 'account' }) })
      expect(shown('drawing-id')).toBe('alice-stage')

      // The REAL api.js channel: a 401 whose request carried the stored token.
      act(() => { noteUnauthorized({ status: 401 }, '/api/session', `Bearer ${token}`) })
      expect(shown('drawing-id')).toBe('null')
      expect(shown('drawing-origin')).toBe('reset')
      expect(oldCurrent()).toBe(false)
      expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
      act(() => {
        expect(oldUpload({ drawing_id: 'obsolete-upload', tenant_kind: 'account' })).toBeNull()
        expect(oldQuery()).toBeNull()
        expect(controls.setFromQuery().origin).toBe('reset')
      })
    } finally {
      localStorage.removeItem('leaf.jwt')
    }
  })

  it('the key derivation itself: subject+org, total, and fail-CLOSED on an opaque token', () => {
    expect(authTenantScope(null)).toBeNull()
    expect(authTenantScope('')).toBeNull()
    expect(authTenantScope(jwtFor({ sub: 'auth0|alice' })))
      .toBe(authTenantScope(jwtFor({ sub: 'auth0|alice', exp: 99 })))
    expect(authTenantScope(jwtFor({ sub: 'auth0|alice', org_id: 'org_1' })))
      .not.toBe(authTenantScope(jwtFor({ sub: 'auth0|alice', org_id: 'org_2' })))
    // Undecodable: its own scope, so two different opaque tokens compare
    // unequal and a switch is over-detected rather than missed.
    expect(() => authTenantScope('not-a-jwt')).not.toThrow()
    expect(authTenantScope('not-a-jwt')).not.toBe(authTenantScope('also-not-a-jwt'))
  })
})

describe('durable scope reset', () => {
  const account = (id) => ({ drawing_id: id, tenant_kind: 'account' })
  const address = () => window.location.pathname + window.location.search + window.location.hash
  let originalAddress, originalState, originalStored, write
  afterEach(() => {
    cleanup()
    write?.mockRestore()
    if (originalAddress !== undefined) window.history.replaceState(originalState, '', originalAddress)
    if (originalStored == null) sessionStorage.removeItem(WORKBENCH_ID_KEY)
    else sessionStorage.setItem(WORKBENCH_ID_KEY, originalStored)
    originalAddress = undefined
    write = undefined
  })
  function mountScope({ url = '/app?drawing=u-upload', mode = DRAWING_MODE_CONSOLE, scene = 'app',
    projectId = 'p', strict = false, stored = 'u-upload', token = jwtFor({ sub: 'principal-a', org_id: 'org-a' }),
    forgetDrawingId } = {}) {
    originalAddress = address()
    originalState = window.history.state
    originalStored = sessionStorage.getItem(WORKBENCH_ID_KEY)
    window.history.replaceState({ retained: true }, '', url)
    if (stored == null) sessionStorage.removeItem(WORKBENCH_ID_KEY)
    else sessionStorage.setItem(WORKBENCH_ID_KEY, stored)
    write = vi.spyOn(window.history, 'replaceState')
    const listeners = new Set()
    const auth = { token }
    let props = { mode, scene, search: window.location.search, publicDemo: false, liveDemo: false,
      readAuthToken: () => auth.token,
      subscribeAuthChange: (listener) => { listeners.add(listener); return () => listeners.delete(listener) },
      ...(forgetDrawingId ? { forgetDrawingId } : {}) }
    let project = projectId
    const tree = () => {
      const body = <DrawingIdentityProvider {...props}><Probe projectId={project} /></DrawingIdentityProvider>
      return strict ? <React.StrictMode>{body}</React.StrictMode> : body
    }
    const view = render(tree())
    return {
      update(over = {}, nextProject = project) { props = { ...props, ...over }; project = nextProject; view.rerender(tree()) },
      changePrincipal(next, inspect) {
        auth.token = next
        act(() => { listeners.forEach((listener) => listener()); inspect?.() })
      },
      fresh() { return seedDrawingIdentity({ mode: props.mode, search: window.location.search,
        liveId: liveDrawingId(), publicDemo: false, liveDemo: false }) },
    }
  }
  function expectReset(h, expectedAddress = '/app') {
    expect(address()).toBe(expectedAddress)
    expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-source')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')
    act(() => { expect(controls.setFromQuery()).toEqual({ drawingId: null, source: null, origin: 'reset' }) })
    expect(h.fresh()).toMatchObject({ drawingId: 'demo', source: 'rooftop_demo', origin: 'mode' })
  }

  it('URL307B-01 first project open keeps U', () => {
    const h = mountScope({ projectId: null })
    const token = controls.scopeToken
    h.update({}, 'p')
    expect(shown('drawing-id')).toBe('u-upload')
    expect(controls.scopeToken).toBe(token)
    expect(address()).toBe('/app?drawing=u-upload')
    expect(liveDrawingId()).toBe('u-upload')
    act(() => { controls.setFromQuery() })
    expect(h.fresh().drawingId).toBe('u-upload')
    expect(write).not.toHaveBeenCalled()
  })

  it('URL307B-02 switching projects clears U', () => {
    const h = mountScope()
    const current = controls.isScopeCurrent
    h.update({}, 'q')
    expect(current()).toBe(false)
    expectReset(h)
    expect(write).toHaveBeenCalledExactlyOnceWith({ retained: true }, '', '/app')
  })

  it('URL307B-03 closing a project clears U', () => {
    const h = mountScope()
    h.update({}, null)
    expectReset(h)
  })

  it('URL307B-04 principal switch clears every mode', () => {
    const h = mountScope()
    h.update({ mode: DRAWING_MODE_OPERATOR })
    act(() => { controls.setFromUpload(account('v-upload')) })
    const operatorCurrent = controls.isScopeCurrent
    h.update({ mode: DRAWING_MODE_CONSOLE })
    const consoleCurrent = controls.isScopeCurrent
    const oldUpload = controls.setFromUpload
    const oldQuery = controls.setFromQuery
    h.changePrincipal(jwtFor({ sub: 'principal-b', org_id: 'org-a' }), () => {
      expect(operatorCurrent()).toBe(false)
      expect(consoleCurrent()).toBe(false)
      expect(liveDrawingId()).toBeNull()
      expect(address()).toBe('/app')
      expect(oldUpload(account('u-upload'))).toBeNull()
      expect(oldQuery()).toBeNull()
    })
    expect(operatorCurrent()).toBe(false)
    expect(consoleCurrent()).toBe(false)
    expectReset(h)
    h.update({ mode: DRAWING_MODE_OPERATOR })
    expect(shown('drawing-origin')).toBe('reset')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('null')
  })

  it('URL307B-07 root reset retains the console and surface', () => {
    const h = mountScope({ url: '/?drawing=u-upload&surface=solar' })
    h.update({}, 'q')
    expectReset(h, '/app?surface=solar')
  })

  it('URL307B-08 try reset retains the console', () => {
    const h = mountScope({ url: '/try?drawing=u-upload' })
    h.update({}, 'q')
    expectReset(h)
  })

  it('URL307B-09 reset preserves the explicit demo address', () => {
    const h = mountScope({ url: '/app?demo=1', stored: null })
    expect(shown('drawing-id')).toBe('demo')
    h.update({}, 'q')
    expectReset(h, '/app?demo=1')
    expect(write).not.toHaveBeenCalled()
  })

  it('URL307B-10 late upload cannot cross reset', () => {
    const h = mountScope({ url: '/app?drawing=v-upload', stored: 'v-upload' })
    const oldUpload = controls.setFromUpload
    act(() => {
      controls.reset()
      expect(oldUpload(account('u-upload'))).toBeNull()
    })
    expectReset(h)
    expect(write).toHaveBeenCalledTimes(1)
  })

  it('URL307B-13 operator upload remains URL neutral', () => {
    const h = mountScope({ url: '/try', mode: DRAWING_MODE_OPERATOR, scene: 'tool', stored: null })
    expect(shown('drawing-origin')).toBe('empty')
    act(() => { controls.setFromUpload(account('u-upload')) })
    expect(address()).toBe('/try')
    expect(liveDrawingId()).toBe('u-upload')
    expect(h.fresh()).toMatchObject({ drawingId: 'u-upload', origin: 'stored' })
    expect(shown('drawing-id')).toBe('u-upload')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-origin')).toBe('empty')
    expect(write).not.toHaveBeenCalled()
  })

  it('URL307B-14 host bridge remains URL neutral', () => {
    const h = mountScope({ url: '/app/leaf-platform', scene: 'leaf-platform', stored: null })
    act(() => { controls.setFromUpload(account('u-upload')) })
    expect(shown('drawing-id')).toBe('u-upload')
    expect(address()).toBe('/app/leaf-platform')
    expect(liveDrawingId()).toBe('u-upload')
    expect(h.fresh().drawingId).toBe('demo')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('demo')
    expect(write).not.toHaveBeenCalled()
    expect(drawingUrlAfterReset({ href: window.location.href, mode: DRAWING_MODE_CONSOLE, scene: 'leaf-platform' })).toBeNull()
  })

  it('URL307B-15 StrictMode resets once without replaying persistence', () => {
    const h = mountScope({ strict: true, projectId: null })
    h.update({}, 'p')
    expect(liveDrawingId()).toBe('u-upload')
    h.update({}, 'q')
    h.update()
    act(() => { controls.reset() })
    expectReset(h)
    expect(write).toHaveBeenCalledTimes(1)
    expect(controls.isScopeCurrent()).toBe(true)
  })

  it('URL307B-16 failed history replacement still invalidates the mounted scope', () => {
    const h = mountScope()
    const current = controls.isScopeCurrent
    write.mockImplementation(() => { throw new Error('History unavailable') })
    act(() => { controls.reset() })
    expect(current()).toBe(false)
    expect(liveDrawingId()).toBeNull()
    expect(shown('drawing-origin')).toBe('reset')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('null')
    expect(address()).toBe('/app?drawing=u-upload')
    expect(h.fresh()).toMatchObject({ drawingId: 'u-upload', origin: 'query' })
  })

  it('URL307B-17 project reset cannot reseed the active mode', () => {
    const h = mountScope()
    h.update({ mode: DRAWING_MODE_OPERATOR })
    act(() => { controls.setFromUpload(account('v-upload')) })
    h.update({ mode: DRAWING_MODE_CONSOLE })
    act(() => { controls.reset() })
    expectReset(h)
    h.update({ mode: DRAWING_MODE_OPERATOR })
    expect(shown('drawing-id')).toBe('v-upload')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('u-upload')
    h.update({ mode: DRAWING_MODE_CONSOLE })
    expectReset(h)
  })

  it('URL307B-18 tenant reset voids an unactivated mode', () => {
    const h = mountScope()
    act(() => { controls.resetAll() })
    expectReset(h)
    h.update({ mode: DRAWING_MODE_OPERATOR })
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')
  })

  it('URL307B-19 token refresh preserves the selection', () => {
    const h = mountScope()
    const token = controls.scopeToken
    h.changePrincipal(jwtFor({ sub: 'principal-a', org_id: 'org-a', exp: 2 }))
    expect(controls.scopeToken).toBe(token)
    expect(shown('drawing-id')).toBe('u-upload')
    expect(liveDrawingId()).toBe('u-upload')
    expect(address()).toBe('/app?drawing=u-upload')
    act(() => { controls.setFromQuery() })
    expect(h.fresh().drawingId).toBe('u-upload')
    expect(write).not.toHaveBeenCalled()
  })

  it('URL307B-20 first sign-in preserves the transient guest', () => {
    const h = mountScope({ url: '/app', stored: null, token: null })
    act(() => { controls.setFromUpload({ drawing_id: 'g-upload', tenant_kind: 'guest' }) })
    const token = controls.scopeToken
    h.changePrincipal(jwtFor({ sub: 'principal-a', org_id: 'org-a' }))
    expect(controls.scopeToken).toBe(token)
    expect(shown('drawing-id')).toBe('g-upload')
    expect(address()).toBe('/app')
    expect(liveDrawingId()).toBeNull()
    expect(h.fresh().drawingId).toBe('demo')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('demo')
    expect(write).not.toHaveBeenCalled()
  })

  it('URL307B-22 a new selection works after reset', () => {
    const h = mountScope()
    const oldCurrent = controls.isScopeCurrent
    const oldUpload = controls.setFromUpload
    act(() => { controls.reset() })
    expectReset(h)
    act(() => { controls.reset() })
    expect(controls.isScopeCurrent()).toBe(true)
    act(() => { controls.setFromUpload(account('u-upload')) })
    expect(oldCurrent()).toBe(false)
    act(() => { expect(oldUpload(account('obsolete-upload'))).toBeNull() })
    expect(shown('drawing-id')).toBe('u-upload')
    act(() => { controls.setFromUpload(account('v-upload')) })
    expect(oldCurrent()).toBe(false)
    expect(address()).toBe('/app?drawing=v-upload')
    expect(liveDrawingId()).toBe('v-upload')
    expect(shown('drawing-id')).toBe('v-upload')
    expect(h.fresh().drawingId).toBe('v-upload')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-origin')).toBe('reset')
    expect(address()).toBe('/app?drawing=v-upload')
  })

  it('URL307B-23 reset removes duplicate selections and preserves the rest', () => {
    const h = mountScope({ url: '/?drawing=u-upload&surface=solar&drawing=v-upload&tag=a&tag=b#viewer' })
    act(() => { controls.reset() })
    expectReset(h, '/app?surface=solar&tag=a&tag=b#viewer')
  })

  it('URL307B-24 another boot override preserves the original path', () => {
    const h = mountScope({ url: '/try?drawing=u-upload&dev=1#viewer' })
    act(() => { controls.reset() })
    expectReset(h, '/try?dev=1#viewer')
  })

  it('URL307B-26 forgetting unavailable storage never aborts reset', () => {
    const values = new Map([[WORKBENCH_ID_KEY, 'u-upload'], ['other', 'kept']])
    const working = { sessionStorage: { removeItem: (key) => values.delete(key) } }
    expect(forgetLiveDrawingId(working)).toBe(true)
    expect(values.has(WORKBENCH_ID_KEY)).toBe(false)
    expect(values.get('other')).toBe('kept')
    expect(forgetLiveDrawingId(working)).toBe(true)
    expect(forgetLiveDrawingId({})).toBe(false)
    const failing = { sessionStorage: { removeItem: () => { throw new Error('Storage unavailable') } } }
    expect(forgetLiveDrawingId(failing)).toBe(false)
    const h = mountScope({ forgetDrawingId: () => forgetLiveDrawingId(failing) })
    const current = controls.isScopeCurrent
    act(() => { controls.reset() })
    expect(current()).toBe(false)
    expect(address()).toBe('/app')
    expect(liveDrawingId()).toBe('u-upload')
    expect(shown('drawing-origin')).toBe('reset')
    act(() => { controls.setFromQuery() })
    expect(shown('drawing-id')).toBe('null')
    expect(h.fresh().drawingId).toBe('demo')
  })

  it('URL307B-27 a retained query setter cannot restore the old seed', () => {
    const h = mountScope()
    const oldQuery = controls.setFromQuery
    act(() => {
      controls.reset()
      expect(oldQuery()).toBeNull()
    })
    expectReset(h)
  })

  it('URL307B-29 empty drawing parameter is removed without an empty replacement', () => {
    const h = mountScope({ url: '/?drawing=&surface=solar' })
    act(() => { controls.reset() })
    expectReset(h, '/app?surface=solar')
    expect(new URLSearchParams(window.location.search).has('drawing')).toBe(false)
    expect(hasDrawingSelection()).toBe(false)
    expect(hasDrawingSelection({ drawingId: '', source: 'source' })).toBe(false)
    expect(hasDrawingSelection({ drawingId: 'id', source: null })).toBe(false)
    expect(hasDrawingSelection({ drawingId: 'Raw ID', source: 'source' })).toBe(true)
    expect(drawingUrlAfterReset({ href: 'malformed', mode: DRAWING_MODE_CONSOLE, scene: 'app' })).toBeNull()
    expect(drawingUrlAfterReset({ href: window.location.href, mode: DRAWING_MODE_CONSOLE, scene: 'app' })).toBeNull()
  })
})
