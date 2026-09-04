/**
 * The command bar's credential guard (standardization slice 8a).
 *
 * The live gap this closes: before it, a pasted API key went into model context
 * with zero friction. So the load-bearing spec here is the NEGATIVE one —
 * onDispatch must not be called — plus the honesty ladder's exact copy, which a
 * reword would otherwise soften silently.
 *
 * `../telemetry.js` is mocked so these specs pin the component's own choke
 * point and can assert the payload carries a pattern id and nothing else.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))

import { track } from '../telemetry.js'
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
const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

/**
 * Renders the bar as a controlled input the test drives, like App does.
 *
 * `credentialMountAvailable` defaults TRUE here because App passes `!mock` and
 * the shipped builds bake VITE_MOCK=0; the mock-mode copy gets its own rows
 * below, where the answer is false.
 */
function setup(initial = '', extra = {}) {
  const onDispatch = vi.fn(() => Promise.resolve({ status: 202 }))
  const onAllowSecretOnce = vi.fn()
  let current = initial
  let overrides = { ...extra }
  const onChange = vi.fn((next) => { current = next; rerenderWith(current) })
  const props = () => ({
    value: current,
    onChange,
    onDispatch,
    onAllowSecretOnce,
    credentialMountAvailable: true,
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
  return { onDispatch, onChange, onAllowSecretOnce, field, rerenderWith, view }
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
})

describe('a named token shape refuses on Enter', () => {
  it('does not dispatch, and says so honestly', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(onDispatch).not.toHaveBeenCalled()
    expect(notice()).toBeInTheDocument()
    expect(notice()).toHaveAttribute('role', 'alert')
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
  })

  it('offers no override — a named shape is a hard refusal', () => {
    const { field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(screen.queryByTestId('secret-send-anyway')).toBeNull()
  })

  it('shows a mask, never the credential', () => {
    const { field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.keyDown(field, { key: 'Enter' })

    const masked = screen.getByTestId('secret-notice-mask').textContent
    expect(masked).toBe(`sk-a${'•'.repeat(MASK_BULLETS)}`)
    // The whole notice subtree — not just the mask span — is free of the value.
    const rendered = notice().textContent
    expect(rendered).not.toContain(FAKE_ANTHROPIC)
    expect(rendered).not.toContain(FAKE_ANTHROPIC.slice(MASK_PREFIX))
  })

  it('emits the pattern id and nothing else', () => {
    const { field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(track).toHaveBeenCalledWith('prompt.secret_refused', { pattern_id: 'anthropic' })
    const [, props] = track.mock.calls.find(([name]) => name === 'prompt.secret_refused')
    expect(Object.keys(props)).toEqual(['pattern_id'])
    expect(JSON.stringify(props)).not.toContain(FAKE_ANTHROPIC.slice(0, 12))
  })
})

describe('the Run chip goes through the same gate', () => {
  it('refuses too — Enter is not a special case', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))

    expect(onDispatch).not.toHaveBeenCalled()
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
  })
})

describe('the generic shape may be overridden, once', () => {
  it('refuses first, with a Send anyway button', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_GENERIC)
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(onDispatch).not.toHaveBeenCalled()
    expect(reason().textContent).toBe(SECRET_REASONS.generic)
    expect(screen.getByTestId('secret-send-anyway')).toBeInTheDocument()
  })

  it('Send anyway dispatches exactly once and clears the notice', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_GENERIC)
    fireEvent.keyDown(field, { key: 'Enter' })
    fireEvent.click(screen.getByTestId('secret-send-anyway'))

    expect(onDispatch).toHaveBeenCalledTimes(1)
    expect(notice()).toBeNull()
  })

  it('does not latch: the NEXT send is refused again', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_GENERIC)
    fireEvent.keyDown(field, { key: 'Enter' })
    fireEvent.click(screen.getByTestId('secret-send-anyway'))
    expect(onDispatch).toHaveBeenCalledTimes(1)

    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onDispatch).toHaveBeenCalledTimes(1)
    expect(reason().textContent).toBe(SECRET_REASONS.generic)
  })

  it('a named shape beside a generic one removes the override', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, `${FAKE_GENERIC} and ${FAKE_ANTHROPIC}`)
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(onDispatch).not.toHaveBeenCalled()
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
    expect(screen.queryByTestId('secret-send-anyway')).toBeNull()
  })
})

describe('the notice never sticks', () => {
  it('clears as soon as the text changes', () => {
    const { field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(notice()).toBeInTheDocument()

    type(field, rerenderWith, 'count entities by layer')
    expect(notice()).toBeNull()
  })
})

describe('ordinary prompts are untouched', () => {
  it('dispatches normally with no notice and no telemetry', () => {
    const { onDispatch, field, rerenderWith } = setup()
    type(field, rerenderWith, 'Rearrange the panels into the shape of a sitting cat')
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(onDispatch).toHaveBeenCalledTimes(1)
    expect(notice()).toBeNull()
    expect(track).not.toHaveBeenCalledWith('prompt.secret_refused', expect.anything())
  })
})

describe('the copy is honest about the mode it is shown in (fix round 2)', () => {
  // THE DEFECT THIS PINS. ClaudeAccountPanel is mounted under `{!mock && ...}`
  // (App.jsx) and returns null under mock on its own, and MOCK_DEFAULT is the
  // unset default (api.js) — a live build also flips itself back into mock on a
  // 401 or an unconfigured Auth0, and on "Back to the demo". In every one of
  // those modes the header has no Claude accounts control, so telling the user
  // to mount the key there names a surface that is not on screen. Same class as
  // the "Link a service" defect this copy already replaced once.
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

  it('mock mode gets the generic sentence, not the header one', () => {
    const { field, rerenderWith } = setup('', { credentialMountAvailable: false })
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(reason().textContent).toBe(SECRET_REASONS_NO_MOUNT.anthropic)
    expect(reason().textContent).not.toContain('Claude accounts')
  })

  it('live mode gets the mountable sentence', () => {
    const { field, rerenderWith } = setup()
    type(field, rerenderWith, FAKE_ANTHROPIC)
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
  })
})

describe('the bar renders the FUNNEL\'s verdict, not only its own (fix round 2)', () => {
  // App's failed-strip Retry chip and its R-key twin call
  // catalogActions.dispatch directly, around this component entirely, so the
  // refusal they raise exists only as controller state. If the bar rendered
  // only its local notice, those paths would refuse SILENTLY.
  const FROM_FUNNEL = {
    id: 'anthropic',
    reason: SECRET_REASONS.anthropic,
    masked: 'sk-a\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022',
    overridable: false,
  }

  it('shows a refusal this component never computed', () => {
    setup('', { secretRefusal: FROM_FUNNEL })

    expect(notice()).toBeInTheDocument()
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
    expect(screen.getByTestId('secret-notice-mask').textContent).toBe(FROM_FUNNEL.masked)
    expect(screen.queryByTestId('secret-send-anyway')).toBeNull()
  })

  it('Send anyway arms the CONTROLLER override, not just the local one', () => {
    const overridable = { ...FROM_FUNNEL, id: 'generic', reason: SECRET_REASONS.generic, overridable: true }
    const { onAllowSecretOnce, onDispatch } = setup('some prompt', { secretRefusal: overridable })

    fireEvent.click(screen.getByTestId('secret-send-anyway'))

    expect(onAllowSecretOnce, 'the funnel is the authority, so it must be told').toHaveBeenCalledTimes(1)
    expect(onDispatch).toHaveBeenCalledTimes(1)
  })

  it('Send anyway is disabled while routing, so it cannot arm what it cannot spend', () => {
    const overridable = { ...FROM_FUNNEL, id: 'generic', reason: SECRET_REASONS.generic, overridable: true }
    const { onAllowSecretOnce } = setup('some prompt', { secretRefusal: overridable, routing: true })

    const button = screen.getByTestId('secret-send-anyway')
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(onAllowSecretOnce).not.toHaveBeenCalled()
  })
})
