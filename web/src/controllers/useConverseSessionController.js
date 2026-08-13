import { useCallback, useEffect, useRef, useState } from 'react'
import {
  classifyAgentError,
  ensureSession,
  postMessage,
  projectActivityProjection,
  resetSession,
} from '../converse.js'

const EMPTY_ACTIVITY = Object.freeze({ queued: 0, executing: 0, total: 0 })

export function createRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  const bytes = new Uint8Array(16)
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes)
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export default function useConverseSessionController({ drawingId, retryNotFound = false }) {
  const [sessionId, setSessionId] = useState(null)
  const [turns, setTurns] = useState([])
  const [activeRequests, setActiveRequests] = useState(EMPTY_ACTIVITY)
  const [requestStatus, setRequestStatus] = useState(null)
  const sessionRef = useRef(null)
  const drawingRef = useRef(drawingId)
  const projectRef = useRef(null)

  useEffect(() => {
    const previousDrawingId = drawingRef.current
    drawingRef.current = drawingId
    if (previousDrawingId === drawingId) return
    resetSession(previousDrawingId)
    sessionRef.current = null
    setSessionId(null)
    setTurns([])
    setActiveRequests(EMPTY_ACTIVITY)
    setRequestStatus(null)
  }, [drawingId])

  const setProjectContext = useCallback((projectId) => {
    const nextProjectId = projectId || null
    if (projectRef.current === nextProjectId) return
    projectRef.current = nextProjectId
    sessionRef.current = null
    setSessionId(null)
    setTurns([])
    setActiveRequests(EMPTY_ACTIVITY)
    setRequestStatus(null)
  }, [])

  const attach = useCallback(async () => {
    const requestedDrawingId = drawingId
    const requestedProjectId = projectRef.current
    const next = await ensureSession(drawingId, requestedProjectId)
    if (drawingRef.current !== requestedDrawingId || projectRef.current !== requestedProjectId) {
      throw new Error('Project or drawing changed while starting the conversation')
    }
    sessionRef.current = next.session_id
    setSessionId(next.session_id)
    if (next.active_requests && typeof next.active_requests === 'object') {
      setActiveRequests(projectActivityProjection(next.active_requests))
    }
    setRequestStatus(next.status || null)
    return next.session_id
  }, [drawingId])

  const clear = useCallback(() => {
    sessionRef.current = null
    setSessionId(null)
    setTurns([])
    setActiveRequests(EMPTY_ACTIVITY)
    setRequestStatus(null)
  }, [])

  const resetCached = useCallback(() => {
    resetSession(drawingId, projectRef.current)
  }, [drawingId])

  const startTurn = useCallback(async (text, classifierHint) => {
    const requestedProjectId = projectRef.current
    const requestId = requestedProjectId ? createRequestId() : null
    const send = async (id) => {
      if (projectRef.current !== requestedProjectId) {
        throw new Error('Project changed while starting the conversation')
      }
      const payload = { text, classifier_hint: classifierHint }
      if (requestedProjectId) {
        payload.queue = true
        payload.request_id = requestId
      }
      const response = await postMessage(id, payload)
      if (projectRef.current !== requestedProjectId) {
        throw new Error('Project changed while starting the conversation')
      }
      if (response.active_requests && typeof response.active_requests === 'object') {
        setActiveRequests(projectActivityProjection(response.active_requests))
      }
      setRequestStatus(response.status || null)
      setTurns((current) => [...current, {
        turnId: response.turn_id || null,
        requestId,
        status: response.status || null,
        text,
      }])
      return response
    }

    const current = sessionRef.current || (await attach())
    try {
      return await send(current)
    } catch (error) {
      if (!retryNotFound || classifyAgentError(error) !== 'not_found') throw error
      resetCached()
      sessionRef.current = null
      setSessionId(null)
      return send(await attach())
    }
  }, [attach, resetCached, retryNotFound])

  return {
    sessionId,
    turns,
    activeRequests,
    requestStatus,
    startTurn,
    clear,
    resetCached,
    setProjectContext,
  }
}
