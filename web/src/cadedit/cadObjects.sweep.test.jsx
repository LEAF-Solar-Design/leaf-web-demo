// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import CadEditSurface from './CadEditSurface.jsx'
import DrawingObjectList from './DrawingObjectList.jsx'
import { drawingOpenFailure } from './EngineHeadOpener.jsx'
import { engineIntake, MAX_POINTS } from './engineIntake.js'

const state = vi.hoisted(() => ({ session: null, objects: null }))
vi.mock('./EngineSessionProvider.jsx', () => ({
  DEFAULT_EDIT_INPUTS: {}, REACH_STATE: {},
  useEngineSessionOptional: () => ({ session: state.session }),
  useEngineSessionContext: () => ({ session: state.session }),
}))
vi.mock('../site/DrawingObjectsContext.jsx', () => ({ useDrawingObjects: () => state.objects }))
vi.mock('../site/DrawingCameraControls.jsx', () => ({ default: () => null }))

const line = { id: '10', type: 'LINE', layer: '0', vertices: [[0, 0, 0], [1, 1, 0]] }
beforeEach(() => {
  state.session = { documentId: 'roof.dxf', engineParsed: true, entities: [line], selectedIds: [], actions: {} }
  state.objects = null
})
afterEach(cleanup)

it('keeps an incomplete-view warning visible outside the Objects disclosure for omitted dimensions', () => {
  const dimensions = ['RADIUS', 'DIAMETER', 'ANGULAR'].map((dimtype, id) => ({ id, type: 'DIMENSION', dimtype }))
  state.session.entities = dimensions
  const original = JSON.stringify(dimensions)
  const view = render(<CadEditSurface enabled />)
  const warning = screen.getByTestId('drawing-incomplete-view')
  expect(warning).toBeVisible()
  expect(warning).not.toHaveAttribute('role')
  expect(screen.getAllByRole('status')).toHaveLength(1)
  expect(warning).toHaveTextContent('1 radius dimension omitted; 1 diameter dimension omitted; 1 angular dimension omitted')
  expect(warning).toHaveTextContent('The original DXF is preserved for export.')
  expect(JSON.stringify(dimensions)).toBe(original)
  state.session = { ...state.session, entities: [line] }
  view.rerender(<CadEditSurface enabled />)
  expect(screen.queryByTestId('drawing-incomplete-view')).toBeNull()
})

it('publishes capped geometry counts and displays them without modifying the source', () => {
  const vertices = Array.from({ length: MAX_POINTS + 1 }, (_, i) => [i, 0, 0])
  state.session.entities = [{ ...line, type: 'LWPOLYLINE', vertices }]
  expect(engineIntake(state.session.entities).diagnostics.truncated).toBe(1)
  render(<CadEditSurface enabled />)
  expect(screen.getByTestId('drawing-incomplete-view')).toHaveTextContent('1 drawing part omitted because the view limit was reached')
  expect(state.session.entities[0].vertices).toBe(vertices)
  expect(vertices).toHaveLength(MAX_POINTS + 1)
})

it('keeps transport diagnostics out of the fetch failure sentence', () => {
  for (const raw of ['GET /api/drawings/private-id/dxf -> 503', 'Failed to fetch', '20000 ms', 'HTTP 404', 'Error at private-id:12']) {
    const failure = drawingOpenFailure(new Error(raw))
    expect(failure.sentence).not.toMatch(/GET|\/api\/|503|Failed to fetch|20000|HTTP|404|private-id|engine/)
    expect(failure.details.message).toBe(raw)
  }
})

it('explains permission, missing files and size limits', () => {
  expect(drawingOpenFailure({ status: 403 }).sentence).toContain('permission')
  expect(drawingOpenFailure({ status: 404 }).sentence).toContain('could not be found')
  expect(drawingOpenFailure({ status: 413 }).sentence).toContain('too large')
})

it('shows and announces a readable focus failure while the drawing switches', () => {
  const announce = vi.fn()
  const record = { id: 'h:A', name: 'Roof edge', bounds: { minX: 0, minY: 0, maxX: 1, maxY: 1 } }
  state.objects = { index: { drawingKey: 'engine:roof.dxf', byHandle: new Map([['A', record]]) },
    focus: () => ({ ok: false, reason: "scene not showing this index's drawing" }) }
  render(<DrawingObjectList session={state.session} announce={announce} />)
  fireEvent.click(screen.getByRole('button', { name: 'Focus' }))
  const message = 'The drawing view is still loading. Try Focus again in a moment.'
  expect(screen.getByText(message)).toBeVisible()
  expect(announce).toHaveBeenLastCalledWith(message)
})

it('uses singular vertices and names centers, insertion points and anchors in both lists', () => {
  state.session.entities = ['LWPOLYLINE', 'CIRCLE', 'TEXT', 'INSERT'].map((type, id) => ({ ...line, id, type, vertices: [[0, 0, 0]] }))
  const { container } = render(<><CadEditSurface enabled /><DrawingObjectList session={state.session} /></>)
  expect(screen.getAllByText(/1 vertex/)).toHaveLength(2)
  expect(screen.getAllByText(/Center point/)).toHaveLength(2)
  expect(screen.getAllByText(/Insertion point/)).toHaveLength(2)
  expect(screen.getAllByText(/Anchor point/)).toHaveLength(2)
  expect(container).not.toHaveTextContent('1 vertices')
})

it('keeps drawing navigation live without naming a generic element or adding a status role', () => {
  render(<CadEditSurface enabled />)
  const navigation = screen.getByTestId('drawing-navigation-live')
  expect(navigation).not.toHaveAttribute('aria-label')
  expect(navigation).not.toHaveAttribute('role')
  expect(navigation).toHaveAttribute('aria-live', 'polite')
  expect(navigation).toHaveAttribute('aria-atomic', 'true')
  expect(screen.getByRole('status')).not.toBe(navigation)
})
