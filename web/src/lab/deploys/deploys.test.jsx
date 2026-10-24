import { act, cleanup, render, screen, within } from '@testing-library/react'
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

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('Deploys', () => {
  it('shows eight ordered kinds, four labelled lines and five static handoffs', () => {
    const { client } = ledger()
    render(<DeploysView client={client} />)
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
    expect(cards[1]).toHaveTextContent('Activation evidence arrives with SEL-312')
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
    const view = render(<DeploysView client={client} />)
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
    const view = render(<DeploysView hidden client={client} />)
    expect(view.container).toBeEmptyDOMElement()
    expect(read).not.toHaveBeenCalled()
    expect(client.subscribe).not.toHaveBeenCalled()
    view.rerender(<DeploysView client={client} />)
    expect(client.subscribe).toHaveBeenCalledTimes(1)
    read.mockClear()
    view.rerender(<DeploysView hidden client={client} />)
    expect(view.container).toBeEmptyDOMElement()
    expect(read).not.toHaveBeenCalled()
    expect(unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('does not fetch while hidden or visible and reports missing ledger evidence', () => {
    const fetchImpl = vi.fn(() => { throw new Error('Unexpected network request') })
    const client = createLabArtifactsClient({ fetchImpl })
    const view = render(<DeploysView hidden client={client} />)
    expect(fetchImpl).not.toHaveBeenCalled()
    view.rerender(<DeploysView client={client} />)
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Unpublished')
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('Publish receipt id unavailable')
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})
