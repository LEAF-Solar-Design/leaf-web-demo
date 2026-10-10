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

// Why an authoring request could not start, for the failures the panel has no
// calmer surface for. Read from structured fields only: never from the server's
// message text and never from the client's own grantRequired heuristic. The
// daily authoring quota is left alone on purpose: its gate carries the numbers.
const AUTHOR_START_REASONS = Object.freeze({
  grant: 'Link your Claude account to start authoring.',
  quota: 'Your Claude usage limit has been reached; try again when usage is available.',
  rate_limited: 'Authoring is temporarily rate limited; wait before trying again.',
  auth: 'Sign in to Leaf again to start authoring.',
  network: 'Could not reach the authoring service; check your connection and try again.',
  transport: 'The authoring service is temporarily unavailable; try again.',
})

// One frozen record per kind, shared by every failure of that kind.
const AUTHOR_START_FAILURES = Object.freeze(Object.fromEntries(
  Object.entries(AUTHOR_START_REASONS).map(([kind, message]) => [kind, Object.freeze({ kind, message })]),
))

// The reasons a 502, 503 or 504 may name and still be a passing outage. Any
// other named reason keeps the server's own sentence: it describes a fault
// that trying again does not clear.
const TRANSIENT_START_CODES = Object.freeze(['customization_harness_unavailable', 'customization_stage_failed'])

// The record is kept beside the error, never on it: a frozen error must not
// throw here, and an error object thrown a second time must not keep the
// record of its first throw.
const authorStartFailures = new WeakMap()

// Read-only: the start failure the controller recorded for this exact error
// object, or null. A property set on an error by anyone else is never read.
export function authorStartFailureOf(error) {
  if (error === null || (typeof error !== 'object' && typeof error !== 'function')) return null
  return authorStartFailures.get(error) || null
}

function classifyAuthorStartFailure(error) {
  if (!error || error.secretRefused === true || error.name === 'AbortError'
    || error.reasonCode === 'signed-out-demo') return null
  const body = error.body && typeof error.body === 'object' ? error.body : null
  const nested = body?.error && typeof body.error === 'object' ? body.error : null
  const codes = [
    error.errorCode, error.code, error.reasonCode,
    body?.errorCode, body?.error_code, body?.reason, body?.reason_code,
    nested?.error_code, nested?.code, nested?.reason_code,
  ].filter((value) => typeof value === 'string').map((value) => value.toLowerCase())
  const has = (code) => codes.includes(code)
  // The reason a refusal names for itself, apart from its error class.
  const reasons = [error.reasonCode, body?.reason, body?.reason_code, nested?.reason_code]
    .filter((value) => typeof value === 'string').map((value) => value.toLowerCase())
  const status = Number(error.status)
  if (has('customization_auth_required')) return null
  if (body?.grant_required === true || nested?.grant_required === true || has('grant_required')) {
    return AUTHOR_START_FAILURES.grant
  }
  if (status === 429 && [body?.quota_kind, nested?.quota_kind]
    .some((value) => typeof value === 'string' && value.toLowerCase() === 'daily_author')) return null
  if (has('llm_quota_exhausted')) return AUTHOR_START_FAILURES.quota
  if (has('llm_rate_limited')) return AUTHOR_START_FAILURES.rate_limited
  if (status === 401) return AUTHOR_START_FAILURES.auth
  if ([502, 503, 504].includes(status)
    || ['broker_unreachable', 'service_unavailable', 'harness_unreachable'].some(has)) {
    return reasons.every((reason) => TRANSIENT_START_CODES.includes(reason))
      ? AUTHOR_START_FAILURES.transport : null
  }
  if ((!Number.isFinite(status) || status === 0)
    && /failed to fetch|fetch failed|networkerror|network error|load failed|err_(connection|network)|econnrefused|connection refused/i
      .test(String(error.message || ''))) {
    return AUTHOR_START_FAILURES.network
  }
  return null
}

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
  // sends no new POST). When supplied, it must resolve a real turn before POST.
  authorityProvider,
} = {}) {
  const storageRef = useRef(storage)
  const accountScope = authorAccountScope(config.tenant, storage)
  const initialPointer = mock ? null : readInflightAuthor(storage)
  const validInitialPointer = authorPointerValid(initialPointer, accountScope) ? initialPointer : null
  if (initialPointer && !validInitialPointer) clearInflightAuthor(null, storage)
  const [pointer, setPointer] = useState(validInitialPointer)
  const [phase, setPhase] = useState(pointer?.draft_only ? 'draft' : pointer?.terminal_failed ? 'failed' : pointer ? 'reconnecting' : 'idle')
  const [progress, setProgress] = useState(pointer?.draft_only ? null : pointer?.terminal_failed ? 'request failed' : pointer ? 'restoring authoring request' : null)
  const [elapsedMs, setElapsedMs] = useState(pointer && !pointer.terminal_failed && !pointer.draft_only ? Math.max(0, Date.now() - pointer.created_at) : 0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(pointer?.terminal_failed ? restoredFailure(pointer) : null)
  const sequenceRef = useRef(0)
  const resumedRef = useRef(false)
  const abortRef = useRef(null)
  const pointerRef = useRef(pointer)
  const runningRef = useRef(false)
  const preparedSubmissionRef = useRef(null)
  // The authority minted for a request, kept with that request's idempotency
  // key. The server binds a request to the turn that first sent it, so a
  // repeat of the same request must carry that same authority: a new turn
  // could never replay it, and starting one would only add a second turn. It
  // is matched by key, so it can never serve another request; the next mint
  // replaces it. It lives in memory only: after a reload nothing is held.
  const heldAuthorityRef = useRef(null)
  storageRef.current = storage

  const updatePointer = useCallback((next) => {
    pointerRef.current = next
    setPointer(next)
    return next
  }, [])

  const persist = useCallback((next) => {
    saveInflightAuthor(next, storageRef.current)
    updatePointer(next)
    return next
  }, [updatePointer])

  const runPointer = useCallback(async (initial, { reconnecting = false, allowSecretOnce = false } = {}) => {
    if (!initial) return null
    if (initial.draft_only) {
      setPhase('draft')
      setProgress(null)
      setResult(null)
      return null
    }
    let acceptedPointer = initial
    abortRef.current?.abort()
    const abortController = new AbortController()
    abortRef.current = abortController
    const sequence = ++sequenceRef.current
    runningRef.current = true
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
      let minted = false
      const held = heldAuthorityRef.current
      if (authorityProvider && !initial.poll_url && held?.key === initial.idempotency_key) {
        // A repeat of a request this controller already minted for (its first
        // POST may or may not have landed): the same authority, no new turn.
        authority = held.authority
      } else if (authorityProvider && !initial.poll_url) {
        try {
          // The one-call credential override also applies to the turn start.
          authority = (await authorityProvider(initial.description, { allowSecretOnce, ...(initial.prior_staged ? { forceFresh: true } : {}) })) || null
          if (!authority?.sessionId || !authority?.turnId) {
            const failure = new Error(mock
              ? 'Tool building is unavailable in this signed-out demo.'
              : 'Could not start authoring in this conversation. Wait for the current turn to finish, then try again.')
            if (mock) failure.reasonCode = 'signed-out-demo'
            throw failure
          }
          minted = true
        } catch (cause) {
          // No stage POST has happened, so do not retain a pointer that would
          // silently start another turn on reconnect. A new click may retry.
          const failure = cause instanceof Error ? cause : new Error(String(cause))
          failure.authorTerminal = true
          failure.description = initial.description
          throw failure
        }
      }
      if (sequenceRef.current !== sequence || abortController.signal.aborted) return null
      // Held only by the run that is still current: an authority that arrives
      // for a superseded run must not replace the one a newer run already sent.
      if (minted) heldAuthorityRef.current = { key: initial.idempotency_key, authority }
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
            if (mock) updatePointer(acceptedPointer)
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
      if (mock) updatePointer(terminalPointer)
      else persist(terminalPointer)
      setPhase('succeeded')
      setProgress('staged for review')
      setResult(staged)
      return staged
    } catch (cause) {
      if (sequenceRef.current !== sequence) return null
      // Only a request that never reached a durable job is a start failure. Any
      // record from an earlier throw of this same object is dropped first.
      // Classifying never decides how the request settles: a failure whose
      // fields cannot be read stays unclassified and settles as it always did.
      let failure = null
      try {
        failure = cause instanceof Error ? cause : new Error(String(cause))
        authorStartFailures.delete(failure)
        if (!acceptedPointer.poll_url && !abortController.signal.aborted) {
          const startFailure = classifyAuthorStartFailure(failure)
          if (startFailure) authorStartFailures.set(failure, startFailure)
        }
      } catch { /* left unclassified */ }
      const terminal = !!cause?.authorTerminal
      if (terminal) {
        const acceptedFailure = acceptedPointer.change_set_id && acceptedPointer.poll_url
          && cause?.body?.change_set_id === acceptedPointer.change_set_id
          && ['failed', 'error'].includes(String(cause?.body?.status || '').toLowerCase())
        if (acceptedFailure) {
          const failedPointer = {
            ...acceptedPointer,
            terminal_failed: true,
            failed_at: acceptedPointer.failed_at || Date.now(),
            failure: boundedAuthorFailure(cause),
          }
          if (mock) updatePointer(failedPointer)
          else persist(failedPointer)
        } else if (initial.prior_staged) {
          const now = Date.now()
          const prepared = {
            idempotency_key: requestId(), description: initial.description,
            target_tool_name: initial.target_tool_name, account_scope: accountScope,
            created_at: now, expires_at: now + AUTHOR_POINTER_TTL_MS,
            change_set_id: null, poll_url: null, retry_after_ms: null,
            draft_only: true, prior_staged: initial.prior_staged,
          }
          if (mock) updatePointer(prepared)
          else persist(prepared)
        } else {
          // A quota refusal or invalid response is not an accepted failed job.
          if (!mock) clearInflightAuthor(acceptedPointer.idempotency_key, storageRef.current)
          updatePointer(null)
        }
        setPhase(!acceptedFailure && initial.prior_staged ? 'draft' : 'failed')
        setProgress(!acceptedFailure && initial.prior_staged ? null : 'request failed')
      } else {
        setPhase('interrupted')
        setProgress('connection interrupted')
      }
      setError(failure || (cause instanceof Error ? cause : new Error(String(cause))))
      return null
    } finally {
      if (sequenceRef.current === sequence) runningRef.current = false
    }
  }, [accountScope, authorityProvider, mock, persist, stageAuthorTool, updatePointer])

  const stage = useCallback((description, targetToolName = null, { allowSecretOnce = false, newAttempt = false } = {}) => {
    if (!enabled) return Promise.resolve(null)
    // THE STORAGE BOUNDARY, guarded by the same seam the transport uses.
    // stageAuthorTool is the authority and refuses this text on the wire, but
    // this function writes the description to localStorage FIRST (the durable
    // authoring pointer), and a credential must not land in storage either.
    // Same decision, same frozen copy, same typed throw — not a second policy.
    const guard = guardedText(description, { allowSecretOnce, credentialMountAvailable: !mock })
    if (!guard.ok) return Promise.reject(new SecretRefusedError(guard.refusal))
    if (preparedSubmissionRef.current) return preparedSubmissionRef.current
    const current = mock ? pointerRef.current : readInflightAuthor(storageRef.current)
    const validCurrent = authorPointerValid(current, accountScope)
    if (validCurrent && current.draft_only) {
      if ((targetToolName || null) !== (current.target_tool_name || null)) return Promise.resolve(null)
      const next = { ...current, description }
      delete next.draft_only
      resumedRef.current = true
      if (mock) updatePointer(next)
      else persist(next)
      const submission = runPointer(next, { allowSecretOnce })
      const coalesced = submission.finally(() => {
        if (preparedSubmissionRef.current === coalesced) preparedSubmissionRef.current = null
      })
      preparedSubmissionRef.current = coalesced
      return coalesced
    }
    if (validCurrent && !(current.terminal_failed && newAttempt)) {
      // A valid pointer that never got its poll_url (its first POST may not
      // have landed) is re-run here with the authority this controller minted
      // for it. When nothing is held (after a reload) its mint runs again, and
      // the caller's live override rides along, or a Send-anyway re-stage
      // would be refused at the mint and swallowed into a null authority. The
      // mount-time resumes below carry no override on purpose: nobody
      // consented on that call.
      return runPointer(current, { reconnecting: true, allowSecretOnce })
    }
    if (!mock && current && !validCurrent) clearInflightAuthor(null, storageRef.current)
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
      ...(validCurrent && current.prior_staged ? { prior_staged: current.prior_staged } : {}),
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
    else updatePointer(next)
    return runPointer(next, { allowSecretOnce })
  }, [accountScope, enabled, mock, persist, runPointer, updatePointer])

  const reviseDraft = useCallback((expectedKey) => {
    if (!enabled || runningRef.current) return null
    const current = mock ? pointerRef.current : readInflightAuthor(storageRef.current)
    const receipt = current?.staged_result?.receipt
    if (!authorPointerValid(current, accountScope) || current.idempotency_key !== expectedKey
      || !current.terminal_staged || !receipt || typeof receipt !== 'object' || Array.isArray(receipt)
      || typeof receipt.change_set_id !== 'string' || !receipt.change_set_id) return null
    sequenceRef.current += 1
    abortRef.current?.abort()
    resumedRef.current = true
    const now = Date.now()
    const next = {
      idempotency_key: requestId(), description: current.description,
      target_tool_name: current.target_tool_name || null, account_scope: accountScope,
      created_at: now, expires_at: now + AUTHOR_POINTER_TTL_MS,
      change_set_id: null, poll_url: null, retry_after_ms: null, draft_only: true,
      prior_staged: { idempotency_key: current.idempotency_key, receipt },
    }
    if (mock) updatePointer(next)
    else persist(next)
    setResult(null); setError(null); setProgress(null); setElapsedMs(0); setPhase('draft')
    return next
  }, [accountScope, enabled, mock, persist, updatePointer])

  const discardDraft = useCallback((expectedKey) => {
    if (!enabled || runningRef.current) return false
    const current = mock ? pointerRef.current : readInflightAuthor(storageRef.current)
    const receipt = current?.staged_result?.receipt
    const staged = current?.terminal_staged && receipt && typeof receipt === 'object' && !Array.isArray(receipt)
      && typeof receipt.change_set_id === 'string' && !!receipt.change_set_id
    if (!authorPointerValid(current, accountScope) || current.idempotency_key !== expectedKey
      || !(current.draft_only || staged)) return false
    if (!mock && !clearInflightAuthor(expectedKey, storageRef.current)) return false
    sequenceRef.current += 1
    abortRef.current?.abort()
    resumedRef.current = true
    updatePointer(null)
    setResult(null); setError(null); setProgress(null); setElapsedMs(0); setPhase('idle')
    return true
  }, [accountScope, enabled, mock, updatePointer])

  const resume = useCallback(() => {
    if (!enabled) return Promise.resolve(null)
    const saved = pointerRef.current || (mock ? null : readInflightAuthor(storageRef.current))
    const current = authorPointerValid(saved, accountScope) ? saved : null
    if (!mock && saved && !current) clearInflightAuthor(null, storageRef.current)
    return current ? runPointer(current, { reconnecting: true }) : Promise.resolve(null)
  }, [accountScope, enabled, mock, runPointer])

  const checkStatus = useCallback(() => {
    if (!enabled || !pointer?.terminal_failed || !pointer.poll_url
      || !authorPointerValid(pointer, accountScope)) return Promise.resolve(null)
    return runPointer({ ...pointer, terminal_failed: false }, { reconnecting: true })
  }, [accountScope, enabled, pointer, runPointer])

  const completePublication = useCallback(() => {
    if (!mock && pointer) clearInflightAuthor(pointer.idempotency_key, storageRef.current)
    updatePointer(null)
  }, [mock, pointer, updatePointer])

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
    enabled,
    draftOnly: !!pointer?.draft_only,
    previousStagedReceipt: pointer?.prior_staged?.receipt || null,
    resumable: phase === 'interrupted' && !!pointer,
    failedRequest: pointer?.terminal_failed ? {
      idempotency_key: pointer.idempotency_key,
      change_set_id: pointer.change_set_id,
      poll_url: pointer.poll_url,
      failed_at: pointer.failed_at,
      failure: pointer.failure,
    } : null,
    stage,
    reviseDraft,
    discardDraft,
    resume,
    checkStatus,
    completePublication,
  }), [active, checkStatus, completePublication, discardDraft, elapsedMs, enabled, error, phase, pointer, progress, result, resume, reviseDraft, stage])
}
