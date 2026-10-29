import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import InviteAccept from './InviteAccept.jsx'
import { acceptOrgInvitation } from '../projects/api.js'
import { isSignedIn, loginWithPopupFromClick, subscribeTokenStored } from '../auth.js'

vi.mock('../projects/api.js', () => ({ acceptOrgInvitation: vi.fn() }))
vi.mock('../auth.js', () => ({ authConfigured: true, isSignedIn: vi.fn(), loginWithPopupFromClick: vi.fn(), subscribeTokenStored: vi.fn() }))

const TOKEN = 'a'.repeat(43)
beforeEach(() => {
  vi.resetAllMocks()
  isSignedIn.mockReturnValue(true)
  subscribeTokenStored.mockReturnValue(() => {})
  window.history.replaceState(null, '', `/app#invite=${TOKEN}`)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/app')
})

describe('accept organization invitation', () => {
  it('renders nothing without an invite fragment', () => {
    window.history.replaceState(null, '', '/app#other=thing')
    const { container } = render(<InviteAccept />)
    expect(container.innerHTML).toBe('')
    expect(window.location.hash).toBe('#other=thing')
  })

  it.each(['short', 'a'.repeat(44), '!'.repeat(43), ''])('rejects malformed token %s and clears it', token => {
    window.history.replaceState(null, '', `/app#invite=${token}`)
    render(<InviteAccept />)
    expect(screen.getByRole('alert')).toHaveTextContent('This invite link is not valid.')
    expect(window.location.hash).toBe('')
    expect(acceptOrgInvitation).not.toHaveBeenCalled()
  })

  it('removes the fragment with replaceState while preserving path, query and history state', () => {
    window.history.replaceState({ key: 'kept' }, '', `/app?view=project#invite=${TOKEN}`)
    const replace = vi.spyOn(window.history, 'replaceState')
    render(<InviteAccept />)
    expect(replace).toHaveBeenCalledWith({ key: 'kept' }, '', '/app?view=project')
    expect(window.location.hash).toBe('')
  })

  it('accepts once and refreshes the workspace with the joined organization', async () => {
    const result = { org_id: 'org-one', binding_id: 'binding-one', role: 'editor' }
    acceptOrgInvitation.mockResolvedValue(result)
    const onAccepted = vi.fn()
    const stored = vi.spyOn(Storage.prototype, 'setItem')
    render(<InviteAccept onAccepted={onAccepted} />)
    expect(screen.getByText('Join this organization as an editor?')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(await screen.findByRole('status')).toHaveTextContent('You joined the organization.')
    expect(acceptOrgInvitation).toHaveBeenCalledTimes(1)
    expect(acceptOrgInvitation).toHaveBeenCalledWith(TOKEN)
    expect(onAccepted).toHaveBeenCalledWith(result)
    expect(stored).not.toHaveBeenCalled()
  })

  it.each([
    [404, 'invitation_unavailable', 'This invite has expired or was already used.'],
    [409, 'already_member', 'You already belong to an organization.'],
  ])('explains failure %s %s', async (status, detail, message) => {
    acceptOrgInvitation.mockRejectedValue({ status, body: { detail } })
    const onAccepted = vi.fn()
    render(<InviteAccept onAccepted={onAccepted} />)
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(await screen.findByRole('status')).toHaveTextContent(message)
    expect(onAccepted).not.toHaveBeenCalled()
  })

  it('holds the token in memory while asking a signed-out user to sign in', async () => {
    isSignedIn.mockReturnValue(false)
    render(<InviteAccept />)
    expect(screen.getByText(/Sign in first/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull()
    expect(window.location.hash).toBe('')
    isSignedIn.mockReturnValue(true)
    act(() => subscribeTokenStored.mock.calls[0][0]())
    acceptOrgInvitation.mockResolvedValue({ org_id: 'org-one' })
    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(acceptOrgInvitation).toHaveBeenCalledWith(TOKEN))
  })

  it('cancel discards the invitation without calling the API', () => {
    const { container } = render(<InviteAccept />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(container.innerHTML).toBe('')
    expect(acceptOrgInvitation).not.toHaveBeenCalled()
  })

  it('uses popup sign-in so the in-memory invitation survives authentication', async () => {
    isSignedIn.mockReturnValue(false)
    loginWithPopupFromClick.mockImplementation(async () => {
      isSignedIn.mockReturnValue(true)
      subscribeTokenStored.mock.calls[0][0]()
      return { ok: true }
    })
    render(<InviteAccept />)
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await screen.findByRole('button', { name: 'Accept' })
    expect(loginWithPopupFromClick).toHaveBeenCalledTimes(1)
    acceptOrgInvitation.mockResolvedValue({ org_id: 'org-one' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(acceptOrgInvitation).toHaveBeenCalledWith(TOKEN))
  })
})
