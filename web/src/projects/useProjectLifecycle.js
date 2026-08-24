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
// Pass-through of the server's own authority read, translated to UI role
// vocabulary. Nothing is computed from role strings here: Membership.jsx
// refuses to render a guessed matrix, and guessing is what a client-side
// derivation would be.
function adaptAuthority(viewer) {
  if (!viewer || !viewer.role) return null
  return {
    role: toUiRole(viewer.role),
    can_invite: viewer.can_invite === true,
    can_manage: viewer.can_manage === true,
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
  const [refreshing, setRefreshing] = useState(false) // post-mutation revalidation
  const [data, setData] = useState(EMPTY)
  const [error, setError] = useState(null)

  // Bumped on every load and on unmount: a stale response must never overwrite
  // a newer one's state, and a resolved fetch after unmount must not set state.
  const generationRef = useRef(0)
  useEffect(() => () => { generationRef.current += 1 }, [])

  // A REFETCH IS NOT A COLD LOAD. Mutations refetch on success, and dropping
  // status to 'loading' unmounted the very dialog that had just been handed its
  // receipt (clone/export/reset all lost their result and re-offered the
  // action). A refresh keeps the last good snapshot on screen and only reports
  // that it is revalidating.
  const load = useCallback(async ({ refresh = false } = {}) => {
    if (!enabled || !projectId) {
      generationRef.current += 1
      setData(EMPTY)
      setError(null)
      setStatus('idle')
      return null
    }
    const generation = ++generationRef.current
    if (refresh) setRefreshing(true)
    else setStatus('loading')
    setError(null)
    try {
      const snapshot = await getProjectLifecycle(projectId)
      if (generationRef.current !== generation) return null
      const members = adaptMembers(snapshot.members)
      // Viewer identity is SERVER truth (platform/project_lifecycle.py
      // project_snapshot emits `viewer`). The browser never learns its own
      // actor binding id any other way, so there is nothing to guess from.
      const viewer = snapshot.viewer || null
      const viewerId = viewer
        ? viewer.membership_id
          || members.find((m) => m.binding_id === viewer.binding_id)?.member_id
          || null
        : null
      setData({
        project: snapshot.project || null,
        members,
        receipts: adaptReceipts(snapshot.receipts),
        authority: adaptAuthority(viewer),
        viewerId,
      })
      setStatus('ready')
      return snapshot
    } catch (e) {
      if (generationRef.current !== generation) return null
      setError(humanizeError(e))
      // A failed REFRESH keeps the last good snapshot readable; only a failed
      // cold load has nothing to show.
      if (!refresh) setStatus('error')
      return null
    } finally {
      if (generationRef.current === generation && refresh) setRefreshing(false)
    }
  }, [enabled, projectId])

  useEffect(() => { load() }, [load])

  // Every mutation refetches BEFORE resolving, so a caller that renders on
  // success renders post-mutation server truth. A failed mutation rethrows
  // untouched — the component that owns the affordance surfaces it.
  const runThenRefetch = useCallback(async (operation) => {
    const result = await operation()
    await load({ refresh: true })
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

  return { status, refreshing, error, refetch: load, actions, ...data }
}
