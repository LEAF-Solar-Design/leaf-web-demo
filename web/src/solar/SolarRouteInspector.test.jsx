// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarRouteInspector from './SolarRouteInspector.jsx'
import { solarGraphOverlay } from './solarGraphOverlay.js'

afterEach(cleanup)

function project(routes, metersPerUnit = 1, drawingKey = 'drawing') {
  const result = solarGraphOverlay({
    graph_schema_version: 1,
    project: { units: { compute_units: 'm', meters_per_unit: metersPerUnit } },
    frames: [],
    panels: [],
    strings: [],
    inverters: [],
    routes,
  }, { metersPerUnit })
  expect(result.refusal).toBeNull()
  return { polylines: result.polylines, drawingKey }
}

const route = (id, route_kind, points) => ({ id, route_kind, points })

describe('SolarRouteInspector', () => {
  it.each([
    ['start homerun', 'Start home run', 1, [[0, 0], [0, 3], [2, 3]], 'X 0, Y 3', 'X 2, Y 3'],
    ['end homerun', 'End home run', 1, [[-4, 1], [-2, 1], [-2, -3]], 'X -2, Y 1', 'X -2, Y -3'],
    ['feeder', 'Feeder', .0254, [[0, 0], [.0254, .0508], [.0508, .0508]], 'X 1, Y 2', 'X 2, Y 2'],
  ])('inspects ordered %s vertices in drawing units', (kind, label, scale, points, middle, end) => {
    render(<SolarRouteInspector overlay={project([route('line', kind, points)], scale)} />)
    fireEvent.click(screen.getByText('Cable routes'))
    expect(screen.getByRole('option', { name: label + ' 1' }).value).toBe('line')
    fireEvent.change(screen.getByLabelText('Route'), { target: { value: 'line' } })
    const previous = screen.getByRole('button', { name: 'Previous point' })
    const next = screen.getByRole('button', { name: 'Next point' })
    expect(previous.disabled).toBe(true)
    fireEvent.click(next)
    expect(screen.getByRole('status').textContent).toBe(`Point 2 of 3 · ${middle} (drawing units)`)
    fireEvent.click(next)
    expect(screen.getByRole('status').textContent).toBe(`Point 3 of 3 · ${end} (drawing units)`)
    expect(next.disabled).toBe(true)
    fireEvent.click(previous)
    expect(screen.getByRole('status').textContent).toContain(middle)
  })

  it('clears the selection when the same geometry is bound to a different drawing', () => {
    const overlay = project([
      route('a', 'feeder', [[0, 0], [2, 2]]),
      route('trench', 'trench', [[0, 0], [1, 1]]),
    ])
    const view = render(<SolarRouteInspector overlay={overlay} />)
    fireEvent.click(screen.getByText('Cable routes'))
    expect(screen.getAllByRole('option')).toHaveLength(2)
    fireEvent.change(screen.getByLabelText('Route'), { target: { value: 'a' } })
    view.rerender(<SolarRouteInspector overlay={{ ...overlay, drawingKey: 'other' }} />)
    view.rerender(<SolarRouteInspector overlay={overlay} />)
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.getByLabelText('Route').value).toBe('')
  })

  it('hides the panel when the active overlay has no drawable cable routes', () => {
    render(<SolarRouteInspector overlay={project([route('a', 'feeder', [[0, 0]])])} />)
    expect(screen.queryByText('Cable routes')).toBeNull()
  })

  it('shares the route selection with the Viewer and resets point navigation on a new pick', () => {
    const overlay = project([
      route('start', 'start homerun', [[0, 0], [0, 3], [2, 3]]),
      route('end', 'end homerun', [[-4, 1], [-2, 1], [-2, -3]]),
      route('feeder', 'feeder', [[4, 0], [4, 2], [8, 2]]),
    ])
    const onRouteSelect = vi.fn()
    const inspector = selectedRouteId => (
      <SolarRouteInspector overlay={overlay} selectedRouteId={selectedRouteId} onRouteSelect={onRouteSelect} />
    )
    const view = render(inspector(null))
    fireEvent.click(screen.getByText('Cable routes'))
    const select = screen.getByLabelText('Route')
    fireEvent.change(select, { target: { value: 'feeder' } })
    expect(onRouteSelect).toHaveBeenLastCalledWith('feeder')
    view.rerender(inspector('feeder'))
    expect(select.value).toBe('feeder')
    fireEvent.click(screen.getByRole('button', { name: 'Next point' }))
    expect(screen.getByRole('status').textContent).toContain('X 4, Y 2')
    for (const [id, firstPoint] of [['start', 'X 0, Y 0'], ['end', 'X -4, Y 1']]) {
      view.rerender(inspector(id))
      expect(select.value).toBe(id)
      expect(screen.getByRole('status').textContent).toContain('Point 1 of 3')
      expect(screen.getByRole('status').textContent).toContain(firstPoint)
    }
    fireEvent.change(select, { target: { value: '' } })
    expect(onRouteSelect).toHaveBeenLastCalledWith(null)
    view.rerender(inspector(null))
    expect(screen.queryByRole('status')).toBeNull()
  })
})
