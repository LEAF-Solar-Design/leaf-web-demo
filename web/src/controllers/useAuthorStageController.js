import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { stageAuthorTool as defaultStageAuthorTool } from '../api.js'

export const INFLIGHT_AUTHOR_KEY = 'leaf.inflightAuthor.v1'

function browserStorage() {
  try { return window.localStorage } catch { return null }
}

export function readInflightAuthor(storage = browserStorage()) {
  try {
    const pointer = JSON.parse(storage?.getItem(INFLIGHT_AUTHOR_KEY) || 'null')
    if (!pointer || typeof pointer !== 'object') return null
    if (typeof pointer.idempotency_key !== 'string' || !pointer.idempotency_key) return null
    if (typeof pointer.description !== 'string' || !pointer.description.trim()) return null
    if (pointer.target_tool_name != null && typeof pointer.target_tool_name !== 'string') return null
    if (!Number.isFinite(Number(pointer.created_at))) return null
    if (pointer.poll_url != null && typeof pointer.poll_url !== 'string') return null
    return pointer
  } catch {
    return null
  }
}

export function saveInflightAuthor(pointer, storage = browserStorage()) {
  try { storage?.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(pointer)) } catch { /* best effort */ }
  return pointer
}

export function clearInflightAuthor(idempotencyKey = null, storage = browserStorage()) {
  try {
    if (idempotencyKey) {
      const current = readInflightAuthor(storage)
      if (current?.idempotency_key !== idempotencyKey) return false
    }
    storage?.removeItem(INFLIGHT_AUTHOR_KEY)
    return true
  } catch {
    return false
  }
}

function requestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `leaf-author-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function isActivePhase(phase) {
  return ['submitting', 'accepted', 'submitted', 'queued', 'pending', 'running', 'authoring', 'reconnecting'].includes(phase)
}

export default function useAuthorStageController({
  mock = false,
  enabled = true,
  storage,
  stageAuthorTool = defaultStageAuthorTool,
} = {}) {
  const storageRef = useRef(storage)
  const [pointer, setPointer] = useState(() => mock ? null : readInflightAuthor(storage))
  const [phase, setPhase] = useState(pointer ? 'reconnecting' : 'idle')
  const [progress, setProgress] = useState(pointer ? 'restoring authoring request' : null)
  const [elapsedMs, setElapsedMs] = useState(pointer ? Math.max(0, Date.now() - pointer.created_at) : 0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const sequenceRef = useRef(0)
  const resumedRef = useRef(false)
  const abortRef = useRef(null)
  storageRef.current = storage

  const persist = useCallback((next) => {
    saveInflightAuthor(next, storageRef.current)
    setPointer(next)
    return next
  }, [])

  const runPointer = useCallback(async (initial, { reconnecting = false } = {}) => {
    if (!initial) return null
    abortRef.current?.abort()
    const abortController = new AbortController()
    abortRef.current = abortController
    const sequence = ++sequenceRef.current
    setResult(null)
    setError(null)
    setPhase(reconnecting ? 'reconnecting' : 'submitting')
    setProgress(reconnecting ? 'restoring authoring request' : 'submitting authoring request')
    try {
      const staged = await stageAuthorTool(
        mock,
        initial.description,
        initial.target_tool_name || null,
        {
          idempotencyKey: initial.idempotency_key,
          pollUrl: initial.poll_url || null,
          changeSetId: initial.change_set_id || null,
          retryAfterMs: initial.retry_after_ms || null,
          signal: abortController.signal,
          onAccepted: (accepted) => {
            if (sequenceRef.current !== sequence) return
            persist({
              ...initial,
              change_set_id: accepted.change_set_id,
              poll_url: accepted.poll_url,
              retry_after_ms: accepted.retry_after_ms,
            })
          },
          onStatus: (update) => {
            if (sequenceRef.current !== sequence) return
            setPhase(String(update?.status || 'running').toLowerCase())
            setProgress(update?.progress || update?.status || 'authoring')
          },
        },
      )
      if (sequenceRef.current !== sequence) return null
      if (initial.target_tool_name && staged?.tool?.name !== initial.target_tool_name) {
        const mismatch = new Error(`The staged revision did not match ${initial.target_tool_name}. It was not made publishable.`)
        throw mismatch
      }
      clearInflightAuthor(initial.idempotency_key, storageRef.current)
      setPointer(null)
      setPhase('succeeded')
      setProgress('staged for review')
      setResult(staged)
      return staged
    } catch (cause) {
      if (sequenceRef.current !== sequence) return null
      const terminal = !!cause?.authorTerminal
      if (terminal) {
        clearInflightAuthor(initial.idempotency_key, storageRef.current)
        setPointer(null)
        setPhase('failed')
      } else {
        setPhase('interrupted')
        setProgress('connection interrupted')
      }
      setError(cause instanceof Error ? cause : new Error(String(cause)))
      return null
    }
  }, [mock, persist, stageAuthorTool])

  const stage = useCallback((description, targetToolName = null) => {
    if (!enabled) return Promise.resolve(null)
    const current = readInflightAuthor(storageRef.current)
    if (current) return runPointer(current, { reconnecting: true })
    resumedRef.current = true
    const next = {
      idempotency_key: requestId(),
      description,
      target_tool_name: targetToolName || null,
      change_set_id: null,
      poll_url: null,
      retry_after_ms: null,
      created_at: Date.now(),
    }
    if (!mock) persist(next)
    else setPointer(next)
    return runPointer(next)
  }, [enabled, mock, persist, runPointer])

  const resume = useCallback(() => {
    if (!enabled) return Promise.resolve(null)
    const current = pointer || readInflightAuthor(storageRef.current)
    return current ? runPointer(current, { reconnecting: true }) : Promise.resolve(null)
  }, [enabled, pointer, runPointer])

  useEffect(() => {
    if (!enabled || mock || !pointer || resumedRef.current) return
    resumedRef.current = true
    runPointer(pointer, { reconnecting: true })
  }, [enabled, mock, pointer, runPointer])

  useEffect(() => {
    if (!isActivePhase(phase)) return undefined
    const started = pointer?.created_at || Date.now()
    const tick = () => setElapsedMs(Math.max(0, Date.now() - started))
    tick()
    const timer = setInterval(tick, 250)
    return () => clearInterval(timer)
  }, [phase, pointer?.created_at])

  useEffect(() => () => {
    sequenceRef.current += 1
    resumedRef.current = false
    abortRef.current?.abort()
  }, [])

  const active = isActivePhase(phase)
  return useMemo(() => ({
    pointer,
    phase,
    progress,
    elapsedMs,
    result,
    error,
    active,
    resumable: phase === 'interrupted' && !!pointer,
    stage,
    resume,
  }), [active, elapsedMs, error, phase, pointer, progress, result, resume, stage])
}
