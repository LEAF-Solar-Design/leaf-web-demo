import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  expect(screen.queryByRole('button', { name: /Link Claude account|Add another mount|Remove|Unlink/ })).not.toBeInTheDocument()
}

describe('Claude account administrative status', () => {
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
  it('shows known mounted status and preserves mutation errors', () => {
    panel({ grant: { linked: true, kind: 'oauth', accounts: [] }, error: 'Could not unlink this account.' })
    expect(screen.getByText('1 mounted')).toBeInTheDocument()
    expect(screen.getByText('Could not unlink this account.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add another mount' })).toBeInTheDocument()
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
    panel({ mock: true, grant: { linked: false } })
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
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
  const onLink = vi.fn().mockResolvedValue(null)
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
