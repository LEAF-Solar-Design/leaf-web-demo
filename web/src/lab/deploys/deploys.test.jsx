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
  it('shows eight ordered kinds, four labelled lines and five static handoffs', () => {
    const { client } = ledger()
    render(<DeploysView client={client} fetchImpl={emptyActivations} />)
    const cards = screen.getAllByRole('article')
    expect(cards).toHaveLength(8)
    expect(screen.getByRole('list').children).toHaveLength(8)
    const titles = ['Revision publish', 'Read-only activation', 'Widget publish', 'Studio release',
      'Ops UI and API release', 'Runner release', 'Trading lifecycle', 'Native renderer']
    cards.forEach((card, index) => {
      expect(card).toHaveAttribute('data-kind', String(index + 1))
      expect(card).toHaveAttribute('data-live', index < 3 ? 'true' : 'false')
      expect(within(card).getByRole('heading')).toHaveTextContent(titles[index])
      expect([...card.querySelectorAll('dt')].map(line => line.textContent)).toEqual(['Trigger', 'Rail', 'Receipt', 'Rollback'])
      expect(card.querySelectorAll('dd')).toHaveLength(4)
      expect(within(card).queryByText('Handoff') !== null).toBe(index >= 3)
    })
    expect(cards[0]).toHaveTextContent('Head r3')
    expect(cards[0]).toHaveTextContent('Published r2')
    expect(cards[0]).toHaveTextContent('publish-2')
    expect(within(cards[1]).getByRole('button', { name: 'Activate published r2' })).toBeEnabled()
    expect(cards[2]).toHaveTextContent('Widget publication arrives with SEL-309')
    expect(cards[3]).toHaveTextContent('--preview')
    expect(cards[3]).toHaveTextContent('observe_only')
    expect(cards[3]).toHaveTextContent('operator_yes')
    expect(cards[4]).toHaveTextContent('ops-dashboard')
    expect(cards[4]).toHaveTextContent('observe_only')
    expect(cards[4]).toHaveTextContent('standing authorization under OPERATOR-WISHES')
    expect(cards[4]).not.toHaveTextContent('operator_yes')
    expect(cards[4].querySelectorAll('dd')[1].querySelectorAll('span')).toHaveLength(2)
    expect(cards[6]).toHaveTextContent('Requested action:')
    expect(cards[6]).toHaveTextContent('Runner outcome:')
    expect(cards[6]).toHaveTextContent('Observed revision:')
    expect(cards[7]).toHaveTextContent('production exposure is separate')
    const legend = screen.getByRole('region', { name: 'Deploy state legend' })
    expect([...legend.querySelectorAll('dt')].map(line => line.textContent)).toEqual(['requested', 'published', 'running', 'observed'])
    for (const state of ['AUTHORIZED pending', 'PROMOTING with activation evidence', 'LIVE receipt', 'Tenant ledger publish receipt']) {
      expect(legend).toHaveTextContent(state)
    }
    expect(legend).toHaveTextContent('<surface>__<candidate>__<utc>__<to_state>.json')
    expect(screen.getByRole('region', { name: 'Deploys' })).toHaveTextContent('migrate-leaf-platform-production-db.yml runs before any migration-bearing image, even when the Lab rail is off')
    expect(screen.getByRole('region', { name: 'Deploys' }).textContent).not.toMatch(/\b(Engine|Venue|Kelly|Book|Cashout)\b/i)
  })

  it('updates only ledger cards on publication and unsubscribes on unmount', () => {
    const { client, update, unsubscribe } = ledger()
    const view = render(<DeploysView client={client} fetchImpl={emptyActivations} />)
    const handoffs = screen.getAllByRole('article').slice(3).map(card => card.textContent)
    update({ published: 3, lastSaved: { revision: 3, receipt_id: 'publish-3' } })
    screen.getAllByRole('article').slice(0, 3).forEach(card => expect(card).toHaveTextContent('Published r3'))
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('publish-3')
    expect(screen.getAllByRole('article').slice(3).map(card => card.textContent)).toEqual(handoffs)
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

  it('fetches activations only when visible and reports missing ledger evidence', async () => {
    const fetchImpl = vi.fn(() => { throw new Error('Unexpected network request') })
    const client = createLabArtifactsClient({ fetchImpl })
    const activationRequest = vi.fn(emptyActivations)
    const view = render(<DeploysView hidden client={client} fetchImpl={activationRequest} />)
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(activationRequest).not.toHaveBeenCalled()
    view.rerender(<DeploysView client={client} fetchImpl={activationRequest} />)
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Unpublished')
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Publish receipt id unavailable')
    expect(fetchImpl).not.toHaveBeenCalled()
    await waitFor(() => expect(activationRequest).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'Activate published revision' })).toBeDisabled()
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

  it('creates, lists and controls activations and shows errors plainly', async () => {
    const { client } = ledger()
    let rows = []
    const fetchImpl = vi.fn(async (url, options) => {
      if (options.method === 'POST') {
        if (url.endsWith('/activations')) {
          expect(JSON.parse(options.body)).toEqual({ doc_id: 'strategy', interval_s: 90 })
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
    expect(card).toHaveTextContent('Coalesced ticks: 2')
    expect(card).toHaveTextContent('"result":7')
    expect(card).toHaveTextContent('new → active (created)')
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Renew' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Off' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Off' }))
    await waitFor(() => expect(card).toHaveTextContent('strategy r2: off'))
    for (const name of ['Pause', 'Resume', 'Renew', 'Off']) expect(screen.getByRole('button', { name })).toBeDisabled()
    fetchImpl.mockResolvedValueOnce({ ok: false, json: async () => ({ detail: 'lab_activation_limit' }) })
    fireEvent.click(screen.getByRole('button', { name: 'Activate published r2' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('lab_activation_limit')
  })
})
