/**
 * Card F-7 acceptance: every surface's own frame content consumes the LIVE
 * tenant capability catalog — the same fold the CAD rail renders — never
 * hardcoded capability strings. The load-bearing assertion is change
 * detection: when the tenant catalog changes, each tab's rendered content
 * changes with it.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import ProductSurfaceTabs, { ProductSurfaceFrame } from './ProductSurfaceTabs.jsx'
import { productSurfaceStates } from '../site/productSurfaces.js'
import { deriveWorkspaceProjectState } from '../site/workspaceProjectState.js'

afterEach(cleanup)

const states = productSurfaceStates({ sessionActive: true, hasDrawing: true, apsLive: true, iosReady: false })

const catalogA = {
  families: [
    { family_id: 'measurement', label: 'Measurement', capabilities: [{ name: 'count-by-layer', label: 'Count by layer' }] },
    { family_id: 'custom', label: 'Custom authored tools', capabilities: [{ name: 'roof-pitch', label: 'Roof pitch' }] },
    { family_id: 'stringing', label: 'Stringing', capabilities: [{ name: 'string-autofill-opt', label: 'String autofill' }] },
  ],
}
const catalogB = {
  families: [
    { family_id: 'measurement', label: 'Measurement', capabilities: [{ name: 'measure-panel-area', label: 'Panel area' }] },
    { family_id: 'custom', label: 'Custom authored tools', capabilities: [{ name: 'setback-check', label: 'Setback check' }] },
    { family_id: 'stringing', label: 'Stringing', capabilities: [{ name: 'autofill-string-targets', label: 'String targets' }] },
  ],
}

function frame(surface, catalog, catalogError = null) {
  return (
    <ProductSurfaceFrame
      activeSurface={surface}
      states={states}
      catalog={catalog}
      catalogError={catalogError}
      projectSlot={<span>slot</span>}
      onOpenCad={() => {}}
    />
  )
}

describe('F-7: surface frames render the live tenant catalog', () => {
  it.each(['browser', 'solar', 'ios'])('%s content changes when the tenant catalog changes', (surface) => {
    const { rerender } = render(frame(surface, catalogA))
    const live = screen.getByTestId('surface-capabilities-live')
    const before = live.textContent
    expect(before).toContain('live tenant catalog')
    rerender(frame(surface, catalogB))
    const after = screen.getByTestId('surface-capabilities-live').textContent
    expect(after).not.toBe(before)
  })

  it('solar features the stringing and placement families, not the whole catalog', () => {
    render(frame('solar', catalogA))
    const live = screen.getByTestId('surface-capabilities-live')
    expect(live.textContent).toContain('String autofill')
    expect(live.textContent).not.toContain('Count by layer')
  })

  it('ios presents the whole tenant catalog (a build ships the full tool set)', () => {
    render(frame('ios', catalogA))
    const live = screen.getByTestId('surface-capabilities-live')
    expect(live.textContent).toContain('Count by layer')
    expect(live.textContent).toContain('String autofill')
  })

  it('an empty featured set says so honestly and still reports the live catalog', () => {
    const noSolar = { families: [
      { family_id: 'measurement', label: 'Measurement', capabilities: [{ name: 'count-by-layer', label: 'Count by layer' }] },
    ] }
    render(frame('solar', noSolar))
    const live = screen.getByTestId('surface-capabilities-live')
    expect(live.textContent).toContain('No stringing or placement tools are registered')
    expect(live.textContent).toContain('1 family · 1 capabilities live')
  })

  it('a catalog error degrades to an honest note, never a fake list', () => {
    render(frame('browser', null, 'families unavailable'))
    expect(screen.getByTestId('surface-capabilities-error').textContent).toContain('families unavailable')
    expect(screen.queryByTestId('surface-capabilities-live')).toBeNull()
  })

  it('an absent catalog renders a loading state, not hardcoded capabilities', () => {
    render(frame('browser', null))
    expect(screen.getByTestId('surface-capabilities-loading')).toBeTruthy()
    expect(screen.queryByTestId('surface-capabilities-live')).toBeNull()
  })

  it('F-8: the continuity rail is the SAME node across profile switches, and pulses instead of remounting', () => {
    const openProject = deriveWorkspaceProjectState({
      openProjectId: 'p-1', projectName: 'rooftop_demo', orgId: 'org-1',
    })
    const nav = (surface) => (
      <ProductSurfaceTabs
        activeSurface={surface}
        states={states}
        onSelect={() => {}}
        workspaceProject={openProject}
        catalog={catalogA}
      />
    )
    const { rerender } = render(nav('browser'))
    const rail = screen.getByTestId('continuity-rail')
    expect(rail.dataset.pulse).toBe('false')
    expect(rail.textContent).toContain('rooftop_demo')
    expect(rail.textContent).toContain('3 families / 3 tools')
    rerender(nav('solar'))
    expect(screen.getByTestId('continuity-rail')).toBe(rail)
    expect(rail.dataset.pulse).toBe('true')
  })

  it('F-8: the rail renders only live data — no catalog, no counts', () => {
    render(
      <ProductSurfaceTabs activeSurface="browser" states={states} onSelect={() => {}} />,
    )
    const rail = screen.getByTestId('continuity-rail')
    expect(rail.textContent).toContain('no project open')
    expect(rail.textContent).not.toContain('families')
  })

  // --- F-9: the mounted drawing vs the workspace project ------------------
  // Regression guard for the 2026-09-01 production contradiction: the header
  // read "Project rooftop_demo · 2345 polylines · 4 layers" while the rail and
  // the Browser / Solar CAD cards said "No project open". Every one of them now
  // reads the SAME derivation, so the three cannot disagree again.
  const drawingOnly = deriveWorkspaceProjectState({
    openProjectId: null, projectName: null, drawingName: 'rooftop_demo', orgId: 'org-1',
  })

  it('F-9: the rail names the mounted drawing instead of claiming nothing is open', () => {
    render(
      <ProductSurfaceTabs
        activeSurface="browser" states={states} onSelect={() => {}}
        workspaceProject={drawingOnly} catalog={catalogA}
      />,
    )
    const rail = screen.getByTestId('continuity-rail')
    expect(rail.dataset.projectState).toBe('drawing-only')
    expect(rail.textContent).toContain('rooftop_demo')
    expect(rail.textContent).toContain('no workspace project')
    // The exact string a pilot user read as a contradiction, gone.
    expect(rail.textContent).not.toContain('no project open')
  })

  it.each(['browser', 'solar'])('F-9: the %s card explains the split and offers the create action', (surface) => {
    const created = []
    render(
      <ProductSurfaceFrame
        activeSurface={surface}
        states={states}
        catalog={catalogA}
        catalogError={null}
        workspaceProject={drawingOnly}
        onCreateProject={(n) => created.push(n)}
      />,
    )
    const slot = screen.getByTestId('surface-project-state')
    expect(slot.dataset.projectState).toBe('drawing-only')
    expect(slot.textContent).toContain('No workspace project')
    expect(slot.textContent).toContain('open and editable in CAD')
    expect(screen.queryByTestId('surface-project-reason')).toBeNull()
    const action = screen.getByTestId('surface-project-action')
    expect(action.disabled).toBe(false)
    fireEvent.click(action)
    // The action creates a project NAMED FOR the drawing, so the two stay
    // visibly connected rather than the user having to invent a name.
    expect(created).toEqual(['rooftop_demo'])
  })

  it('F-9: a blocked create says why, instead of a silent dead-end button', () => {
    const noOrg = deriveWorkspaceProjectState({ drawingName: 'rooftop_demo', orgId: null })
    render(
      <ProductSurfaceFrame
        activeSurface="browser" states={states} catalog={catalogA} catalogError={null}
        workspaceProject={noOrg} onCreateProject={() => {}}
      />,
    )
    const action = screen.getByTestId('surface-project-action')
    const reason = screen.getByTestId('surface-project-reason')
    expect(action.disabled).toBe(true)
    expect(reason.textContent).toContain('Create a workspace first')
    // The reason must be ASSOCIATED with the control, not merely adjacent: a
    // disabled button's title is not reliably announced.
    expect(action.getAttribute('aria-describedby')).toBe(reason.id)
    expect(reason.id).toBeTruthy()
  })

  it('F-9: an open workspace project renders as the plain project name, no action', () => {
    const open = deriveWorkspaceProjectState({
      openProjectId: 'p-1', projectName: 'Maple St retrofit', drawingName: 'rooftop_demo', orgId: 'org-1',
    })
    render(
      <ProductSurfaceFrame
        activeSurface="browser" states={states} catalog={catalogA} catalogError={null}
        workspaceProject={open} onCreateProject={() => {}}
      />,
    )
    const slot = screen.getByTestId('surface-project-state')
    expect(slot.dataset.projectState).toBe('project')
    expect(slot.textContent).toBe('Maple St retrofit')
    expect(screen.queryByTestId('surface-project-action')).toBeNull()
  })

  it.each(['browser', 'solar'])(
    'F-9: the %s frame renders the state ITSELF, alongside any caller slot',
    (surface) => {
      // sol-critic RED on #888: /try passed its header switcher as projectSlot,
      // so its cards lost the explainer and the action entirely. The frame owns
      // the state now, so a caller slot composes WITH it and cannot displace it.
      render(
        <ProductSurfaceFrame
          activeSurface={surface} states={states} catalog={catalogA} catalogError={null}
          workspaceProject={drawingOnly} onCreateProject={() => {}}
          projectSlot={<span data-testid="caller-slot">switcher</span>}
        />,
      )
      expect(screen.getByTestId('caller-slot')).toBeTruthy()
      expect(screen.getByTestId('surface-project-state').textContent).toContain('No workspace project')
      expect(screen.getByTestId('surface-project-action')).toBeTruthy()
    },
  )

  it('F-9: iOS keeps its own project line — the ship lane owns that slot', () => {
    render(
      <ProductSurfaceFrame
        activeSurface="ios" states={states} catalog={catalogA} catalogError={null}
        workspaceProject={drawingOnly} onCreateProject={() => {}}
        projectSlot={<span data-testid="ios-lane">ship lane</span>}
      />,
    )
    expect(screen.getByTestId('ios-lane')).toBeTruthy()
    expect(screen.queryByTestId('surface-project-state')).toBeNull()
  })

  it('F-9: BOTH shells hand the frame the derivation — no call-site can opt out', () => {
    // The finding was a composition defect invisible to a component test, so
    // this binds the two real call sites: /app (App.jsx) and /try (ToolCast).
    for (const file of ['../App.jsx', '../site/ToolCast.jsx']) {
      const src = readFileSync(`${process.cwd()}/src/components/${file}`.replace('/components/../', '/'), 'utf8')
      const frame = src.slice(src.indexOf('<ProductSurfaceFrame'))
      expect(frame).toContain('workspaceProject={workspaceProjectState}')
      expect(frame).toContain('onCreateProject=')
    }
  })

  it('F-9: the rail CONSUMES the derivation and never performs one', () => {
    // sol-critic finding 2: the rail used to synthesize a literal 'legacy'
    // openProjectId from a legacy prop — fabricating an identifier inside the
    // fix for fabricated state. It reads the shared frozen resting state now.
    const src = readFileSync(`${process.cwd()}/src/components/ProductSurfaceTabs.jsx`, 'utf8')
    expect(src).not.toContain('deriveWorkspaceProjectState')
    expect(src).not.toContain("'legacy'")
    render(<ProductSurfaceTabs activeSurface="browser" states={states} onSelect={() => {}} />)
    expect(screen.getByTestId('continuity-rail').dataset.projectState).toBe('empty')
  })

  it('F-9: an omitted workspaceProject degrades to the honest empty state, not to nothing', () => {
    // sol-critic finding 2: the prop defaulted to null and the frame then
    // rendered NO state, so a call site could silently drop it. Omission is a
    // stated empty state now; it can never be silence.
    render(
      <ProductSurfaceFrame
        activeSurface="browser" states={states} catalog={catalogA} catalogError={null}
      />,
    )
    const slot = screen.getByTestId('surface-project-state')
    expect(slot.dataset.projectState).toBe('empty')
    expect(slot.textContent).toContain('No project open')
  })

  it('F-9: a create with no handler is disabled WITH a stated blocker', () => {
    // sol-critic finding 3: `disabled` counted the missing handler but the
    // reason did not, so this rendered a dead button with no explanation —
    // the exact dead end this slot replaced.
    render(
      <ProductSurfaceFrame
        activeSurface="browser" states={states} catalog={catalogA} catalogError={null}
        workspaceProject={drawingOnly}
      />,
    )
    const action = screen.getByTestId('surface-project-action')
    expect(action.disabled).toBe(true)
    const reason = screen.getByTestId('surface-project-reason')
    expect(reason.textContent).toContain('not wired to create projects')
    expect(action.getAttribute('aria-describedby')).toBe(reason.id)
  })

  it('F-9: rail and card agree — one derivation, never two answers on one screen', () => {
    render(
      <>
        <ProductSurfaceTabs
          activeSurface="browser" states={states} onSelect={() => {}}
          workspaceProject={drawingOnly} catalog={catalogA}
        />
        <ProductSurfaceFrame
          activeSurface="browser" states={states} catalog={catalogA} catalogError={null}
          workspaceProject={drawingOnly} onCreateProject={() => {}}
        />
      </>,
    )
    expect(screen.getByTestId('continuity-rail').dataset.projectState)
      .toBe(screen.getByTestId('surface-project-state').dataset.projectState)
  })

  it('F-8: every new motion rule is disabled under prefers-reduced-motion', () => {
    const css = readFileSync(`${process.cwd()}/src/site/landing.css`, 'utf8')
    const reduced = css.split('@media (prefers-reduced-motion: reduce)')[1] || ''
    expect(reduced).toContain('.tc-product-morph { animation: none; }')
    expect(reduced).toContain('.tc-continuity[data-pulse="true"] .tc-continuity-item { animation: none; }')
    expect(reduced).toContain('.tc-product-tabs button { transition: none; }')
  })

  it('F-8: the frame morph wrapper keys on the surface so switches animate', () => {
    const src = readFileSync(`${process.cwd()}/src/components/ProductSurfaceTabs.jsx`, 'utf8')
    expect(src).toContain('key={surface.id} className="tc-product-morph"')
  })

  // --- persistent sign-out affordance ---------------------------------
  // 2026-09-02 reconciliation (row B11): the deployed /try console had no
  // reachable sign-out control, only a toast buried behind Trust panel ->
  // Account details. The nav is the ONE always-mounted element that never
  // remounts across a surface switch (same F-8 node-identity contract as
  // the continuity rail), so it is the persistent home for the control.
  it('renders no sign-out control when signed out', () => {
    render(
      <ProductSurfaceTabs
        activeSurface="browser" states={states} onSelect={() => {}}
        signedIn={false} onSignOut={() => {}}
      />,
    )
    expect(screen.queryByRole('button', { name: /sign out/i })).toBeNull()
  })

  it('renders no sign-out control when signed in but no handler is wired', () => {
    render(
      <ProductSurfaceTabs
        activeSurface="browser" states={states} onSelect={() => {}}
        signedIn
      />,
    )
    expect(screen.queryByRole('button', { name: /sign out/i })).toBeNull()
  })

  it('renders a persistent sign-out control when signed in, and invokes signOut on click', () => {
    const signOut = []
    render(
      <ProductSurfaceTabs
        activeSurface="browser" states={states} onSelect={() => {}}
        signedIn onSignOut={() => signOut.push(true)}
      />,
    )
    const btn = screen.getByRole('button', { name: /sign out/i })
    fireEvent.click(btn)
    expect(signOut).toEqual([true])
  })

  it('the sign-out control survives a surface switch on the same always-mounted nav', () => {
    const nav = (surface) => (
      <ProductSurfaceTabs
        activeSurface={surface} states={states} onSelect={() => {}}
        signedIn onSignOut={() => {}}
      />
    )
    const { rerender } = render(nav('browser'))
    const before = screen.getByRole('button', { name: /sign out/i })
    rerender(nav('solar'))
    expect(screen.getByRole('button', { name: /sign out/i })).toBe(before)
  })

  it('no hardcoded per-surface capability strings survive in the sources', () => {
    const tabs = readFileSync(`${process.cwd()}/src/components/ProductSurfaceTabs.jsx`, 'utf8')
    const surfaces = readFileSync(`${process.cwd()}/src/site/productSurfaces.js`, 'utf8')
    for (const src of [tabs, surfaces]) {
      expect(src).not.toContain('additions')
      expect(src).not.toContain('Solar automations')
      expect(src).not.toContain('Browser artifacts')
      expect(src).not.toContain('not loaded yet')
    }
  })
})
