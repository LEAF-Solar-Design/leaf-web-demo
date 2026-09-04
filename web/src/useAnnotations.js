import { useCallback, useEffect, useRef, useState } from 'react'

import {
  acceptAnnotation,
  fetchCurrentAnnotation,
  rejectAnnotation,
  retryAnnotation,
  undoAnnotation,
} from './annotationClient.js'
import { ensureSession, normalizeScope, openStream, sessionCacheKey } from './converse.js'

const EVENT_TYPES = new Set([
  'annotation_previewed',
  'annotation_accepted',
  'annotation_rejected',
  'annotation_retry_previewed',
  'annotation_undo_previewed',
])

function actionKey(prefix) {
  const nonce = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `${prefix}-${nonce}`
}

/**
 * Resolve `session` -> a session id, for the TWO shapes this hook accepts
 * since slice 6b:
 *
 *   useAnnotations(sessionId, ...)          a bare id, exactly as before
 *   useAnnotations({kind, handle, drawingId?}, ...)   a conversation scope
 *
 * A scope is attached with the SAME idempotent `ensureSession` cache the
 * composer uses, so a scope and a bare id for the same conversation resolve to
 * one session and one stream, never two. Until it resolves (and forever, if
 * the attach fails) the hook holds `null`, which is the disabled path it has
 * always had: no fetch, no stream, no annotation card. Fails closed; never
 * throws into the caller's render.
 */
function useResolvedSessionId(session, enabled) {
  const isScope = !!session && typeof session === 'object'
  const scope = isScope ? normalizeScope(session) : null
  // A stable primitive dependency: two structurally equal scope OBJECTS are
  // different identities every render, which would restart the attach on every
  // render. The cache key is what actually names the conversation.
  const scopeKey = scope ? sessionCacheKey(scope) : null
  const [resolved, setResolved] = useState(null)
  useEffect(() => {
    if (!isScope) return undefined
    if (!enabled || !scopeKey || !scope) {
      setResolved(null)
      return undefined
    }
    let live = true
    setResolved(null)
    ensureSession(scope)
      .then((created) => {
        if (live && created && created.session_id) setResolved(created.session_id)
      })
      .catch(() => {
        // The annotation lane degrades to "no card", the same as an
        // unattached session. The composer surfaces the attach failure.
        if (live) setResolved(null)
      })
    return () => { live = false }
    // `scope` is rebuilt from `session` each render; `scopeKey` is its identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isScope, enabled, scopeKey])
  if (!isScope) return typeof session === 'string' && session ? session : null
  return resolved
}

export function useAnnotations(session, { enabled = true } = {}) {
  const sessionId = useResolvedSessionId(session, enabled)
  const [annotation, setAnnotation] = useState(null)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [confirmation, setConfirmation] = useState(null)
  const generationRef = useRef(0)
  const sessionRef = useRef(sessionId)
  const enabledRef = useRef(enabled)
  const actionRef = useRef(false)
  const readSeqRef = useRef(0)

  useEffect(() => {
    generationRef.current += 1
    sessionRef.current = sessionId
    enabledRef.current = enabled
    actionRef.current = false
    setBusy(false)
    setError(null)
    setConfirmation(null)
    setLoaded(false)
    if (!enabled || !sessionId) setAnnotation(null)
  }, [enabled, sessionId])

  const refresh = useCallback(async () => {
    if (!enabledRef.current || !sessionRef.current) return null
    const generation = generationRef.current
    const readSession = sessionRef.current
    const sequence = ++readSeqRef.current
    try {
      const current = await fetchCurrentAnnotation(readSession)
      if (generation !== generationRef.current
          || sequence !== readSeqRef.current
          || !enabledRef.current
          || readSession !== sessionRef.current) return null
      setAnnotation(current)
      setError(null)
      setLoaded(true)
      return current
    } catch {
      if (generation === generationRef.current
          && sequence === readSeqRef.current
          && enabledRef.current
          && readSession === sessionRef.current) {
        setError('Annotation status is unavailable. Nothing changed.')
        setLoaded(true)
      }
      return null
    }
  }, [])

  useEffect(() => {
    if (!enabled || !sessionId) return undefined
    void refresh()
    const stream = openStream(sessionId, 0, {
      onEvent: (envelope) => {
        if (EVENT_TYPES.has(envelope?.type)) void refresh()
      },
    })
    return () => stream.close()
  }, [enabled, refresh, sessionId])

  const act = useCallback(async (action) => {
    if (actionRef.current || !enabledRef.current || !sessionRef.current) return null
    const current = annotation
    if (!current) return null
    actionRef.current = true
    setBusy(true)
    setError(null)
    setConfirmation(null)
    const generation = generationRef.current
    const actionSession = sessionRef.current
    try {
      let result
      if (action === 'accept') {
        result = await acceptAnnotation(current.batchId, actionKey('annotation-decision'))
      } else if (action === 'reject') {
        result = await rejectAnnotation(current.batchId, actionKey('annotation-decision'))
      } else if (action === 'retry') {
        if (!['rejected', 'expired'].includes(current.state)) return null
        result = await retryAnnotation(current.batchId, actionKey('annotation-retry'))
      } else if (action === 'undo') {
        if (current.state !== 'accepted') return null
        result = await undoAnnotation(current.batchId, actionKey('annotation-undo'))
      } else if (action === 'preview') {
        return await refresh()
      } else {
        return null
      }
      if (generation === generationRef.current
          && enabledRef.current
          && actionSession === sessionRef.current) {
        setConfirmation(result)
        await refresh()
      }
      return result
    } catch {
      if (generation === generationRef.current
          && enabledRef.current
          && actionSession === sessionRef.current) {
        setError('Annotation action did not complete. Nothing changed.')
      }
      return null
    } finally {
      if (generation === generationRef.current
          && actionSession === sessionRef.current) {
        actionRef.current = false
        setBusy(false)
      }
    }
  }, [annotation, refresh])

  return {
    annotation,
    loaded,
    busy,
    error,
    confirmation,
    preview: () => act('preview'),
    accept: () => act('accept'),
    reject: () => act('reject'),
    retry: () => act('retry'),
    undo: () => act('undo'),
    reload: refresh,
  }
}
