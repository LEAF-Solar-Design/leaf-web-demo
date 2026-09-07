// W4g-7b-03c-d: EngineDockProperties, the consumer that portals the dock's
// Color/Linetype/Lineweight rows into the slot App renders (the fix for the
// PR #1121 proof finding: App's own hook call sat outside the provider and
// always read null). EngineSessionProvider.jsx is mocked to a thin passthrough
// so the session value is fully controlled here, the same way a consumer test
// would stand up any other context.
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import EngineDockProperties, { DOCK_PROPERTIES_SLOT_ID } from './EngineDockProperties.jsx'

const mockState = { engine: null }
vi.mock('./EngineSessionProvider.jsx', () => ({
  default: ({ children }) => children,
  useEngineSessionOptional: () => mockState.engine,
}))

const LINE = { id: 's1', aci: 1, linetype: 'HIDDEN', lineweight: 25 }

function mountSlot() {
  const slot = document.createElement('div')
  slot.id = DOCK_PROPERTIES_SLOT_ID
  document.body.appendChild(slot)
  return slot
}

afterEach(() => {
  cleanup()
  mockState.engine = null
  // mountSlot() appends a plain DOM node outside any render() tree, so RTL's
  // cleanup() never removes it; a leftover slot from an earlier test would
  // make "the slot has not mounted" pass for the wrong reason.
  document.querySelectorAll(`[id="${DOCK_PROPERTIES_SLOT_ID}"]`).forEach((el) => el.remove())
})

describe('EngineDockProperties: the slot consumer inside the provider', () => {
  it('portals Color/Linetype/Lineweight into the slot when the engine holds a selection', () => {
    mountSlot()
    mockState.engine = { session: { selectedId: 's1', entities: [LINE] } }
    render(<EngineDockProperties />)
    const dl = document.querySelector('[data-testid="dock-properties"]')
    expect(dl).not.toBeNull()
    expect([...dl.querySelectorAll('dd')].map((d) => d.textContent)).toEqual(['red (1)', 'HIDDEN', '0.25 mm'])
  })

  it('renders nothing when the engine holds no selection', () => {
    mountSlot()
    mockState.engine = { session: { selectedId: null, entities: [LINE] } }
    render(<EngineDockProperties />)
    expect(document.querySelector('[data-testid="dock-properties"]')).toBeNull()
  })

  it('renders nothing when no provider is mounted (engine null)', () => {
    mountSlot()
    mockState.engine = null
    render(<EngineDockProperties />)
    expect(document.querySelector('[data-testid="dock-properties"]')).toBeNull()
  })

  it('renders nothing when the slot has not mounted', () => {
    mockState.engine = { session: { selectedId: 's1', entities: [LINE] } }
    render(<EngineDockProperties />)
    expect(document.querySelector('[data-testid="dock-properties"]')).toBeNull()
  })

  it('W4g-7b-04c: a selected DIMENSION shows its measurement, read-only, 3dp trimmed', () => {
    mountSlot()
    const dim = { id: 'd1', type: 'DIMENSION', dimtype: 'ALIGNED', aci: 256, linetype: 'ByLayer', lineweight: -1, measurement: 5 }
    mockState.engine = { session: { selectedId: 'd1', entities: [dim] } }
    render(<EngineDockProperties />)
    const dl = document.querySelector('[data-testid="dock-properties"]')
    expect([...dl.querySelectorAll('dt')].map((d) => d.textContent)).toEqual(['Color', 'Linetype', 'Lineweight', 'Measurement'])
    expect([...dl.querySelectorAll('dd')].map((d) => d.textContent)).toEqual(['ByLayer', 'ByLayer', 'ByLayer', '5'])
  })

  it('a non-dimension selection carries no Measurement row', () => {
    mountSlot()
    mockState.engine = { session: { selectedId: 's1', entities: [LINE] } }
    render(<EngineDockProperties />)
    const dl = document.querySelector('[data-testid="dock-properties"]')
    expect([...dl.querySelectorAll('dt')].map((d) => d.textContent)).not.toContain('Measurement')
  })

  it('re-renders when the selection changes', () => {
    mountSlot()
    mockState.engine = { session: { selectedId: 's1', entities: [LINE] } }
    const { rerender } = render(<EngineDockProperties />)
    expect(document.querySelector('[data-testid="dock-properties"] dd').textContent).toBe('red (1)')
    mockState.engine = { session: { selectedId: 's2', entities: [{ id: 's2', aci: 256, linetype: 'ByLayer', lineweight: -1 }] } }
    rerender(<EngineDockProperties />)
    expect(document.querySelector('[data-testid="dock-properties"] dd').textContent).toBe('ByLayer')
  })

  // W4g-7b-03c-h D2: a true-coloured entity reads as its rgb value AND its
  // nearest standard index, never the index alone (which hid that the colour
  // was ever approximate); a plain entity (no trueColor) still reads its own name.
  it('a true-coloured entity reads as rgb plus the nearest index', () => {
    mountSlot()
    mockState.engine = { session: { selectedId: 's3', entities: [{ id: 's3', aci: 3, trueColor: [10, 20, 30], linetype: 'ByLayer', lineweight: -1 }] } }
    render(<EngineDockProperties />)
    expect(document.querySelector('[data-testid="dock-properties"] dd').textContent).toBe('rgb(10,20,30) (nearest green (3))')
  })

  it('trueColor: null still reads the plain ACI name', () => {
    mountSlot()
    mockState.engine = { session: { selectedId: 's4', entities: [{ id: 's4', aci: 3, trueColor: null, linetype: 'ByLayer', lineweight: -1 }] } }
    render(<EngineDockProperties />)
    expect(document.querySelector('[data-testid="dock-properties"] dd').textContent).toBe('green (3)')
  })
})
