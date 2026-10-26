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
 * component owns no fetch or polling. `authority`
 * and `members` are the server's answer to "what can this viewer do, and who
 * is actually on the project right now". Authority flags gate actions, with
 * the server's owner-only restrictions applied to protected roles. The parent re-fetches and
 * passes fresh `members`/`authority` after any mutation; that fresh read is
 * what "next read" means here — if the viewer's own record is gone from it,
 * this component drops the whole project instead of rendering stale rows.
 */
import { useRef, useState } from 'react'
import { humanizeError } from '../errorHumanize.js'

export const ROLES = ['owner', 'editor', 'reviewer', 'read-only']

export function memberLabel(member) {
  return [member.label, member.display_name, member.name, member.email]
    .find((value) => typeof value === 'string' && value.trim())?.trim()
    || 'Unnamed member'
}

function identityText(identity) {
  const label = memberLabel(identity)
  return identity.role && identity.created_at
    ? `${label} · ${identity.role} · joined ${identity.created_at.slice(0, 10)}`
    : label
}

function errorMessage(e, fallback) {
  return humanizeError(typeof e?.body?.detail === 'string' ? e.body.detail : e || fallback)
}

export default function Membership({
  viewerId,
  authority, // server truth: { role, can_invite, can_manage, can_label }, never computed here
  members,   // server truth: [{ member_id, name, email, role }]
  onInvite,      // async (identifier, role) => void
  onChangeRole,  // async (memberId, role) => void
  onRevoke,      // async (memberId) => void
  onSetLabel,    // async (memberId, displayName) => void
  identities, // existing bindings in this organization only
}) {
  const [inviteBinding, setInviteBinding] = useState('')
  const [search, setSearch] = useState('')
  const [inviteRole, setInviteRole] = useState('read-only')
  const [inviting, setInviting] = useState(false)
  const invitingRef = useRef(false)
  const [pendingRoleIds, setPendingRoleIds] = useState(() => new Set())
  const [pendingRevokeIds, setPendingRevokeIds] = useState(() => new Set())
  const [labelDrafts, setLabelDrafts] = useState({})
  const [pendingLabelIds, setPendingLabelIds] = useState(() => new Set())
  const pendingLabelsRef = useRef(new Set())
  const [error, setError] = useState(null)
  const panelRef = useRef(null)

  if (!authority) return null // no server authority read yet — render nothing, never a guessed matrix

  const roster = members || []
  const viewerRecord = viewerId ? roster.find((m) => m.member_id === viewerId) : undefined

  // The server's next read no longer carries this viewer — their access was
  // revoked. Drop the project entirely; do not render any stale roster row.
  if (viewerId && !viewerRecord) {
    return (
      <section ref={panelRef} tabIndex={-1} className="membership-panel" aria-label="Membership">
        <p className="membership-revoked" role="status">
          You no longer have access to this project.
        </p>
      </section>
    )
  }

  const canInvite = authority.can_invite === true
  const canManage = authority.can_manage === true
  const canLabel = authority.can_label === true && typeof onSetLabel === 'function'
  const isOwner = authority.role === 'owner'
  const allowedRoles = isOwner ? ROLES : ROLES.filter((role) => !['owner', 'editor'].includes(role))
  const selectedInviteRole = allowedRoles.includes(inviteRole) ? inviteRole : 'read-only'
  const canManageMember = (member) => canManage && (isOwner || !['owner', 'editor'].includes(member.role))
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
    if (!canInvite || !selected || invitingRef.current) return
    invitingRef.current = true
    setInviting(true)
    setError(null)
    try {
      await onInvite(selected.binding_id, selectedInviteRole)
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

  const saveLabel = async (member) => {
    const id = member.member_id
    if (pendingLabelsRef.current.has(id) || !Object.hasOwn(labelDrafts, id)) return
    const trimmed = labelDrafts[id].trim()
    if (trimmed === memberLabel(member)) return
    setError(null)
    if (Array.from(trimmed).length > 100) {
      setError('A display name can be at most 100 characters.')
      return
    }
    pendingLabelsRef.current.add(id)
    setPendingLabelIds((prev) => new Set(prev).add(id))
    try {
      await onSetLabel(id, trimmed)
      setLabelDrafts((prev) => {
        const next = { ...prev }
        delete next[id]
        return next
      })
    } catch (e) {
      setError(errorMessage(e, 'The name was not saved. Nothing changed.'))
    } finally {
      pendingLabelsRef.current.delete(id)
      setPendingLabelIds((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }

  const changeRole = (member, role) => {
    if (!canManageMember(member) || !allowedRoles.includes(role)) return
    return withPending(setPendingRoleIds, member.member_id, () => onChangeRole(member.member_id, role))()
  }

  const revoke = (member) => {
    if (!canManageMember(member) || pendingRevokeIds.has(member.member_id)) return
    const question = member.member_id === viewerId
      ? 'Remove your own access? You will no longer be able to open this project.'
      : `Remove ${memberLabel(member)} from this project? They will lose access.`
    if (!window.confirm(question)) return
    return withPending(setPendingRevokeIds, member.member_id, async () => {
      await onRevoke(member.member_id)
      panelRef.current?.focus()
    })()
  }

  return (
    <section ref={panelRef} tabIndex={-1} className="membership-panel" aria-label="Membership">
      <div className="membership-head">
        <span className="membership-title">Members</span>
        <span className="membership-role">your role: {authority.role || 'unknown'}</span>
      </div>

      {canInvite && choices.length === 0 && (
        <p role="status">{identities == null
          ? 'Load organization members to choose someone to invite.'
          : 'No organization members are available to invite.'}</p>
      )}
      {canInvite && choices.length > 0 && (
        <form className="membership-invite" onSubmit={submitInvite}>
          <label>
            Search organization members
            {/* Autocomplete is off because this search concerns another member's data. */}
            <input
              type="search"
              autoComplete="off"
              enterKeyHint="search"
              value={search}
              onChange={(event) => { setSearch(event.target.value); setInviteBinding('') }}
              disabled={inviting}
              title={inviting ? 'Wait for the invite to finish.' : undefined}
            />
          </label>
          <label>
            Invite member
            <select
              value={selected ? inviteBinding : ''}
              onChange={(event) => setInviteBinding(event.target.value)}
              disabled={inviting || filteredChoices.length === 0}
              title={inviting ? 'Wait for the invite to finish.' : filteredChoices.length === 0 ? 'No members match your search.' : undefined}
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
              value={selectedInviteRole}
              onChange={(event) => setInviteRole(event.target.value)}
              disabled={inviting}
              title={inviting ? 'Wait for the invite to finish.' : undefined}
            >
              {allowedRoles.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          {!isOwner && <p>Only an owner can invite owners or editors.</p>}
          <button type="submit" className="chip-act" disabled={inviting || !selected}
            title={inviting ? 'Wait for the invite to finish.' : !selected ? 'Choose an organization member first.' : undefined}>
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
          const labelChanged = Object.hasOwn(labelDrafts, id) && labelDrafts[id].trim() !== label
          return (
            <li key={id} className="membership-row">
              <span className="membership-member">{label}{member.created_at && ` · joined ${member.created_at.slice(0, 10)}`}</span>
              {id === viewerId && <span className="membership-self"> (you)</span>}
              {canLabel && (
                <>
                  {/* Autocomplete is off because a display name may be another member's data. */}
                  <input
                    aria-label={`Display name for ${label}`}
                    autoComplete="off"
                    enterKeyHint="done"
                    value={labelDrafts[id] ?? label}
                    onChange={(event) => setLabelDrafts((prev) => ({ ...prev, [id]: event.target.value }))}
                    disabled={pendingLabelIds.has(id)}
                    title={pendingLabelIds.has(id) ? 'Wait for the display name to finish saving.' : undefined}
                  />
                  <button
                    type="button"
                    className="chip-act"
                    aria-label={`Save display name for ${label}`}
                    disabled={pendingLabelIds.has(id) || !labelChanged}
                    title={pendingLabelIds.has(id) ? 'Wait for the display name to finish saving.' : !labelChanged ? 'Edit the display name before saving.' : undefined}
                    onClick={() => saveLabel(member)}
                  >
                    {pendingLabelIds.has(id) ? 'Saving…' : 'Save display name'}
                  </button>
                </>
              )}
              {canManage ? (
                <>
                  <select
                    aria-label={`Role for ${label}`}
                    value={member.role}
                    disabled={roleBusy || !canManageMember(member)}
                    title={!canManageMember(member) ? 'Only an owner can change an owner or editor.' : roleBusy ? 'Wait for the role change to finish.' : undefined}
                    onChange={(event) => changeRole(member, event.target.value)}
                  >
                    {ROLES.filter((r) => allowedRoles.includes(r) || r === member.role).map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                  <button
                    type="button"
                    className="chip-act membership-revoke"
                    disabled={revokeBusy || !canManageMember(member)}
                    title={!canManageMember(member) ? 'Only an owner can remove an owner or editor.' : revokeBusy ? 'Wait for access removal to finish.' : undefined}
                    onClick={() => revoke(member)}
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
