import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import useConverseSessionController from './useConverseSessionController.js'
import useDrawingVersionController from './useDrawingVersionController.js'

const WorkspaceControllerContext = createContext(null)

export function WorkspaceControllerProvider({ drawingId, drawingOptions = {}, retryNotFound = false, children }) {
  const instanceIdRef = useRef(null)
  if (!instanceIdRef.current) {
    instanceIdRef.current = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
  }
  const [drawingEvent, setDrawingEvent] = useState(null)
  const [drawingError, setDrawingError] = useState(null)
  const onVersionEvent = useCallback((event) => {
    drawingOptions.onVersionEvent?.(event)
    setDrawingEvent({ ...event, sequence: Date.now() + Math.random() })
  }, [drawingOptions])
  const onError = useCallback((error, context) => {
    drawingOptions.onError?.(error, context)
    setDrawingError({ error, context, sequence: Date.now() + Math.random() })
  }, [drawingOptions])
  const converse = useConverseSessionController({ drawingId, retryNotFound })
  const drawing = useDrawingVersionController({
    ...drawingOptions,
    onVersionEvent,
    onError,
  })
  const value = useMemo(() => ({
    converse,
    bindConverseProject: converse.setProjectContext,
    drawing,
    drawingEvent,
    drawingError,
    instanceId: instanceIdRef.current,
  }), [converse, drawing, drawingError, drawingEvent])
  return (
    <WorkspaceControllerContext.Provider value={value}>
      {children}
    </WorkspaceControllerContext.Provider>
  )
}

export function useWorkspaceControllers() {
  const value = useContext(WorkspaceControllerContext)
  if (!value) throw new Error('useWorkspaceControllers must be used inside WorkspaceControllerProvider')
  return value
}
