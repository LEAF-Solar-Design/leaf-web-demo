import { useCallback, useEffect, useRef, useState } from 'react'
import {
  classifyAgentError,
  ensureSession,
  postMessage,
  resetSession,
} from '../converse.js'

export default function useConverseSessionController({ drawingId, retryNotFound = false }) {
  const [sessionId, setSessionId] = useState(null)
  const [turns, setTurns] = useState([])
  const sessionRef = useRef(null)
  const drawingRef = useRef(drawingId)

  useEffect(() => {
    const previousDrawingId = drawingRef.current
    drawingRef.current = drawingId
    if (previousDrawingId === drawingId) return
    resetSession(previousDrawingId)
    sessionRef.current = null
    setSessionId(null)
    setTurns([])
  }, [drawingId])

  const attach = useCallback(async () => {
    const requestedDrawingId = drawingId
    const next = await ensureSession(drawingId)
    if (drawingRef.current !== requestedDrawingId) {
      throw new Error('Drawing changed while starting the conversation')
    }
    sessionRef.current = next.session_id
    setSessionId(next.session_id)
    return next.session_id
  }, [drawingId])

  const clear = useCallback(() => {
    sessionRef.current = null
    setSessionId(null)
    setTurns([])
  }, [])

  const resetCached = useCallback(() => {
    resetSession(drawingId)
  }, [drawingId])

  const startTurn = useCallback(async (text, classifierHint) => {
    const send = async (id) => {
      const response = await postMessage(id, { text, classifier_hint: classifierHint })
      setTurns((current) => [...current, { turnId: response.turn_id, text }])
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

  return { sessionId, turns, startTurn, clear, resetCached }
}
