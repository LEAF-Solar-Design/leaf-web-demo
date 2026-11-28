import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DeploysView from './DeploysView.jsx'
import { createLabArtifactsClient } from '../history/labArtifactsClient.js'

function ledger() {
  const listeners = new Set()
  const unsubscribe = vi.fn()
  const state = { head: 3, published: 2, lastSaved: { revision: 2, receipt_id: 'publish-2' } }
  const read = vi.fn(() => state)
  const client = {
    get state() { return read() },
    subscribe: vi.fn(listener => {
      listeners.add(listener)
      return () => { listeners.delete(listener); unsubscribe() }
    }),
  }
  return { client, read, unsubscribe, update: patch => {
    Object.assign(state, patch)
    act(() => listeners.forEach(listener => listener()))
  } }
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

const emptyActivations = async () => ({ ok: true, json: async () => ({ activations: [] }) })

describe('Deploys', () => {
  it('shows three ordered workspace actions and four plain status lines without internal handoffs', () => {
    const { client } = ledger()
    render(<DeploysView client={client} fetchImpl={emptyActivations} />)
    const cards = screen.getAllByRole('article')
    expect(cards).toHaveLength(3)
    expect(screen.getByRole('list').children).toHaveLength(3)
    const titles = ['Revision publication', 'Scheduled runs', 'Widget publication']
    cards.forEach((card, index) => {
      expect(card).toHaveAttribute('data-kind', String(index + 1))
      expect(card).toHaveAttribute('data-live', index < 2 ? 'true' : 'false')
      expect(within(card).getByRole('heading')).toHaveTextContent(titles[index])
      expect([...card.querySelectorAll('dt')].map(line => line.textContent)).toEqual(['Action', 'Status', 'Confirmation', 'Undo'])
      expect(card.querySelectorAll('dd')).toHaveLength(4)
      expect(within(card).queryByText('Handoff')).toBeNull()
    })
    expect(cards[0]).toHaveTextContent('Saved revision 3')
    expect(cards[0]).toHaveTextContent('Published r2')
    expect(cards[0]).toHaveTextContent('Publication confirmed')
    expect(within(cards[0]).getByRole('link', { name: 'Open revision history' })).toHaveAttribute('href', expect.stringContaining('/app/lab/history'))
    expect(within(cards[1]).getByRole('button', { name: 'Activate published r2' })).toBeEnabled()
    expect(within(cards[2]).getByRole('button', { name: 'Publish widget' })).toBeDisabled()
    expect(within(cards[2]).getByRole('button', { name: 'Publish widget' })).toHaveAttribute('title', 'Widget publication is not available in this workspace yet.')
    expect(cards[2]).toHaveTextContent('Widget publication is not available in this workspace yet.')
    const legend = screen.getByRole('region', { name: 'Deploy state legend' })
    expect([...legend.querySelectorAll('dt')].map(line => line.textContent)).toEqual(['Published', 'Scheduled', 'Paused'])
    for (const state of ['Publishing does not start a run.', 'run at the chosen interval.', 'on hold until you resume them.']) {
      expect(legend).toHaveTextContent(state)
    }
    expect(screen.getByRole('region', { name: 'Deploys' }).textContent).not.toMatch(/SEL-309|\.claude|deploy-web\.py|--preview|observe_only|operator_yes|ECS|\.yml|publish-2|Tenant ledger|\b(Engine|Venue|Kelly|Book|Cashout)\b/i)
  })

  it('updates publication status and confirmation from the subscription and unsubscribes on unmount', () => {
    const { client, update, unsubscribe } = ledger()
    const view = render(<DeploysView client={client} fetchImpl={emptyActivations} />)
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Last saved r2')
    update({ published: 3, lastSaved: { revision: 3, receipt_id: 'publish-3' } })
    screen.getAllByRole('article').slice(0, 3).forEach(card => expect(card).toHaveTextContent('Published r3'))
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Last saved r3')
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Publication confirmed')
    expect(screen.getAllByRole('article')[0]).not.toHaveTextContent(/publish-2|publish-3/)
    update({ published: 4 })
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Published r4')
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Publication confirmation unavailable')
    view.unmount()
    expect(unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('makes no hidden reads or subscriptions and releases the subscription when hidden', () => {
    const { client, read, unsubscribe } = ledger()
    const view = render(<DeploysView hidden client={client} fetchImpl={emptyActivations} />)
    expect(view.container).toBeEmptyDOMElement()
    expect(read).not.toHaveBeenCalled()
    expect(client.subscribe).not.toHaveBeenCalled()
    view.rerender(<DeploysView client={client} fetchImpl={emptyActivations} />)
    expect(client.subscribe).toHaveBeenCalledTimes(1)
    read.mockClear()
    view.rerender(<DeploysView hidden client={client} fetchImpl={emptyActivations} />)
    expect(view.container).toBeEmptyDOMElement()
    expect(read).not.toHaveBeenCalled()
    expect(unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('fetches activations only when visible and reports missing publication confirmation', async () => {
    const fetchImpl = vi.fn(() => { throw new Error('Unexpected network request') })
    const client = createLabArtifactsClient({ fetchImpl })
    const activationRequest = vi.fn(emptyActivations)
    const view = render(<DeploysView hidden client={client} fetchImpl={activationRequest} />)
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(activationRequest).not.toHaveBeenCalled()
    view.rerender(<DeploysView client={client} fetchImpl={activationRequest} />)
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Unpublished')
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Publication confirmation unavailable')
    expect(fetchImpl).not.toHaveBeenCalled()
    await waitFor(() => expect(activationRequest).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'Activate published revision' })).toBeDisabled()
  })

  it('previews the pinned corpus plan with a GET and clears it when the interval or publication changes', async () => {
    const { client, update } = ledger()
    const fetchImpl = vi.fn(async (url, options) => {
      if (!url.includes('preview_doc_id')) return emptyActivations()
      const query = new URL(url, 'http://localhost').searchParams
      expect(query.get('preview_doc_id')).toBe('strategy')
      const plan = {
        doc_id: query.get('preview_doc_id'), revision: Number(query.get('revision')), interval_s: Number(query.get('interval_s')),
        graph_sha256: 'a'.repeat(64), registry_digest: 'b'.repeat(64), input_digest: 'c'.repeat(64),
        ops_revision: 'lab-ops.v1', max_run_wall_s: 600, pipes: [], outputs: {},
        blocks: [{ id: 'eval', registry: { id: 'corpus-evaluator', version: '1.0.0', code_sha256: 'd'.repeat(64) },
          settings_sha256: 'e'.repeat(64), effect: 'read', inputs: { corpus: { material_id: 'corpus-3',
            byte_size: 7, sha256: 'f'.repeat(64) } } }],
        blockers: [{ code: 'lab_activation_corpus_authority_unavailable', block: 'eval' }],
      }
      expect(options.method).toBe('GET')
      expect(options.body).toBeUndefined()
      return { ok: true, json: async () => plan }
    })
    render(<DeploysView client={client} fetchImpl={fetchImpl} />)
    fireEvent.click(screen.getByRole('button', { name: 'Preview execution plan' }))
    const plan = await screen.findByRole('region', { name: 'Execution plan' })
    expect(plan).toHaveTextContent('Published r2. Proposed interval 60 seconds.')
    expect(plan).toHaveTextContent('Corpus material corpus-3, 7 bytes.')
    expect(screen.getByRole('button', { name: 'Activate published r2' })).toBeDisabled()
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '90' } })
    expect(screen.queryByRole('region', { name: 'Execution plan' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Preview execution plan' }))
    expect(await screen.findByRole('region', { name: 'Execution plan' }))
      .toHaveTextContent('Published r2. Proposed interval 90 seconds.')
    update({ published: 3 })
    expect(screen.queryByRole('region', { name: 'Execution plan' })).toBeNull()
    expect(fetchImpl.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true)
  })

  it('keeps the control result when an older poll completes later', async () => {
    const { client } = ledger()
    const row = { activation_id: 'a1', doc_id: 'strategy', revision: 2, state: 'active', interval_s: 60,
      lease_expires_at: '2026-10-26T12:00:00Z', coalesced_ticks: 0,
      last_output: null, last_summary: null, transitions: [] }
    let poll, staleResponse
    const setInterval = globalThis.setInterval
    vi.spyOn(globalThis, 'setInterval').mockImplementation((callback, delay) => {
      if (delay === 15000) poll = callback
      return setInterval(callback, delay)
    })
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ activations: [row] }) })
      .mockImplementationOnce(() => new Promise(resolve => { staleResponse = resolve }))
      .mockResolvedValueOnce({ ok: true, json: async () => ({ state: 'paused' }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ activations: [{ ...row, state: 'paused' }] }) })
    render(<DeploysView client={client} fetchImpl={fetchImpl} />)
    await screen.findByRole('button', { name: 'Pause' })
    act(() => { poll() })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled())
    await act(async () => {
      staleResponse({ ok: true, json: async () => ({ activations: [row] }) })
    })
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled()
  })

  it('F3 polls at fifteen second intervals only while visible', async () => {
    const { client } = ledger()
    let poll
    const timer = vi.spyOn(globalThis, 'setInterval').mockImplementation((callback, delay) => {
      expect(delay).toBeGreaterThanOrEqual(15000)
      poll = callback
      return 123
    })
    const clear = vi.spyOn(globalThis, 'clearInterval')
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const fetchImpl = vi.fn(emptyActivations)
    const view = render(<DeploysView client={client} fetchImpl={fetchImpl} />)
    await act(async () => {})
    expect(timer).toHaveBeenCalledWith(expect.any(Function), 15000)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    visibility.mockReturnValue('hidden')
    await act(async () => { await poll() })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    visibility.mockReturnValue('visible')
    await act(async () => { await poll() })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    view.rerender(<DeploysView hidden client={client} fetchImpl={fetchImpl} />)
    expect(clear).toHaveBeenCalledWith(123)
  })

  it.each([
    ['documents at the same revision and cadence', { doc_id: 'other' }, '', 'Document other · Revision 2 · Every 60 seconds'],
    ['revisions of the same document', { revision: 1, state: 'stopped' }, '', 'Document strategy · Revision 1 · Every 60 seconds'],
    ['cadences of the same revision', { interval_s: 120 }, '', 'Document strategy · Revision 2 · Every 120 seconds'],
    ['duplicate schedules', {}, ' · Schedule first-id', 'Document strategy · Revision 2 · Every 60 seconds · Schedule second-id'],
  ])('distinguishes %s and turns off only the selected schedule', async (_, difference, firstSuffix, secondLabel) => {
    const { client } = ledger()
    const first = { activation_id: 'first-id', doc_id: 'strategy', revision: 2, interval_s: 60, state: 'active' }
    const second = { ...first, ...difference, activation_id: 'second-id' }
    const fetchImpl = vi.fn(async (url, { method }) => {
      if (method === 'POST') {
        expect(url).toMatch(/\/activations\/second-id\/off$/)
        second.state = 'off'
      }
      return { ok: true, json: async () => ({ activations: [first, second] }) }
    })
    render(<DeploysView client={client} fetchImpl={fetchImpl} />)
    const target = await screen.findByRole('group', { name: secondLabel, exact: true })
    const sibling = screen.getByRole('group', {
      name: 'Document strategy · Revision 2 · Every 60 seconds' + firstSuffix, exact: true,
    })
    fireEvent.click(within(target).getByRole('button', { name: 'Off' }))
    await waitFor(() => expect(within(target).getByRole('button', { name: 'Off' })).toBeDisabled())
    expect(within(target).getByText('Off', { selector: 'p' })).toBeVisible()
    expect(within(sibling).getByRole('button', { name: 'Off' })).toBeEnabled()
    expect(within(sibling).getByText('Scheduled')).toBeVisible()
  })

  it('creates, lists and controls activations and shows errors plainly', async () => {
    const { client } = ledger()
    let rows = []
    const fetchImpl = vi.fn(async (url, options) => {
      if (options.method === 'POST') {
        if (url.endsWith('/activations')) {
          expect(JSON.parse(options.body)).toEqual({ doc_id: 'strategy', interval_s: 90, expected_revision: 2 })
          rows = [{ activation_id: 'a1', doc_id: 'strategy', revision: 2, state: 'active', interval_s: 90,
            lease_expires_at: '2026-10-26T12:00:00Z', coalesced_ticks: 2,
            last_output: { result: 7 }, last_summary: { state: 'finished' },
            transitions: [{ seq: 1, from_state: null, to_state: 'active', reason: 'created', at: 'now' }] }]
        } else {
          const action = url.split('/').at(-1)
          rows[0].state = { pause: 'paused', resume: 'active', renew: 'active', off: 'off' }[action]
        }
      }
      return { ok: true, json: async () => ({ activations: rows }) }
    })
    render(<DeploysView client={client} fetchImpl={fetchImpl} />)
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '59' } })
    expect(screen.getByRole('button', { name: 'Activate published r2' })).toBeDisabled()
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '90' } })
    fireEvent.click(screen.getByRole('button', { name: 'Activate published r2' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
    const card = screen.getAllByRole('article')[1]
    const schedule = within(card).getByRole('group', { name: 'Document strategy · Revision 2 · Every 90 seconds' })
    expect(within(schedule).getByText('Scheduled')).toBeVisible()
    expect(card).not.toHaveTextContent(/Coalesced ticks|"result"|created|a1/)
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Renew' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Off' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Off' }))
    await waitFor(() => expect(within(schedule).getByText('Off', { selector: 'p' })).toBeVisible())
    for (const name of ['Pause', 'Resume', 'Renew', 'Off']) expect(screen.getByRole('button', { name })).toBeDisabled()
    fetchImpl.mockResolvedValueOnce({ ok: false, json: async () => ({ detail: 'lab_activation_limit' }) })
    fireEvent.click(screen.getByRole('button', { name: 'Activate published r2' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong. Try again.')
    expect(card).not.toHaveTextContent('lab_activation_limit')
  })
})
