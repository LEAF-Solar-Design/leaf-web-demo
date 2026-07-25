import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  attachToJob as defaultAttachToJob,
  closeJobBeacon as defaultCloseJobBeacon,
  getJob as defaultGetJob,
  listJobs as defaultListJobs,
  recordToEnvelope as defaultRecordToEnvelope,
} from '../api.js'

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

const defaultServices = {
  attachToJob: defaultAttachToJob,
  closeJobBeacon: defaultCloseJobBeacon,
  getJob: defaultGetJob,
  listJobs: defaultListJobs,
  recordToEnvelope: defaultRecordToEnvelope,
}

const defaultError = (error) => String(error?.message || error)
const isUnauthorized = (error) => error?.status === 401 || / -> 401$/.test(String(error?.message || ''))
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

  const sequenceRef = useRef(0)
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

  const finishEnvelope = useCallback(async (envelope, sequence, toolName) => {
    if (sequenceRef.current !== sequence) return false
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
  }, [])

  const attachJob = useCallback(async (jobId, {
    toolName = 'job',
    persist = false,
    record = null,
    reattach = false,
  } = {}) => {
    if (!jobId || mock) return null
    const sequence = ++sequenceRef.current
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
        await finishEnvelope(envelope, sequence, toolName || record.tool)
        return sequenceRef.current === sequence ? envelope : null
      }
      if (record?.status === 'running') markRunning(record.started_at)
      else if (record?.status) setStatus(record.status)
      const envelope = await servicesRef.current.attachToJob(jobId, {
        onStatus: (update) => acceptStatus(update, sequence),
      })
      await finishEnvelope(envelope, sequence, toolName)
      return sequenceRef.current === sequence ? envelope : null
    } catch (cause) {
      if (sequenceRef.current === sequence) {
        setError(formatLifecycleError(cause, callbacksRef.current.formatError))
      }
      return null
    } finally {
      if (sequenceRef.current === sequence) {
        resetActivity()
        setReattaching(false)
        setInflight(null)
      }
      clearInflightJob(jobId, storageRef.current)
      refreshJobs()
    }
  }, [acceptStatus, finishEnvelope, markRunning, mock, refreshJobs, resetActivity, setJobId])

  const runJob = useCallback(async ({ toolName, execute }) => {
    if (!execute) throw new TypeError('runJob requires an execute callback')
    const sequence = ++sequenceRef.current
    let submittedJobId = null
    setJobId(null)
    setJobTool(toolName || 'job')
    setRunning(true)
    setError(null)
    setResult(null)
    setStatus(null)
    setProgress(null)
    setElapsedMs(null)
    runningSinceRef.current = null
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
      await finishEnvelope(envelope, sequence, toolName)
      return sequenceRef.current === sequence ? envelope : null
    } catch (cause) {
      if (sequenceRef.current === sequence) {
        setError(formatLifecycleError(cause, callbacksRef.current.formatError))
      }
      return null
    } finally {
      if (sequenceRef.current === sequence) {
        resetActivity()
        setInflight(null)
      }
      if (submittedJobId) clearInflightJob(submittedJobId, storageRef.current)
      if (!mock) refreshJobs()
    }
  }, [acceptStatus, finishEnvelope, mock, refreshJobs, resetActivity, setJobId])

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
    const timer = setInterval(() => {
      if (runningSinceRef.current != null) setElapsedMs(Date.now() - runningSinceRef.current)
    }, 200)
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
      } catch {
        if (alive && sequenceRef.current === sequenceAtLoad) {
          clearInflightJob(pointer.job_id, storageRef.current)
          setInflight(null)
        }
        return
      }
      if (!alive || sequenceRef.current !== sequenceAtLoad) return
      await attachJob(pointer.job_id, {
        toolName: pointer.tool || record.tool || 'job',
        persist: true,
        record,
        reattach: !isTerminal(record.status),
      })
    })()
    return () => { alive = false }
  }, [attachJob, mock])

  useEffect(() => {
    if (mock || typeof window === 'undefined' || typeof document === 'undefined') return undefined
    const closeInflight = () => {
      const pointer = readInflightJob(storageRef.current)
      if (pointer?.job_id) servicesRef.current.closeJobBeacon(pointer.job_id)
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
  }
}
