/**
 * Membership. Card B-U2 acceptance oracle, one describe block per assertion:
 *   1. Owner invites/demotes/revokes; a revoked member's UI drops the project
 *      on next read (mirrors server immediate-revocation semantics).
 *   2. The role matrix renders from the server's authority response, never
 *      inferred client-side; read-only sees no mutation affordances.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import Membership from './Membership.jsx'

afterEach(cleanup)

const OWNER_AUTHORITY = { role: 'owner', can_invite: true, can_manage: true }
const EDITOR_AUTHORITY = { role: 'editor', can_invite: false, can_manage: false }
const READONLY_AUTHORITY = { role: 'read-only', can_invite: false, can_manage: false }

const MEMBERS = [
  { member_id: 'u-owner', name: 'Owner One', email: 'owner@x.com', role: 'owner' },
  { member_id: 'u-editor', name: 'Editor Two', email: 'editor@x.com', role: 'editor' },
  { member_id: 'u-reviewer', name: 'Reviewer Three', email: 'reviewer@x.com', role: 'reviewer' },
]

function setup(over = {}) {
  const onInvite = over.onInvite || vi.fn().mockResolvedValue(undefined)
  const onChangeRole = over.onChangeRole || vi.fn().mockResolvedValue(undefined)
  const onRevoke = over.onRevoke || vi.fn().mockResolvedValue(undefined)
  const rendered = render(
    <Membership
      viewerId={over.viewerId ?? 'u-owner'}
      authority={over.authority ?? OWNER_AUTHORITY}
      members={over.members ?? MEMBERS}
      onInvite={onInvite}
      onChangeRole={onChangeRole}
      onRevoke={onRevoke}
      onSetLabel={over.onSetLabel}
      identities={Object.hasOwn(over, 'identities') ? over.identities : [{ binding_id: 'binding-new', label: 'New Member' }]}
    />,
  )
  return { onInvite, onChangeRole, onRevoke, ...rendered }
}

it('B5 an org owner saves a display name for a roster member', async () => {
  const onSetLabel = vi.fn().mockResolvedValue(undefined)
  setup({ authority: { ...OWNER_AUTHORITY, can_label: true }, onSetLabel })
  fireEvent.change(screen.getByLabelText('Display name for Editor Two'), {
    target: { value: '  Ada Lovelace  ' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Save display name for Editor Two' }))
  await waitFor(() => expect(onSetLabel).toHaveBeenCalledWith('u-editor', 'Ada Lovelace'))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save display name for Editor Two' }).disabled).toBe(false))
  fireEvent.change(screen.getByLabelText('Display name for Editor Two'), { target: { value: '   ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save display name for Editor Two' }))
  await waitFor(() => expect(onSetLabel).toHaveBeenLastCalledWith('u-editor', ''))
})

it('B5 the display name editor is absent unless the server grants can_label', () => {
  const onSetLabel = vi.fn()
  const { rerender } = setup({ onSetLabel })
  expect(screen.queryByLabelText('Display name for Editor Two')).toBeNull()
  rerender(<Membership members={MEMBERS} authority={{ ...OWNER_AUTHORITY, can_label: false }} onSetLabel={onSetLabel} />)
  expect(screen.queryByLabelText('Display name for Editor Two')).toBeNull()
  rerender(<Membership members={MEMBERS} authority={{ ...OWNER_AUTHORITY, can_label: true }} />)
  expect(screen.queryByLabelText('Display name for Editor Two')).toBeNull()
})

it('B5 a refused display name shows the server message', async () => {
  const onSetLabel = vi.fn().mockRejectedValue({ body: { detail: 'only the organization owner can name members' } })
  setup({ authority: { ...OWNER_AUTHORITY, can_label: true }, onSetLabel })
  fireEvent.change(screen.getByLabelText('Display name for Editor Two'), { target: { value: 'Ada Lovelace' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save display name for Editor Two' }))
  expect((await screen.findByRole('alert')).textContent).toBe('only the organization owner can name members')
  expect(screen.getByLabelText('Display name for Editor Two').value).toBe('Ada Lovelace')
})

it('B5 a display name over 100 characters is refused before sending', () => {
  const onSetLabel = vi.fn()
  setup({ authority: { ...OWNER_AUTHORITY, can_label: true }, onSetLabel })
  fireEvent.change(screen.getByLabelText('Display name for Editor Two'), { target: { value: 'a'.repeat(101) } })
  fireEvent.click(screen.getByRole('button', { name: 'Save display name for Editor Two' }))
  expect(screen.getByRole('alert').textContent).toBe('A display name can be at most 100 characters.')
  expect(onSetLabel).not.toHaveBeenCalled()
})

describe('acceptance #1: owner invites/demotes/revokes; revoked member drops the project on next read', () => {
  it('an owner invites an existing organization binding by label and role', async () => {
    // w4h-b4: the scoped picker replaces an email the API cannot accept.
    const { onInvite, container } = setup()
    expect(container.querySelector('input[type="email"]')).toBeNull()
    fireEvent.change(screen.getByLabelText('Search organization members'), { target: { value: 'new' } })
    fireEvent.change(screen.getByLabelText('Invite member'), { target: { value: 'binding-new' } })
    fireEvent.change(screen.getByLabelText(/invite role/i), { target: { value: 'editor' } })
    fireEvent.click(screen.getByRole('button', { name: /^invite$/i }))
    await waitFor(() => expect(onInvite).toHaveBeenCalledWith('binding-new', 'editor'))
    // Fields clear once the invite lands — no stale draft left behind.
    await waitFor(() => expect(screen.getByLabelText('Invite member').value).toBe(''))
  })

  it('an owner demotes a member by changing their role', async () => {
    const { onChangeRole } = setup()
    fireEvent.change(screen.getByLabelText('Role for Editor Two'), { target: { value: 'read-only' } })
    await waitFor(() => expect(onChangeRole).toHaveBeenCalledWith('u-editor', 'read-only'))
  })

  it('an owner revokes a member', async () => {
    const { onRevoke } = setup()
    fireEvent.click(screen.getByRole('button', { name: /revoke reviewer three/i }))
    await waitFor(() => expect(onRevoke).toHaveBeenCalledWith('u-reviewer'))
  })

  it("a revoked member's UI drops the whole project on next read, not just their own row", () => {
    const { rerender } = setup({ viewerId: 'u-editor', authority: EDITOR_AUTHORITY })
    // Before revocation: the roster (and the rest of the project surface it
    // gates) is visible to the viewer.
    expect(screen.getByText('Owner One')).toBeTruthy()
    expect(screen.getByText('Reviewer Three')).toBeTruthy()

    // Next read: the server's authoritative member list no longer includes
    // this viewer (they were revoked). No prop says "revoked" explicitly —
    // absence from the authoritative list IS the revocation, exactly like the
    // server's immediate-revocation semantics.
    rerender(
      <Membership
        viewerId="u-editor"
        authority={EDITOR_AUTHORITY}
        members={MEMBERS.filter((m) => m.member_id !== 'u-editor')}
        onInvite={vi.fn()}
        onChangeRole={vi.fn()}
        onRevoke={vi.fn()}
      />,
    )

    expect(screen.queryByText('Owner One')).toBeNull()
    expect(screen.queryByText('Reviewer Three')).toBeNull()
    expect(screen.getByRole('status').textContent).toMatch(/no longer have access/i)
  })
})

describe('w4h-b4 scoped picker and roster labels', () => {
  it('B4-c row4 locks two synchronous invite submits immediately', async () => {
    let finishInvite
    const onInvite = vi.fn(() => new Promise((resolve) => { finishInvite = resolve }))
    setup({ onInvite })
    const picker = screen.getByLabelText('Invite member')
    fireEvent.change(picker, { target: { value: 'binding-new' } })
    const form = picker.closest('form')
    act(() => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    expect(onInvite).toHaveBeenCalledTimes(1)
    await act(async () => { finishInvite(); await Promise.resolve() })
  })

  it('B4-c row6 replaces email entry with two labeled organization choices', async () => {
    const { container, onInvite } = setup({ identities: [
      { binding_id: 'binding-alex', label: 'Alex' },
      { binding_id: 'binding-sam', label: 'Sam' },
    ] })
    const picker = screen.getByRole('combobox', { name: 'Invite member' })
    expect(picker.tagName).toBe('SELECT')
    expect(screen.getByRole('option', { name: 'Alex' }).value).toBe('binding-alex')
    expect(screen.getByRole('option', { name: 'Sam' }).value).toBe('binding-sam')
    expect(container.querySelector('input[type="email"]')).toBeNull()
    fireEvent.change(picker, { target: { value: 'binding-sam' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Invite role' }), { target: { value: 'reviewer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }))
    await waitFor(() => expect(onInvite).toHaveBeenCalledWith('binding-sam', 'reviewer'))
  })

  it('distinguishes short labels by role and joined date and searches the full option text', async () => {
    const { onInvite } = setup({ identities: [
      { binding_id: '12345678-owner', label: 'Member 12345678', role: 'owner', created_at: '2026-08-01T00:00:00Z' },
      { binding_id: '12345678-editor', label: 'Member 12345678', role: 'editor', created_at: '2026-08-02T00:00:00Z' },
    ] })
    expect(screen.getByRole('option', { name: 'Member 12345678 · owner · joined 2026-08-01' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Member 12345678 · editor · joined 2026-08-02' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Search organization members'), { target: { value: 'editor' } })
    const picker = screen.getByLabelText('Invite member')
    expect(Array.from(picker.options, (option) => option.value)).toEqual(['', '12345678-editor'])
    fireEvent.change(picker, { target: { value: '12345678-editor' } })
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }))
    await waitFor(() => expect(onInvite).toHaveBeenCalledWith('12345678-editor', 'read-only'))
  })

  it.each([[], undefined])('offers no invite when identities are empty or unavailable: %s', (identities) => {
    setup({ identities })
    expect(screen.getByRole('status').textContent).toMatch(/organization members.*(available|unavailable)/i)
    expect(screen.queryByLabelText('Invite member')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Invite' })).toBeNull()
  })

  it('uses the supplied label, then name, email, and a short binding label, and marks self', () => {
    setup({ members: [
      { member_id: 'u-owner', label: 'Owner label', name: 'Ignored name', role: 'owner' },
      { member_id: 'two', name: 'Display name', email: 'ignored@example.com', role: 'editor' },
      { member_id: 'three', email: 'reader@example.com', role: 'reviewer' },
      { member_id: 'four', binding_id: '12345678-abcd-4321-8765-123456789abc', role: 'read-only' },
    ] })
    for (const label of ['Owner label', 'Display name', 'reader@example.com', 'Member 12345678']) {
      expect(screen.getByLabelText(`Role for ${label}`)).toBeTruthy()
    }
    expect(screen.getByText('(you)')).toBeTruthy()
    expect(screen.queryByText('12345678-abcd-4321-8765-123456789abc')).toBeNull()
  })
})

describe('acceptance #2: role matrix from server authority, never inferred client-side; read-only sees no mutation affordances', () => {
  it("renders each member's role exactly as the server sent it", () => {
    setup()
    expect(screen.getByLabelText('Role for Owner One').value).toBe('owner')
    expect(screen.getByLabelText('Role for Editor Two').value).toBe('editor')
    expect(screen.getByLabelText('Role for Reviewer Three').value).toBe('reviewer')
  })

  it('a read-only viewer sees no invite form and no per-row mutation affordances', () => {
    const readonlyMembers = [...MEMBERS, { member_id: 'u-readonly', name: 'Reader Four', email: 'reader@x.com', role: 'read-only' }]
    setup({ viewerId: 'u-readonly', authority: READONLY_AUTHORITY, members: readonlyMembers })

    expect(screen.queryByLabelText('Invite member')).toBeNull()
    expect(screen.queryByRole('button', { name: /^invite$/i })).toBeNull()
    expect(screen.queryAllByRole('combobox').length).toBe(0)
    expect(screen.queryAllByRole('button', { name: /revoke/i }).length).toBe(0)
    // The role is still shown, just as inert text, not a mutation control.
    expect(screen.getByText('owner')).toBeTruthy()
  })

  it('gates mutation affordances on the server authority flag, never on a client-side role comparison', () => {
    // Deliberately contradicts client-inferrable "owner" role on the viewer's
    // own roster row with an authority response that says can_manage:false —
    // if the component were comparing role strings itself instead of trusting
    // `authority.can_manage`, this would wrongly render mutation controls.
    const contradictingMembers = [
      { member_id: 'u-owner', name: 'Owner One', email: 'owner@x.com', role: 'owner' },
      ...MEMBERS.slice(1),
    ]
    setup({ viewerId: 'u-owner', authority: EDITOR_AUTHORITY, members: contradictingMembers })

    expect(screen.queryByLabelText('Invite member')).toBeNull()
    expect(screen.queryAllByRole('combobox').length).toBe(0)
    expect(screen.queryAllByRole('button', { name: /revoke/i }).length).toBe(0)
  })

  it('renders nothing while no authority response has been read yet (no guessed matrix)', () => {
    const { container } = render(
      <Membership viewerId="u-owner" authority={null} members={MEMBERS} onInvite={vi.fn()} onChangeRole={vi.fn()} onRevoke={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()
  })
})
