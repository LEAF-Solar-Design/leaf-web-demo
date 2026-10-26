import { useEffect, useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createLabArtifactsClient } from './labArtifactsClient.js'
import WidgetInspector from '../widgets/WidgetInspector.jsx'
import WidgetsView from '../widgets/WidgetsView.jsx'
import { createWidgetDoc } from '../widgets/useWidgetEditor.js'
import { createTextElement } from '../widgets/widgetDoc.js'
import LabWorkbench from '../LabWorkbench.jsx'
import GraphView from '../canvas/GraphView.jsx'
import { addBlock, emptyGraph } from '../canvas/graphDoc.js'
import { registry } from '../canvas/registry.js'
import { productSurfaceStates, surfaceRailState } from '../../site/productSurfaces.js'

vi.mock('./HistoryView.jsx', () => ({ default: () => <p>History content</p> }))
vi.mock('../runs/RunsView.jsx', () => ({ default: () => null }))
vi.mock('../deploys/DeploysView.jsx', () => ({ default: () => null }))
vi.mock('../canvas/LabCanvas.jsx', () => ({ default: function Canvas({ onChange, initialDocument }) {
  const [document, setDocument] = useState(() => initialDocument || emptyGraph())
  useEffect(() => { onChange({ document }) }, [document, onChange])
  return <button onClick={() => setDocument(addBlock(document, Object.values(registry)[0]))}>Draft blocks: {document.blocks.length}</button>
} }))

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

function inspector() {
  const doc = createWidgetDoc()
  const element = createTextElement('text-1')
  doc.formats.la.els = [element]
  const dispatch = vi.fn()
  render(<WidgetInspector doc={doc} selectedId={element.id} dispatch={dispatch} local />)
  return dispatch
}

describe('Lab issue sweep', () => {
  it('S1-188 gives HTML failures a readable message', async () => {
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: false, status: 502, json: async () => { throw new Error('HTML') } }) })
    const result = await client.head('strategy')
    expect(result.detail).toBe('Something went wrong. Try again.')
  })

  it('S1-188 extracts a structured message without object interpolation', async () => {
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: false, status: 400, json: async () => ({ detail: { message: 'Choose a saved revision.' } }) }) })
    expect((await client.head('strategy')).detail).toBe('Choose a saved revision.')
  })

  it('S1-188 humanizes network failures without a Test prefix', async () => {
    const client = createLabArtifactsClient({ fetchImpl: async () => { throw new TypeError('Failed to fetch') } })
    expect((await client.head('strategy')).detail).toBe("Couldn't reach the service. Check the connection and retry.")
  })

  it('S1-191 permits empty input and multi-digit widths before blur', () => {
    const dispatch = inspector()
    const input = screen.getByLabelText('W')
    fireEvent.change(input, { target: { value: '' } })
    expect(input).toHaveValue(null)
    fireEvent.change(input, { target: { value: '4' } })
    expect(input).toHaveValue(4)
    expect(dispatch).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: '40' } })
    fireEvent.blur(input)
    expect(dispatch).toHaveBeenCalledWith({ type: 'edit', patch: { w: 40 } })
  })

  it('S1-191 clamps only when Enter commits the width', () => {
    const dispatch = inspector()
    const input = screen.getByLabelText('W')
    fireEvent.change(input, { target: { value: '4' } })
    expect(dispatch).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue(6)
    expect(dispatch).toHaveBeenCalledWith({ type: 'edit', patch: { w: 6 } })
  })

  it('S1-192 points to the Document ID controls', () => {
    render(<WidgetsView />)
    expect(screen.getByText('Enter a Document ID, then choose New or Load.')).toBeVisible()
  })

  it('S1-193 changes views without discarding the mounted draft', async () => {
    window.history.replaceState(null, '', '/app/lab')
    render(<LabWorkbench route={{ view: 'graph' }} status={{ state: 'on' }} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Draft blocks: 0' }))
    fireEvent.click(screen.getByRole('link', { name: 'Test' }))
    expect(screen.getByText('History content')).toBeVisible()
    expect(window.location.pathname).toBe('/app/lab/history')
    fireEvent.click(screen.getByRole('link', { name: 'Strategy' }))
    expect(screen.getByRole('button', { name: 'Draft blocks: 1' })).toBeVisible()
  })

  it('S1-193 guards departures while graph edits are unsaved', async () => {
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: false, status: 404, json: async () => ({}) }) })
    render(<GraphView client={client} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Draft blocks: 0' }))
    const departure = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(departure)
    expect(departure.defaultPrevented).toBe(true)
  })

  it('S1-194 associates the disabled run control with its save requirement', async () => {
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: false, status: 404, json: async () => ({}) }) })
    render(<GraphView client={client} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run graph' })).toHaveAccessibleDescription('Save this graph before running it'))
    expect(screen.getByRole('button', { name: 'Run graph' })).toBeDisabled()
  })

  it('S1-217 retains an unavailable Lab tab on direct links for keyboard recovery', () => {
    window.history.replaceState(null, '', '/app/lab')
    expect(surfaceRailState('lab', { state: 'off' }, 'app')).toBe(true)
    expect(productSurfaceStates({ labStatus: { state: 'off' } }).lab).toEqual({ state: 'unavailable', label: 'Lab is unavailable right now.' })
  })
})
