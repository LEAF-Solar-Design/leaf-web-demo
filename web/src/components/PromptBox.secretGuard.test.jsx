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
import PromptBox, { SECRET_REASONS } from './PromptBox.jsx'
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

/** Renders the bar as a controlled input the test drives, like App does. */
function setup(initial = '') {
  const onDispatch = vi.fn(() => Promise.resolve({ status: 202 }))
  let current = initial
  const onChange = vi.fn((next) => { current = next; rerenderWith(current) })
  const props = () => ({
    value: current,
    onChange,
    onDispatch,
    routing: false,
    hintLane: null,
    projectName: 'proj',
    routeActive: false,
  })
  const view = render(<PromptBox {...props()} />)
  function rerenderWith(next) {
    current = next
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
    ['anthropic', 'That looks like an Anthropic API key. Credentials never go to the model. Mount it under Claude accounts in the header instead.'],
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
