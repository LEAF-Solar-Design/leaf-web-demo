import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import {
  attachToJob as defaultAttachToJob,
  closeJobBeacon as defaultCloseJobBeacon,
  getJob as defaultGetJob,
  listJobs as defaultListJobs,
  recordToEnvelope as defaultRecordToEnvelope,
  subscribeUnauthorized,
} from '../api.js'
import { track } from '../telemetry.js'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'

export const INFLIGHT_JOB_KEY = 'leaf.inflightJob'

function browserStorage() {
  try { return window.localStorage } catch { return null }
}

export function readInflightJob(storage = browserStorage()) {
  try { return JSON.parse(storage?.getItem(INFLIGHT_JOB_KEY) || 'null') } catch { return null }
}

export function saveInflightJob(jobId, tool, storage = browserStorage()) {
  const pointer = { job_id: jobId, tool, ts: Date.now() }
  try { storage?.setItem(INFLIGHT_JOB_KEY, JSON.stringify(pointer)) } catch { /* storage is best effort */ }
  return pointer
}

export function clearInflightJob(jobId = null, storage = browserStorage()) {
  try {
    if (jobId) {
      const current = readInflightJob(storage)
      if (current?.job_id !== jobId) return false
    }
    storage?.removeItem(INFLIGHT_JOB_KEY)
    return true
  } catch {
    return false
  }
}

// A run that was not yet submitted when sign-in expired: kept across the
// sign-in redirect so the user can review and run it again. Bounded (size,
// age, grammar) and read fail closed: anything malformed is removed, never used.
export const PENDING_RUN_KEY = 'leaf.pendingRun'
export const PENDING_RUN_LOGOUT_KEY = 'leaf.pendingRun.logout'
const PENDING_RUN_SESSION_KEY = 'leaf.pendingRun.session'
export const PENDING_RUN_TTL_MS = 1800000
export const MAX_PENDING_RUN_BYTES = 65536
// Results that finished after a newer run started, newest first.
export const MAX_STALE_RESULTS = 5

const NAME_CHARS = /^[A-Za-z0-9_.:-]+$/
const MAX_TOOL_NAME_CHARS = 128
const MAX_JOB_ID_CHARS = 200
const NO_ROWS = Object.freeze([])

const isToolName = (value) => typeof value === 'string'
  && value.length <= MAX_TOOL_NAME_CHARS && NAME_CHARS.test(value)
const isJobId = (value) => typeof value === 'string'
  && value.length <= MAX_JOB_ID_CHARS && NAME_CHARS.test(value)
const isPlainParams = (value) => !!value && typeof value === 'object' && !Array.isArray(value)

export function clearPendingRun(storage = browserStorage()) {
  try { storage?.removeItem(PENDING_RUN_KEY) } catch { /* storage is best effort */ }
  try { window.sessionStorage.removeItem(PENDING_RUN_SESSION_KEY) } catch { /* storage is best effort */ }
}

export function pendingRunAccount(storage = browserStorage()) {
  try {
    const token = storage?.getItem('leaf.jwt')
    if (!token) return null
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
    const org = storage?.getItem('leaf.org_id') || ''
    return typeof payload.sub === 'string' && payload.sub ? JSON.stringify([payload.iss || '', payload.sub, org]) : null
  } catch { return null }
}

// Anonymous records are browser-wide, so every signed-out tab may see them.
export function canSeePendingRun(record, currentAccount) {
  const account = typeof currentAccount === 'string' && currentAccount ? currentAccount : null
  return !!record && record.account === account
}

export function readPendingRun(storage = browserStorage(), now = Date.now(), account = pendingRunAccount(storage)) {
  let raw
  try { raw = storage?.getItem(PENDING_RUN_KEY) } catch { return null }
  if (raw == null) return null
  let record = null
  try {
    record = typeof raw === 'string' && raw.length <= MAX_PENDING_RUN_BYTES ? JSON.parse(raw) : null
  } catch { record = null }
  const age = record && Number.isFinite(record.ts) ? now - record.ts : NaN
  const valid = isPlainParams(record) && record.v === 2
    && (record.account === null || typeof record.account === 'string') && isToolName(record.tool)
    && isPlainParams(record.params) && age >= 0 && age <= PENDING_RUN_TTL_MS
  if (!valid) {
    clearPendingRun(storage)
    return null
  }
  // Another account/org may return later for its saved inputs. Legacy iss:sub
  // owner strings no longer match the full scope: hide them without deleting.
  if (typeof record.account === 'string' && typeof account === 'string' && record.account !== account) return null
  // An expired token cannot identify the returning account yet. Keep its
  // bounded record private until sign-in establishes who is returning.
  if (account === null && record.account !== null) return null
  if (record.account === null && account !== null) {
    // Only the tab/session that paused this anonymous submission may bind it
    // to the first sign-in. The proof survives the sign-in redirect.
    try {
      if (window.sessionStorage.getItem(PENDING_RUN_SESSION_KEY) !== raw) return null
      record.account = account
      storage?.setItem(PENDING_RUN_KEY, JSON.stringify(record))
      window.sessionStorage.removeItem(PENDING_RUN_SESSION_KEY)
    } catch { return null }
  }
  if (!canSeePendingRun(record, account)) {
    clearPendingRun(storage)
    return null
  }
  return { tool: record.tool, params: record.params, ts: record.ts }
}

export function savePendingRun(tool, params, storage = browserStorage(), now = Date.now(), account = pendingRunAccount(storage)) {
  const body = params == null ? {} : params
  if (!isToolName(tool) || !isPlainParams(body)) return null
  let serialized
  try { serialized = JSON.stringify({ v: 2, account, tool, params: body, ts: now }) } catch { return null }
  if (typeof serialized !== 'string' || serialized.length > MAX_PENDING_RUN_BYTES) return null
  try { storage?.setItem(PENDING_RUN_KEY, serialized) } catch { return null }
  try {
    if (account === null) window.sessionStorage.setItem(PENDING_RUN_SESSION_KEY, serialized)
    else window.sessionStorage.removeItem(PENDING_RUN_SESSION_KEY)
  } catch { /* anonymous binding fails closed when session storage is unavailable */ }
  return { tool, params: body, ts: now }
}

const defaultServices = {
  attachToJob: defaultAttachToJob,
  closeJobBeacon: defaultCloseJobBeacon,
  getJob: defaultGetJob,
  listJobs: defaultListJobs,
  recordToEnvelope: defaultRecordToEnvelope,
}

const defaultError = (error) => String(error?.message || error)
const isUnauthorized = (error) => error?.status === 401 || / -> 401$/.test(String(error?.message || ''))
// The server's 401 arrives as an ordinary envelope (apiFetch never throws), so
// runToolAsync RESOLVES it. It counts as a sign-in expiry only before a job id
// exists; after submit the envelope is the job's own failure, because a 401 on
// the user's token while polling rejects instead of resolving.
const isUnauthenticatedEnvelope = (envelope) => envelope?.ok === false
  && envelope?.error?.error_code === 'UNAUTHENTICATED'
const isTerminal = (status) => status === 'complete' || status === 'failed'

function formatLifecycleError(error, formatter) {
  try { return formatter(error) } catch { return defaultError(error) }
}

/**
 * Owns the transport-neutral lifecycle for one active job plus the recent-job rail.
 * Callers supply UI effects such as notices and seating a completed drawing version.
 * `execute` is normally api.runToolAsync; it receives the guarded onSubmit/onStatus
 * callbacks that keep an older request from changing newer state.
 */
export default function useJobController({
  mock = false,
  readsPaused = false,
  resetKey = null,
  services = defaultServices,
  storage,
  pollIntervalMs = 2500,
  formatError = defaultError,
  onCompleteVersion,
  onNotice,
  onAuthRequired,
  drawingKey,
} = {}) {
  const [jobs, setJobs] = useState([])
  const [authRequired, setAuthRequired] = useState(false)
  const [currentJobId, setCurrentJobId] = useState(null)
  const [jobTool, setJobTool] = useState(null)
  const [inflight, setInflight] = useState(null)
  const [reattaching, setReattaching] = useState(false)
  const [running, setRunning] = useState(false)
  const [status, setStatus] = useState(null)
  const [progress, setProgress] = useState(null)
  const [elapsedMs, setElapsedMs] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [localError, setLocalError] = useState(null)
  useEffect(() => { setLocalError(null) }, [readsPaused, resetKey])
  const [pollGeneration, setPollGeneration] = useState(0)
  const [staleResults, setStaleResults] = useState([])
  const [pendingRun, setPendingRun] = useState(() => (mock ? null : readPendingRun(storage)))
  const storageRef = useRef(storage)
  storageRef.current = storage
  const pendingAccountRef = useRef(pendingRunAccount(storage))
  const currentAccount = pendingRunAccount(storage)
  if (currentAccount) pendingAccountRef.current = currentAccount
  const publishPendingRun = useCallback(() => {
    const target = storageRef.current ?? browserStorage()
    const account = pendingRunAccount(target)
    let owner = null
    try {
      owner = JSON.parse(target?.getItem(PENDING_RUN_KEY) || 'null')?.account ?? null
    } catch { /* readPendingRun validates and clears malformed records below */ }
    let scoped = null
    if (typeof owner === 'string' && !canSeePendingRun({ account: owner }, account)) {
      readPendingRun(target, Date.now(), owner)
    } else {
      scoped = readPendingRun(target, Date.now(), account)
    }
    setPendingRun(scoped)
    return scoped
  }, [])
  useEffect(() => {
    const clearSaved = () => {
      pendingIntentRef.current += 1
      pendingAccountRef.current = null
      clearPendingRun(storageRef.current)
      setPendingRun(null)
    }
    const refreshSaved = () => {
      const nextAccount = pendingRunAccount(storageRef.current)
      if (nextAccount && pendingAccountRef.current !== nextAccount) pendingIntentRef.current += 1
      if (nextAccount) pendingAccountRef.current = nextAccount
      publishPendingRun()
    }
    const clear = subscribeBeforeLogout(() => {
      // Only explicit logout publishes this marker. A 401 removes the token
      // too, but must preserve the saved inputs in other tabs.
      try {
        const target = storageRef.current ?? browserStorage()
        target?.setItem(PENDING_RUN_LOGOUT_KEY, `${Date.now()}:${Math.random()}`)
      } catch { /* storage is best effort */ }
      clearSaved()
    })
    const refresh = subscribeTokenStored(refreshSaved)
    const unauthorized = subscribeUnauthorized(refreshSaved)
    const onStorage = event => {
      if (event.key === PENDING_RUN_LOGOUT_KEY || event.key === null) clearSaved()
      else if (event.key === 'leaf.jwt' || event.key === 'leaf.org_id') refreshSaved()
    }
    window.addEventListener('storage', onStorage)
    return () => { clear(); refresh(); unauthorized(); window.removeEventListener('storage', onStorage) }
  }, [publishPendingRun])

  const sequenceRef = useRef(0)
  // Bumped by reset(): a result from before a reset belongs to a context this
  // view has left, so it is not listed as stale either.
  const epochRef = useRef(0)
  const runningSinceRef = useRef(null)
  const callbacksRef = useRef({ formatError, onCompleteVersion, onNotice, onAuthRequired })
  const servicesRef = useRef(services)
  const drawingKeyRef = useRef(drawingKey)
  drawingKeyRef.current = drawingKey
  // Read at entry points and after every await: while a known guest's reads are paused, nothing reads,
  // publishes, or clears the stored pointer on the signed-in session's behalf.
  const readsPausedRef = useRef(readsPaused)
  readsPausedRef.current = readsPaused
  // Bumped each time reads pause. An operation compares the value it started with, so a response that
  // settles after a pause, even after a later resume, is still known to be superseded by that pause.
  const pauseGenerationRef = useRef(0)
  // Transport this hook started (subscriptions and run executions), aborted when reads pause.
  const activeControllersRef = useRef(new Set())
  const pointerOwnerRef = useRef(null)
  // Every runJob call, paused or not, takes the next intent, and only the newest intent may save pending inputs.
  const pendingIntentRef = useRef(0)
  // The sequence value when reads last resumed: a late submit receipt only triggers a reattach if
  // nothing else started since.
  const resumeSequenceRef = useRef(null)
  const [reattachGeneration, setReattachGeneration] = useState(0)
  callbacksRef.current = { formatError, onCompleteVersion, onNotice, onAuthRequired }
  servicesRef.current = { ...defaultServices, ...services }

  const setJobId = useCallback((jobId) => {
    setCurrentJobId(jobId)
  }, [])

  const setAuth = useCallback((required) => {
    setAuthRequired(required)
    callbacksRef.current.onAuthRequired?.(required)
  }, [])

  const resetActivity = useCallback(() => {
    setRunning(false)
    setStatus(null)
    setProgress(null)
    setElapsedMs(null)
    runningSinceRef.current = null
  }, [])

  const reset = useCallback(({ clearPointer = false } = {}) => {
    sequenceRef.current += 1
    epochRef.current += 1
    setJobId(null)
    setJobTool(null)
    setInflight(null)
    setReattaching(false)
    setResult(null)
    setError(null)
    resetActivity()
    if (clearPointer) clearInflightJob(null, storageRef.current)
  }, [resetActivity, setJobId])

  const reportError = useCallback((cause, { local = false } = {}) => {
    // Local refusal copy contains no signed-in job data and remains visible to guests.
    if (local && readsPausedRef.current) {
      setLocalError(formatLifecycleError(cause, callbacksRef.current.formatError))
      return
    }
    if (readsPausedRef.current) return
    setError(typeof cause === 'string'
      ? cause
      : formatLifecycleError(cause, callbacksRef.current.formatError))
  }, [])

  const clearError = useCallback(() => { setError(null); setLocalError(null) }, [])

  const adoptEnvelope = useCallback((envelope, { jobId = null, toolName = null } = {}) => {
    if (readsPausedRef.current) return null
    sequenceRef.current += 1
    setJobId(jobId)
    setJobTool(toolName || envelope?.tool || 'job')
    setResult(envelope || null)
    setError(null)
    setInflight(null)
    setReattaching(false)
    resetActivity()
    return envelope
  }, [resetActivity, setJobId])

  const adoptRecord = useCallback((record) => {
    if (!record) return null
    return adoptEnvelope(servicesRef.current.recordToEnvelope(record), {
      jobId: record.job_id,
      toolName: record.tool,
    })
  }, [adoptEnvelope])

  const markRunning = useCallback((startedAtSec) => {
    if (runningSinceRef.current == null) {
      runningSinceRef.current = startedAtSec ? startedAtSec * 1000 : Date.now()
    }
    setStatus('running')
    setElapsedMs(Math.max(0, Date.now() - runningSinceRef.current))
  }, [])

  const acceptStatus = useCallback((update, sequence) => {
    if (sequenceRef.current !== sequence) return
    setProgress(update?.progress || null)
    if (update?.status === 'running') markRunning(update.started_at)
    else setStatus(update?.status || 'running')
  }, [markRunning])

  const refreshJobs = useCallback(async () => {
    if (mock || readsPausedRef.current) return []
    const pauseGeneration = pauseGenerationRef.current
    try {
      const next = await servicesRef.current.listJobs()
      if (readsPausedRef.current || pauseGenerationRef.current !== pauseGeneration) return null
      setJobs(next)
      setAuth(false)
      return next
    } catch (cause) {
      if (readsPausedRef.current || pauseGenerationRef.current !== pauseGeneration) return null
      if (isUnauthorized(cause)) setAuth(true)
      return null
    }
  }, [mock, setAuth])

  // A superseded ok result is listed, never applied: the server already
  // committed its version, but it is not loaded into this view, and no
  // notice, result or version seat follows. The record carries no envelope
  // body, one row per job id, newest first, bounded to MAX_STALE_RESULTS.
  const recordStale = useCallback((envelope, { jobId, toolName, epoch }) => {
    if (!envelope?.ok || !isJobId(jobId)) return
    // An adopt or detach supersedes the active presentation just like a newer
    // run does. Keep a successful committed result visible in the stale list
    // unless reset() moved the controller to another drawing/context.
    if (epochRef.current !== epoch) return
    const tool = isToolName(toolName) && toolName !== 'job'
      ? toolName
      : (isToolName(envelope.tool) ? envelope.tool : 'job')
    const entry = { job_id: jobId, tool, new_version: envelope.result?.new_version ?? null }
    setStaleResults((current) => [entry, ...current.filter((row) => row.job_id !== jobId)]
      .slice(0, MAX_STALE_RESULTS))
  }, [])

  const dismissStaleResult = useCallback((jobId) => {
    setStaleResults((current) => current.filter((row) => row.job_id !== jobId))
  }, [])

  const finishEnvelope = useCallback(async (envelope, sequence, toolName, stale = null) => {
    if (sequenceRef.current !== sequence) {
      if (stale) recordStale(envelope, { ...stale, toolName })
      return false
    }
    setResult(envelope)
    if (envelope?.ok) {
      try {
        callbacksRef.current.onNotice?.({
          kind: 'job_complete',
          text: `${toolName || envelope.tool || 'job'} complete`,
          envelope,
        })
      } catch { /* a presentation callback cannot change the job result */ }
      if (envelope.result?.new_version) {
        try {
          await callbacksRef.current.onCompleteVersion?.(envelope.result.new_version, envelope)
        } catch { /* the caller owns its post-write refresh failure state */ }
      }
    }
    return sequenceRef.current === sequence
  }, [recordStale])

  const attachJob = useCallback(async (jobId, {
    toolName = 'job',
    persist = false,
    record = null,
    reattach = false,
  } = {}) => {
    if (!jobId || mock || readsPausedRef.current) return null
    const sequence = ++sequenceRef.current
    const pauseGeneration = pauseGenerationRef.current
    const controller = new AbortController()
    activeControllersRef.current.add(controller)
    let terminal = false
    const stale = { jobId, epoch: epochRef.current }
    let keepPointer = false
    setJobId(jobId)
    setJobTool(toolName)
    setRunning(true)
    setError(null)
    setResult(null)
    setStatus(null)
    setProgress(null)
    setElapsedMs(null)
    runningSinceRef.current = null
    if (persist) {
      const pointer = saveInflightJob(jobId, toolName, storageRef.current)
      pointerOwnerRef.current = { sequence, jobId }
      setInflight(pointer)
    }
    if (reattach) setReattaching(true)

    try {
      if (record && isTerminal(record.status)) {
        const envelope = servicesRef.current.recordToEnvelope(record)
        terminal = true
        await finishEnvelope(envelope, sequence, toolName || record.tool, stale)
        return sequenceRef.current === sequence ? envelope : null
      }
      if (record?.status === 'running') markRunning(record.started_at)
      else if (record?.status) setStatus(record.status)
      const envelope = await servicesRef.current.attachToJob(jobId, {
        onStatus: (update) => acceptStatus(update, sequence),
        signal: controller.signal,
      })
      terminal = true
      await finishEnvelope(envelope, sequence, toolName, stale)
      return sequenceRef.current === sequence ? envelope : null
    } catch (cause) {
      if (sequenceRef.current === sequence) {
        setError(formatLifecycleError(cause, callbacksRef.current.formatError))
      }
      if (isUnauthorized(cause)) {
        // The job is already on the server: keep its pointer so it
        // reattaches after sign-in instead of being lost.
        keepPointer = true
        // Once reads resume, a sign-in failure from an attach a pause superseded is stale: it publishes nothing.
        if (pauseGenerationRef.current === pauseGeneration || readsPausedRef.current) setAuth(true)
      }
      return null
    } finally {
      activeControllersRef.current.delete(controller)
      // A pause superseded the attach before the job finished: keep its pointer so it reattaches after sign-in.
      if (!terminal && pauseGenerationRef.current !== pauseGeneration) keepPointer = true
      if (sequenceRef.current === sequence) {
        resetActivity()
        setReattaching(false)
        setInflight(null)
      }
      if (!keepPointer) clearInflightJob(jobId, storageRef.current)
      refreshJobs()
    }
  }, [acceptStatus, finishEnvelope, markRunning, mock, refreshJobs, resetActivity, setAuth, setJobId])

  // `submission` ({ params }) is optional: given, a sign-in expiry before the
  // job is submitted keeps it as the pending run. Without it (ToolCast) the
  // lifecycle is unchanged.
  const runJob = useCallback(async ({ toolName, execute, submission = null }) => {
    if (!execute) throw new TypeError('runJob requires an execute callback')
    const intent = ++pendingIntentRef.current
    const runAccount = pendingAccountRef.current
    if (readsPausedRef.current) {
      if (submission && pendingIntentRef.current === intent) {
        const saved = savePendingRun(toolName, submission.params, storageRef.current, Date.now(), runAccount)
        if (saved) publishPendingRun()
      }
      setAuth(true)
      return null
    }
    const capturedDrawingKey = drawingKeyRef.current
    const origin = typeof capturedDrawingKey === 'string' && capturedDrawingKey.length > 0
      ? { drawingKey: capturedDrawingKey } : undefined
    const sequence = ++sequenceRef.current
    const pauseGeneration = pauseGenerationRef.current
    const startedAt = Date.now()
    const controller = new AbortController()
    activeControllersRef.current.add(controller)
    let terminal = false
    const epoch = epochRef.current
    let submittedJobId = null
    let keepPointer = false
    setJobId(null)
    setJobTool(toolName || 'job')
    setRunning(true)
    setError(null)
    setResult(null)
    setStatus(null)
    setProgress(null)
    setElapsedMs(null)
    runningSinceRef.current = null
    const signInExpired = () => {
      const supersededByPause = pauseGenerationRef.current !== pauseGeneration
      // Once reads resume, a sign-in failure from a run a pause superseded is stale: it publishes nothing.
      if (supersededByPause && !readsPausedRef.current) return
      if (submittedJobId) {
        // Submitted: re-running would duplicate an apply, so keep the
        // pointer and let the job reattach after sign-in.
        keepPointer = true
      } else if ((sequenceRef.current === sequence || supersededByPause) && pendingIntentRef.current === intent && submission) {
        const saved = savePendingRun(toolName, submission.params, storageRef.current, Date.now(), runAccount)
        if (saved) publishPendingRun()
      }
      setAuth(true)
    }
    try {
      const output = await execute({
        onSubmit: (jobId) => {
          if (sequenceRef.current === sequence) {
            submittedJobId = jobId
            setJobId(jobId)
            const pointer = saveInflightJob(jobId, toolName, storageRef.current)
            pointerOwnerRef.current = { sequence, jobId }
            setInflight(pointer)
          } else if (pauseGenerationRef.current !== pauseGeneration) {
            // Superseded by a pause after the submit went out: the job exists, so keep a pointer to it unless a
            // newer run owns the pointer by run sequence and job id, never by a millisecond timestamp alone.
            // Also keep a strictly newer pointer from another tab; otherwise reattach once reads resume.
            submittedJobId = jobId
            const stored = readInflightJob(storageRef.current)
            const owner = pointerOwnerRef.current
            const newerRunOwnsPointer = Boolean(stored?.job_id) && owner !== null && owner.sequence > sequence && owner.jobId === stored.job_id
            if (!newerRunOwnsPointer && !(Number.isFinite(stored?.ts) && stored.ts > startedAt)) {
              saveInflightJob(jobId, toolName, storageRef.current)
              pointerOwnerRef.current = { sequence, jobId }
            }
            if (!readsPausedRef.current && sequenceRef.current === resumeSequenceRef.current) {
              setReattachGeneration((value) => value + 1)
            }
          }
        },
        onStatus: (update) => acceptStatus(update, sequence),
        signal: controller.signal,
      })
      terminal = true
      const envelope = origin && output ? { ...output, origin } : output
      if (!submittedJobId && isUnauthenticatedEnvelope(envelope)) signInExpired()
      await finishEnvelope(envelope, sequence, toolName, { jobId: submittedJobId, epoch })
      return sequenceRef.current === sequence ? envelope : null
    } catch (cause) {
      if (sequenceRef.current === sequence) {
        setError(formatLifecycleError(cause, callbacksRef.current.formatError))
      }
      if (isUnauthorized(cause)) signInExpired()
      return null
    } finally {
      activeControllersRef.current.delete(controller)
      // A pause superseded the run before the job finished: keep its pointer so it reattaches after sign-in.
      if (!terminal && pauseGenerationRef.current !== pauseGeneration) keepPointer = true
      if (sequenceRef.current === sequence) {
        resetActivity()
        setInflight(null)
      }
      if (submittedJobId && !keepPointer) clearInflightJob(submittedJobId, storageRef.current)
      if (!mock) refreshJobs()
    }
  }, [acceptStatus, finishEnvelope, mock, publishPendingRun, refreshJobs, resetActivity, setAuth, setJobId])

  const takePendingRun = useCallback(() => {
    if (readsPausedRef.current) return null
    const record = readPendingRun(storageRef.current)
    clearPendingRun(storageRef.current)
    setPendingRun(null)
    return record
  }, [])

  // Preparation preserves inputs. Only a successfully armed caller completes
  // the handoff; a refusal or failed catalog load leaves the saved run intact.
  const preparePendingRun = useCallback(async (loadTools) => {
    if (readsPausedRef.current) {
      setAuth(true)
      return null
    }
    if (!readPendingRun(storageRef.current)) {
      setPendingRun(null)
      return null
    }
    const pauseGeneration = pauseGenerationRef.current
    const tools = await loadTools()
    if (readsPausedRef.current || pauseGenerationRef.current !== pauseGeneration) return null
    const pending = readPendingRun(storageRef.current)
    if (!pending) return null
    const tool = (Array.isArray(tools) ? tools : [])
      .find((candidate) => candidate?.name === pending.tool) || null
    const complete = () => {
      if (readsPausedRef.current || pauseGenerationRef.current !== pauseGeneration) return
      const current = readPendingRun(storageRef.current)
      if (JSON.stringify(current) === JSON.stringify(pending)) takePendingRun()
    }
    return { pending, tool, complete }
  }, [setAuth, takePendingRun])

  const discardPendingRun = useCallback(() => {
    clearPendingRun(storageRef.current)
    setPendingRun(null)
  }, [])

  const detachJob = useCallback(() => {
    sequenceRef.current += 1
    const pointer = readInflightJob(storageRef.current)
    if (pointer?.job_id) clearInflightJob(pointer.job_id, storageRef.current)
    setInflight(null)
    setReattaching(false)
    resetActivity()
    refreshJobs()
  }, [refreshJobs, resetActivity])

  const resumeJobPolling = useCallback(() => setPollGeneration((value) => value + 1), [])

  useEffect(() => {
    reset()
  }, [mock, reset, resetKey])

  // A layout effect runs in the same task as the commit, before any timer or response callback can run
  // against the paused tree.
  useLayoutEffect(() => {
    if (!readsPaused) {
      resumeSequenceRef.current = sequenceRef.current
      return
    }
    pauseGenerationRef.current += 1
    for (const controller of activeControllersRef.current) controller.abort()
    activeControllersRef.current.clear()
    setJobs([])
    setStaleResults([])
    reset()
  }, [readsPaused, reset])

  useEffect(() => {
    if (status !== 'running') return undefined
    // 1s cadence: fmtElapsed shows one decimal of seconds, so a 200ms tick
    // was five renders for every user-visible digit change. 1s still reads
    // as live without the redundant re-renders.
    const timer = setInterval(() => {
      if (runningSinceRef.current != null) setElapsedMs(Date.now() - runningSinceRef.current)
    }, 1000)
    return () => clearInterval(timer)
  }, [status])

  useEffect(() => {
    if (mock) {
      setJobs([])
      setAuth(false)
      return undefined
    }
    // A known guest's reads are paused: no jobs poll until the session can read.
    if (readsPaused) return undefined
    let alive = true
    let timer = null
    const tick = async () => {
      if (!alive || readsPausedRef.current) return
      try {
        const next = await servicesRef.current.listJobs()
        if (alive && !readsPausedRef.current) {
          setJobs(next)
          setAuth(false)
        }
      } catch (cause) {
        if (readsPausedRef.current) return
        if (alive && isUnauthorized(cause)) {
          setAuth(true)
          if (timer) clearInterval(timer)
          timer = null
        }
      }
    }
    tick()
    timer = setInterval(tick, pollIntervalMs)
    return () => {
      alive = false
      if (timer) clearInterval(timer)
    }
  }, [mock, readsPaused, pollGeneration, pollIntervalMs, setAuth])

  useEffect(() => {
    if (mock) {
      setInflight(null)
      setReattaching(false)
      return undefined
    }
    // Keep the stored pointer; reattach once the session can read.
    if (readsPaused) return undefined
    const pointer = readInflightJob(storageRef.current)
    if (!pointer?.job_id) return undefined
    setInflight(pointer)
    let alive = true
    const sequenceAtLoad = sequenceRef.current
    const pauseGeneration = pauseGenerationRef.current
    ;(async () => {
      let record
      try {
        record = await servicesRef.current.getJob(pointer.job_id)
      } catch (cause) {
        if (isUnauthorized(cause)) {
          // Sign-in expired: the job is still on the server, so keep its
          // pointer and in-flight state and reattach after sign-in.
          // A run or attach started since this read reports its own sign-in failures.
          if (alive && sequenceRef.current === sequenceAtLoad) setAuth(true)
          return
        }
        if (readsPausedRef.current || pauseGenerationRef.current !== pauseGeneration) return
        if (alive && sequenceRef.current === sequenceAtLoad) {
          clearInflightJob(pointer.job_id, storageRef.current)
          setInflight(null)
        }
        return
      }
      if (!alive || readsPausedRef.current || pauseGenerationRef.current !== pauseGeneration || sequenceRef.current !== sequenceAtLoad) return
      // P2 wave C-2: does session-survival UX earn its keep? Counted only for
      // a job still in flight (a terminal record is just a result render, not
      // a re-attach). This boot path is the ONLY reattach:true call site.
      if (!isTerminal(record.status)) {
        track('run.reattached', {
          from: 'boot',
          ...(Number.isFinite(pointer.ts)
            ? { job_age_s: Math.round((Date.now() - pointer.ts) / 1000) }
            : {}),
        })
      }
      await attachJob(pointer.job_id, {
        toolName: pointer.tool || record.tool || 'job',
        persist: true,
        record,
        reattach: !isTerminal(record.status),
      })
    })()
    return () => { alive = false }
  }, [attachJob, mock, readsPaused, reattachGeneration, setAuth])

  useEffect(() => {
    if (mock || typeof window === 'undefined' || typeof document === 'undefined') return undefined
    let closedJobId = null
    const closeInflight = () => {
      const pointer = readInflightJob(storageRef.current)
      if (!pointer?.job_id || pointer.job_id === closedJobId) return
      closedJobId = pointer.job_id
      servicesRef.current.closeJobBeacon(pointer.job_id)
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') closeInflight()
    }
    window.addEventListener('pagehide', closeInflight)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.removeEventListener('pagehide', closeInflight)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [mock])

  useEffect(() => () => { sequenceRef.current += 1 }, [])

  const currentJob = useMemo(() => {
    if (!jobTool) return null
    let jobStatus
    if (running) jobStatus = status || 'running'
    else if (result) jobStatus = result.ok ? 'complete' : 'failed'
    else return null
    return {
      job_id: currentJobId,
      tool: jobTool,
      status: jobStatus,
      progress: progress || status || 'running',
      elapsed_ms: elapsedMs != null ? elapsedMs : (result?.timing_ms ?? null),
      degraded_mode: !!result?.degraded_mode,
      error: result?.error || null,
      cost: result?.cost || null,
      entitlement_required: !!result?.entitlement_required,
    }
  }, [currentJobId, elapsedMs, jobTool, progress, result, running, status])

  return {
    jobs: readsPaused ? NO_ROWS : jobs,
    authRequired,
    currentJobId: readsPaused ? null : currentJobId,
    currentJob: readsPaused ? null : currentJob,
    inflight: readsPaused ? null : inflight,
    reattaching: readsPaused ? false : reattaching,
    running: readsPaused ? false : running,
    status: readsPaused ? null : status,
    progress: readsPaused ? null : progress,
    elapsedMs: readsPaused ? null : elapsedMs,
    result: readsPaused ? null : result,
    error: readsPaused ? localError : error,
    runJob,
    attachJob,
    detachJob,
    reset,
    reportError,
    clearError,
    adoptEnvelope,
    adoptRecord,
    refreshJobs,
    resumeJobPolling,
    staleResults: readsPaused ? NO_ROWS : staleResults,
    dismissStaleResult,
    pendingRun,
    takePendingRun,
    preparePendingRun,
    discardPendingRun,
  }
}
