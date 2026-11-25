import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import ClaudeAccountPanel from './ClaudeAccountPanel.jsx'
import { getClaudeGrant } from '../api.js'
import { createPlatformTrustController } from '../controllers/platform/createPlatformTrustController.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function panel(props = {}) {
  return render(<ClaudeAccountPanel mock={false} open loading={false} busy={false}
    onToggle={vi.fn()} onLink={vi.fn()} onUnlink={vi.fn()} {...props} />)
}
function noManagement() {
  expect(screen.queryByLabelText('Claude token')).not.toBeInTheDocument()
  expect(screen.queryByText('not linked')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Link Claude account|Link another account|Remove|Unlink/ })).not.toBeInTheDocument()
}

describe('Claude account administrative status', () => {
  it('invites signed-out visitors to sign in without showing errors or management controls', () => {
    const props = { mock: false, signedOut: true, grant: null, error: 'Unauthenticated',
      loading: false, onToggle: vi.fn(), onLink: vi.fn(), onUnlink: vi.fn() }
    const view = panel({ ...props, open: false })
    expect(screen.getByText('sign in')).toBeInTheDocument()
    expect(screen.queryByText('unavailable')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Claude accounts/ }))
    expect(props.onToggle).toHaveBeenCalledWith(true)
    view.rerender(<ClaudeAccountPanel {...props} open />)
    const dialog = screen.getByRole('dialog', { name: 'Claude accounts' })
    expect(within(dialog).getAllByRole('status')).toHaveLength(1)
    expect(within(dialog).getByRole('status')).toHaveTextContent('Sign in to view or link Claude accounts.')
    expect(dialog.querySelectorAll('p')).toHaveLength(1)
    expect(dialog.querySelectorAll('input, a')).toHaveLength(0)
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
    expect(within(dialog).getAllByRole('button')).toHaveLength(1)
    expect(within(dialog).getByRole('button', { name: 'Close Claude account panel' })).toBeInTheDocument()
  })
  for (const state of [
    { loading: true, grant: null },
    { grant: { read_status: 'restricted' } },
    { grant: { linked: true, kind: 'oauth' } },
    { grant: { linked: false } },
  ]) {
    it(`prioritizes sign in over retained account state ${JSON.stringify(state)}`, () => {
      panel({ ...state, signedOut: true })
      expect(screen.getByText('sign in')).toBeInTheDocument()
      const dialog = screen.getByRole('dialog', { name: 'Claude accounts' })
      expect(dialog.querySelectorAll('p')).toHaveLength(1)
      expect(dialog.querySelectorAll('input, a')).toHaveLength(0)
      expect(within(dialog).getAllByRole('button')).toHaveLength(1)
    })
  }
  for (const [status, label] of [[403, 'access restricted'], [503, 'unavailable']]) {
    it(`propagates HTTP ${status} through transport/controller to honest UI`, async () => {
      vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: { code: status === 403 ? 'FORBIDDEN' : 'UNAVAILABLE', message: 'Status could not be read' } }), { status, headers: { 'Content-Type': 'application/json' } })))
      await expect(getClaudeGrant()).rejects.toMatchObject({ status })
      const controller = createPlatformTrustController({ services: { getClaudeGrant } })
      await controller.loadGrant()
      const state = controller.getSnapshot()
      panel({ grant: state.grant, error: state.grantErr })
      expect(screen.getByText(label)).toBeInTheDocument()
      noManagement()
      expect(state.grant.linked).toBeUndefined()
      expect(state.authRequired).toBe(false)
    })
  }
  it('propagates a network error and shows unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Network unavailable') }))
    await expect(getClaudeGrant()).rejects.toThrow()
    const controller = createPlatformTrustController({ services: { getClaudeGrant } })
    await controller.loadGrant()
    panel({ grant: controller.getSnapshot().grant })
    expect(screen.getByText('unavailable')).toBeInTheDocument()
    noManagement()
  })
  it('offers setup only after successful linked:false', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ linked: false }), { status: 200 })))
    panel({ grant: await getClaudeGrant() })
    expect(screen.getByText('not linked')).toBeInTheDocument()
    expect(screen.getByLabelText('Claude token')).toBeInTheDocument()
  })
  it('shows known linked status and preserves mutation errors', () => {
    panel({ grant: { linked: true, kind: 'oauth', accounts: [] }, error: 'Could not unlink this account.' })
    expect(screen.getByText('1 linked')).toBeInTheDocument()
    expect(screen.getByText('Could not unlink this account.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Link another account' })).toBeInTheDocument()
  })
  it('suppresses controls while checking even with a previous linked result', () => {
    panel({ loading: true, grant: { linked: true } })
    expect(screen.getByText('checking')).toBeInTheDocument()
    noManagement()
  })
  it('does not offer setup for unknown initial status', () => {
    panel({ grant: null })
    expect(screen.getByText('unavailable')).toBeInTheDocument()
    noManagement()
  })
  it('retains mock suppression', () => {
    panel({ mock: true, signedOut: true, grant: { linked: false } })
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('preserves focus in an outside editor when the account panel closes', () => {
    const props = { mock: false, grant: { linked: false }, onToggle: vi.fn() }
    const view = render(<><input aria-label="Drawing name" /><ClaudeAccountPanel {...props} open /></>)
    const editor = screen.getByLabelText('Drawing name')
    editor.focus()
    view.rerender(<><input aria-label="Drawing name" /><ClaudeAccountPanel {...props} open={false} /></>)
    expect(editor).toHaveFocus()
  })
})


function retainedKey(target, key, marks, modifiers = {}) {
  const event = createEvent.keyDown(target, {
    key, bubbles: true, cancelable: true, ...modifiers, ...marks,
  })
  expect(event.isComposing).toBe(marks.isComposing)
  expect(event.keyCode).toBe(marks.keyCode)
  const stop = event.stopPropagation.bind(event)
  event.stopPropagation = vi.fn(() => stop())
  return event
}

function expectYielded(event) {
  expect(event.defaultPrevented).toBe(false)
  expect(event.stopPropagation).not.toHaveBeenCalled()
}

async function credentialComposition(marks) {
  const onLink = vi.fn().mockResolvedValue({ linked: true })
  panel({ grant: { linked: false }, onLink })
  fireEvent.click(screen.getByRole('radio', { name: 'Anthropic API key' }))
  const input = screen.getByLabelText('Anthropic API key')
  fireEvent.change(input, { target: { value: 'draft-value' } })
  input.focus()
  expect(input).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Link API key' })).toBeEnabled()
  const event = retainedKey(input, 'Enter', marks)
  fireEvent(input, event)
  expectYielded(event)
  expect(onLink).not.toHaveBeenCalled()
  expect(input.value).toBe('draft-value')
  expect(input).toHaveFocus()
  const ordinary = retainedKey(input, 'Enter', { isComposing: false, keyCode: 0 })
  fireEvent(input, ordinary)
  expect(ordinary.defaultPrevented).toBe(false)
  expect(onLink).toHaveBeenCalledTimes(1)
  expect(onLink).toHaveBeenCalledWith('draft-value', 'api_key', expect.any(String), undefined)
  await waitFor(() => expect(input.value).toBe(''))
}

it('KEYS-D19 credential Enter yields native composition', async () => {
  await credentialComposition({ isComposing: true, keyCode: 0 })
})

it('KEYS-D20 credential Enter yields key code 229', async () => {
  await credentialComposition({ isComposing: false, keyCode: 229 })
})

describe('Claude account onboarding instructions', () => {
  it('tells a new owner how to get a subscription token and an API key, with a copyable command', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    panel({ grant: { linked: false, accounts: [] } })
    const dialog = screen.getByRole('dialog', { name: 'Claude accounts' })
    expect(dialog).toHaveTextContent('No Claude account is linked yet.')
    const steps = within(dialog).getByRole('list', { name: 'How to get a Claude setup token' })
    expect(within(steps).getAllByRole('listitem')).toHaveLength(4)
    expect(within(steps).getByRole('link', { name: 'Claude Code' })).toHaveAttribute('href', expect.stringMatching(/^https:\/\/docs\.anthropic\.com\//))
    expect(steps).toHaveTextContent('claude setup-token')
    expect(steps).toHaveTextContent('Approve the request in the browser')
    expect(steps).toHaveTextContent('sk-ant-oat01-')
    fireEvent.click(within(steps).getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith('claude setup-token')
    await waitFor(() => expect(within(steps).getByRole('button', { name: 'Copied' })).toBeInTheDocument())
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Anthropic API key' }))
    expect(dialog).toHaveTextContent('Create a key at console.anthropic.com under API keys, then paste it below.')
    expect(within(dialog).getByRole('link', { name: 'console.anthropic.com' })).toHaveAttribute('href', 'https://console.anthropic.com/settings/keys')
  })
})

describe('Claude account link form (rehearsal issues 24 and 25)', () => {
  it('explains a missing plan instead of silently doing nothing', () => {
    const onLink = vi.fn()
    panel({ grant: { linked: false, accounts: [] }, onLink })
    const dialog = screen.getByRole('dialog', { name: 'Claude accounts' })
    fireEvent.change(within(dialog).getByLabelText('Claude token'), { target: { value: 'sk-ant-oat01-example' } })
    const link = within(dialog).getByRole('button', { name: 'Link Claude account' })
    expect(link).not.toBeDisabled()
    fireEvent.click(link)
    expect(onLink).not.toHaveBeenCalled()
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Choose your Claude plan above')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Max' }))
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.click(link)
    expect(onLink).toHaveBeenCalledWith('sk-ant-oat01-example', 'oauth', 'My Claude account', 'max')
  })

  it('masks the credential without a password field, so browsers do not offer to save it', () => {
    panel({ grant: { linked: false, accounts: [] } })
    const field = screen.getByLabelText('Claude token')
    expect(field).toHaveAttribute('type', 'text')
    expect(field).toHaveAttribute('autocomplete', 'off')
    expect(field).toHaveClass('ca-secret')
    expect(document.querySelector('input[type="password"]')).toBeNull()
  })
})

describe('Claude account card placement', () => {
  it('anchors the card to the left when its trigger sits near the left edge', () => {
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 70, right: 200, top: 40, bottom: 60, width: 130, height: 20 })
    try {
      panel({ grant: { linked: false, accounts: [] } })
      expect(screen.getByRole('dialog', { name: 'Claude accounts' })).toHaveAttribute('data-align', 'start')
    } finally { rect.mockRestore() }
  })
  it('keeps the right anchor when there is room', () => {
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 1200, right: 1320, top: 40, bottom: 60, width: 120, height: 20 })
    try {
      panel({ grant: { linked: false, accounts: [] } })
      expect(screen.getByRole('dialog', { name: 'Claude accounts' })).not.toHaveAttribute('data-align')
    } finally { rect.mockRestore() }
  })
})
