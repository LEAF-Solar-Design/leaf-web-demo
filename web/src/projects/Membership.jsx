/**
 * Membership — the project's owner/editor/reviewer/read-only roster.
 *
 * Card B-U2 acceptance:
 *   1. Owner invites/demotes/revokes; a revoked member's UI drops the project
 *      on next read (mirrors the server's immediate-revocation semantics).
 *   2. The role matrix renders from the server's authority response, never
 *      inferred client-side; a read-only viewer sees no mutation affordances.
 *
 * Purely props-driven (ApprovalCard.jsx / EntitlementGate.jsx pattern): this
 * component owns no fetch, no polling, no client-side role math. `authority`
 * and `members` are the server's answer to "what can this viewer do, and who
 * is actually on the project right now" — every gate below reads a flag off
 * `authority`, never compares role strings itself. The parent re-fetches and
 * passes fresh `members`/`authority` after any mutation; that fresh read is
 * what "next read" means here — if the viewer's own record is gone from it,
 * this component drops the whole project instead of rendering stale rows.
 */
import { useRef, useState } from 'react'

export const ROLES = ['owner', 'editor', 'reviewer', 'read-only']

export function memberLabel(member) {
  return [member.label, member.display_name, member.name, member.email]
    .find((value) => typeof value === 'string' && value.trim())?.trim()
    || `Member ${String(member.binding_id || member.member_id || '').slice(0, 8)}`
}

function identityText(identity) {
  const label = memberLabel(identity)
  return identity.role && identity.created_at
    ? `${label} · ${identity.role} · joined ${identity.created_at.slice(0, 10)}`
    : label
}

function errorMessage(e, fallback) {
  return e?.body?.detail || e?.message || fallback
}

export default function Membership({
  viewerId,
  authority, // server truth: { role, can_invite, can_manage } — never computed here
  members,   // server truth: [{ member_id, name, email, role }]
  onInvite,      // async (identifier, role) => void
  onChangeRole,  // async (memberId, role) => void
  onRevoke,      // async (memberId) => void
  identities, // existing bindings in this organization only
}) {
  const [inviteBinding, setInviteBinding] = useState('')
  const [search, setSearch] = useState('')
  const [inviteRole, setInviteRole] = useState('read-only')
  const [inviting, setInviting] = useState(false)
  const invitingRef = useRef(false)
  const [pendingRoleIds, setPendingRoleIds] = useState(() => new Set())
  const [pendingRevokeIds, setPendingRevokeIds] = useState(() => new Set())
  const [error, setError] = useState(null)

  if (!authority) return null // no server authority read yet — render nothing, never a guessed matrix

  const roster = members || []
  const viewerRecord = viewerId ? roster.find((m) => m.member_id === viewerId) : undefined

  // The server's next read no longer carries this viewer — their access was
  // revoked. Drop the project entirely; do not render any stale roster row.
  if (viewerId && !viewerRecord) {
    return (
      <section className="membership-panel" aria-label="Membership">
        <p className="membership-revoked" role="status">
          You no longer have access to this project.
        </p>
      </section>
    )
  }

  const canInvite = authority.can_invite === true
  const canManage = authority.can_manage === true
  const choices = identities || []
  const filteredChoices = choices.filter((identity) =>
    identityText(identity).toLowerCase().includes(search.trim().toLowerCase()))
  const selected = filteredChoices.find((identity) => identity.binding_id === inviteBinding)

  const withPending = (setFn, id, fn) => async () => {
    setFn((prev) => new Set(prev).add(id))
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(errorMessage(e, 'That action did not go through. Nothing changed.'))
    } finally {
      setFn((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }

  const submitInvite = async (event) => {
    event.preventDefault()
    if (!selected || invitingRef.current) return
    invitingRef.current = true
    setInviting(true)
    setError(null)
    try {
      await onInvite(selected.binding_id, inviteRole)
      setInviteBinding('')
      setSearch('')
      setInviteRole('read-only')
    } catch (e) {
      setError(errorMessage(e, 'The invite did not go through. Nothing changed.'))
    } finally {
      invitingRef.current = false
      setInviting(false)
    }
  }

  const changeRole = (memberId, role) =>
    withPending(setPendingRoleIds, memberId, () => onChangeRole(memberId, role))()

  const revoke = (memberId) =>
    withPending(setPendingRevokeIds, memberId, () => onRevoke(memberId))()

  return (
    <section className="membership-panel" aria-label="Membership">
      <div className="membership-head">
        <span className="membership-title">Members</span>
        <span className="membership-role">your role: {authority.role || 'unknown'}</span>
      </div>

      {canInvite && choices.length === 0 && (
        <p role="status">{identities == null
          ? 'Organization members are unavailable. No one can be invited yet.'
          : 'No organization members are available to invite.'}</p>
      )}
      {canInvite && choices.length > 0 && (
        <form className="membership-invite" onSubmit={submitInvite}>
          <label>
            Search organization members
            <input
              type="search"
              value={search}
              onChange={(event) => { setSearch(event.target.value); setInviteBinding('') }}
              disabled={inviting}
            />
          </label>
          <label>
            Invite member
            <select
              value={selected ? inviteBinding : ''}
              onChange={(event) => setInviteBinding(event.target.value)}
              disabled={inviting || filteredChoices.length === 0}
              required
            >
              <option value="">Choose an organization member</option>
              {filteredChoices.map((identity) => (
                <option key={identity.binding_id} value={identity.binding_id}>{identityText(identity)}</option>
              ))}
            </select>
          </label>
          {filteredChoices.length === 0 && <p role="status">No organization members match your search.</p>}
          <label>
            Role
            <select
              aria-label="Invite role"
              value={inviteRole}
              onChange={(event) => setInviteRole(event.target.value)}
              disabled={inviting}
            >
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <button type="submit" className="chip-act" disabled={inviting || !selected}>
            {inviting ? 'Inviting…' : 'Invite'}
          </button>
        </form>
      )}

      <ul className="membership-roster">
        {roster.map((member) => {
          const id = member.member_id
          const label = memberLabel(member)
          const roleBusy = pendingRoleIds.has(id)
          const revokeBusy = pendingRevokeIds.has(id)
          return (
            <li key={id} className="membership-row">
              <span className="membership-member">{label}{member.created_at && ` · joined ${member.created_at.slice(0, 10)}`}</span>
              {id === viewerId && <span className="membership-self"> (you)</span>}
              {canManage ? (
                <>
                  <select
                    aria-label={`Role for ${label}`}
                    value={member.role}
                    disabled={roleBusy}
                    onChange={(event) => changeRole(id, event.target.value)}
                  >
                    {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                  <button
                    type="button"
                    className="chip-act membership-revoke"
                    disabled={revokeBusy}
                    onClick={() => revoke(id)}
                  >
                    {revokeBusy ? 'Revoking…' : `Revoke ${label}`}
                  </button>
                </>
              ) : (
                <span className="membership-role-static">{member.role}</span>
              )}
            </li>
          )
        })}
      </ul>

      {error && <p className="membership-error" role="alert">{error}</p>}
    </section>
  )
}
