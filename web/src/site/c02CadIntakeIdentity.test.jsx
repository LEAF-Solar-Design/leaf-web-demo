import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { drawingHeaderName } from './drawingHeaderName.js'
import WorkspaceEnvironment from './WorkspaceEnvironment.jsx'
import { engineIntake } from '../cadedit/engineIntake.js'
import { drawingExtents } from './PropertiesDock.jsx'

afterEach(cleanup)
const shown = { dwg: '/samples/example.dwg' }
const intake = { documentId: 'imported-layout.dxf' }
const imported = { documentId: intake.documentId, documentOrigin: 'import' }

describe('C02 browser intake identity and supported entry', () => {
  it('names a committed hand import instead of the fallback drawing', () => {
    expect(drawingHeaderName(shown, intake, imported)).toBe('imported-layout')
  })
  it('uses provenance even when a hand import looks like a head filename', () => {
    const doc = { documentId: 'example-v1.dxf' }
    expect(drawingHeaderName(shown, doc, { ...doc, documentOrigin: 'import' })).toBe('example-v1')
  })
  it.each(['head', 'starter', null])('keeps the server name for origin %s', documentOrigin => {
    expect(drawingHeaderName(shown, intake, { ...imported, documentOrigin })).toBe('example')
  })
  it('does not use a pending or retired import and handles an empty workspace', () => {
    expect(drawingHeaderName(shown, null, imported)).toBe('example')
    expect(drawingHeaderName(shown, { documentId: 'older.dxf' }, imported)).toBe('example')
    expect(drawingHeaderName(null, null, null)).toBeNull()
  })
  it('preserves the explicit incomplete nested-block and omitted geometry boundary', () => {
    const entities = [{ id: '32', type: 'INSERT', name: 'Outer', layer: 'Rows', ip: [10, 20, 0], scale: [1, 1, 1], rotationDeg: 0 },
      { id: '33', type: 'OTHER', layer: 'Boundary', vertices: [] }]
    entities.blocks = [{ name: 'Outer', base: [0, 0, 0], children: [], complete: false }]
    const before = structuredClone(entities)
    const view = engineIntake(entities, 'synthetic.dxf')
    expect(view.polylines).toEqual([])
    expect(view.inserts).toHaveLength(1)
    expect(view.inserts[0]).toMatchObject({ incomplete: true, pt: [10, 20, 0] })
    expect(drawingExtents(view.polylines)).toBeNull()
    expect(entities).toEqual(before)
  })
  it('enters CAD normally through Continue workspace before accessing the drawing', () => {
    const { container } = render(<WorkspaceEnvironment scopeKey="synthetic/account" profile="cad"
      profiles={[{ id: 'cad', label: 'CAD' }]} hasContent onPanel={vi.fn()} />)
    expect(container.querySelector('.workspace-environment').dataset.entered).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Continue workspace' }))
    expect(container.querySelector('.workspace-environment').dataset.entered).toBe('true')
    expect(container.querySelector('.environment-chooser').hasAttribute('inert')).toBe(true)
  })
})
