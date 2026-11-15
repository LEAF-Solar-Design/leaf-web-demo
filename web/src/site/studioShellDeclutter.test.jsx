// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AgentPanel, CheckoutChip, CockpitStatus, DrawingTabLabel, ShellDiagnostics, ShellKpis } from './DrawingCockpit.jsx'
import DraftingRibbon from './DraftingRibbon.jsx'
import DrawingNavigationTools, { DrawingObjectsMount } from './DrawingNavigationTools.jsx'
import { projectSolarOpenStatus } from '../solar/solarProjectAppModel.js'

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
  it('leaves the CAD command dock unwrapped without claiming Escape', () => {
    const { container } = render(<AgentPanel enabled={false}><input aria-label="CAD command" /></AgentPanel>)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(container.querySelector('.studio-agent-panel')).toBeNull()
    expect(screen.getByRole('textbox')).toBeTruthy()
  })
  it('starts Find and Layers closed in the left rail', () => {
    const { container } = render(<DrawingNavigationTools navigation={{ syncScope: vi.fn() }} />)
    expect(container.querySelector('.drawing-left-rail').dataset.collapsed).toBe('true')
    expect(Array.from(container.querySelectorAll('details')).every((menu) => !menu.open)).toBe(true)
    expect(screen.queryByRole('combobox')).toBeNull()
    fireEvent.click(container.querySelector('.drawing-rail-find > summary'))
    expect(screen.getByRole('combobox')).toBeTruthy()
  })
  it('collapses the agent panel to its pill on Escape and preserves the message history', () => {
    const { container } = render(<AgentPanel messages={[{ id: 'one', text: 'String every roof' }]}><input aria-label="Ask Leaf" /></AgentPanel>)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(container.querySelector('.studio-agent-panel').dataset.collapsed).toBe('true')
    expect(screen.getByRole('button', { name: 'Ask Leaf' }).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Ask Leaf' }))
    expect(screen.getByText('String every roof')).toBeTruthy()
    expect(container.querySelector('.studio-agent-panel').dataset.collapsed).toBe('false')
  })
  it('shows coordinates and selection while withholding scale and render diagnostics', () => {
    const viewerRef = { current: null }
    const { container, rerender } = render(<CockpitStatus ground={null} viewerRef={viewerRef} shown={{}} />)
    expect(screen.getByText('X')).toBeTruthy()
    expect(screen.getByText('Y')).toBeTruthy()
    expect(screen.getByText('no selection')).toBeTruthy()
    expect(container.querySelector('.cockpit-scale')).toBeNull()
    expect(container.querySelector('.cockpit-count')).toBeNull()
    rerender(<CockpitStatus ground={null} viewerRef={viewerRef} shown={{}} customerMode={false} />)
    expect(container.querySelector('.cockpit-scale')).toBeTruthy()
    expect(container.querySelector('.cockpit-count')).toBeTruthy()
  })
  it('gates plan, tool count and daily spend while retaining coordinates', () => {
    const status = (customerMode) => <footer>
      <ShellDiagnostics customerMode={customerMode} plan="admin" toolCount={63} costToday={0} />
      <CockpitStatus customerMode={customerMode} ground={null} viewerRef={{ current: null }} />
    </footer>
    const { rerender } = render(status(true))
    expect(screen.queryByText(/Plan:/)).toBeNull()
    expect(screen.queryByText(/Tools:/)).toBeNull()
    expect(screen.queryByText(/today/)).toBeNull()
    expect(screen.getByText('X')).toBeTruthy()
    rerender(status(false))
    expect(screen.getByText('Tools: 63 available · Plan: admin')).toBeTruthy()
    expect(screen.getByText('$0.000 today')).toBeTruthy()
    expect(screen.getByText('X')).toBeTruthy()
  })
  it('uses the drawing name and puts the short id behind a separated tooltip', () => {
    render(<DrawingTabLabel name="Roof plan" shortId="abc123" />)
    expect(screen.getByText('Roof plan').title).toBe('Roof plan · abc123')
    expect(screen.queryByText('abc123')).toBeNull()
  })
  it('omits absent KPI values without omitting a real zero', () => {
    render(<ShellKpis items={[{ label: 'Panels', value: 0 }, { label: 'Power', value: null }, { label: 'Version' }]} />)
    expect(screen.getByText('0')).toBeTruthy()
    expect(screen.queryByText('Power')).toBeNull()
    expect(screen.queryByText('Version')).toBeNull()
  })
  it('keeps checkout actions behind a quiet chip in both checkout states', () => {
    const take = vi.fn()
    const { container, rerender } = render(<CheckoutChip><button onClick={take}>Take</button></CheckoutChip>)
    expect(screen.getByText('View only')).toBeTruthy()
    expect(container.querySelector('details').open).toBe(false)
    fireEvent.click(screen.getByText('View only'))
    fireEvent.click(screen.getByText('Take'))
    expect(take).toHaveBeenCalledOnce()
    rerender(<CheckoutChip checkedOut><button>Release</button></CheckoutChip>)
    expect(screen.getByText('Checked out').className).toBe('checked-out')
  })
  it('reserves full checkout refusal copy for attempted writes', () => {
    expect(projectSolarOpenStatus('checkout_required')).toBeNull()
    expect(projectSolarOpenStatus('checkout_required', { attemptedWrite: true })).toBe('Take the project checkout before changing this drawing.')
  })
  it('starts with the drafting tools collapsed behind Draw in the Studio shell', () => {
    const { container } = render(<div className="app" data-studio-shell="cockpit" data-surface="solar"><DraftingRibbon clusters={[]} /></div>)
    expect(container.querySelector('.drafting-ribbon').dataset.collapsed).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Draw' }))
    expect(container.querySelector('.drafting-ribbon').dataset.collapsed).toBeUndefined()
  })
})
