// W4e round 2: the pane's Drawing section renders the document's own facts
// and never an invented number; extents are one pass over the intake's
// vertices, tolerant of the two point shapes the intake has carried.
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

afterEach(cleanup)

import PropertiesDock, { DrawingRows, drawingExtents } from './PropertiesDock.jsx'

describe('drawingExtents', () => {
  it('folds [x, y] pairs and {x, y} points into one box and skips non-finite vertices', () => {
    const box = drawingExtents([
      { pts: [[0, 0], [10, 5]] },
      { pts: [{ x: -3, y: 7 }, { x: Number.NaN, y: 99 }] },
      { pts: 'not-a-list' },
      null,
    ])
    expect(box).toEqual({ minX: -3, minY: 0, maxX: 10, maxY: 7 })
  })

  it('is null when no finite vertex exists', () => {
    expect(drawingExtents([])).toBeNull()
    expect(drawingExtents([{ pts: [[Number.NaN, 1]] }])).toBeNull()
    expect(drawingExtents(undefined)).toBeNull()
  })
})

describe('DrawingRows', () => {
  it('renders every fact as a label | field row and dashes for what is absent', () => {
    render(<DrawingRows drawing={{ name: 'roof.dwg', entities: 2345, polylines: 2345, inserts: 0, faces: 0, layers: 4, layersShown: 3, extents: null, source: 'sample data' }} />)
    const rows = screen.getByTestId('dock-drawing')
    expect(within(rows).getByText('roof.dwg')).toBeInTheDocument()
    // Entities and Polylines both read 2,345 on a polyline-only drawing.
    expect(within(rows).getAllByText('2,345', { selector: 'dd' })).toHaveLength(2)
    expect(within(rows).getByText('Layers shown').nextSibling).toHaveTextContent('3')
    expect(within(rows).getByText('Width').nextSibling).toHaveTextContent('—')
    expect(within(rows).getByText('Extents X').nextSibling).toHaveTextContent('—')
  })

  it('formats extents and derives width and height from them', () => {
    render(<DrawingRows drawing={{ name: 'a', entities: 1, polylines: 1, inserts: 0, faces: 0, layers: 1, layersShown: 1, extents: { minX: 0, minY: -2.5, maxX: 100, maxY: 47.5 }, source: 'x' }} />)
    const rows = screen.getByTestId('dock-drawing')
    expect(within(rows).getByText('Width').nextSibling).toHaveTextContent('100')
    expect(within(rows).getByText('Height').nextSibling).toHaveTextContent('50')
  })

  it('is absent from the dock when no drawing summary is given, and present when one is', () => {
    const { rerender } = render(<PropertiesDock layers={<div>L</div>} selection={<div>S</div>} geometry={null} />)
    expect(screen.queryByTestId('dock-drawing')).toBeNull()
    rerender(<PropertiesDock layers={<div>L</div>} selection={<div>S</div>} geometry={null} drawing={{ name: 'b.dwg', entities: 0, polylines: 0, inserts: 0, faces: 0, layers: 0, layersShown: 0, extents: null, source: 's' }} />)
    expect(screen.getByTestId('dock-drawing')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Drawing/ })).toBeInTheDocument()
  })
})
