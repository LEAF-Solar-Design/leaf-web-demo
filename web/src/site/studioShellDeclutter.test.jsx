// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import DraftingRibbon from './DraftingRibbon.jsx'
import DrawingNavigationTools, { DrawingObjectsMount } from './DrawingNavigationTools.jsx'

const navigationFixture = vi.hoisted(() => ({
  index: { drawingKey: 'test', records: [], byId: new Map(), byHandle: new Map(), intake: { source: 'engine' } },
  focusId: null,
}))
vi.mock('./DrawingObjectsContext.jsx', async (original) => ({ ...await original(), useDrawingObjects: () => navigationFixture }))
vi.mock('../cadedit/EngineSessionProvider.jsx', async (original) => ({ ...await original(), useEngineSessionOptional: () => null }))
vi.mock('./SurfaceFrame.jsx', async (original) => ({ ...await original(), useSurfaceFrame: () => ({ contract: { ground: 'drawing' } }) }))
vi.mock('./studioGround.js', async (original) => ({ ...await original(), useStudioGround: () => ({}) }))

afterEach(cleanup)

describe('quiet Studio shell', () => {
  it('seats Objects beside Find and Layers in the same initially collapsed rail', () => {
    const { container } = render(<>
      <DrawingObjectsMount><details className="drawing-objects-panel"><summary>Objects</summary><p>Object list</p></details></DrawingObjectsMount>
      <DrawingNavigationTools navigation={{ syncScope: vi.fn() }} />
    </>)
    const rail = container.querySelector('.drawing-left-rail')
    expect(rail.querySelector('.drawing-objects-panel')).toBeTruthy()
    expect(rail.querySelectorAll('details').length).toBe(3)
    expect(Array.from(rail.querySelectorAll('details')).every((menu) => !menu.open)).toBe(true)
    expect(container.querySelectorAll('.drawing-objects-panel').length).toBe(1)
  })
  it('starts Find and Layers closed in the left rail', () => {
    const { container } = render(<DrawingNavigationTools navigation={{ syncScope: vi.fn() }} />)
    expect(container.querySelector('.drawing-left-rail').dataset.collapsed).toBe('true')
    expect(Array.from(container.querySelectorAll('details')).every((menu) => !menu.open)).toBe(true)
    expect(screen.queryByRole('combobox')).toBeNull()
    fireEvent.click(container.querySelector('.drawing-rail-find > summary'))
    expect(screen.getByRole('combobox')).toBeTruthy()
  })
  it('starts with the drafting tools collapsed behind Draw in the Studio shell', () => {
    const { container } = render(<div className="app" data-studio-shell="cockpit" data-surface="solar"><DraftingRibbon clusters={[]} /></div>)
    expect(container.querySelector('.drafting-ribbon').dataset.collapsed).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Draw' }))
    expect(container.querySelector('.drafting-ribbon').dataset.collapsed).toBeUndefined()
  })
})
