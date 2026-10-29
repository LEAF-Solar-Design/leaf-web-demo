import { useEffect, useRef, useState } from 'react'
import { createOrgInvitation, listOrgInvitations, revokeOrgInvitation } from './api.js'

function invitationError(error) {
  if (error.status === 409 && error.body?.detail === 'too_many_pending') {
    return 'This organization already has 20 pending invitations.'
  }
  return error.message || 'The invitation action could not be completed.'
}

function InvitationControls({ orgId, onForbidden }) {
  const [label, setLabel] = useState('')
  const [invitations, setInvitations] = useState([])
  const [loading, setLoading] = useState(true)
  const [created, setCreated] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const current = useRef(false)
  const pending = useRef(false)

  useEffect(() => {
    current.current = true
    let cancelled = false
    async function load() {
      try {
        const data = await listOrgInvitations(orgId)
        if (!cancelled) setInvitations(data.invitations || [])
      } catch (e) {
        if (!cancelled) {
          if (e.status === 403) onForbidden(true)
          else setError(invitationError(e))
        }
      }
      finally { if (!cancelled) setLoading(false) }
    }
    void load()
    return () => { cancelled = true; current.current = false }
  }, [orgId, onForbidden])

  async function run(action) {
    if (pending.current) return
    pending.current = true
    setBusy(true)
    setError(null)
    try { await action() }
    catch (e) { if (current.current) setError(invitationError(e)) }
    finally { pending.current = false; if (current.current) setBusy(false) }
  }

  const link = created ? `${window.location.origin}/app#invite=${created.token}` : null
  return <>
    <form onSubmit={(event) => {
      event.preventDefault()
      void run(async () => {
        setCreated(null)
        const result = await createOrgInvitation(orgId, label)
        if (!current.current) return
        setCreated(result)
        setLabel('')
        const data = await listOrgInvitations(orgId)
        if (current.current) setInvitations(data.invitations || [])
      })
    }}>
      <label>Invitation label (optional)<input maxLength={80} value={label} onChange={event => setLabel(event.target.value)} /></label>
      <button type="submit" disabled={busy}>Create invite link</button>
    </form>
    {link && <div>
      <label>Invite link<input readOnly value={link} /></label>
      <button type="button" disabled={busy} onClick={() => void run(() => navigator.clipboard.writeText(link))}>Copy</button>
      <p>Shown once. Anyone signed in with this link joins as an editor; it expires in 7 days.</p>
    </div>}
    {error && <p role="alert">{error}</p>}
    {loading && <p role="status" aria-label="Loading invitations">Loading invitations</p>}
    {!loading && !error && invitations.length === 0 && <p>No pending invitations.</p>}
    <ul aria-label="Pending invitations">
      {invitations.map(invitation => <li key={invitation.invitation_id}>
        <span>{invitation.label || 'Unlabeled invitation'}</span>{' '}
        <span>Created: {invitation.created_at}</span>{' '}
        <span>Expires: {invitation.expires_at}</span>{' '}
        <button type="button" disabled={busy} onClick={() => void run(async () => {
          await revokeOrgInvitation(orgId, invitation.invitation_id)
          if (!current.current) return
          setInvitations(rows => rows.filter(row => row.invitation_id !== invitation.invitation_id))
          if (created?.invitation_id === invitation.invitation_id) setCreated(null)
        })}>Revoke</button>
      </li>)}
    </ul>
  </>
}

export default function OrgInvitations({ orgId }) {
  const [open, setOpen] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  if (!orgId || forbidden) return null
  return <section aria-label="Invite to organization">
    <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>Invite to organization</button>
    {open && <InvitationControls key={orgId} orgId={orgId} onForbidden={setForbidden} />}
  </section>
}
