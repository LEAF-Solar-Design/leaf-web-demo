import { useSyncExternalStore } from 'react'
import { serviceState } from '../lib/serviceAuthSession.js'

export const ACCOUNT_EMAIL_KEY = 'leaf.account_email'

const listeners = new Set()
let snapshot = { email: null, workspace: null }

function validEmail(value) {
  if (typeof value !== 'string' || /[\u0000-\u001f\u007f]/.test(value)) return null
  const email = value.trim()
  return email.length >= 3 && email.length <= 254 && email.includes('@') ? email : null
}

// Display comparison only; the server remains the authentication authority.
function readSubject() {
  try {
    const part = localStorage.getItem('leaf.jwt')?.split('.')[1]
    if (!part) return null
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/')
    const claims = JSON.parse(globalThis.atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, '=')))
    return typeof claims?.sub === 'string' && claims.sub.trim() ? claims.sub : null
  } catch { return null }
}

function readEmail() {
  try {
    if (serviceState().mode) return null
    const sub = readSubject()
    if (!sub) return null
    const stored = JSON.parse(localStorage.getItem(ACCOUNT_EMAIL_KEY))
    return stored?.sub === sub ? validEmail(stored.email) : null
  } catch { return null }
}

function notify() {
  for (const listener of listeners) listener()
}

export function readAccountIdentity() {
  const email = readEmail()
  if (email !== snapshot.email) snapshot = { ...snapshot, email }
  return snapshot
}

export function setAccountEmail(value) {
  const email = validEmail(value)
  const sub = readSubject()
  try {
    if (email && sub) localStorage.setItem(ACCOUNT_EMAIL_KEY, JSON.stringify({ sub, email }))
    else localStorage.removeItem(ACCOUNT_EMAIL_KEY)
  } catch { /* display only; storage may be unavailable */ }
  readAccountIdentity()
  notify()
}

export function setAccountWorkspace(value) {
  let workspace = null
  if (value && typeof value === 'object' && typeof value.id === 'string'
    && value.id.trim() && value.id.length <= 64) {
    workspace = {
      id: value.id,
      name: typeof value.name === 'string' ? value.name.trim().slice(0, 120) || null : null,
    }
  }
  if (workspace?.id !== snapshot.workspace?.id || workspace?.name !== snapshot.workspace?.name) {
    snapshot = { ...snapshot, workspace }
  }
  notify()
}

export function clearAccountIdentity() {
  try { localStorage.removeItem(ACCOUNT_EMAIL_KEY) } catch { /* display only */ }
  if (snapshot.email !== null || snapshot.workspace !== null) {
    snapshot = { email: null, workspace: null }
  }
  notify()
}

export function subscribeAccountIdentity(listener) {
  listeners.add(listener)
  const onStorage = (event) => {
    if (event.key === ACCOUNT_EMAIL_KEY || event.key === 'leaf.jwt' || event.key === null) {
      readAccountIdentity()
      listener()
    }
  }
  if (typeof window !== 'undefined') window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    if (typeof window !== 'undefined') window.removeEventListener('storage', onStorage)
  }
}

export function formatAccountLine({ email, workspace }) {
  const workspaceLine = workspace
    ? workspace.name ? `${workspace.name} (${workspace.id.slice(0, 8)})` : `workspace ${workspace.id.slice(0, 8)}`
    : null
  return email && workspaceLine ? `${email} · ${workspaceLine}` : email || workspaceLine || null
}

export function useAccountIdentity() {
  return useSyncExternalStore(subscribeAccountIdentity, readAccountIdentity, readAccountIdentity)
}
