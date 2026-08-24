/**
 * useProjectLifecycle — ONE GET /api/projects/{id}/lifecycle read, shared by
 * every lifecycle component, refetched after every mutation.
 *
 * Each of Membership / ReceiptPanel / CloneDialog / ExportDialog / DangerZone
 * documents the same contract in its own header: it owns no fetch, and "the
 * parent re-fetches and passes fresh props after any mutation". This hook IS
 * that parent half. A mutation resolves, then `refetch()` runs before the
 * action's promise settles, so a component that re-renders on success is
 * already looking at post-mutation server truth — never at optimistic state
 * this hook invented.
 *
 * ADAPTERS, and why they exist: the snapshot's wire shape and the components'
 * prop shapes were built against each other but not identically.
 *   - project_snapshot() ships NO `authority` object. Membership requires one
 *     and refuses to guess a role matrix. The viewer's role is therefore read
 *     verbatim off the server's own member row for this actor binding; only the
 *     can_invite/can_manage booleans are derived here, and they mirror the
 *     server's WRITE_ROLES exactly (project_lifecycle.py:26). The server still
 *     enforces the real gate — these booleans only decide what to render.
 *   - roles cross the wire as `read_only` and render as `read-only`. Both
 *     directions are translated here so neither side has to learn the other's
 *     spelling.
 *   - receipts arrive as {receipt_id, action, input_digest, created_at};
 *     ReceiptPanel reads {kind|action, time, fields}. Mapped, never invented.
 *   - members arrive keyed by `membership_id` with a `binding_id`;
 *     Membership keys rows by `member_id`. member_id === membership_id here,
 *     which is exactly what the revoke route wants back.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { humanizeError } from '../errorHumanize.js'
import {
  cloneProject,
  deleteProject,
  exportProject,
  getProjectLifecycle,
  getStoredActorBindingId,
  inviteMember,
  resetProject,
  revokeMember,
} from './api.js'

export function toUiRole(role) {
  return role === 'read_only' ? 'read-only' : role
}

export function toApiRole(role) {
  return role === 'read-only' ? 'read_only' : role
}

// Mirrors platform/project_lifecycle.py WRITE_ROLES. Rendering only — the
// server re-checks the same set on every mutation and is the real authority.
const UI_WRITE_ROLES = new Set(['owner', 'editor'])

function adaptMembers(members) {
  return (members || []).map((m) => ({
    member_id: m.membership_id,
    binding_id: m.binding_id,
    name: m.binding_id, // the snapshot carries no display name or email
    role: toUiRole(m.role),
  }))
}

function adaptReceipts(receipts) {
  return (receipts || []).map((r) => ({
    receipt_id: r.receipt_id,
    kind: r.action,
    time: r.created_at,
    fields: r.input_digest ? { input_digest: r.input_digest } : {},
  }))
}

// The viewer's own row IS the authority read: absent means revoked, and
// Membership drops the project on exactly that absence.
function adaptAuthority(members, viewerId) {
  if (!viewerId) return null
  const own = (members || []).find((m) => m.member_id === viewerId)
  const role = own?.role || null
  const canWrite = UI_WRITE_ROLES.has(role)
  return {
    role: role || 'unknown',
    can_invite: canWrite,
    can_manage: role === 'owner',
  }
}

const EMPTY = { project: null, members: [], receipts: [], authority: null, viewerId: null }

/**
 * @param {string|null} projectId   the open project, or null for "no project"
 * @param {{enabled?: boolean}} options  `enabled` false keeps this hook inert
 *        (no fetch, no state churn) so a caller can mount it behind a gate.
 */
export default function useProjectLifecycle(projectId, { enabled = true } = {}) {
  const [status, setStatus] = useState('idle') // idle | loading | ready | error
  const [data, setData] = useState(EMPTY)
  const [error, setError] = useState(null)

  // Bumped on every load and on unmount: a stale response must never overwrite
  // a newer one's state, and a resolved fetch after unmount must not set state.
  const generationRef = useRef(0)
  useEffect(() => () => { generationRef.current += 1 }, [])

  const load = useCallback(async () => {
    if (!enabled || !projectId) {
      generationRef.current += 1
      setData(EMPTY)
      setError(null)
      setStatus('idle')
      return null
    }
    const generation = ++generationRef.current
    setStatus('loading')
    setError(null)
    try {
      const snapshot = await getProjectLifecycle(projectId)
      if (generationRef.current !== generation) return null
      const members = adaptMembers(snapshot.members)
      const bindingId = getStoredActorBindingId()
      const viewerId = bindingId
        ? members.find((m) => m.binding_id === bindingId)?.member_id || null
        : null
      setData({
        project: snapshot.project || null,
        members,
        receipts: adaptReceipts(snapshot.receipts),
        authority: adaptAuthority(members, viewerId),
        viewerId,
      })
      setStatus('ready')
      return snapshot
    } catch (e) {
      if (generationRef.current !== generation) return null
      setError(humanizeError(e))
      setStatus('error')
      return null
    }
  }, [enabled, projectId])

  useEffect(() => { load() }, [load])

  // Every mutation refetches BEFORE resolving, so a caller that renders on
  // success renders post-mutation server truth. A failed mutation rethrows
  // untouched — the component that owns the affordance surfaces it.
  const runThenRefetch = useCallback(async (operation) => {
    const result = await operation()
    await load()
    return result
  }, [load])

  const bindingFor = useCallback(
    (memberId) => data.members.find((m) => m.member_id === memberId)?.binding_id || null,
    [data.members],
  )

  const actions = useMemo(() => ({
    // Membership hands back whatever the invite field held. The server takes a
    // binding id (there is no binding-by-email lookup route), so a non-UUID is
    // rejected here with a message that says so instead of a blind 422.
    invite: (identifier, uiRole) =>
      runThenRefetch(() => inviteMember(projectId, identifier, toApiRole(uiRole))),
    // A role change is an invite of the SAME binding with a new role
    // (platform/project_lifecycle.py:496-503 updates in place).
    changeRole: (memberId, uiRole) =>
      runThenRefetch(() => inviteMember(projectId, bindingFor(memberId), toApiRole(uiRole))),
    revoke: (memberId) => runThenRefetch(() => revokeMember(projectId, memberId)),
    clone: (name) => runThenRefetch(() => cloneProject(projectId, name)),
    export: (options) => runThenRefetch(() => exportProject(projectId, options)),
    reset: () => runThenRefetch(() => resetProject(projectId)),
    remove: () => deleteProject(projectId), // no refetch: the project is gone
  }), [bindingFor, projectId, runThenRefetch])

  return { status, error, refetch: load, actions, ...data }
}
