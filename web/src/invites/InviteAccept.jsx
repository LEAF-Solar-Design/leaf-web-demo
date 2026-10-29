import { useLayoutEffect, useRef, useState } from 'react'
import { authConfigured, isSignedIn, loginWithPopupFromClick, subscribeTokenStored } from '../auth.js'
import { acceptOrgInvitation } from '../projects/api.js'

export default function InviteAccept({ signedIn, onAccepted }) {
  const [invite, setInvite] = useState(() => {
    const hash = window.location.hash
    if (!hash.startsWith('#invite=')) return null
    const token = hash.slice('#invite='.length)
    return /^[A-Za-z0-9_-]{43}$/.test(token) ? { token } : { invalid: true }
  })
  const [authenticated, setAuthenticated] = useState(() => isSignedIn())
  const [message, setMessage] = useState(null)
  const [busy, setBusy] = useState(false)
  const [signInError, setSignInError] = useState(null)
  const pending = useRef(false)
  const active = useRef(false)

  useLayoutEffect(() => {
    if (window.location.hash.startsWith('#invite=')) {
      window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}`)
    }
    active.current = true
    const unsubscribe = subscribeTokenStored(() => setAuthenticated(isSignedIn()))
    return () => { active.current = false; unsubscribe() }
  }, [])

  if (!invite) return null
  if (invite.invalid) return <p role="alert">This invite link is not valid.</p>
  if (message) return <p role="status">{message}</p>
  if (!(signedIn ?? authenticated)) return <section aria-label="Accept organization invitation">
    <p>Sign in first to accept this invitation. Keep this page open, then sign in.</p>
    {authConfigured && <button type="button" disabled={busy} onClick={async () => {
      setBusy(true)
      setSignInError(null)
      try {
        const result = await loginWithPopupFromClick()
        if (active.current && !result.ok) setSignInError('Sign-in did not complete. Please try again.')
      } catch { if (active.current) setSignInError('Sign-in did not complete. Please try again.') }
      finally { if (active.current) setBusy(false) }
    }}>Sign in</button>}
    {signInError && <p role="alert">{signInError}</p>}
  </section>

  async function accept() {
    if (pending.current) return
    pending.current = true
    setBusy(true)
    try {
      const result = await acceptOrgInvitation(invite.token)
      if (!active.current) return
      setInvite({})
      setMessage('You joined the organization.')
      try { await onAccepted?.(result) }
      catch {
        if (active.current) setMessage('You joined the organization. Reload this page to refresh your workspace.')
      }
    } catch (error) {
      if (!active.current) return
      setMessage(error.status === 404 ? 'This invite has expired or was already used.'
        : error.status === 409 && error.body?.detail === 'already_member' ? 'You already belong to an organization.'
          : error.message || 'The invitation could not be accepted.')
      setInvite({})
    } finally { pending.current = false; if (active.current) setBusy(false) }
  }

  return <section aria-label="Accept organization invitation">
    <p>Join this organization as an editor?</p>
    <button type="button" disabled={busy} onClick={() => void accept()}>Accept</button>
    <button type="button" disabled={busy} onClick={() => setInvite(null)}>Cancel</button>
  </section>
}
