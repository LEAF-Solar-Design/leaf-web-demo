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
  expect(screen.queryByLabelText('Claude token')).not.toBeInTheDocument()
  expect(screen.queryByText('not linked')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Link Claude account|Link another account|Remove|Unlink/ })).not.toBeInTheDocument()
}

const account = { id: 'old', label: 'Work', kind: 'oauth', plan: 'max', active: true, auth_refused_code: 'authentication_failed' }
const linkedResult = { linked: true, active_account_id: 'new', accounts: [{ id: 'new', label: 'New', kind: 'oauth', plan: 'max', active: true }] }

function enterSubscription(value = 'sk-ant-oat01-FAKE-secret') {
  fireEvent.click(screen.getByRole('radio', { name: 'Max' }))
  fireEvent.change(screen.getByLabelText('Claude token'), { target: { value } })
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

  it.each(['https://signin.test/#FAKE', 'http://signin.test', 'FAKE#code', 'claude setup-token'])('explains mistaken subscription input %s without sending it', (value) => {
    const onLink = vi.fn()
    panel({ grant: { linked: false }, onLink })
    enterSubscription(value)
    fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' }))
    expect(onLink).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Claude token')).toHaveValue(value)
    expect(screen.getByRole('alert')).toHaveTextContent(value.startsWith('claude ')
      ? 'That is the command. Run it in a terminal, then paste the token it prints.'
      : 'That is the sign-in code or link, not the token.')
    expect(screen.getByRole('alert').textContent).not.toContain(value)
  })

  it.each(['failure', 'rejection', 'success'])('removes a fake token everywhere after submit, including %s', async (outcome) => {
    const fakeToken = 'sk-ant-oat01-FAKE-no-lingering-secret'
    let resolve, reject
    const onLink = vi.fn(() => new Promise((done, fail) => { resolve = done; reject = fail }))
    const logs = ['log', 'warn', 'error'].map((name) => vi.spyOn(console, name))
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    try {
      panel({ grant: { linked: false }, onLink })
      enterSubscription(fakeToken)
      const input = screen.getByLabelText('Claude token')
      expect(input).toHaveAttribute('data-bwignore', 'true')
      expect(input).toHaveAttribute('writingsuggestions', 'false')
      expect(input.closest('form')).toBeNull()
      fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' }))
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
    enterSubscription('  sk-ant-oat01-FAKE wrapped\tvalue  ')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' })) })
    expect(onLink).toHaveBeenCalledWith('sk-ant-oat01-FAKEwrappedvalue', 'oauth', 'My Claude account', 'max')
    expect(screen.getByLabelText('Claude token')).toHaveValue('')
  })

  it('clears the field on close and sign out', () => {
    const props = { grant: { linked: false }, onToggle: vi.fn() }
    const view = panel(props)
    enterSubscription()
    view.rerender(<ClaudeAccountPanel {...props} open={false} />)
    view.rerender(<ClaudeAccountPanel {...props} open />)
    expect(screen.getByLabelText('Claude token')).toHaveValue('')
    enterSubscription()
    view.rerender(<ClaudeAccountPanel {...props} open signedOut />)
    view.rerender(<ClaudeAccountPanel {...props} open />)
    expect(screen.getByLabelText('Claude token')).toHaveValue('')
  })

  it.each([
    [linkedResult, '1 account linked, Max. Claude checks it on your first request.'],
    [{ linked: true }, '1 account linked. Claude checks it on your first request.'],
    [{ linked: true, accounts: [{ id: 'a', active: true, plan: 'unknown' }] }, '1 account linked. Claude checks it on your first request.'],
    [{ linked: true, accounts: [{ id: 'a', active: true, kind: 'api_key' }] }, '1 account linked, API key. Claude checks it on your first request.'],
    [{ ...linkedResult, accounts: [...linkedResult.accounts, { id: 'other' }] }, '2 accounts linked. Added: Max. Claude checks it on your first request.'],
    [{ linked: true, accounts: [{ id: 'a' }, { id: 'b' }] }, '2 accounts linked. Claude checks it on your first request.'],
  ])('announces and focuses result-derived success', async (result, text) => {
    const props = { grant: { linked: false }, onLink: vi.fn().mockResolvedValue(result), onToggle: vi.fn() }
    const view = panel(props)
    enterSubscription()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' })) })
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
    expect(screen.getByLabelText('Account label')).toHaveValue('Work')
    expect(screen.getByRole('radio', { name: 'Max' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.change(screen.getByLabelText('Claude token'), { target: { value: 'sk-ant-oat01-FAKE-replacement' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' })) })
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

  it('clears the token and local error on Cancel', () => {
    panel({ grant: { linked: true, accounts: [account] } })
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Work' }))
    fireEvent.change(screen.getByLabelText('Claude token'), { target: { value: 'claude setup-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' }))
    expect(screen.getByRole('alert')).toHaveTextContent('That is the command.')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(document.documentElement.outerHTML).not.toContain('value="claude setup-token"')
    fireEvent.click(screen.getByRole('button', { name: 'Link another account' }))
    expect(screen.getByLabelText('Claude token')).toHaveValue('')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('clears a retained local error when Link another account opens the form', () => {
    const props = { grant: { linked: true, accounts: [account] }, onToggle: vi.fn() }
    const view = panel(props)
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Work' }))
    fireEvent.change(screen.getByLabelText('Claude token'), { target: { value: 'claude setup-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' }))
    expect(screen.getByRole('alert')).toBeInTheDocument()
    // A refreshed account list closes the form without discarding the local error.
    view.rerender(<ClaudeAccountPanel {...props} grant={{ linked: true, accounts: [account, ...linkedResult.accounts] }} open />)
    fireEvent.click(screen.getByRole('button', { name: 'Link another account' }))
    expect(screen.getByLabelText('Claude token')).toHaveValue('')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows one server error beside submit before the note and scrolls errors into view', () => {
    const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
    const scroll = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll })
    try {
      const props = { grant: { linked: true, accounts: [account] }, onToggle: vi.fn() }
      const view = panel(props)
      fireEvent.click(screen.getByRole('button', { name: 'Link another account' }))
      view.rerender(<ClaudeAccountPanel {...props} error="Could not link this account." open />)
      const alert = screen.getByRole('alert')
      expect(screen.getAllByRole('alert')).toHaveLength(1)
      expect(alert.previousElementSibling).toBe(screen.getByRole('button', { name: 'Link Claude account' }))
      expect(alert.closest('.ca-setup')).not.toBeNull()
      expect(alert.compareDocumentPosition(screen.getByText('Credentials are stored securely for your workspace and are hidden after linking.')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
      expect(scroll.mock.contexts).toContain(alert)
      scroll.mockClear()
      fireEvent.change(screen.getByLabelText('Claude token'), { target: { value: 'fake-input' } })
      fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' }))
      const localAlert = screen.getByText('Choose your Claude plan above (Pro, Max, Team or Enterprise).')
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
    enterSubscription()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' })) })
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
    fireEvent.change(screen.getByLabelText('Claude token'), { target: { value: 'sk-ant-oat01-FAKE-replacement' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' })) })
    expect(onUnlink).not.toHaveBeenCalled()
  })

  it.each(['cancel', 'close'])('forgets the replacement entry after %s', async (action) => {
    const result = { ...linkedResult, accounts: [account, ...linkedResult.accounts] }
    const props = { grant: { linked: true, accounts: [account] }, onLink: vi.fn().mockResolvedValue(result), onUnlink: vi.fn(), onToggle: vi.fn() }
    const view = panel(props)
    fireEvent.click(screen.getByRole('button', { name: 'Link again: Work' }))
    if (action === 'cancel') {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      fireEvent.click(screen.getByRole('button', { name: 'Link another account' }))
    } else {
      view.rerender(<ClaudeAccountPanel {...props} open={false} />)
      view.rerender(<ClaudeAccountPanel {...props} open />)
    }
    enterSubscription()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' })) })
    expect(props.onUnlink).not.toHaveBeenCalled()
  })

  it('does not restore a pending success after the panel closes', async () => {
    let resolve
    const props = { grant: { linked: false }, onLink: () => new Promise((done) => { resolve = done }), onToggle: vi.fn() }
    const view = panel(props)
    enterSubscription()
    fireEvent.click(screen.getByRole('button', { name: 'Link Claude account' }))
    view.rerender(<ClaudeAccountPanel {...props} open={false} />)
    await act(async () => { resolve(linkedResult) })
    view.rerender(<ClaudeAccountPanel {...props} grant={linkedResult} open />)
    expect(screen.queryByText(/Claude checks it on your first request/)).not.toBeInTheDocument()
  })

  it('copies each command independently and reports clipboard denial', async () => {
    vi.useFakeTimers()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const view = panel({ grant: { linked: false } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy install command' })) })
    expect(screen.getByRole('button', { name: 'Install command copied' })).toHaveTextContent('Copied')
    expect(screen.getByRole('button', { name: 'Copy setup command' })).toHaveTextContent('Copy')
    expect(writeText).toHaveBeenCalledWith('irm https://claude.ai/install.ps1 | iex')
    act(() => { vi.advanceTimersByTime(1000) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy setup command' })) })
    expect(screen.getByRole('button', { name: 'Setup command copied' })).toHaveTextContent('Copied')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(screen.getByRole('button', { name: 'Copy install command' })).toHaveTextContent('Copy')
    expect(screen.getByRole('button', { name: 'Setup command copied' })).toHaveTextContent('Copied')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(screen.getByRole('button', { name: 'Copy setup command' })).toHaveTextContent('Copy')
    writeText.mockRejectedValueOnce(new Error('Denied'))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy install command' })) })
    expect(screen.getByRole('status')).toHaveTextContent('Copy was blocked. Select the command and copy it by hand.')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy setup command' })) })
    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
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
    expect(within(steps).getByRole('link', { name: 'Claude Code' })).toHaveAttribute('href', 'https://code.claude.com/docs/en/setup')
    expect(steps).toHaveTextContent('claude setup-token')
    expect(steps).toHaveTextContent('Approve the request in the browser')
    expect(steps).toHaveTextContent('sk-ant-oat01-')
    for (const name of ['Copy install command', 'Copy setup command']) {
      const command = within(steps).getByRole('button', { name }).closest('.ca-cmd')
      expect(command.tagName).toBe('SPAN')
      expect(command.querySelector('code')).not.toBeNull()
    }
    fireEvent.click(within(steps).getByRole('button', { name: 'Copy setup command' }))
    expect(writeText).toHaveBeenCalledWith('claude setup-token')
    await waitFor(() => expect(within(steps).getByRole('button', { name: 'Setup command copied' })).toBeInTheDocument())
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
