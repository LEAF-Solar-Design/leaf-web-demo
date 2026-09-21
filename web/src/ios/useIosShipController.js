import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  emptyIosShipReadiness, fetchIosShipReadiness, getIosShipExecution,
  getIosShipReceipt, iosShipLaunchAffordance, makeIosShipLaunchKey,
  requestIosShipLaunch, fetchIosShipSources, requestIosShipApproval, iosSourceApprovalState,
} from '../site/iosShipReadiness.js'

const terminal = (execution) => ['succeeded', 'failed'].includes(execution?.status)
const message = (cause) => cause?.envelope?.message || cause?.message || 'The iOS ship status is unavailable.'
const FOLLOW_LIMIT = 30 * 60 * 1000
const SOURCE_DEFAULTS = Object.freeze({ sources: [], approvals: [], sync: null, canApprove: false, sourcesError: null })
const UNKNOWN_APPROVAL = "The last approval's outcome is unknown. Refresh the sources to confirm it before approving again."

// Catalog reads belong to the project, while approval writes belong to a drawing version.
function useShipSources({ projectId, tenantKey, enabled, sessionActive }) {
  const pendingApproval = useMemo(() => ({ current: null }), [projectId, tenantKey])
  const scope = useMemo(() => ({}), [projectId, tenantKey, enabled, sessionActive])
  const current = useRef(scope)
  current.current = scope
  const mounted = useRef(false)
  const sourcesGeneration = useRef(0)
  const [state, setState] = useState(() => ({ scope, ...SOURCE_DEFAULTS }))
  const live = useCallback(() => mounted.current && current.current === scope, [scope])
  const update = useCallback((patch) => {
    if (live()) setState((old) => live() ? { ...(old.scope === scope ? old : SOURCE_DEFAULTS), scope, ...patch } : old)
  }, [live, scope])
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const refreshSources = useCallback(async () => {
    if (!live()) return null
    const generation = ++sourcesGeneration.current
    if (!enabled || !sessionActive || !projectId) {
      update(SOURCE_DEFAULTS)
      return null
    }
    try {
      const next = await fetchIosShipSources({ projectId })
      if (!live() || generation !== sourcesGeneration.current) return null
      pendingApproval.current = null
      update({ ...next, sourcesError: null })
      return next
    } catch (cause) {
      if (!live() || generation !== sourcesGeneration.current) return null
      update({ sourcesError: message(cause) })
      return null
    }
  }, [enabled, sessionActive, projectId, live, update])
  useEffect(() => { refreshSources() }, [refreshSources])
  return { ...(state.scope === scope ? state : SOURCE_DEFAULTS), refreshSources, update, pendingApproval }
}

export function shipSetupState(readiness) {
  const reason = readiness?.reason
  if (['no_approved_project_revision', 'revision_mismatch'].includes(reason)) return 'no-approved-revision'
  if (['executor_busy', 'mini_busy'].includes(reason)) return 'executor-busy'
  if (['provider_unavailable', 'dispatch_unavailable', 'app_color_unavailable'].includes(reason)) return 'executor-unavailable'
  const grant = readiness?.grantStatus ?? readiness?.grant_status
  if ((grant && grant !== 'healthy') || /grant/i.test(readiness?.setupAction || readiness?.setup_action || '')) return 'grant-not-ready'
  return 'none'
}

const decorate = (readiness) => Object.freeze({ ...readiness, setupState: shipSetupState(readiness) })
function storage(key, action, value) {
  if (!key) return null
  try {
    if (action === 'read') return JSON.parse(sessionStorage.getItem(key))
    if (action === 'write') sessionStorage.setItem(key, JSON.stringify(value))
    if (action === 'remove') sessionStorage.removeItem(key)
  } catch { /* Storage can be unavailable in private mode. */ }
  return null
}

export function useIosShipController({ projectId, revision, sessionActive, enabled = true, tenantKey }) {
  const catalog = useShipSources({ projectId, tenantKey, enabled, sessionActive })
  const { refreshSources, update: updateSources, pendingApproval } = catalog
  const owner = JSON.stringify([tenantKey, projectId, revision])
  const ownership = useMemo(() => ({}), [owner])
  const currentOwnership = useRef(ownership)
  currentOwnership.current = ownership
  const scope = useMemo(() => ({}), [owner, revision, sessionActive, enabled])
  const current = useRef(scope)
  current.current = scope
  const mounted = useRef(false)
  const flight = useRef(null)
  const approveFlight = useRef(null)
  const receiptReads = useRef(new Map())
  const following = useRef(null)
  const executionGeneration = useRef(0)
  const readGeneration = useRef(0)
  const [follow, setFollow] = useState(0)
  const [state, setState] = useState(() => ({ owner, ...SOURCE_DEFAULTS, approving: false, readiness: decorate(emptyIosShipReadiness()), execution: null, receipt: null, error: null, loading: false, launching: false }))
  const visible = state.owner === owner ? state : { ...SOURCE_DEFAULTS, approving: false, readiness: decorate(emptyIosShipReadiness()), execution: null, receipt: null, error: null, loading: false, launching: false }
  const pointerKey = tenantKey && projectId ? `leaf.ios-ship.pointer:${tenantKey}:${projectId}` : null
  const live = useCallback(() => mounted.current && current.current === scope, [scope])
  const update = useCallback((patch) => {
    if (live()) {
      if (Object.hasOwn(patch, 'sourcesError')) updateSources({ sourcesError: patch.sourcesError })
      setState((old) => live() ? ({ ...(old.owner === owner ? old : { ...SOURCE_DEFAULTS, approving: false, execution: null, receipt: null }), owner, ...patch }) : old)
    }
  }, [live, owner, updateSources])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const refresh = useCallback(async () => {
    if (!live()) return
    const generation = ++readGeneration.current
    if (!following.current) setFollow((n) => n + 1)
    if (!enabled || !sessionActive || !projectId || !revision) {
      update({ readiness: decorate(emptyIosShipReadiness('no_approved_project_revision', null, projectId)), loading: false, launching: false, error: null })
      return
    }
    update({ loading: true, error: null })
    try {
      const next = await fetchIosShipReadiness({ projectId, revision })
      if (generation !== readGeneration.current) return
      update({ readiness: decorate(next), loading: false, error: next.launchable ? null : next.setupAction || next.reason })
    } catch (cause) {
      if (generation !== readGeneration.current) return
      update({ readiness: decorate(emptyIosShipReadiness('unreachable', null, projectId)), loading: false, error: message(cause) })
    }
  }, [enabled, sessionActive, projectId, revision, live, update])

  useEffect(() => {
    update({ readiness: decorate(emptyIosShipReadiness('loading', null, projectId)), loading: false, launching: false, error: null })
    refresh()
  }, [refresh, update, projectId])

  useEffect(() => {
    if (!enabled || !sessionActive || !projectId) return undefined
    let cancelled = false
    const generation = executionGeneration.current
    const pointer = storage(pointerKey, 'read')
    if (typeof pointer?.execution_id !== 'string' || !pointer.execution_id || pointer.revision !== revision) return undefined
    getIosShipExecution({ projectId, executionId: pointer.execution_id }).then((response) => {
      if (!cancelled && live() && executionGeneration.current === generation) update({ execution: response.execution })
    }).catch((cause) => {
      if (cancelled || !live() || executionGeneration.current !== generation) return
      if (cause?.status === 404) storage(pointerKey, 'remove')
      else update({ error: message(cause) })
    })
    return () => { cancelled = true }
  }, [enabled, sessionActive, projectId, revision, pointerKey, live, update])

  const executionId = visible.execution?.execution_id
  useEffect(() => {
    if (!enabled || !sessionActive || !executionId) return undefined
    let cancelled = false
    let timer
    let deadlineTimer
    const generation = executionGeneration.current
    const valid = () => !cancelled && live() && executionGeneration.current === generation
    const token = {}
    following.current = token
    const stop = () => {
      clearTimeout(timer)
      clearTimeout(deadlineTimer)
      if (following.current === token) following.current = null
    }
    const settle = async (execution) => {
      if (!terminal(execution)) return false
      clearTimeout(timer)
      clearTimeout(deadlineTimer)
      if (execution.receipt_id && visible.receipt?.receipt_id !== execution.receipt_id) {
        const receiptKey = JSON.stringify([owner, execution.receipt_id])
        let read = receiptReads.current.get(receiptKey)
        if (!read) {
          read = getIosShipReceipt({ projectId, receiptId: execution.receipt_id }).catch((cause) => {
            if (receiptReads.current.get(receiptKey) === read) receiptReads.current.delete(receiptKey)
            throw cause
          })
          receiptReads.current.set(receiptKey, read)
        }
        const response = await read
        if (!mounted.current || currentOwnership.current !== ownership || executionGeneration.current !== generation) return true
        setState((old) => mounted.current && currentOwnership.current === ownership
          && executionGeneration.current === generation && old.owner === owner ? { ...old, receipt: response.receipt } : old)
      }
      if (valid()) {
        storage(pointerKey, 'remove')
        stop()
      }
      return true
    }
    const poll = async () => {
      try {
        const response = await getIosShipExecution({ projectId, executionId })
        if (!valid()) return
        update({ execution: response.execution, error: null })
        if (!await settle(response.execution) && valid()) timer = setTimeout(poll, 2000)
      } catch (cause) {
        if (valid()) update({ error: message(cause) })
        stop()
      }
    }
    if (terminal(visible.execution)) {
      settle(visible.execution).catch((cause) => { if (valid()) update({ error: message(cause) }); stop() })
    } else {
      deadlineTimer = setTimeout(() => {
        if (valid()) update({ error: 'still running; refresh to keep following' })
        cancelled = true
        stop()
      }, FOLLOW_LIMIT)
      timer = setTimeout(poll, 2000)
    }
    return () => { cancelled = true; stop() }
    // Status updates belong to this follower, not a new 30-minute window.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, sessionActive, projectId, pointerKey, executionId, live, update, follow])

  const launch = useCallback(async () => {
    if (flight.current !== null) return null
    if (!live() || !enabled
      || !iosShipLaunchAffordance(visible.readiness, { projectId, revision, sessionActive })) return undefined
    flight.current = scope
    executionGeneration.current += 1
    update({ launching: true, error: null })
    try {
      const response = await requestIosShipLaunch({ projectId, approvedLaunch: visible.readiness.approvedLaunch,
        idempotencyKey: makeIosShipLaunchKey(projectId, visible.readiness.approvedLaunch) })
      if (!live()) return undefined
      storage(pointerKey, 'write', { execution_id: response.execution.execution_id, revision, at: new Date().toISOString() })
      update({ execution: response.execution, receipt: null, launching: false })
      setFollow((n) => n + 1)
      return response
    } catch (cause) {
      update({ error: message(cause), launching: false })
      return undefined
    } finally {
      if (flight.current === scope) flight.current = null
    }
  }, [live, enabled, scope, visible.execution, visible.readiness, projectId, revision, sessionActive, update, pointerKey])

  const approve = useCallback(async (sourceRevision) => {
    if (approveFlight.current !== null) return null
    if (!live() || !enabled || !sessionActive || catalog.canApprove !== true || !revision) return undefined
    const source = catalog.sources.find((item) => item.source_revision === sourceRevision)
    if (!source || iosSourceApprovalState(source, catalog.approvals, revision) !== 'unapproved') return undefined
    if (pendingApproval.current?.revision === revision && pendingApproval.current.sourceRevision === sourceRevision) {
      update({ sourcesError: UNKNOWN_APPROVAL })
      return undefined
    }
    approveFlight.current = scope
    update({ approving: true, sourcesError: null })
    try {
      const approval = await requestIosShipApproval({ projectId, revision, source })
      if (!live()) return undefined
      await refreshSources()
      if (!live()) return undefined
      await refresh()
      update({ approving: false })
      return { approval, readBack: false }
    } catch (cause) {
      if (!live()) return undefined
      if (cause?.status != null) {
        update({ sourcesError: message(cause), approving: false })
        await refreshSources()
        update({ sourcesError: message(cause), approving: false })
        return undefined
      }
      update({ sourcesError: message(cause) })
      const fresh = await refreshSources()
      if (!live()) return undefined
      if (fresh && iosSourceApprovalState(source, fresh.approvals, revision) !== 'unapproved') {
        await refresh()
        update({ approving: false })
        return { approval: null, readBack: true }
      }
      if (!fresh) pendingApproval.current = { revision, sourceRevision }
      update({ sourcesError: message(cause), approving: false })
      return undefined
    } finally {
      if (approveFlight.current === scope) approveFlight.current = null
    }
  }, [live, enabled, sessionActive, catalog.canApprove, catalog.sources, catalog.approvals, revision, scope, update, projectId, refreshSources, refresh, pendingApproval])

  const readiness = visible.readiness || decorate(emptyIosShipReadiness())
  const phase = !enabled || !sessionActive ? 'idle'
    : visible.launching ? 'launching'
      : visible.execution ? terminal(visible.execution) ? visible.execution.status : 'running'
        : visible.loading ? 'loading'
          : iosShipLaunchAffordance(readiness, { projectId, revision, sessionActive }) ? 'ready'
            : /^(http_|unreachable$|readiness_unavailable$|provider_unavailable$)/.test(readiness.reason || '') ? 'unavailable'
              : readiness.setupState !== 'none' ? 'setup-required' : 'unavailable'
  return Object.freeze({ readiness, execution: visible.execution, receipt: visible.receipt, error: visible.error,
    busy: phase === 'launching', phase, launch, refresh,
    sources: catalog.sources, approvals: catalog.approvals, sync: catalog.sync, canApprove: catalog.canApprove,
    sourcesError: catalog.sourcesError, approving: visible.approving, approve, refreshSources })
}

export default useIosShipController
