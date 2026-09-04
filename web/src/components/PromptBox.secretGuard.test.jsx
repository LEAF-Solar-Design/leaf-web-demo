/**
 * The command bar's credential REFUSAL (standardization slice 8a, round 3).
 *
 * Read the title carefully: this file no longer tests a guard, because the bar
 * no longer holds one. The guard is on the wire (lib/secretGuardTransport.js,
 * called by api.nlPrompt), the controller catches its typed refusal, and this
 * component's whole job is to render that verdict and to offer a per-call
 * "Send anyway" for the one overridable shape.
 *
 * That split is the round-3 fix and these rows are what hold it in place:
 *   - the bar must NOT pre-judge (a local copy of the decision is what made
 *     rounds 1 and 2 look guarded while other paths stayed open),
 *   - it must render a refusal it never computed (the Retry chip and the R key
 *     dispatch around this component entirely),
 *   - "Send anyway" must be a PARAMETER on one dispatch and nothing else, so a
 *     click the host short-circuits authorises nothing.
 *
 * The frozen copy is pinned here too: it is the sentence a user reads, and a
 * reword should be a deliberate act rather than a silent softening.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import PromptBox, { SECRET_REASONS, SECRET_REASONS_NO_MOUNT } from './PromptBox.jsx'
import ClaudeAccountPanel from './ClaudeAccountPanel.jsx'
import {
  MASK_BULLETS,
  MASK_PREFIX,
  MOUNTABLE_NEXT_STEP,
  SECRET_PATTERNS,
  UNMOUNTABLE_NEXT_STEP,
} from '../lib/secretPatterns.js'

// Structurally valid, entirely fake.
const FAKE_ANTHROPIC = `sk-ant-api03-${'A9_-'.repeat(12)}`
const MASKED = `sk-a${'•'.repeat(MASK_BULLETS)}`

const HARD_REFUSAL = Object.freeze({
  id: 'anthropic',
  reason: SECRET_REASONS.anthropic,
  masked: MASKED,
  overridable: false,
})
const SOFT_REFUSAL = Object.freeze({
  id: 'generic',
  reason: SECRET_REASONS.generic,
  masked: `api_${'•'.repeat(MASK_BULLETS)}`,
  overridable: true,
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

/** Renders the bar as a controlled input the test drives, like App does. */
function setup(initial = '', extra = {}) {
  const onDispatch = vi.fn(() => Promise.resolve({ status: 202 }))
  let current = initial
  let overrides = { ...extra }
  const onChange = vi.fn((next) => { current = next; rerenderWith(current) })
  const props = () => ({
    value: current,
    onChange,
    onDispatch,
    routing: false,
    hintLane: null,
    projectName: 'proj',
    routeActive: false,
    ...overrides,
  })
  const view = render(<PromptBox {...props()} />)
  function rerenderWith(next, nextOverrides) {
    current = next
    if (nextOverrides) overrides = { ...overrides, ...nextOverrides }
    view.rerender(<PromptBox {...props()} />)
  }
  const field = screen.getByLabelText('Command bar')
  return { onDispatch, onChange, field, rerenderWith, view }
}

const type = (field, rerenderWith, text) => {
  fireEvent.change(field, { target: { value: text, selectionStart: text.length } })
  rerenderWith(text)
}

const notice = () => screen.queryByTestId('secret-notice')
const reason = () => screen.getByTestId('secret-notice-reason')

describe('the honesty ladder copy is frozen', () => {
  it('carries one exact sentence per pattern, and no extras', () => {
    expect(Object.isFrozen(SECRET_REASONS)).toBe(true)
    expect(Object.keys(SECRET_REASONS).sort()).toEqual(SECRET_PATTERNS.map((p) => p.id).sort())
  })

  it.each([
    ['anthropic', 'That looks like an Anthropic API key. Credentials never go to the model. Mount it under Claude accounts instead.'],
    ['openai', 'That looks like an OpenAI API key. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.'],
    ['github', 'That looks like a GitHub token. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.'],
    ['aws_access_key', 'That looks like an AWS access key ID. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.'],
    ['aws_secret_key', 'That looks like an AWS secret access key. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.'],
    ['slack', 'That looks like a Slack token. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.'],
    ['jwt', 'That looks like a JSON Web Token. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.'],
    ['private_key', 'That looks like a private key. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.'],
    ['generic', 'That looks like a credential. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.'],
  ])('%s reads exactly', (id, sentence) => {
    expect(SECRET_REASONS[id]).toBe(sentence)
  })

  it('every sentence states the rule and ends in a next step, never a bare block', () => {
    for (const line of Object.values(SECRET_REASONS)) {
      expect(line).toContain('Credentials never go to the model.')
      expect([MOUNTABLE_NEXT_STEP, UNMOUNTABLE_NEXT_STEP].some((tail) => line.endsWith(tail))).toBe(true)
    }
  })

  // The honesty defect this replaced: every sentence pointed at "Link a
  // service", a surface that exists nowhere in this product, and the pin froze
  // that in place. A next step may only name a surface a user can actually
  // reach TODAY — the header's Claude accounts panel, which mounts Anthropic
  // credentials and nothing else (Surface Contract: `integrations: null`).
  it('names a real surface only where one exists, and never invents one', () => {
    const pointing = Object.entries(SECRET_REASONS)
      .filter(([, line]) => line.endsWith(MOUNTABLE_NEXT_STEP))
      .map(([id]) => id)
    expect(pointing).toEqual(['anthropic'])
    for (const line of Object.values(SECRET_REASONS)) {
      expect(line).not.toContain('Link a service')
    }
  })

  it('points at the header control by the name the header actually renders', () => {
    // ClaudeAccountPanel's trigger reads "Claude accounts"; if that label is
    // renamed, this refusal starts sending users to a control that is not
    // there, so the copy and the control are pinned together.
    render(<ClaudeAccountPanel mock={false} grant={null} loading={false} busy={false} error={null} open={false} onToggle={() => {}} onLink={() => {}} onUnlink={() => {}} />)
    const trigger = screen.getByRole('button', { name: /claude accounts/i })
    expect(MOUNTABLE_NEXT_STEP).toContain(trigger.querySelector('.ca-k').textContent)
  })

  it('the two tables differ in exactly one sentence', () => {
    expect(Object.isFrozen(SECRET_REASONS_NO_MOUNT)).toBe(true)
    const differing = Object.keys(SECRET_REASONS)
      .filter((id) => SECRET_REASONS[id] !== SECRET_REASONS_NO_MOUNT[id])
    expect(differing).toEqual(['anthropic'])
    expect(SECRET_REASONS_NO_MOUNT.anthropic).toBe(
      'That looks like an Anthropic API key. Credentials never go to the model. No surface here can hold one yet, so keep it out of the message.',
    )
  })

  it('no sentence in the no-mount table names a control at all', () => {
    for (const line of Object.values(SECRET_REASONS_NO_MOUNT)) {
      expect(line.endsWith(UNMOUNTABLE_NEXT_STEP)).toBe(true)
      expect(line).not.toContain('Claude accounts')
    }
  })
})

describe('the bar does not judge credentials itself (round 3)', () => {
  // The load-bearing NEGATIVE row of the new design. If this component ever
  // grows its own evaluateSecretGuard call again, this goes red — and it
  // should, because a composer that pre-judges is a composer the next reviewer
  // will mistake for the authority while some other path stays open.
  it('hands a credential-shaped prompt to the dispatcher, which is what the wire refuses', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(onDispatch).toHaveBeenCalledTimes(1)
    expect(notice(), 'no verdict exists until the transport returns one').toBeNull()
  })

  it('carries no override on an ordinary dispatch', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, 'count entities by layer')
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(onDispatch.mock.calls[0][1]).toMatchObject({ allowSecretOnce: false })
  })
})

describe('it renders the verdict the wire produced', () => {
  it('shows a refusal this component never computed', () => {
    setup('', { secretRefusal: HARD_REFUSAL })

    expect(notice()).toBeInTheDocument()
    expect(notice()).toHaveAttribute('role', 'alert')
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
    expect(screen.getByTestId('secret-notice-mask').textContent).toBe(MASKED)
  })

  it('shows a mask, never the credential', () => {
    setup(FAKE_ANTHROPIC, { secretRefusal: HARD_REFUSAL })

    const rendered = notice().textContent
    expect(rendered).not.toContain(FAKE_ANTHROPIC)
    expect(rendered).not.toContain(FAKE_ANTHROPIC.slice(MASK_PREFIX))
  })

  it('offers no override for a named shape — it is a hard refusal', () => {
    setup('', { secretRefusal: HARD_REFUSAL })
    expect(screen.queryByTestId('secret-send-anyway')).toBeNull()
  })

  it('clears when the controller clears it', () => {
    const { rerenderWith } = setup('', { secretRefusal: HARD_REFUSAL })
    expect(notice()).toBeInTheDocument()

    rerenderWith('count entities by layer', { secretRefusal: null })
    expect(notice()).toBeNull()
  })

  it('shows the mock-mode sentence when that is what the wire decided', () => {
    setup('', { secretRefusal: { ...HARD_REFUSAL, reason: SECRET_REASONS_NO_MOUNT.anthropic } })
    expect(reason().textContent).toBe(SECRET_REASONS_NO_MOUNT.anthropic)
    expect(reason().textContent).not.toContain('Claude accounts')
  })
})

describe('Send anyway is a parameter, not a latch (round 3)', () => {
  it('re-issues the same dispatch carrying the authorisation', () => {
    const { onDispatch } = setup('some prompt', { secretRefusal: SOFT_REFUSAL })

    fireEvent.click(screen.getByTestId('secret-send-anyway'))

    expect(onDispatch).toHaveBeenCalledTimes(1)
    expect(onDispatch.mock.calls[0][1]).toMatchObject({ allowSecretOnce: true })
  })

  // ROUND 2'S DEFECT, ported. Then: the click armed a latch, App's `running`
  // short-circuit swallowed the dispatch, the latch stayed armed and the next
  // unrelated Enter went out unguarded. Now the authorisation rides the
  // swallowed call and dies with it.
  it('a swallowed click authorises nothing for the next dispatch', () => {
    const onDispatch = vi.fn(() => undefined) // the host short-circuits
    let value = 'some prompt'
    let refusal = SOFT_REFUSAL
    const props = () => ({
      value,
      onChange: (next) => { value = next },
      onDispatch,
      routing: false,
      hintLane: null,
      projectName: 'proj',
      routeActive: false,
      secretRefusal: refusal,
    })
    const view = render(<PromptBox {...props()} />)

    fireEvent.click(screen.getByTestId('secret-send-anyway'))
    expect(onDispatch.mock.calls[0][1]).toMatchObject({ allowSecretOnce: true })

    // The next keystroke dispatch must be unauthorised again.
    refusal = null
    view.rerender(<PromptBox {...props()} />)
    fireEvent.keyDown(screen.getByLabelText('Command bar'), { key: 'Enter' })
    expect(onDispatch).toHaveBeenCalledTimes(2)
    expect(onDispatch.mock.calls[1][1]).toMatchObject({ allowSecretOnce: false })
  })

  it('is disabled while routing, and a disabled click authorises nothing', () => {
    const { onDispatch } = setup('some prompt', { secretRefusal: SOFT_REFUSAL, routing: true })

    const button = screen.getByTestId('secret-send-anyway')
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(onDispatch).not.toHaveBeenCalled()
  })
})
