import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { config, stageAuthorTool as defaultStageAuthorTool } from '../api.js'
import { SecretRefusedError, guardedText } from '../lib/secretGuardTransport.js'
import {
  AUTHOR_POINTER_TTL_MS,
  authorAccountScope,
  authorPointerValid,
  boundedAuthorFailure,
  clearInflightAuthor,
  readInflightAuthor,
  saveInflightAuthor,
} from '../authorStagePointer.js'

export { INFLIGHT_AUTHOR_KEY, clearInflightAuthor, readInflightAuthor } from '../authorStagePointer.js'

function requestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `leaf-author-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function isActivePhase(phase) {
  return ['submitting', 'accepted', 'submitted', 'queued', 'pending', 'running', 'authoring', 'reconnecting'].includes(phase)
}

function stagedHandoff(staged) {
  if (!staged) return null
  const keys = ['receipt', 'tool', 'preview', 'code', 'source', 'static_scan', 'validation', 'diff_summary', 'diff', 'telemetry', 'fallback']
  return Object.fromEntries(keys.filter((key) => staged[key] !== undefined).map((key) => [key, staged[key]]))
}

function restoredFailure(pointer) {
  const error = new Error(pointer.failure.message)
  error.authorTerminal = true
  error.restored = true
  error.code = pointer.failure.reason_code
  return error
}

export default function useAuthorStageController({
  mock = false,
  enabled = true,
  storage,
  stageAuthorTool = defaultStageAuthorTool,
  // Optional turn-authority provider: async () => ({ sessionId, turnId } | null).
  // Called once per initial stage submission (never on a poll/reconnect, which
  // sends no new POST). A null/thrown result proceeds WITHOUT authority headers
  // -- the server still fail-closes on its own; this never invents a client-side
  // refusal.
  authorityProvider,
} = {}) {
  const storageRef = useRef(storage)
  const accountScope = authorAccountScope(config.tenant, storage)
  const initialPointer = mock ? null : readInflightAuthor(storage)
  const validInitialPointer = authorPointerValid(initialPointer, accountScope) ? initialPointer : null
  if (initialPointer && !validInitialPointer) clearInflightAuthor(null, storage)
  const [pointer, setPointer] = useState(validInitialPointer)
  const [phase, setPhase] = useState(pointer?.terminal_failed ? 'failed' : pointer ? 'reconnecting' : 'idle')
  const [progress, setProgress] = useState(pointer?.terminal_failed ? 'request failed' : pointer ? 'restoring authoring request' : null)
  const [elapsedMs, setElapsedMs] = useState(pointer && !pointer.terminal_failed ? Math.max(0, Date.now() - pointer.created_at) : 0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(pointer?.terminal_failed ? restoredFailure(pointer) : null)
  const sequenceRef = useRef(0)
  const resumedRef = useRef(false)
  const abortRef = useRef(null)
  storageRef.current = storage

  const persist = useCallback((next) => {
    saveInflightAuthor(next, storageRef.current)
    setPointer(next)
    return next
  }, [])

  const runPointer = useCallback(async (initial, { reconnecting = false, allowSecretOnce = false } = {}) => {
    if (!initial) return null
    let acceptedPointer = initial
    abortRef.current?.abort()
    const abortController = new AbortController()
    abortRef.current = abortController
    const sequence = ++sequenceRef.current
    setResult(null)
    setError(null)
    setPhase(reconnecting ? 'reconnecting' : 'submitting')
    setProgress(reconnecting ? 'restoring authoring request' : 'submitting authoring request')
    try {
      if (initial.terminal_staged && initial.staged_result) {
        setPhase('succeeded')
        setProgress('staged for review')
        setResult(initial.staged_result)
        return initial.staged_result
      }
      if (initial.terminal_failed) {
        setPhase('failed')
        setProgress('request failed')
        setError(restoredFailure(initial))
        return null
      }
      // Only the initial POST carries authority; a poll/reconnect (poll_url
      // already set) never re-submits, so skip minting/reusing a turn for it.
      let authority = null
      if (authorityProvider && !initial.poll_url) {
        try {
          // `allowSecretOnce` MUST reach the mint too: an AuthorPanel "Send
          // anyway" re-stages credential-shaped text with the override, and a
          // provider that guards its own POST (the converse turn start does)
          // would otherwise refuse the mint itself and swallow it into a
          // silent no-authority fallback — the override never actually landed.
          authority = (await authorityProvider(initial.description, { allowSecretOnce })) || null
        } catch {
          authority = null
        }
      }
      const staged = await stageAuthorTool(
        mock,
        initial.description,
        initial.target_tool_name || null,
        {
          allowSecretOnce,
          idempotencyKey: initial.idempotency_key,
          pollUrl: initial.poll_url || null,
          changeSetId: initial.change_set_id || null,
          retryAfterMs: initial.retry_after_ms || null,
          authority,
          signal: abortController.signal,
          onAccepted: (accepted) => {
            if (sequenceRef.current !== sequence) return
            acceptedPointer = {
              ...initial,
              change_set_id: accepted.change_set_id,
              poll_url: accepted.poll_url,
              retry_after_ms: accepted.retry_after_ms,
            }
            if (mock) setPointer(acceptedPointer)
            else persist(acceptedPointer)
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
      const terminalPointer = { ...acceptedPointer, terminal_failed: false, terminal_staged: true, staged_result: stagedHandoff(staged) }
      if (mock) setPointer(terminalPointer)
      else persist(terminalPointer)
      setPhase('succeeded')
      setProgress('staged for review')
      setResult(staged)
      return staged
    } catch (cause) {
      if (sequenceRef.current !== sequence) return null
      const terminal = !!cause?.authorTerminal
      if (terminal) {
        const failedPointer = {
          ...acceptedPointer,
          terminal_failed: true,
          failed_at: acceptedPointer.failed_at || Date.now(),
          failure: boundedAuthorFailure(cause),
        }
        if (mock) setPointer(failedPointer)
        else persist(failedPointer)
        setPhase('failed')
        setProgress('request failed')
      } else {
        setPhase('interrupted')
        setProgress('connection interrupted')
      }
      setError(cause instanceof Error ? cause : new Error(String(cause)))
      return null
    }
  }, [authorityProvider, mock, persist, stageAuthorTool])

  const stage = useCallback((description, targetToolName = null, { allowSecretOnce = false, newAttempt = false } = {}) => {
    if (!enabled) return Promise.resolve(null)
    // THE STORAGE BOUNDARY, guarded by the same seam the transport uses.
    // stageAuthorTool is the authority and refuses this text on the wire, but
    // this function writes the description to localStorage FIRST (the durable
    // authoring pointer), and a credential must not land in storage either.
    // Same decision, same frozen copy, same typed throw — not a second policy.
    const guard = guardedText(description, { allowSecretOnce, credentialMountAvailable: !mock })
    if (!guard.ok) return Promise.reject(new SecretRefusedError(guard.refusal))
    const current = readInflightAuthor(storageRef.current)
    const validCurrent = authorPointerValid(current, accountScope)
    if (validCurrent && !(current.terminal_failed && newAttempt)) {
      // A valid pointer that never got its poll_url (the first POST did not
      // land) is re-run here, and its mint runs again: the caller's live
      // override rides along, or a Send-anyway re-stage would be refused at
      // the mint and swallowed into a null authority. The mount-time resumes
      // below carry no override on purpose: nobody consented on that call.
      return runPointer(current, { reconnecting: true, allowSecretOnce })
    }
    if (current && !validCurrent) clearInflightAuthor(null, storageRef.current)
    resumedRef.current = true
    const next = {
      idempotency_key: requestId(),
      description,
      target_tool_name: targetToolName || null,
      change_set_id: null,
      poll_url: null,
      retry_after_ms: null,
      created_at: Date.now(),
      expires_at: Date.now() + AUTHOR_POINTER_TTL_MS,
      account_scope: accountScope,
      ...(validCurrent && current.terminal_failed && current.change_set_id && current.poll_url ? {
        prior_failure: {
          idempotency_key: current.idempotency_key,
          change_set_id: current.change_set_id,
          poll_url: current.poll_url,
          failed_at: current.failed_at,
        },
      } : {}),
    }
    if (!mock) persist(next)
    else setPointer(next)
    return runPointer(next, { allowSecretOnce })
  }, [accountScope, enabled, mock, persist, runPointer])

  const resume = useCallback(() => {
    if (!enabled) return Promise.resolve(null)
    const saved = pointer || readInflightAuthor(storageRef.current)
    const current = authorPointerValid(saved, accountScope) ? saved : null
    if (saved && !current) clearInflightAuthor(null, storageRef.current)
    return current ? runPointer(current, { reconnecting: true }) : Promise.resolve(null)
  }, [accountScope, enabled, pointer, runPointer])

  const checkStatus = useCallback(() => {
    if (!enabled || !pointer?.terminal_failed || !pointer.poll_url
      || !authorPointerValid(pointer, accountScope)) return Promise.resolve(null)
    return runPointer({ ...pointer, terminal_failed: false }, { reconnecting: true })
  }, [accountScope, enabled, pointer, runPointer])

  const completePublication = useCallback(() => {
    if (pointer) clearInflightAuthor(pointer.idempotency_key, storageRef.current)
    setPointer(null)
  }, [pointer])

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
    failedRequest: pointer?.terminal_failed ? {
      idempotency_key: pointer.idempotency_key,
      change_set_id: pointer.change_set_id,
      poll_url: pointer.poll_url,
      failed_at: pointer.failed_at,
      failure: pointer.failure,
    } : null,
    stage,
    resume,
    checkStatus,
    completePublication,
  }), [active, checkStatus, completePublication, elapsedMs, error, phase, pointer, progress, result, resume, stage])
}
