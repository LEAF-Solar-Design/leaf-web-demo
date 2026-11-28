import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import ClaudeAccountPanel from './ClaudeAccountPanel.jsx'
import { getClaudeGrant } from '../api.js'
import { createPlatformTrustController } from '../controllers/platform/createPlatformTrustController.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })

function panel(props = {}) {
  return render(<ClaudeAccountPanel mock={false} open loading={false} busy={false}
    onToggle={vi.fn()} onLink={vi.fn()} onUnlink={vi.fn()} {...props} />)
}
function noManagement() {
  expect(screen.queryByLabelText('Anthropic API key')).not.toBeInTheDocument()
  expect(screen.queryByText('not linked')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Link API key|Add an API key|Remove|Unlink/ })).not.toBeInTheDocument()
}

const account = { id: 'old', label: 'Work', kind: 'api_key', active: true, auth_refused_code: 'authentication_failed' }
const linkedResult = { linked: true, active_account_id: 'new', accounts: [{ id: 'new', label: 'New', kind: 'oauth', plan: 'max', active: true }] }

function enterApiKey(value = 'sk-ant-api03-FAKE-secret') {
  fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value } })
}

describe('Claude link safety and recovery', () => {
  it('refreshes only on a later closed to open transition for a signed-in live panel', () => {
    const onRefresh = vi.fn()
    const props = { grant: { linked: false }, onRefresh, onToggle: vi.fn() }
    const view = panel(props)
    expect(onRefresh).not.toHaveBeenCalled()
    view.rerender(<ClaudeAccountPanel {...props} open={false} />)
    view.rerender(<ClaudeAccountPanel {...props} open />)
    view.rerender(<ClaudeAccountPanel {...props} open />)
    expect(onRefresh).toHaveBeenCalledTimes(1)
    view.rerender(<ClaudeAccountPanel {...props} open={false} signedOut />)
    view.rerender(<ClaudeAccountPanel {...props} open signedOut />)
    view.rerender(<ClaudeAccountPanel {...props} open={false} mock />)
    view.rerender(<ClaudeAccountPanel {...props} open mock />)
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it.each(['https://signin.test/#FAKE', 'https://signin.test', 'http://signin.test', 'FAKE#code', 'claude setup-token'])('explains mistaken API key input %s without sending it', (value) => {
    const onLink = vi.fn()
    panel({ grant: { linked: false }, onLink })
    enterApiKey(value)
    fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
    expect(onLink).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue(value)
    expect(screen.getByRole('alert')).toHaveTextContent('That is not an API key. Copy the key from Claude Console (it starts with sk-ant-api).')
    expect(screen.getByRole('alert').textContent).not.toContain(value)
  })

  it('rejects a setup token locally while keeping it masked', () => {
    const onLink = vi.fn()
    panel({ grant: { linked: false }, onLink })
    const value = 'sk-ant-oat01-FAKE-not-an-api-key-fixture-only'
    enterApiKey(value)
    fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
    expect(onLink).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue(value)
    expect(screen.getByRole('alert')).toHaveTextContent('That is not an API key. Create an API key in Claude Console and paste that instead.')
    expect(screen.getByRole('alert').textContent).not.toContain(value)
  })

  it('keeps an existing max subscription and never replaces it automatically', async () => {
    const old = { ...account, kind: 'oauth', plan: 'max' }
    const result = { ...linkedResult, accounts: [old, { ...linkedResult.accounts[0], kind: 'api_key' }] }
    const onUnlink = vi.fn()
    panel({ grant: { linked: true, accounts: [old] }, onLink: vi.fn().mockResolvedValue(result), onUnlink })
    expect(screen.getByText('max subscription')).toBeInTheDocument()
    expect(screen.getByText('Claude no longer accepts this account. It expired or was revoked.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Link again:/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add an API key instead of Work' }))
    enterApiKey()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    expect(onUnlink).not.toHaveBeenCalled()
  })

  it('offers one ordinary add button for a working subscription', () => {
    panel({ grant: { linked: true, accounts: [{ ...account, kind: 'oauth', plan: 'max', auth_refused_code: null }] } })
    expect(screen.getByText('max subscription')).toBeInTheDocument()
    const button = screen.getByRole('button', { name: 'Add an API key' })
    expect(button).not.toHaveAttribute('aria-label')
  })

  it.each(['failure', 'rejection', 'success'])('removes a fake token everywhere after submit, including %s', async (outcome) => {
    const fakeToken = 'sk-ant-api03-FAKE-no-lingering-secret'
    let resolve, reject
    const onLink = vi.fn(() => new Promise((done, fail) => { resolve = done; reject = fail }))
    const logs = ['log', 'warn', 'error'].map((name) => vi.spyOn(console, name))
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    try {
      panel({ grant: { linked: false }, onLink })
      enterApiKey(fakeToken)
      const input = screen.getByLabelText('Anthropic API key')
      expect(input).toHaveAttribute('data-bwignore', 'true')
      expect(input).toHaveAttribute('writingsuggestions', 'false')
      expect(input.closest('form')).toBeNull()
      fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
      const noLeak = () => {
        expect(document.documentElement.outerHTML).not.toContain(fakeToken)
        document.querySelectorAll('input').forEach((field) => expect(field.value).not.toContain(fakeToken))
        expect(JSON.stringify(logs.map((spy) => spy.mock.calls))).not.toContain(fakeToken)
        expect(JSON.stringify(storage.mock.calls)).not.toContain(fakeToken)
        expect(JSON.stringify(localStorage)).not.toContain(fakeToken)
        expect(JSON.stringify(sessionStorage)).not.toContain(fakeToken)
        expect(location.href).not.toContain(fakeToken)
      }
      noLeak()
      // A second Enter before completion cannot dispatch another request.
      fireEvent.change(input, { target: { value: 'second-fake-value' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(onLink).toHaveBeenCalledTimes(1)
      fireEvent.change(input, { target: { value: '' } })
      await act(async () => {
        if (outcome === 'rejection') reject(new Error(fakeToken))
        else resolve(outcome === 'success' ? linkedResult : null)
      })
      noLeak()
      if (outcome === 'rejection') expect(screen.getByRole('alert')).toHaveTextContent('The link could not be finished. Reload and check your Claude accounts before you retry.')
    } finally { logs.forEach((spy) => spy.mockRestore()); storage.mockRestore() }
  })

  it('normalizes a terminal-wrapped token and clears it before the callback settles', async () => {
    const onLink = vi.fn().mockResolvedValue(null)
    panel({ grant: { linked: false }, onLink })
    enterApiKey('  sk-ant-api03-FAKE wrapped\tvalue  ')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    expect(onLink).toHaveBeenCalledWith('sk-ant-api03-FAKEwrappedvalue', 'api_key', 'My API key', undefined)
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
  })

  it('clears the field on close and sign out', () => {
    const props = { grant: { linked: false }, onToggle: vi.fn() }
    const view = panel(props)
    enterApiKey()
    view.rerender(<ClaudeAccountPanel {...props} open={false} />)
    view.rerender(<ClaudeAccountPanel {...props} open />)
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
    enterApiKey()
    view.rerender(<ClaudeAccountPanel {...props} open signedOut />)
    view.rerender(<ClaudeAccountPanel {...props} open />)
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
    enterApiKey()
    view.rerender(<ClaudeAccountPanel {...props} open mock />)
    view.rerender(<ClaudeAccountPanel {...props} open />)
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
  })

  it.each([
    [linkedResult, '1 account linked, Max. Claude checks it on your first request.'],
    [{ linked: true }, '1 account linked. Claude checks it on your first request.'],
    [{ linked: true, accounts: [{ id: 'a', active: true, plan: 'unknown' }] }, '1 account linked. Claude checks it on your first request.'],
    [{ linked: true, accounts: [{ id: 'a', active: true, kind: 'api_key' }] }, '1 API key linked. Claude checks it on your first request.'],
    [{ ...linkedResult, accounts: [...linkedResult.accounts, { id: 'other' }] }, '2 accounts linked. Added: Max. Claude checks it on your first request.'],
    [{ linked: true, accounts: [{ id: 'a', active: true, kind: 'api_key' }, { id: 'b' }] }, '2 accounts linked. Added: API key. Claude checks it on your first request.'],
    [{ linked: true, accounts: [{ id: 'a' }, { id: 'b' }] }, '2 accounts linked. Claude checks it on your first request.'],
  ])('announces and focuses result-derived success', async (result, text) => {
    const props = { grant: { linked: false }, onLink: vi.fn().mockResolvedValue(result), onToggle: vi.fn() }
    const view = panel(props)
    enterApiKey()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    view.rerender(<ClaudeAccountPanel {...props} grant={result} open />)
    expect(screen.getByRole('status')).toHaveTextContent(text)
    expect(screen.getByRole('status')).toHaveFocus()
    view.rerender(<ClaudeAccountPanel {...props} grant={{ ...result, accounts: [...(result.accounts || []), { id: 'later', label: 'Later' }] }} open />)
    expect(screen.getByRole('status')).toHaveTextContent(text)
    view.rerender(<ClaudeAccountPanel {...props} grant={result} open={false} />)
    view.rerender(<ClaudeAccountPanel {...props} grant={result} open />)
    expect(screen.queryByText(text)).not.toBeInTheDocument()
  })

  it.each(['removed', 'falsy', 'reject', 'still-listed'])('relinks a refused entry and handles cleanup: %s', async (outcome) => {
    const result = { ...linkedResult, accounts: [account, ...linkedResult.accounts] }
    const onUnlink = vi.fn(async () => {
      if (outcome === 'reject') throw new Error('Failed')
      return outcome === 'falsy' ? null : outcome === 'still-listed' ? result : linkedResult
    })
    const props = { grant: { linked: true, accounts: [account] }, onLink: vi.fn().mockResolvedValue(result), onUnlink, onToggle: vi.fn() }
    const view = panel(props)
    expect(screen.getByText('link again needed')).not.toHaveClass('on')
    expect(screen.getByText('Needs a new link')).toBeInTheDocument()
    expect(screen.queryByText('Recently used')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Work' }))
    expect(screen.getByLabelText('Key label')).toHaveValue('Work')
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'sk-ant-api03-FAKE-replacement' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    view.rerender(<ClaudeAccountPanel {...props} grant={outcome === 'removed' ? linkedResult : result} open />)
    expect(onUnlink).toHaveBeenCalledExactlyOnceWith('old')
    expect(screen.getByRole('status')).toHaveTextContent(outcome === 'removed' ? '1 account linked, Max.' : '2 accounts linked. Added: Max.')
    expect(screen.getByRole('status').textContent.includes('The old entry is still listed. You can unlink it.')).toBe(outcome !== 'removed')
  })

  it('shows usable counts and organization refusal details', () => {
    panel({ grant: { linked: true, accounts: [{ ...account, auth_refused_code: 'oauth_org_not_allowed' }, ...linkedResult.accounts] } })
    expect(screen.getByText('1 of 2 linked')).toHaveClass('on')
    const explanation = screen.getByText("This account's organization does not allow it here.")
    expect(explanation).toHaveClass('ca-refused-why')
    expect(explanation.previousElementSibling).toHaveClass('ca-account-main')
    expect(explanation.closest('.ca-account-main')).toBeNull()
    expect(within(explanation.previousElementSibling).getByText('Work').tagName).toBe('B')
  })

  it('explains a linked API key with no credit', () => {
    panel({ grant: { linked: true, accounts: [{ ...account, auth_refused_code: 'billing_error' }] } })
    expect(screen.getByText('This API key has no credit. Add credit in Claude Console, then resend.')).toHaveClass('ca-refused-why')
  })

  it('clears the token and local error on Cancel', () => {
    panel({ grant: { linked: true, accounts: [account] } })
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Work' }))
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'claude setup-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
    expect(screen.getByRole('alert')).toHaveTextContent('That is not an API key.')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(document.documentElement.outerHTML).not.toContain('value="claude setup-token"')
    fireEvent.click(screen.getByRole('button', { name: 'Add an API key' }))
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('clears a retained local error when Add an API key opens the form', () => {
    const props = { grant: { linked: true, accounts: [account] }, onToggle: vi.fn() }
    const view = panel(props)
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Work' }))
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'claude setup-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
    expect(screen.getByRole('alert')).toBeInTheDocument()
    // A refreshed account list closes the form without discarding the local error.
    view.rerender(<ClaudeAccountPanel {...props} grant={{ linked: true, accounts: [account, ...linkedResult.accounts] }} open />)
    fireEvent.click(screen.getByRole('button', { name: 'Add an API key' }))
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows one server error beside submit before the note and scrolls errors into view', () => {
    const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
    const scroll = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll })
    try {
      const props = { grant: { linked: true, accounts: [account] }, onToggle: vi.fn() }
      const view = panel(props)
      fireEvent.click(screen.getByRole('button', { name: 'Add an API key' }))
      view.rerender(<ClaudeAccountPanel {...props} error="Could not link this account." open />)
      const alert = screen.getByRole('alert')
      expect(screen.getAllByRole('alert')).toHaveLength(1)
      expect(alert.previousElementSibling).toBe(screen.getByRole('button', { name: 'Link API key' }))
      expect(alert.closest('.ca-setup')).not.toBeNull()
      expect(alert.compareDocumentPosition(screen.getByText('Credentials are stored securely for your workspace and are hidden after linking.')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
      expect(scroll.mock.contexts).toContain(alert)
      scroll.mockClear()
      fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'claude fake-input' } })
      fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
      const localAlert = screen.getByText('That is not an API key. Copy the key from Claude Console (it starts with sk-ant-api).')
      expect(scroll.mock.contexts).toContain(localAlert)
      expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(screen.getAllByRole('alert')).toHaveLength(1)
      expect(screen.getByRole('alert').closest('.ca-setup')).toBeNull()
    } finally {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', descriptor)
      else delete HTMLElement.prototype.scrollIntoView
    }
  })

  it('uses the active account id ahead of the active flag for success and clears success on unlink', async () => {
    const result = { linked: true, active_account_id: 'b', accounts: [
      { id: 'a', label: 'First', active: true, plan: 'pro' },
      { id: 'b', label: 'Second', plan: 'team' },
    ] }
    const props = { grant: { linked: false }, onLink: vi.fn().mockResolvedValue(result), onToggle: vi.fn() }
    const view = panel(props)
    enterApiKey()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    view.rerender(<ClaudeAccountPanel {...props} grant={result} open />)
    expect(screen.getByRole('status')).toHaveTextContent('2 accounts linked. Added: Team.')
    fireEvent.click(screen.getAllByRole('button', { name: 'Unlink' })[0])
    expect(screen.queryByText(/Claude checks it on your first request/)).not.toBeInTheDocument()
  })

  it.each(['missing-old', 'no-usable'])('does not unlink without a returned old entry and a usable replacement: %s', async (shape) => {
    const result = shape === 'missing-old' ? linkedResult : { linked: true, accounts: [account] }
    const onUnlink = vi.fn()
    panel({ grant: { linked: true, accounts: [account] }, onLink: vi.fn().mockResolvedValue(result), onUnlink })
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Work' }))
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'sk-ant-api03-FAKE-replacement' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    expect(onUnlink).not.toHaveBeenCalled()
  })

  it.each(['cancel', 'close'])('forgets the replacement entry after %s', async (action) => {
    const result = { ...linkedResult, accounts: [account, ...linkedResult.accounts] }
    const props = { grant: { linked: true, accounts: [account] }, onLink: vi.fn().mockResolvedValue(result), onUnlink: vi.fn(), onToggle: vi.fn() }
    const view = panel(props)
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Work' }))
    if (action === 'cancel') {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      fireEvent.click(screen.getByRole('button', { name: 'Add an API key' }))
    } else {
      view.rerender(<ClaudeAccountPanel {...props} open={false} />)
      view.rerender(<ClaudeAccountPanel {...props} open />)
    }
    enterApiKey()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    expect(props.onUnlink).not.toHaveBeenCalled()
  })

  it('does not restore a pending success after the panel closes', async () => {
    let resolve
    const props = { grant: { linked: false }, onLink: () => new Promise((done) => { resolve = done }), onToggle: vi.fn() }
    const view = panel(props)
    enterApiKey()
    fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
    view.rerender(<ClaudeAccountPanel {...props} open={false} />)
    await act(async () => { resolve(linkedResult) })
    view.rerender(<ClaudeAccountPanel {...props} grant={linkedResult} open />)
    expect(screen.queryByText(/Claude checks it on your first request/)).not.toBeInTheDocument()
  })

})

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
    expect(screen.getByLabelText('Anthropic API key')).toBeInTheDocument()
  })
  it('shows known linked status and preserves mutation errors', () => {
    panel({ grant: { linked: true, kind: 'oauth', accounts: [] }, error: 'Could not unlink this account.' })
    expect(screen.getByText('1 linked')).toBeInTheDocument()
    expect(screen.getByText('Could not unlink this account.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add an API key' })).toBeInTheDocument()
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
  it('offers only an API key with three Console steps', () => {
    panel({ grant: { linked: false, accounts: [] } })
    const dialog = screen.getByRole('dialog', { name: 'Claude accounts' })
    expect(dialog).toHaveTextContent('No Anthropic API key is linked yet.')
    expect(dialog.textContent).not.toMatch(/subscription|setup-token|Claude Code|\bPro\b|\bMax\b/)
    expect(within(dialog).queryByRole('radiogroup')).not.toBeInTheDocument()
    const steps = within(dialog).getByRole('list', { name: 'Steps to create a key' })
    expect(within(steps).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Sign in to Claude Console and open API keys.',
      'Create a key and copy it. Console shows it only once.',
      'Paste it below.',
    ])
    expect(within(steps).getByRole('link', { name: 'Claude Console' })).toHaveAttribute('href', 'https://platform.claude.com/settings/keys')
    expect(dialog).toHaveTextContent('Usage bills to your Anthropic account at API rates.')
    expect(screen.getByLabelText('Key label')).toHaveValue('My API key')
    expect(screen.getByLabelText('Key label')).toHaveAttribute('maxlength', '120')
  })
})

describe('Claude account link form (rehearsal issues 24 and 25)', () => {
  it('links an API key without a plan choice', async () => {
    const onLink = vi.fn().mockResolvedValue(null)
    panel({ grant: { linked: false, accounts: [] }, onLink })
    fireEvent.change(screen.getByLabelText('Key label'), { target: { value: '  My API key  ' } })
    enterApiKey()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    expect(onLink).toHaveBeenCalledWith('sk-ant-api03-FAKE-secret', 'api_key', 'My API key', undefined)
  })

  it('masks the credential without a password field, so browsers do not offer to save it', () => {
    panel({ grant: { linked: false, accounts: [] } })
    const field = screen.getByLabelText('Anthropic API key')
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

describe('Claude account form safety', () => {
  it('refuses a token typed into the visible account label and clears it', () => {
    const onLink = vi.fn()
    panel({ grant: { linked: false, accounts: [] }, onLink })
    fireEvent.change(screen.getByLabelText('Key label'), { target: { value: 'sk-ant-api03-FAKE' } })
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'sk-ant-api03-FAKE' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
    expect(onLink).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Key label')).toHaveValue('')
    expect(screen.getByRole('alert')).toHaveTextContent('The key goes in the key field, not the label.')
  })
  it('does not unlink an account that is no longer refused when its replacement links', async () => {
    const old = { id: 'a', label: 'Mine', kind: 'api_key' }
    const onUnlink = vi.fn()
    const onLink = vi.fn().mockResolvedValue({ linked: true, active_account_id: 'b', accounts: [old, { id: 'b', label: 'Mine', kind: 'oauth', plan: 'max', active: true }] })
    panel({ grant: { linked: true, accounts: [{ ...old, auth_refused_code: 'authentication_failed' }] }, onLink, onUnlink })
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Mine' }))
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value: 'sk-ant-api03-FAKE' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
    expect(await screen.findByText(/2 accounts linked/)).toBeInTheDocument()
    expect(onUnlink).not.toHaveBeenCalled()
  })
})

describe('API key paste mistakes', () => {
  it.each(['CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-FAKE', 'export ANTHROPIC_API_KEY=sk-ant-api03-FAKE'])('refuses %s without sending it', (value) => {
    const onLink = vi.fn()
    panel({ grant: { linked: false, accounts: [] }, onLink })
    fireEvent.change(screen.getByLabelText('Anthropic API key'), { target: { value } })
    fireEvent.click(screen.getByRole('button', { name: 'Link API key' }))
    expect(onLink).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('That is not an API key.')
    expect(screen.getByRole('alert').textContent).not.toContain('FAKE')
  })
})
