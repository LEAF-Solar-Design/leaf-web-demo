/**
 * The assistant reply box's credential REFUSAL (slice 8a, round 3).
 *
 * The panel no longer decides anything about credentials. converse.postMessage
 * does, on the wire, and this box's job is to catch that typed refusal, render
 * it as its own notice rather than as an outage banner, and offer a per-call
 * "Send anyway" for the one overridable shape.
 *
 * `../converse.js` is mocked, but its postMessage runs the REAL guard seam
 * (vi.importActual), so these rows exercise the actual refusal contract — the
 * override really has to arrive as a call parameter for the second send to go
 * through. A hand-rolled fake refusal would prove only that the panel can
 * render an object.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', async () => {
  const guard = await vi.importActual('../lib/secretGuardTransport.js')
  return {
    openStream: vi.fn(() => ({ close: vi.fn() })),
    // The real transport contract, standing in for the real transport.
    postMessage: vi.fn(async (sessionId, { text, allowSecretOnce = false } = {}) => {
      if (text != null) {
        const verdict = guard.guardedText(text, { allowSecretOnce })
        if (!verdict.ok) throw new guard.SecretRefusedError(verdict.refusal)
      }
      return { turn_id: 'turn-1', status: 'started' }
    }),
    resolveApproval: vi.fn(),
    listPendingApprovals: vi.fn(() => Promise.resolve([])),
    cancelTurn: vi.fn(),
    classifyAgentError: vi.fn(() => 'unreachable'),
  }
})

import { track } from '../telemetry.js'
import * as converse from '../converse.js'
import ConversePanel from './ConversePanel.jsx'
import { setCredentialMountAvailable } from '../lib/secretGuardTransport.js'
import {
  MASK_BULLETS,
  MASK_PREFIX,
  SECRET_REASONS,
  SECRET_REASONS_NO_MOUNT,
} from '../lib/secretPatterns.js'

// Structurally valid, entirely fake — no real credential appears in this repo.
const FAKE_ANTHROPIC = `sk-ant-api03-${'A9_-'.repeat(12)}`
const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`

beforeEach(() => {
  // The live app: App mounts this panel and ClaudeAccountPanel under the same
  // `!mock`, so where this can refuse, that control is on screen.
  setCredentialMountAvailable(true)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  setCredentialMountAvailable(false)
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

describe('a named token shape is refused on the wire and shown here', () => {
  it('renders the refusal as its own notice, not as a failure banner', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(notice()).toHaveAttribute('role', 'alert')
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
    // "The assistant is unavailable" would be a lie about a decision that never
    // left the browser, so the send-error banner must stay absent.
    expect(screen.queryByTestId('converse-send-error')).toBeNull()
    expect(document.body.textContent).not.toContain('could not be delivered')
  })

  // The Send button used to be `onClick={send}`, which handed the click EVENT
  // to send()'s text parameter: it posted the literal string "[object Object]"
  // and never looked at the input at all. This pins the fixed half.
  it('the Send button posts the typed reply, never the click event', async () => {
    setup()
    type('count the panels on the roofline layer')
    fireEvent.click(screen.getByRole('button', { name: /^send$/i }))

    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1), SETTLE)
    expect(converse.postMessage.mock.calls[0][1]).toMatchObject({
      text: 'count the panels on the roofline layer',
      allowSecretOnce: false,
    })
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
    // Not one window of the credential's entropy may appear in the NOTICE.
    const shown = notice().textContent
    const entropy = FAKE_ANTHROPIC.slice(MASK_PREFIX)
    for (let i = 0; i + 8 <= entropy.length; i += 1) {
      expect(shown).not.toContain(entropy.slice(i, i + 8))
    }
  })

  it('never counts a refused reply as a sent message', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(track).not.toHaveBeenCalledWith('conversation.message_sent', expect.anything())
    for (const call of track.mock.calls) {
      expect(JSON.stringify(call)).not.toContain(FAKE_ANTHROPIC.slice(MASK_PREFIX))
    }
  })
})

describe('the fuzzy generic shape is the only overridable one', () => {
  it('offers Send anyway, and the re-issued call carries the authorisation', async () => {
    setup()
    type(FAKE_GENERIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(reason().textContent).toBe(SECRET_REASONS.generic)
    expect(converse.postMessage).toHaveBeenCalledTimes(1)
    expect(converse.postMessage.mock.calls[0][1]).toMatchObject({ allowSecretOnce: false })

    fireEvent.click(screen.getByTestId('converse-secret-send-anyway'))
    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(2), SETTLE)
    expect(converse.postMessage.mock.calls[1][1]).toMatchObject({
      text: FAKE_GENERIC,
      allowSecretOnce: true,
    })
    await waitFor(() => expect(notice()).toBeNull(), SETTLE)
  })

  it('a named shape beside a labelled assignment is never overridable', async () => {
    setup()
    type(`${FAKE_GENERIC} and ${FAKE_ANTHROPIC}`)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
    expect(screen.queryByTestId('converse-secret-send-anyway')).toBeNull()
  })
})

describe('the refusal stays out of the way of ordinary replies', () => {
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

describe('THE LATCH SCENARIO, rewritten: there is no arm step at all (round 3)', () => {
  // ROUNDS 1 AND 2 BOTH DIED HERE. The override was a ref: clicking "Send
  // anyway" armed it, and a click that landed while `busy` was true returned
  // before spending it, so the ref stayed armed and the NEXT Enter posted a
  // hard-refusal shape unguarded. Round 2 moved the read-and-disarm above the
  // early return; round 3 deleted the ref.
  //
  // So the scenario cannot be written as "arm, then dispatch a hard shape".
  // What it becomes is the sequence a user can actually perform, asserted at
  // the wire: whatever a click does or fails to do, EVERY later send carries
  // allowSecretOnce: false, and a named shape is refused every time.
  const IN_FLIGHT = [{ turnId: 'turn-in-flight', text: 'an earlier request' }]

  const renderWith = (userTurns) => (
    <ConversePanel sessionId="session-1" onDismiss={vi.fn()} userTurns={userTurns} />
  )

  it('a click that cannot send authorises nothing for any later send', async () => {
    const { rerender } = render(renderWith([]))
    type(FAKE_GENERIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(converse.postMessage).toHaveBeenCalledTimes(1)

    // A turn starts elsewhere in the app while the notice is still up.
    rerender(renderWith(IN_FLIGHT))
    const sendAnyway = screen.getByTestId('converse-secret-send-anyway')
    expect(sendAnyway, 'a control that cannot send must not look sendable').toBeDisabled()
    fireEvent.click(sendAnyway)
    expect(converse.postMessage).toHaveBeenCalledTimes(1)

    // The turn finishes; the user types a HARD-refusal shape and hits Enter.
    rerender(renderWith([]))
    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })

    await waitFor(() => expect(reason().textContent).toBe(SECRET_REASONS.anthropic), SETTLE)
    // It reached the transport and the transport refused it. The load-bearing
    // half is the flag: nothing carried an authorisation forward.
    expect(converse.postMessage).toHaveBeenCalledTimes(2)
    expect(converse.postMessage.mock.calls[1][1]).toMatchObject({ allowSecretOnce: false })
  })

  it('a SPENT override does not carry to the next send either', async () => {
    setup()
    type(FAKE_GENERIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    // The override send is made to FAIL, so no turn goes pending and the box
    // stays enabled. What is under test is the authorisation, not the wire.
    converse.postMessage.mockRejectedValueOnce(new Error('network down'))
    fireEvent.click(screen.getByTestId('converse-secret-send-anyway'))
    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(2), SETTLE)
    expect(converse.postMessage.mock.calls[1][1]).toMatchObject({ allowSecretOnce: true })

    type(FAKE_ANTHROPIC)
    fireEvent.keyDown(replyBox(), { key: 'Enter' })
    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(3), SETTLE)
    expect(converse.postMessage.mock.calls[2][1]).toMatchObject({ allowSecretOnce: false })
    await waitFor(() => expect(reason().textContent).toBe(SECRET_REASONS.anthropic), SETTLE)
  })
})

describe('the copy is honest about the mode it is shown in', () => {
  // ClaudeAccountPanel returns null under mock, and App mounts it under
  // `{!mock && ...}`, so a notice that says "Mount it under Claude accounts"
  // in a mode that renders no such control is a lie in a friendly voice. The
  // shells answer the question once, for the transports (setCredentialMount-
  // Available), so this pins the answer rather than a prop.
  it('names no surface where the Claude accounts panel is not mounted', async () => {
    setCredentialMountAvailable(false)
    setup()
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
