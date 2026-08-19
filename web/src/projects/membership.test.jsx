/**
 * Membership. Card B-U2 acceptance oracle, one describe block per assertion:
 *   1. Owner invites/demotes/revokes; a revoked member's UI drops the project
 *      on next read (mirrors server immediate-revocation semantics).
 *   2. The role matrix renders from the server's authority response, never
 *      inferred client-side; read-only sees no mutation affordances.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

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
    />,
  )
  return { onInvite, onChangeRole, onRevoke, ...rendered }
}

describe('acceptance #1: owner invites/demotes/revokes; revoked member drops the project on next read', () => {
  it('an owner invites a new member by email and role', async () => {
    const { onInvite } = setup()
    fireEvent.change(screen.getByLabelText(/invite by email/i), { target: { value: 'new@x.com' } })
    fireEvent.change(screen.getByLabelText(/invite role/i), { target: { value: 'editor' } })
    fireEvent.click(screen.getByRole('button', { name: /^invite$/i }))
    await waitFor(() => expect(onInvite).toHaveBeenCalledWith('new@x.com', 'editor'))
    // Fields clear once the invite lands — no stale draft left behind.
    await waitFor(() => expect(screen.getByLabelText(/invite by email/i).value).toBe(''))
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

    expect(screen.queryByLabelText(/invite by email/i)).toBeNull()
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

    expect(screen.queryByLabelText(/invite by email/i)).toBeNull()
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
