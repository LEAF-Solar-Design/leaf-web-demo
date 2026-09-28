import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  attachToJob as defaultAttachToJob,
  closeJobBeacon as defaultCloseJobBeacon,
  getJob as defaultGetJob,
  listJobs as defaultListJobs,
  recordToEnvelope as defaultRecordToEnvelope,
} from '../api.js'
import { track } from '../telemetry.js'

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
export const PENDING_RUN_TTL_MS = 1800000
export const MAX_PENDING_RUN_BYTES = 65536
// Results that finished after a newer run started, newest first.
export const MAX_STALE_RESULTS = 5

const NAME_CHARS = /^[A-Za-z0-9_.:-]+$/
const MAX_TOOL_NAME_CHARS = 128
const MAX_JOB_ID_CHARS = 200

const isToolName = (value) => typeof value === 'string'
  && value.length <= MAX_TOOL_NAME_CHARS && NAME_CHARS.test(value)
const isJobId = (value) => typeof value === 'string'
  && value.length <= MAX_JOB_ID_CHARS && NAME_CHARS.test(value)
const isPlainParams = (value) => !!value && typeof value === 'object' && !Array.isArray(value)

export function clearPendingRun(storage = browserStorage()) {
  try { storage?.removeItem(PENDING_RUN_KEY) } catch { /* storage is best effort */ }
}

export function readPendingRun(storage = browserStorage(), now = Date.now()) {
  let raw
  try { raw = storage?.getItem(PENDING_RUN_KEY) } catch { return null }
  if (raw == null) return null
  let record = null
  try {
    record = typeof raw === 'string' && raw.length <= MAX_PENDING_RUN_BYTES ? JSON.parse(raw) : null
  } catch { record = null }
  const age = record && Number.isFinite(record.ts) ? now - record.ts : NaN
  const valid = isPlainParams(record) && record.v === 1 && isToolName(record.tool)
    && isPlainParams(record.params) && age >= 0 && age <= PENDING_RUN_TTL_MS
  if (!valid) {
    clearPendingRun(storage)
    return null
  }
  return { tool: record.tool, params: record.params, ts: record.ts }
}

export function savePendingRun(tool, params, storage = browserStorage(), now = Date.now()) {
  const body = params == null ? {} : params
  if (!isToolName(tool) || !isPlainParams(body)) return null
  let serialized
  try { serialized = JSON.stringify({ v: 1, tool, params: body, ts: now }) } catch { return null }
  if (typeof serialized !== 'string' || serialized.length > MAX_PENDING_RUN_BYTES) return null
  try { storage?.setItem(PENDING_RUN_KEY, serialized) } catch { return null }
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
  resetKey = null,
  services = defaultServices,
  storage,
  pollIntervalMs = 2500,
  formatError = defaultError,
  onCompleteVersion,
  onNotice,
  onAuthRequired,
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
  const [pollGeneration, setPollGeneration] = useState(0)
  const [staleResults, setStaleResults] = useState([])
  const [pendingRun, setPendingRun] = useState(() => (mock ? null : readPendingRun(storage)))

  const sequenceRef = useRef(0)
  // The sequence of the newest runJob/attachJob start. A result whose own
  // sequence is no longer this one was superseded by a newer run.
  const runStartRef = useRef(0)
  // Bumped by reset(): a result from before a reset belongs to a context this
  // view has left, so it is not listed as stale either.
  const epochRef = useRef(0)
  const runningSinceRef = useRef(null)
  const callbacksRef = useRef({ formatError, onCompleteVersion, onNotice, onAuthRequired })
  const servicesRef = useRef(services)
  const storageRef = useRef(storage)
  callbacksRef.current = { formatError, onCompleteVersion, onNotice, onAuthRequired }
  servicesRef.current = { ...defaultServices, ...services }
  storageRef.current = storage

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

  const reportError = useCallback((cause) => {
    setError(typeof cause === 'string'
      ? cause
      : formatLifecycleError(cause, callbacksRef.current.formatError))
  }, [])

  const clearError = useCallback(() => setError(null), [])

  const adoptEnvelope = useCallback((envelope, { jobId = null, toolName = null } = {}) => {
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
    if (mock) return []
    try {
      const next = await servicesRef.current.listJobs()
      setJobs(next)
      setAuth(false)
      return next
    } catch (cause) {
      if (isUnauthorized(cause)) setAuth(true)
      return null
    }
  }, [mock, setAuth])

  // A superseded ok result is listed, never applied: the server already
  // committed its version, but it is not loaded into this view, and no
  // notice, result or version seat follows. The record carries no envelope
  // body, one row per job id, newest first, bounded to MAX_STALE_RESULTS.
  const recordStale = useCallback((envelope, sequence, { jobId, toolName, epoch }) => {
    if (!envelope?.ok || !isJobId(jobId)) return
    // Declared, not covered: a result superseded by an adopt or a detach
    // (rather than a newer run) still returns here and is dropped silently.
    if (runStartRef.current === sequence || epochRef.current !== epoch) return
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
      if (stale) recordStale(envelope, sequence, { ...stale, toolName })
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
    if (!jobId || mock) return null
    const sequence = ++sequenceRef.current
    runStartRef.current = sequence
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
      setInflight(pointer)
    }
    if (reattach) setReattaching(true)

    try {
      if (record && isTerminal(record.status)) {
        const envelope = servicesRef.current.recordToEnvelope(record)
        await finishEnvelope(envelope, sequence, toolName || record.tool, stale)
        return sequenceRef.current === sequence ? envelope : null
      }
      if (record?.status === 'running') markRunning(record.started_at)
      else if (record?.status) setStatus(record.status)
      const envelope = await servicesRef.current.attachToJob(jobId, {
        onStatus: (update) => acceptStatus(update, sequence),
      })
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
        setAuth(true)
      }
      return null
    } finally {
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
    const sequence = ++sequenceRef.current
    runStartRef.current = sequence
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
      if (submittedJobId) {
        // Submitted: re-running would duplicate an apply, so keep the
        // pointer and let the job reattach after sign-in.
        keepPointer = true
      } else if (sequenceRef.current === sequence && submission) {
        const saved = savePendingRun(toolName, submission.params, storageRef.current)
        if (saved) setPendingRun(saved)
      }
      setAuth(true)
    }
    try {
      const envelope = await execute({
        onSubmit: (jobId) => {
          if (sequenceRef.current !== sequence) return
          submittedJobId = jobId
          setJobId(jobId)
          const pointer = saveInflightJob(jobId, toolName, storageRef.current)
          setInflight(pointer)
        },
        onStatus: (update) => acceptStatus(update, sequence),
      })
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
      if (sequenceRef.current === sequence) {
        resetActivity()
        setInflight(null)
      }
      if (submittedJobId && !keepPointer) clearInflightJob(submittedJobId, storageRef.current)
      if (!mock) refreshJobs()
    }
  }, [acceptStatus, finishEnvelope, mock, refreshJobs, resetActivity, setAuth, setJobId])

  const takePendingRun = useCallback(() => {
    const record = readPendingRun(storageRef.current)
    clearPendingRun(storageRef.current)
    setPendingRun(null)
    return record
  }, [])

  // Loads the catalog BEFORE taking the pending run, so a failed load rejects
  // with the saved inputs still stored. Resolves null when nothing is pending,
  // else { pending, tool } with tool null when the catalog no longer has it.
  const preparePendingRun = useCallback(async (loadTools) => {
    if (!readPendingRun(storageRef.current)) {
      setPendingRun(null)
      return null
    }
    const tools = await loadTools()
    const pending = takePendingRun()
    if (!pending) return null
    const tool = (Array.isArray(tools) ? tools : [])
      .find((candidate) => candidate?.name === pending.tool) || null
    return { pending, tool }
  }, [takePendingRun])

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
    let alive = true
    let timer = null
    const tick = async () => {
      try {
        const next = await servicesRef.current.listJobs()
        if (alive) {
          setJobs(next)
          setAuth(false)
        }
      } catch (cause) {
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
  }, [mock, pollGeneration, pollIntervalMs, setAuth])

  useEffect(() => {
    if (mock) {
      setInflight(null)
      setReattaching(false)
      return undefined
    }
    const pointer = readInflightJob(storageRef.current)
    if (!pointer?.job_id) return undefined
    setInflight(pointer)
    let alive = true
    const sequenceAtLoad = sequenceRef.current
    ;(async () => {
      let record
      try {
        record = await servicesRef.current.getJob(pointer.job_id)
      } catch (cause) {
        if (isUnauthorized(cause)) {
          // Sign-in expired: the job is still on the server, so keep its
          // pointer and in-flight state and reattach after sign-in.
          if (alive) setAuth(true)
          return
        }
        if (alive && sequenceRef.current === sequenceAtLoad) {
          clearInflightJob(pointer.job_id, storageRef.current)
          setInflight(null)
        }
        return
      }
      if (!alive || sequenceRef.current !== sequenceAtLoad) return
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
  }, [attachJob, mock, setAuth])

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
    jobs,
    authRequired,
    currentJobId,
    currentJob,
    inflight,
    reattaching,
    running,
    status,
    progress,
    elapsedMs,
    result,
    error,
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
    staleResults,
    dismissStaleResult,
    pendingRun,
    takePendingRun,
    preparePendingRun,
    discardPendingRun,
  }
}
