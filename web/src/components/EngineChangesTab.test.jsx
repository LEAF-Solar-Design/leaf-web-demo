import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'

vi.mock('../engineChanges.js', async (importOriginal) => ({
  ...await importOriginal(),
  listEngineChanges: vi.fn(), getEngineChange: vi.fn(),
  markEngineChangeRead: vi.fn(), requestEngineChangeHold: vi.fn(),
}))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })), postMessage: vi.fn(),
  resolveApproval: vi.fn(), listPendingApprovals: vi.fn(async () => []),
  cancelTurn: vi.fn(), classifyAgentError: vi.fn(() => 'unknown'),
}))
vi.mock('../telemetry.js', () => ({ track: vi.fn() }))

import {
  listEngineChanges, getEngineChange, markEngineChangeRead,
  requestEngineChangeHold, engineChangeDiscussText,
} from '../engineChanges.js'
import { postMessage } from '../converse.js'
import EngineChangesTab from './EngineChangesTab.jsx'
import ConversePanel from './ConversePanel.jsx'

const card = {
  card_id: 'change-1', created_at: '2026-10-01T12:00:00Z', updated_at: '2026-10-01T12:00:00Z',
  title: 'Restore selection focus', state: 'accepted', feature_id: 'selection', unread: true,
  summary: 'Panel selection lost focus after refresh. Focus now returns to the selected row.',
  change: { pr_number: 123, pr_url: 'https://github.com/leaf/repo/pull/123', head_sha: 'abcdef1234567890', files: ['web/src/selection.js'], diff_stat: { additions: 4, deletions: 2 } },
  evidence: { regression_spec: 'web/e2e/selection.spec.mjs', before_ref: '/artifacts/before.png', after_ref: '/artifacts/after.png', receipt_ids: [] },
  acceptance: { verdict: 'accepted', acceptor: 'Fable', accepted_at: '2026-10-01T12:00:00Z' },
  deployment_identity: { release: 'studio-42', head_sha: 'abcdef1234567890' },
  hold_requested_at: null, hold_requested_by: null,
}
const older = { ...card, card_id: 'change-2', title: 'Repair export', feature_id: 'export', state: 'live', created_at: '2026-09-30T12:00:00Z' }
const list = { kind: 'ok', cards: [older, card], unread_count: 2, next_cursor: null }
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }

function ControlledTab({ result = list, onCardChange, ...props }) {
  const [current, setCurrent] = useState(result)
  return <EngineChangesTab result={current} {...props} onCardChange={(id, patch) => {
    setCurrent((previous) => ({ ...previous, cards: previous.cards?.map((item) => item.card_id === id ? { ...item, ...patch } : item) }))
    onCardChange?.(id, patch)
  }} />
}
const setup = (props = {}) => render(<ControlledTab onDiscuss={vi.fn()} onRetry={vi.fn()} {...props} />)
const openCard = async () => {
  fireEvent.click(screen.getByRole('button', { name: /Restore selection focus/ }))
  await screen.findByRole('heading', { name: 'What changed' })
}

beforeEach(() => {
  vi.clearAllMocks()
  listEngineChanges.mockResolvedValue(list)
  getEngineChange.mockResolvedValue({ kind: 'ok', card })
  markEngineChangeRead.mockResolvedValue({ kind: 'ok', card: { ...card, unread: false } })
  requestEngineChangeHold.mockResolvedValue({ kind: 'ok', card: { ...card, hold_requested_at: '2026-10-01T13:00:00Z', hold_requested_by: 'Admin' } })
  postMessage.mockResolvedValue({ turn_id: 'discussion-turn', status: 'started' })
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('engine changes list and detail', () => {
  it('loads older pages with the server cursor and appends without duplicate cards', async () => {
    const oldest = { ...older, card_id: 'change-3', title: 'Repair save', created_at: '2026-09-29T12:00:00Z' }
    const pending = deferred()
    listEngineChanges.mockReturnValueOnce(pending.promise)
    setup({ result: { ...list, cards: [card], next_cursor: 'cursor-one' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load older changes' }))
    expect(listEngineChanges).toHaveBeenCalledWith({ before: 'cursor-one' })
    expect(screen.getByRole('button', { name: 'Loading older changes…' }).disabled).toBe(true)
    await act(async () => pending.resolve({ ...list, cards: [card, older], next_cursor: 'cursor-two' }))
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    listEngineChanges.mockResolvedValueOnce({ ...list, cards: [older, oldest], next_cursor: null })
    fireEvent.click(screen.getByRole('button', { name: 'Load older changes' }))
    await screen.findByRole('button', { name: /Repair save/ })
    expect(listEngineChanges).toHaveBeenLastCalledWith({ before: 'cursor-two' })
    expect(screen.getAllByRole('listitem')).toHaveLength(3)
    expect(screen.queryByRole('button', { name: 'Load older changes' })).toBeNull()
  })
  it('keeps loaded rows and retries the same cursor after a page failure', async () => {
    listEngineChanges.mockResolvedValueOnce({ ...list, cards: [older], next_cursor: 'cursor-two' })
      .mockResolvedValueOnce({ kind: 'unavailable' })
      .mockResolvedValueOnce({ ...list, cards: [], next_cursor: null })
    setup({ result: { ...list, cards: [card], next_cursor: 'cursor-one' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load older changes' }))
    await screen.findByRole('button', { name: /Repair export/ })
    fireEvent.click(screen.getByRole('button', { name: 'Load older changes' }))
    const retry = await screen.findByRole('button', { name: 'Retry loading older changes' })
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    fireEvent.click(retry)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry loading older changes' })).toBeNull())
    expect(listEngineChanges.mock.calls.map(([input]) => input.before)).toEqual(['cursor-one', 'cursor-two', 'cursor-two'])
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })
  it('shows newest first, states, feature IDs, relative time, and unread indicators', () => {
    setup()
    const rows = screen.getAllByRole('listitem')
    expect(rows[0].textContent).toContain(card.title)
    expect(rows[1].textContent).toContain(older.title)
    expect(within(rows[0]).getByText('Accepted')).toBeTruthy()
    expect(within(rows[1]).getByText('Live')).toBeTruthy()
    expect(within(rows[0]).getByText('selection')).toBeTruthy()
    expect(rows[0].querySelector('time').textContent).toMatch(/ago|Just now/)
    expect(screen.getAllByLabelText('Unread')).toHaveLength(2)
  })
  it('shows the empty and reserved loading states', () => {
    const view = setup({ result: { ...list, cards: [] } })
    expect(screen.getByText('No engine changes have been accepted yet.')).toBeTruthy()
    view.unmount()
    setup({ result: null, loading: true })
    expect(screen.getByRole('status').textContent).toBe('Loading engine changes…')
    expect(screen.queryByText('No engine changes have been accepted yet.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })
  it('offers retry when the store is unavailable', () => {
    const onRetry = vi.fn()
    setup({ result: { kind: 'unavailable' }, onRetry })
    expect(screen.getByText(/Engine changes are unavailable/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })
  it('opens full detail, optimistically marks read, and restores row focus on Back', async () => {
    const read = deferred()
    markEngineChangeRead.mockReturnValue(read.promise)
    const onCardChange = vi.fn()
    setup({ onCardChange })
    await openCard()
    expect(getEngineChange).toHaveBeenCalledWith(card.card_id)
    expect(markEngineChangeRead).toHaveBeenCalledTimes(1)
    expect(onCardChange).toHaveBeenCalledWith(card.card_id, { unread: false })
    expect(screen.getByText(card.summary)).toBeTruthy()
    expect(screen.getByText('abcdef12')).toBeTruthy()
    expect(screen.getByText(card.change.files[0])).toBeTruthy()
    expect(screen.getByText(/additions: 4/)).toBeTruthy()
    expect(screen.getByText('Fable')).toBeTruthy()
    expect(screen.getByText(/studio-42/)).toBeTruthy()
    const pr = screen.getByRole('link', { name: card.change.pr_url })
    expect(pr.getAttribute('target')).toBe('_blank')
    expect(pr.getAttribute('rel')).toContain('noopener')
    expect(screen.getByRole('button', { name: 'Back' })).toBe(document.activeElement)
    await act(async () => read.resolve({ kind: 'ok', card: { ...card, unread: false } }))
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getAllByLabelText('Unread')).toHaveLength(1)
    expect(screen.getByRole('button', { name: /Restore selection focus/ })).toBe(document.activeElement)
    await openCard()
    expect(markEngineChangeRead).toHaveBeenCalledTimes(1)
  })
  it('reverts the optimistic read when the receipt fails and Escape restores focus', async () => {
    markEngineChangeRead.mockResolvedValue({ kind: 'unavailable' })
    const onCardChange = vi.fn()
    setup({ onCardChange })
    await openCard()
    await screen.findByText('Could not mark this change as read.')
    expect(onCardChange).toHaveBeenLastCalledWith(card.card_id, { unread: true })
    fireEvent.keyDown(screen.getByRole('button', { name: 'Back' }), { key: 'Escape' })
    expect(screen.getAllByLabelText('Unread')).toHaveLength(2)
    expect(screen.getByRole('button', { name: /Restore selection focus/ })).toBe(document.activeElement)
  })
  it('keeps a failed detail read recoverable without repeating the read POST', async () => {
    getEngineChange.mockResolvedValueOnce({ kind: 'unavailable' }).mockResolvedValue({ kind: 'ok', card })
    setup()
    fireEvent.click(screen.getByRole('button', { name: /Restore selection focus/ }))
    await screen.findByText(/Engine change details are unavailable/)
    expect(screen.getByRole('button', { name: 'Discuss' }).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByRole('heading', { name: 'What changed' })
    expect(markEngineChangeRead).toHaveBeenCalledTimes(1)
  })
  it('ignores a late detail response after Back', async () => {
    const detail = deferred()
    getEngineChange.mockReturnValue(detail.promise)
    setup()
    fireEvent.click(screen.getByRole('button', { name: /Restore selection focus/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await act(async () => detail.resolve({ kind: 'ok', card }))
    expect(screen.queryByRole('heading', { name: 'What changed' })).toBeNull()
  })
  it('passes a seeded turn to the injected discussion sender and explains a disabled composer', async () => {
    const onDiscuss = vi.fn()
    setup({ onDiscuss })
    await openCard()
    fireEvent.click(screen.getByRole('button', { name: 'Discuss' }))
    expect(onDiscuss).toHaveBeenCalledWith(engineChangeDiscussText(card))
    cleanup()
    setup({ onDiscuss, discussDisabledReason: 'Wait for the assistant to finish.' })
    await openCard()
    expect(screen.getByRole('button', { name: 'Discuss' }).disabled).toBe(true)
    expect(screen.getByText('Wait for the assistant to finish.')).toBeTruthy()
  })
  it('renders executable references as text without turning them into links', async () => {
    getEngineChange.mockResolvedValue({ kind: 'ok', card: { ...card, evidence: { ...card.evidence, before_ref: 'javascript:alert(1)' } } })
    setup()
    await openCard()
    expect(screen.getByText('javascript:alert(1)').tagName).toBe('SPAN')
  })
})

describe('hold requests', () => {
  it('confirms inline, latches double clicks, and displays the persisted requester and time', async () => {
    const held = deferred()
    requestEngineChangeHold.mockReturnValue(held.promise)
    setup()
    await openCard()
    fireEvent.click(screen.getByRole('button', { name: 'Request hold' }))
    expect(requestEngineChangeHold).not.toHaveBeenCalled()
    const confirm = screen.getByRole('button', { name: 'Confirm request' })
    fireEvent.click(confirm)
    fireEvent.click(confirm)
    expect(requestEngineChangeHold).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Requesting hold…' }).disabled).toBe(true)
    await act(async () => held.resolve({ kind: 'ok', card: { ...card, hold_requested_by: 'Admin', hold_requested_at: '2026-10-01T13:00:00Z' } }))
    expect(screen.getByText(/Hold requested by Admin at/)).toBeTruthy()
    expect(screen.getByText('2026-10-01T13:00:00Z')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Request hold' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await openCard()
    expect(screen.getByText(/Hold requested by Admin at/)).toBeTruthy()
    expect(requestEngineChangeHold).toHaveBeenCalledTimes(1)
  })
  it('shows an existing hold without offering another POST', async () => {
    getEngineChange.mockResolvedValue({ kind: 'ok', card: { ...card, hold_requested_by: 'Other admin', hold_requested_at: '2026-10-01T13:00:00Z' } })
    setup()
    await openCard()
    expect(screen.getByText(/Hold requested by Other admin at/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Request hold' })).toBeNull()
    expect(requestEngineChangeHold).not.toHaveBeenCalled()
  })
  it('can cancel the confirmation without requesting a hold', async () => {
    setup()
    await openCard()
    fireEvent.click(screen.getByRole('button', { name: 'Request hold' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Request hold' })).toBeTruthy()
    expect(requestEngineChangeHold).not.toHaveBeenCalled()
  })
  it('re-reads after an ambiguous hold failure instead of sending a second POST', async () => {
    requestEngineChangeHold.mockResolvedValue({ kind: 'unavailable' })
    setup()
    await openCard()
    fireEvent.click(screen.getByRole('button', { name: 'Request hold' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm request' }))
    await screen.findByText(/Could not confirm the hold request/)
    expect(screen.getByRole('button', { name: 'Request hold' }).disabled).toBe(true)
    getEngineChange.mockResolvedValue({ kind: 'ok', card: { ...card, hold_requested_by: 'Admin', hold_requested_at: '2026-10-01T13:00:00Z' } })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh card' }))
    await screen.findByText(/Hold requested by Admin at/)
    expect(requestEngineChangeHold).toHaveBeenCalledTimes(1)
  })
})

describe('assistant tab integration', () => {
  it('does not add a tablist for a forbidden user', async () => {
    listEngineChanges.mockResolvedValue({ kind: 'forbidden' })
    render(<ConversePanel sessionId="session-1" />)
    await waitFor(() => expect(listEngineChanges).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('tablist')).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' })).toBeTruthy()
  })
  it('Discuss sends only the seed and preserves an unsent composer image', async () => {
    const revoke = vi.fn()
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => 'blob:pending-image')
      static revokeObjectURL = revoke
    })
    const readImage = vi.spyOn(FileReader.prototype, 'readAsDataURL')
    render(<ConversePanel sessionId="session-1" />)
    const changes = await screen.findByRole('tab', { name: 'Engine changes, 2 unread' })
    const file = new File(['unsent image'], 'pending.png', { type: 'image/png' })
    fireEvent.paste(screen.getByRole('textbox', { name: 'Reply to the assistant' }), {
      clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }] },
    })
    expect(screen.getByRole('button', { name: 'Remove image attachment' })).toBeTruthy()
    fireEvent.click(changes)
    await openCard()
    fireEvent.click(screen.getByRole('button', { name: 'Discuss' }))
    await waitFor(() => expect(postMessage).toHaveBeenCalledTimes(1))
    expect(postMessage).toHaveBeenCalledWith('session-1', { text: engineChangeDiscussText(card), allowSecretOnce: false })
    expect(readImage).not.toHaveBeenCalled()
    expect(revoke).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Remove image attachment' })).toBeTruthy()
    expect(screen.getByAltText('Pending image attachment').getAttribute('src')).toBe('blob:pending-image')
    expect(screen.queryByAltText('User attached image')).toBeNull()
  })
  it('supports roving tabs, updates the unread badge, and sends Discuss into the existing session', async () => {
    render(<ConversePanel sessionId="session-1" />)
    const changes = await screen.findByRole('tab', { name: 'Engine changes, 2 unread' })
    const conversation = screen.getByRole('tab', { name: 'Conversation' })
    conversation.focus()
    fireEvent.keyDown(conversation, { key: 'ArrowRight' })
    expect(changes).toBe(document.activeElement)
    expect(changes.getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tabpanel').id).toBe(changes.getAttribute('aria-controls'))
    await openCard()
    await screen.findByRole('tab', { name: 'Engine changes, 1 unread' })
    fireEvent.click(screen.getByRole('button', { name: 'Discuss' }))
    await waitFor(() => expect(postMessage).toHaveBeenCalledWith('session-1', { text: engineChangeDiscussText(card), allowSecretOnce: false }))
    expect(conversation.getAttribute('aria-selected')).toBe('true')
    await waitFor(() => expect(screen.getByRole('log').textContent).toContain(card.title))
  })
  it('hides zero badges, caps visible badges at 9+, and exposes the full accessible count', async () => {
    listEngineChanges.mockResolvedValue({ ...list, unread_count: 12 })
    const view = render(<ConversePanel sessionId="session-1" />)
    expect((await screen.findByRole('tab', { name: 'Engine changes, 12 unread' })).textContent).toContain('9+')
    view.unmount()
    listEngineChanges.mockResolvedValue({ ...list, unread_count: 0 })
    render(<ConversePanel sessionId="session-1" />)
    const tab = await screen.findByRole('tab', { name: 'Engine changes' })
    expect(tab.querySelector('.engine-changes-badge')).toBeNull()
  })
  it('refreshes when opened and keeps unavailable results retryable after access is established', async () => {
    listEngineChanges.mockResolvedValueOnce(list).mockResolvedValueOnce({ kind: 'unavailable' }).mockResolvedValue(list)
    render(<ConversePanel sessionId="session-1" />)
    fireEvent.click(await screen.findByRole('tab', { name: /Engine changes/ }))
    await screen.findByText(/Engine changes are unavailable/)
    expect(screen.getByRole('tab', { name: 'Engine changes, 2 unread' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByRole('button', { name: /Restore selection focus/ })
    expect(listEngineChanges).toHaveBeenCalledTimes(3)
  })
  it('polls only while visible, prevents overlap, and stops polling on unmount', async () => {
    vi.useFakeTimers()
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const pending = deferred()
    listEngineChanges.mockReturnValueOnce(pending.promise).mockResolvedValue(list)
    const view = render(<ConversePanel sessionId="session-1" />)
    await act(async () => vi.advanceTimersByTimeAsync(60_000))
    expect(listEngineChanges).toHaveBeenCalledTimes(1)
    await act(async () => pending.resolve(list))
    await act(async () => vi.advanceTimersByTimeAsync(30_000))
    expect(listEngineChanges).toHaveBeenCalledTimes(2)
    visibility.mockReturnValue('hidden')
    await act(async () => vi.advanceTimersByTimeAsync(60_000))
    expect(listEngineChanges).toHaveBeenCalledTimes(2)
    view.unmount()
    visibility.mockReturnValue('visible')
    await act(async () => vi.advanceTimersByTimeAsync(60_000))
    expect(listEngineChanges).toHaveBeenCalledTimes(2)
  })
  it('does not let a stale in-flight list resurrect an optimistic unread badge', async () => {
    const stale = deferred()
    listEngineChanges.mockResolvedValueOnce(list).mockReturnValueOnce(stale.promise)
    render(<ConversePanel sessionId="session-1" />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Engine changes, 2 unread' }))
    await openCard()
    await screen.findByRole('tab', { name: 'Engine changes, 1 unread' })
    await act(async () => stale.resolve(list))
    expect(screen.getByRole('tab', { name: 'Engine changes, 1 unread' })).toBeTruthy()
  })
})
