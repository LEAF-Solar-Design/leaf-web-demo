import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import LabSaveBar from './LabSaveBar.jsx'
import { createLabArtifactsClient, isValidDocId } from './labArtifactsClient.js'

const graph = { schema: 'leaf.lab-graph.v1', blocks: [], pipes: [], outputs: {} }
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data })
function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
function fixture() {
  const fetchImpl = vi.fn(async url => {
    const path = new URL(url, 'https://example.test').pathname
    if (path === '/api/lab/docs/roof-plan_2/head') return response({ revision: 3 })
    if (path === '/api/lab/docs/roof-plan_2/revisions/3') return response({ revision: 3, graph })
    if (path.endsWith('/save')) return response({ revision: 1 })
    return response({ detail: 'lab_document_not_found' }, 404)
  })
  const client = createLabArtifactsClient({ fetchImpl })
  const take = vi.fn()
  render(<LabSaveBar document={graph} ready client={client} onTakeTheirs={take} />)
  const input = screen.getByRole('textbox', { name: 'Graph document', exact: true })
  const choose = (id, action) => {
    fireEvent.change(input, { target: { value: id } })
    fireEvent.click(screen.getByRole('button', { name: action, exact: true }))
  }
  return { client, fetchImpl, take, input, choose }
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('Graph document picker', () => {
  it('uses the client identifier rules and disables invalid document actions', () => {
    for (const id of ['strategy', 'roof-plan_2', 'a'.repeat(128)]) expect(isValidDocId(id)).toBe(true)
    for (const id of ['', '-x', 'a b', 'a'.repeat(129), 'a\n', null, 12]) expect(isValidDocId(id)).toBe(false)
    const { input } = fixture()
    expect(input).toHaveValue('strategy')
    expect(input).toHaveAttribute('maxlength', '128')
    fireEvent.change(input, { target: { value: '-x' } })
    expect(screen.getByRole('button', { name: 'Open', exact: true })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'New', exact: true })).toBeDisabled()
  })

  it('opens the head revision of another graph document', async () => {
    const { client, fetchImpl, take, input, choose } = fixture()
    choose('roof-plan_2', 'Open')
    await screen.findByText('Opened roof-plan_2 r3')
    expect(client.currentDocId()).toBe('roof-plan_2')
    expect(input).toHaveValue('roof-plan_2')
    expect(fetchImpl.mock.calls.map(([url]) => new URL(url, 'https://example.test').pathname)).toEqual([
      '/api/lab/docs/roof-plan_2/head', '/api/lab/docs/roof-plan_2/revisions/3',
    ])
    expect(take).toHaveBeenCalledExactlyOnceWith(graph)
  })

  it('reports a missing graph after switching to open it', async () => {
    const { client, take, choose } = fixture()
    choose('missing', 'Open')
    await screen.findByText('No saved graph named missing. Choose New to start it.')
    expect(client.currentDocId()).toBe('missing')
    expect(take).not.toHaveBeenCalled()
  })

  it('does not reset the generation when opening the current document', async () => {
    const { client, fetchImpl, choose, take } = fixture()
    fetchImpl.mockResolvedValueOnce(response({ revision: 3 }))
    fetchImpl.mockResolvedValueOnce(response({ revision: 3, graph }))
    const generation = client.currentGeneration()
    choose('strategy', 'Open')
    await screen.findByText('Opened strategy r3')
    expect(client.currentGeneration()).toBe(generation)
    expect(take).toHaveBeenCalledExactlyOnceWith(graph)
  })

  it('checks New before switching and saves the new document at its own URL', async () => {
    const { client, fetchImpl, choose } = fixture()
    const lookup = deferred()
    fetchImpl.mockImplementationOnce(() => lookup.promise)
    // GraphView hydrates the head whenever the document generation changes.
    let generation = client.currentGeneration()
    const unsubscribe = client.subscribe(() => {
      if (generation === client.currentGeneration()) return
      generation = client.currentGeneration()
      void client.head(client.currentDocId())
    })
    choose('new-roof', 'New')
    expect(client.currentDocId()).toBe('strategy')
    expect(screen.getByRole('button', { name: 'New', exact: true })).toBeDisabled()
    await act(async () => lookup.resolve(response({}, 404)))
    await screen.findByText('New graph new-roof. Save to keep it.')
    expect(client.currentDocId()).toBe('new-roof')
    expect(client.state.restoredGraph).toBeNull()
    const save = screen.getByRole('button', { name: 'Save', exact: true })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    await screen.findByText('Saved r1')
    const [url, options] = fetchImpl.mock.calls.find(([, init]) => init.method === 'POST')
    expect(new URL(url, 'https://example.test').pathname).toBe('/api/lab/docs/new-roof/save')
    expect(JSON.parse(options.body)).toEqual({ graph, expected_head: null })
    unsubscribe()
  })

  it('keeps the current document when New finds an existing graph', async () => {
    const { client, take, choose } = fixture()
    choose('roof-plan_2', 'New')
    await screen.findByText('A graph named roof-plan_2 already exists. Choose Open.')
    expect(client.currentDocId()).toBe('strategy')
    expect(take).not.toHaveBeenCalled()
  })

  it.each(['Open', 'New'])('reports %s lookup failures', async action => {
    const { fetchImpl, choose, take } = fixture()
    fetchImpl.mockResolvedValueOnce(response({ detail: 'Offline' }, 503))
    choose('unavailable', action)
    await screen.findByText(new RegExp(`^${action} failed: `))
    expect(take).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: action, exact: true })).toBeEnabled()
  })

  it.each(['head', 'revision'])('drops a stale Open %s response after a newer switch', async stage => {
    const { client, fetchImpl, choose, take, input } = fixture()
    const earlier = deferred()
    if (stage === 'revision') fetchImpl.mockResolvedValueOnce(response({ revision: 8 }))
    fetchImpl.mockImplementationOnce(() => earlier.promise)
    choose('earlier', 'Open')
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(stage === 'head' ? 1 : 2))
    expect(screen.getByRole('button', { name: 'Open', exact: true })).toBeDisabled()
    act(() => client.setDocId('intermediate'))
    expect(input).toHaveValue('intermediate')
    choose('roof-plan_2', 'Open')
    await screen.findByText('Opened roof-plan_2 r3')
    await act(async () => earlier.resolve(response({ revision: 8, graph: { ...graph, outputs: { stale: true } } })))
    expect(client.currentDocId()).toBe('roof-plan_2')
    expect(screen.getByText('Opened roof-plan_2 r3')).toBeVisible()
    expect(take).toHaveBeenCalledExactlyOnceWith(graph)
  })

  it('does not switch for a stale New lookup', async () => {
    const { client, fetchImpl, choose, take } = fixture()
    const earlier = deferred()
    fetchImpl.mockImplementationOnce(() => earlier.promise)
    choose('earlier', 'New')
    act(() => client.setDocId('intermediate'))
    choose('roof-plan_2', 'Open')
    await screen.findByText('Opened roof-plan_2 r3')
    await act(async () => earlier.resolve(response({}, 404)))
    expect(client.currentDocId()).toBe('roof-plan_2')
    expect(screen.getByText('Opened roof-plan_2 r3')).toBeVisible()
    expect(take).toHaveBeenCalledExactlyOnceWith(graph)
  })
})
