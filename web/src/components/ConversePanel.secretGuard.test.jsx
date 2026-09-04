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
import {
  MASK_BULLETS,
  MASK_PREFIX,
  SECRET_REASONS,
  SECRET_REASONS_NO_MOUNT,
} from '../lib/secretPatterns.js'

// Structurally valid, entirely fake — no real credential appears in this repo.
const FAKE_ANTHROPIC = `sk-ant-api03-${'A9_-'.repeat(12)}`
const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const setup = (props = {}) => render(
  <ConversePanel sessionId="session-1" onDismiss={vi.fn()} credentialMountAvailable {...props} />,
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

describe('the override cannot latch open (fix round 2)', () => {
  // THE DEFECT THIS PINS. Round 1 read-and-disarmed the override BELOW send()'s
  // `if ((!text && !attachments.length) || busy) return false`. This panel is
  // mounted beside the command bar and both drive the same session, so a bar
  // dispatch (or a promoting queued turn, or a resumed in-flight turn) flips
  // `busy` true while a refusal notice is still on screen. Clicking "Send
  // anyway" in that window armed the ref and returned without spending it;
  // onChange cleared the notice but NOT the ref, and the very next Enter
  // skipped the guard entirely and posted a hard-refusal shape.
  //
  // The fix has two halves and both are pinned: the button is disabled while
  // busy (here), and the read-and-disarm moved above every early return, so
  // even a click that got through would spend the override on the no-op
  // (pinned structurally in composer.test.mjs, which reads the send() source,
  // because a disabled button cannot be clicked from jsdom at all).
  const IN_FLIGHT = [{ turnId: 'turn-in-flight', text: 'an earlier request' }]

  const renderWith = (userTurns) => (
    <ConversePanel
      sessionId="session-1"
      onDismiss={vi.fn()}
      credentialMountAvailable
      userTurns={userTurns}
    />
  )

  it('clicking Send anyway while busy arms nothing: the next named shape still refuses', async () => {
    const { rerender } = render(renderWith([]))
    type(FAKE_GENERIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)

    // A turn starts elsewhere in the app while the notice is still up.
    rerender(renderWith(IN_FLIGHT))
    const sendAnyway = screen.getByTestId('converse-secret-send-anyway')
    expect(sendAnyway, 'a control that cannot spend an override must not arm one').toBeDisabled()
    fireEvent.click(sendAnyway)
    expect(converse.postMessage).not.toHaveBeenCalled()

    // The turn finishes; the user types a HARD-refusal shape and hits Enter.
    rerender(renderWith([]))
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(reason().textContent).toBe(SECRET_REASONS.anthropic), SETTLE)
    expect(converse.postMessage, 'a latched override must never post a credential').not.toHaveBeenCalled()
  })

  it('an edit clears the notice but can never re-arm the spent override', async () => {
    const { rerender } = render(renderWith([]))
    type(FAKE_GENERIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)

    rerender(renderWith(IN_FLIGHT))
    fireEvent.click(screen.getByTestId('converse-secret-send-anyway'))
    rerender(renderWith([]))

    type('never mind')
    await waitFor(() => expect(notice()).toBeNull(), SETTLE)
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(converse.postMessage).not.toHaveBeenCalled()
  })
})

describe('the copy is honest about the mode it is shown in', () => {
  // ClaudeAccountPanel returns null under mock, and App mounts it under
  // `{!mock && ...}`, so a notice that says "Mount it under Claude accounts"
  // in a mode that renders no such control is a lie in a friendly voice.
  it('names no surface where the Claude accounts panel is not mounted', async () => {
    render(<ConversePanel sessionId="session-1" onDismiss={vi.fn()} />)
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(reason().textContent).toBe(SECRET_REASONS_NO_MOUNT.anthropic)
    expect(reason().textContent).not.toContain('Claude accounts')
  })

  it('names it where it is mounted', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
  })
})
