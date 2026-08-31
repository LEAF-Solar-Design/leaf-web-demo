/**
 * Card F-7 acceptance: every surface's own frame content consumes the LIVE
 * tenant capability catalog — the same fold the CAD rail renders — never
 * hardcoded capability strings. The load-bearing assertion is change
 * detection: when the tenant catalog changes, each tab's rendered content
 * changes with it.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { cleanup, render, screen } from '@testing-library/react'
import ProductSurfaceTabs, { ProductSurfaceFrame } from './ProductSurfaceTabs.jsx'
import { productSurfaceStates } from '../site/productSurfaces.js'

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
    const nav = (surface) => (
      <ProductSurfaceTabs
        activeSurface={surface}
        states={states}
        onSelect={() => {}}
        project="rooftop_demo"
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
