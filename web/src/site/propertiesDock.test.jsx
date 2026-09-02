// @vitest-environment jsdom
//
// The right palette's contract (W4c-V2): it HOSTS the passed elements (one
// source of truth - never a re-implementation), renders client-derived
// geometry honestly, folds per section, and offers NO edit affordance.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import PropertiesDock, { GeometryRows } from './PropertiesDock.jsx'

afterEach(cleanup)

describe('PropertiesDock', () => {
  it('hosts the passed layer and selection elements inside labelled sections', () => {
    render(
      <PropertiesDock
        layers={<div data-testid="hosted-legend">legend</div>}
        selection={<div data-testid="hosted-readout">readout</div>}
        geometry={{ vertices: 4, closed: true, length: 40, area: 100 }}
      />,
    )
    const dock = screen.getByRole('complementary', { name: 'Properties' })
    expect(dock.contains(screen.getByTestId('hosted-legend'))).toBe(true)
    expect(dock.contains(screen.getByTestId('hosted-readout'))).toBe(true)
    expect(screen.getByText('Perimeter')).toBeTruthy()
    expect(screen.getByText('100.00 u²')).toBeTruthy()
  })

  it('sections fold independently and re-open', () => {
    render(<PropertiesDock layers={<div data-testid="hosted-legend" />} selection={null} geometry={null} />)
    const head = screen.getByRole('button', { name: /Layers/ })
    expect(head.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(head)
    expect(head.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByTestId('hosted-legend')).toBeNull()
    fireEvent.click(head)
    expect(screen.getByTestId('hosted-legend')).toBeTruthy()
  })

  it('offers no edit affordance: the only dock-owned buttons are the section folds', () => {
    render(
      <PropertiesDock
        layers={null}
        selection={null}
        geometry={{ vertices: 4, closed: false, length: 30, area: null }}
      />,
    )
    const buttons = screen.getAllByRole('button')
    expect(buttons.map((b) => b.className)).toEqual(['dock-section-head', 'dock-section-head'])
  })
})

describe('GeometryRows', () => {
  it('open polyline: Length (not Perimeter), no area row', () => {
    render(<GeometryRows geometry={{ vertices: 3, closed: false, length: 12, area: null }} />)
    expect(screen.getByText('Length')).toBeTruthy()
    expect(screen.queryByText('Area')).toBeNull()
  })

  it('insert pose renders with degree formatting; null geometry renders nothing', () => {
    const { container, rerender } = render(
      <GeometryRows geometry={{ position: [5, 6], rotation: 45, scale: [1, 2, 1] }} />,
    )
    expect(screen.getByText('45.0°')).toBeTruthy()
    expect(screen.getByText('1.00 · 2.00 · 1.00')).toBeTruthy()
    rerender(<GeometryRows geometry={null} />)
    expect(container.querySelector('.dock-geometry')).toBeNull()
  })
})
