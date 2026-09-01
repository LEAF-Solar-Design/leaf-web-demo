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
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { bootWantsApp } from '../site/authBoot.js'
import {
  DRAWING_MODE_CONSOLE,
  DRAWING_MODE_OPERATOR,
  classifyDemo,
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

afterEach(cleanup)

// A probe that renders the identity and exposes its mutators, so the tests
// drive the real provider rather than its internals.
const controls = {}
function readout(identity) {
  controls.setFromUpload = identity.setFromUpload
  controls.setFromQuery = identity.setFromQuery
  controls.reset = identity.reset
  return (
    <>
      <span data-testid="drawing-id">{String(identity.drawingId)}</span>
      <span data-testid="drawing-source">{String(identity.source)}</span>
      <span data-testid="drawing-origin">{identity.origin}</span>
      <span data-testid="drawing-mode">{identity.mode}</span>
    </>
  )
}

function Probe({ projectId = undefined }) {
  const identity = useDrawingIdentity()
  useDrawingScopeReset(projectId)
  return readout(identity)
}

// The console arm's consumer. App.jsx READS the identity and mounts no
// project-scope hook (ToolCast owns the project selection), so the model must
// not mount one either — and it is a different component type, which is what
// makes React unmount the stage's consumer on a scene change exactly as
// SiteRoot does.
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
    // `?demo=` ON /try stays on the stage — and picks the stage's drawing.
    { search: '?demo=1', path: '/try', signedIn: false, boot: false, drawingId: 'demo', source: 'demo' },
    { search: '?demo=1', path: '/try', signedIn: true, boot: false, drawingId: 'rooftop_demo', source: 'rooftop_demo' },
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
    expect(rememberDrawingId).not.toHaveBeenCalled()

    act(() => { controls.setFromUpload({ drawing_id: 'account-upload-1', tenant_kind: 'account' }) })
    expect(shown('drawing-id')).toBe('account-upload-1')
    expect(rememberDrawingId).toHaveBeenCalledWith('account-upload-1')
  })

  it('a receipt with no drawing id promotes NOTHING (the old early return)', () => {
    expect(identityFromUploadReceipt({ tenant_kind: 'account' })).toBeNull()
    expect(identityFromUploadReceipt(null)).toBeNull()
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
  })

  it('clears the uploaded drawing when the open project CLOSES', () => {
    const view = mount('project-a')
    act(() => { controls.setFromUpload({ drawing_id: 'project-a-drawing', tenant_kind: 'account' }) })
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
  })

  it('does NOT clear on the first project open — that is not a switch', () => {
    const view = mount(null)
    act(() => { controls.setFromUpload({ drawing_id: 'uploaded-before-open', tenant_kind: 'account' }) })
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
function SiteRootModel({ scene, projectId = undefined, search = '' }) {
  return (
    <DrawingIdentityProvider
      mode={scene === 'app' ? DRAWING_MODE_CONSOLE : DRAWING_MODE_OPERATOR}
      search={search}
      publicDemo={false}
      liveDemo={false}
      readLiveDrawingId={() => null}
      rememberDrawingId={() => true}
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

    view.rerender(<SiteRootModel scene="tool" projectId="project-b" />)
    expect(shown('drawing-id')).toBe('null')
    expect(shown('drawing-origin')).toBe('reset')

    view.rerender(<SiteRootModel scene="app" />)
    expect(shown('drawing-id')).toBe('console-upload')
  })
})
