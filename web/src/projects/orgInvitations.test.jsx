import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import OrgInvitations from './OrgInvitations.jsx'
import { createOrgInvitation, listOrgInvitations, revokeOrgInvitation } from './api.js'

vi.mock('./api.js', () => ({
  createOrgInvitation: vi.fn(), listOrgInvitations: vi.fn(), revokeOrgInvitation: vi.fn(),
}))

const ORG = '11111111-2222-4333-8444-555555555555'
const TOKEN = 'a'.repeat(43)
const ROW = { invitation_id: 'invite-one', label: 'Design team', created_at: '2026-10-22', expires_at: '2026-10-29' }

beforeEach(() => {
  vi.resetAllMocks()
  listOrgInvitations.mockResolvedValue({ invitations: [] })
  createOrgInvitation.mockResolvedValue({ invitation_id: ROW.invitation_id, token: TOKEN, expires_at: ROW.expires_at })
  revokeOrgInvitation.mockResolvedValue({})
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function open() {
  const view = render(<OrgInvitations orgId={ORG} />)
  fireEvent.click(screen.getByRole('button', { name: 'Invite to organization' }))
  await waitFor(() => expect(listOrgInvitations).toHaveBeenCalledWith(ORG))
  return view
}

describe('organization invitation links', () => {
  it('creates a link, copies it, and never recovers it from the pending list', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const view = await open()
    fireEvent.change(screen.getByLabelText('Invitation label (optional)'), { target: { value: 'Design team' } })
    expect(screen.getByLabelText('Invitation label (optional)').maxLength).toBe(80)
    fireEvent.click(screen.getByRole('button', { name: 'Create invite link' }))
    const input = await screen.findByLabelText('Invite link')
    const link = `${window.location.origin}/app#invite=${TOKEN}`
    expect(input.value).toBe(link)
    expect(createOrgInvitation).toHaveBeenCalledWith(ORG, 'Design team')
    expect(screen.getByText('Shown once. Anyone signed in with this link joins as an editor; it expires in 7 days.')).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copy' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(link))
    view.unmount()
    listOrgInvitations.mockResolvedValue({ invitations: [ROW] })
    await open()
    await screen.findByText('Design team')
    expect(screen.queryByLabelText('Invite link')).toBeNull()
  })

  it('lists labels and dates and revokes a pending invitation', async () => {
    listOrgInvitations.mockResolvedValue({ invitations: [ROW] })
    await open()
    const row = (await screen.findByText('Design team')).closest('li')
    expect(within(row).getByText(`Created: ${ROW.created_at}`)).toBeTruthy()
    expect(within(row).getByText(`Expires: ${ROW.expires_at}`)).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(revokeOrgInvitation).toHaveBeenCalledWith(ORG, ROW.invitation_id))
    await waitFor(() => expect(screen.queryByText('Design team')).toBeNull())
  })

  it('explains the pending invitation limit', async () => {
    createOrgInvitation.mockRejectedValue({ status: 409, body: { detail: 'too_many_pending' } })
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Create invite link' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('This organization already has 20 pending invitations.')
  })

  it('renders nothing when listing invitations is forbidden', async () => {
    listOrgInvitations.mockRejectedValue({ status: 403, message: 'owner role required' })
    const { container } = await open()
    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows an alert when listing invitations fails with a server error', async () => {
    listOrgInvitations.mockRejectedValue({ status: 500, message: 'Invitations could not be loaded.' })
    await open()
    expect(await screen.findByRole('alert')).toHaveTextContent('Invitations could not be loaded.')
    expect(screen.getByRole('region', { name: 'Invite to organization' })).toBeTruthy()
  })

  it('keeps the token out of localStorage and the current URL', async () => {
    const stored = vi.spyOn(Storage.prototype, 'setItem')
    const originalUrl = window.location.href
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Create invite link' }))
    await screen.findByLabelText('Invite link')
    expect(stored).not.toHaveBeenCalled()
    expect(window.location.href).toBe(originalUrl)
  })

  it('surfaces a forbidden invitation action', async () => {
    revokeOrgInvitation.mockRejectedValue(new Error('You do not have permission to do that in this project.'))
    listOrgInvitations.mockResolvedValue({ invitations: [ROW] })
    await open()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('You do not have permission')
    expect(screen.getByText('Design team')).toBeTruthy()
  })
})
