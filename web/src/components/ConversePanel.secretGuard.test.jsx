/**
 * The assistant reply box's credential guard (slice 8a, fix round 1).
 *
 * THE GAP THIS CLOSES: the command bar refused credential-shaped text, but this
 * panel's reply input posted straight to the SAME endpoint the bar's guard
 * protects (POST /api/sessions/{id}/messages, converse.js postMessage) with no
 * check at all. A user who read "Credentials never go to the model" in the bar
 * could paste the identical string two inches below and it reached model
 * context. So the load-bearing spec here is the NEGATIVE one: postMessage must
 * not be called.
 *
 * `../telemetry.js` and `../converse.js` are mocked so these specs pin this
 * panel's own choke point, never the transport underneath.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(() => Promise.resolve({ turn_id: 'turn-1', status: 'started' })),
  resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(() => Promise.resolve([])),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
}))

import { track } from '../telemetry.js'
import * as converse from '../converse.js'
import ConversePanel from './ConversePanel.jsx'
import { MASK_BULLETS, MASK_PREFIX, SECRET_REASONS } from '../lib/secretPatterns.js'

// Structurally valid, entirely fake — no real credential appears in this repo.
const FAKE_ANTHROPIC = `sk-ant-api03-${'A9_-'.repeat(12)}`
const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const setup = (props = {}) => render(
  <ConversePanel sessionId="session-1" onDismiss={vi.fn()} {...props} />,
)

const replyBox = () => screen.getByRole('textbox', { name: /reply to the assistant/i })
const notice = () => screen.queryByTestId('converse-secret-notice')
const reason = () => screen.getByTestId('converse-secret-notice-reason')

const type = (text) => fireEvent.change(replyBox(), { target: { value: text } })

// A generous ceiling, not a delay: these assertions settle in a few ms, and the
// wait only ever costs that much on a loaded CI box (the default 1 s ceiling
// flaked once under a parallel full-suite run).
const SETTLE = { timeout: 5000 }

describe('a named token shape refuses on Enter', () => {
  it('never posts the message, and says so honestly', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(converse.postMessage).not.toHaveBeenCalled()
    expect(notice()).toHaveAttribute('role', 'alert')
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
  })

  // The Send button used to be `onClick={send}`, which handed the click EVENT
  // to send()'s text parameter: it posted the literal string "[object Object]"
  // and never looked at the input at all, so it was outside the guard by
  // accident rather than by design. These two specs pin both halves — the
  // button sends what was typed, and the guard refuses it.
  it('refuses the Send button on the same terms', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.click(screen.getByRole('button', { name: /^send$/i }))

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(converse.postMessage).not.toHaveBeenCalled()
  })

  it('the Send button posts the typed reply, never the click event', async () => {
    setup()
    type('count the panels on the roofline layer')
    fireEvent.click(screen.getByRole('button', { name: /^send$/i }))

    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1), SETTLE)
    expect(converse.postMessage.mock.calls[0][1]).toEqual({ text: 'count the panels on the roofline layer' })
  })

  it('offers no override — a named shape is a hard refusal', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(screen.queryByTestId('converse-secret-send-anyway')).toBeNull()
  })

  it('shows a shape prefix behind fixed bullets, never the credential', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    const masked = screen.getByTestId('converse-secret-notice-mask').textContent
    expect(masked).toBe(`${FAKE_ANTHROPIC.slice(0, MASK_PREFIX)}${'•'.repeat(MASK_BULLETS)}`)
    // Not one window of the credential's entropy may appear anywhere in the DOM.
    const dom = document.body.textContent
    const entropy = FAKE_ANTHROPIC.slice(MASK_PREFIX)
    for (let i = 0; i + 8 <= entropy.length; i += 1) {
      expect(dom).not.toContain(entropy.slice(i, i + 8))
    }
  })

  it('telemetries the pattern identity and nothing else', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(track).toHaveBeenCalledWith('conversation.secret_refused', { pattern_id: 'anthropic' })
    for (const call of track.mock.calls) {
      expect(JSON.stringify(call)).not.toContain(FAKE_ANTHROPIC.slice(MASK_PREFIX))
    }
    // The refused text never counts as a sent message.
    expect(track).not.toHaveBeenCalledWith('conversation.message_sent', expect.anything())
  })
})

describe('the fuzzy generic shape is the only overridable one', () => {
  it('offers Send anyway, and only then posts', async () => {
    setup()
    type(FAKE_GENERIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(reason().textContent).toBe(SECRET_REASONS.generic)
    expect(converse.postMessage).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('converse-secret-send-anyway'))
    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1), SETTLE)
    expect(converse.postMessage.mock.calls[0][1]).toEqual({ text: FAKE_GENERIC })
  })

  it('spends the override once — the next credential refuses again', async () => {
    // The override send is made to FAIL so no turn goes pending and the box
    // stays enabled; what is under test is the ref, not the transport.
    converse.postMessage.mockRejectedValueOnce(new Error('network down'))
    setup()
    type(FAKE_GENERIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    fireEvent.click(screen.getByTestId('converse-secret-send-anyway'))
    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1), SETTLE)

    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(converse.postMessage).toHaveBeenCalledTimes(1)
  })

  it('a named shape beside a labelled assignment is never overridable', async () => {
    setup()
    type(`${FAKE_GENERIC} and ${FAKE_ANTHROPIC}`)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
    expect(screen.queryByTestId('converse-secret-send-anyway')).toBeNull()
    expect(converse.postMessage).not.toHaveBeenCalled()
  })
})

describe('the guard stays out of the way of ordinary replies', () => {
  it('sends plain text untouched, with no notice', async () => {
    setup()
    type('count the panels on the roofline layer')
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1), SETTLE)
    expect(notice()).toBeNull()
  })

  it('retires the notice as soon as the text changes', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)

    type('never mind')
    await waitFor(() => expect(notice()).toBeNull(), SETTLE)
  })
})
