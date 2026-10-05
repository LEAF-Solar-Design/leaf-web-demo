// @vitest-environment jsdom
//
// The right palette's contract (W4c-V2): it HOSTS the passed elements (one
// source of truth - never a re-implementation), renders client-derived
// geometry honestly, folds per section, and offers NO edit affordance.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import PropertiesDock, { GeometryRows, drawingPropertyName } from './PropertiesDock.jsx'
import { StatusTabs } from './DrawingCockpit.jsx'

afterEach(cleanup)

describe('Drawing name', () => {
  it.each([
    ['sample', 'rooftop_demo.dwg', 'demo-v1.dxf'],
    ['uploaded drawing', 'private_roof.dwg', 'bc0e36e8-f107-4954-8d68-5882409b031f'],
  ])('shows the %s display name used by the document tab', (_, displayName, documentId) => {
    render(<>
      <StatusTabs name={displayName} />
      <PropertiesDock drawing={{ name: drawingPropertyName(displayName, documentId) }} />
    </>)
    const name = screen.getByText('Name').nextSibling
    const tab = screen.getByTestId('cockpit-status-tabs').querySelector('.foot-doc-tab')
    expect(name.textContent).toBe(displayName)
    expect(name.textContent).toBe(tab.textContent)
    expect(name.title).toBe(displayName)
    expect(screen.queryByText(documentId)).toBeNull()
  })

  it('falls back to the intake document id when no drawing display name is available', () => {
    render(<>
      <StatusTabs name="" />
      <PropertiesDock drawing={{ name: drawingPropertyName('', 'demo-v1.dxf') }} />
    </>)
    const name = screen.getByText('Name').nextSibling
    expect(name.textContent).toBe('demo-v1.dxf')
    expect(name.title).toBe('demo-v1.dxf')
    expect(screen.getByTestId('cockpit-status-tabs').querySelector('.foot-doc-tab')).toBeNull()
    expect(drawingPropertyName('', undefined)).toBe('')
  })
})

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
  it('shows chained open endpoints through the drawing-unit formatter only when both exist', () => {
    const geometry = { vertices: 2, closed: false, length: 10, area: null, first: [10, -0], last: [10, 10] }
    const { rerender } = render(<GeometryRows geometry={geometry} />)
    expect(screen.getByText('Start').nextSibling.textContent).toBe('10.00, 0.00')
    expect(screen.getByText('End').nextSibling.textContent).toBe('10.00, 10.00')
    rerender(<GeometryRows geometry={{ ...geometry, last: null }} />)
    expect(screen.queryByText('Start')).toBeNull()
    expect(screen.queryByText('End')).toBeNull()
  })

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
