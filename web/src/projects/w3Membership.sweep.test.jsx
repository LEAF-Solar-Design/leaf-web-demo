import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import Membership from './Membership.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

const members = [
  { member_id: 'owner-id', name: 'Olivia', role: 'owner' },
  { member_id: 'editor-id', name: 'Evan', role: 'editor' },
  { member_id: 'reader-id', email: 'reader@example.com', role: 'read-only' },
]

function setup(overrides = {}) {
  const props = {
    viewerId: 'owner-id',
    authority: { role: 'owner', can_invite: true, can_manage: true, can_label: true },
    members,
    identities: [{ binding_id: 'invite-id', name: 'Iris' }],
    onInvite: vi.fn().mockResolvedValue(undefined),
    onChangeRole: vi.fn().mockResolvedValue(undefined),
    onRevoke: vi.fn().mockResolvedValue(undefined),
    onSetLabel: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
  return { ...render(<Membership {...props} />), props }
}

describe('w3 membership sweep', () => {
  it('S1-162 offers editors only the invite roles the server permits', () => {
    setup({ viewerId: 'editor-id', authority: { role: 'editor', can_invite: true, can_manage: true } })
    const picker = screen.getByRole('combobox', { name: 'Invite role' })
    expect(within(picker).queryByRole('option', { name: 'owner' })).toBeNull()
    expect(within(picker).queryByRole('option', { name: 'editor' })).toBeNull()
    expect(Array.from(picker.options, option => option.value)).toEqual(['reviewer', 'read-only'])
  })

  it('S1-162 prevents editors from changing or revoking protected members with a stated reason', async () => {
    const { props } = setup({ viewerId: 'editor-id', authority: { role: 'editor', can_invite: true, can_manage: true } })
    for (const name of ['Olivia', 'Evan']) {
      const role = screen.getByRole('combobox', { name: `Role for ${name}` })
      const revoke = screen.getByRole('button', { name: `Revoke ${name}` })
      expect(role.disabled).toBe(true)
      expect(revoke.disabled).toBe(true)
      expect(role.title).toMatch(/Only an owner/)
      expect(revoke.title).toMatch(/Only an owner/)
      fireEvent.change(role, { target: { value: 'reviewer' } })
      fireEvent.click(revoke)
    }
    expect(props.onChangeRole).not.toHaveBeenCalled()
    expect(props.onRevoke).not.toHaveBeenCalled()
    const readerRole = screen.getByRole('combobox', { name: 'Role for reader@example.com' })
    expect(Array.from(readerRole.options, option => option.value)).toEqual(['reviewer', 'read-only'])
    fireEvent.change(readerRole, { target: { value: 'reviewer' } })
    await waitFor(() => expect(props.onChangeRole).toHaveBeenCalledWith('reader-id', 'reviewer'))
  })

  it('S1-169 describes unloaded members as an invitation to load them', () => {
    setup({ identities: null })
    expect(screen.getByRole('status').textContent).toBe('Load organization members to choose someone to invite.')
    expect(screen.queryByText(/unavailable/i)).toBeNull()
  })

  it('S1-173 cancels access removal without sending a mutation', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { props } = setup()
    const button = screen.getByRole('button', { name: 'Revoke Evan' })
    button.focus()
    fireEvent.click(button)
    expect(confirm).toHaveBeenCalledWith('Remove Evan from this project? They will lose access.')
    expect(props.onRevoke).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(button)
  })

  it('S1-173 explains self-removal and restores focus after the member disappears', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { props, rerender } = setup()
    screen.getByRole('button', { name: 'Revoke Olivia' }).focus()
    fireEvent.click(screen.getByRole('button', { name: 'Revoke Olivia' }))
    expect(confirm).toHaveBeenCalledWith('Remove your own access? You will no longer be able to open this project.')
    await waitFor(() => expect(props.onRevoke).toHaveBeenCalledWith('owner-id'))
    rerender(<Membership {...props} members={members.slice(1)} />)
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('region', { name: 'Membership' })))
    expect(screen.getByRole('status').textContent).toMatch(/no longer have access/)
  })

  it('S1-174 keeps derived names unsaved until an actual edit', async () => {
    const { props } = setup({ members: [...members, { member_id: 'private-internal-id', role: 'reviewer' }] })
    for (const name of ['reader@example.com', 'Unnamed member']) {
      const button = screen.getByRole('button', { name: `Save display name for ${name}` })
      expect(button.disabled).toBe(true)
      expect(button.title).toBe('Edit the display name before saving.')
      fireEvent.click(button)
    }
    expect(props.onSetLabel).not.toHaveBeenCalled()
    expect(screen.queryByText(/private-internal-id/)).toBeNull()
    fireEvent.change(screen.getByLabelText('Display name for Unnamed member'), { target: { value: '  Alex  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save display name for Unnamed member' }))
    await waitFor(() => expect(props.onSetLabel).toHaveBeenCalledWith('private-internal-id', 'Alex'))
  })

  it('S1-174 treats whitespace around an unchanged name as no edit', () => {
    const { props } = setup()
    fireEvent.change(screen.getByLabelText('Display name for Evan'), { target: { value: '  Evan  ' } })
    const save = screen.getByRole('button', { name: 'Save display name for Evan' })
    expect(save.disabled).toBe(true)
    fireEvent.click(save)
    expect(props.onSetLabel).not.toHaveBeenCalled()
  })

  it('keeps a failed removal visible and allows a confirmed retry', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onRevoke = vi.fn().mockRejectedValueOnce(new Error('DELETE /api/members -> 500')).mockResolvedValueOnce(undefined)
    setup({ onRevoke })
    fireEvent.click(screen.getByRole('button', { name: 'Revoke Evan' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('The service had a temporary problem. Try again.')
    const retry = screen.getByRole('button', { name: 'Revoke Evan' })
    await waitFor(() => expect(retry.disabled).toBe(false))
    fireEvent.click(retry)
    await waitFor(() => expect(onRevoke).toHaveBeenCalledTimes(2))
  })
})
