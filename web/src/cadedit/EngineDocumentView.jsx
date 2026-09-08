/**
 * W4f slice A0: the engine document on the canvas.
 *
 * While the browser engine holds an imported DXF, the console's viewer keeps
 * drawing the console's own drawing, so the prompts had nothing on screen to
 * point at. This consumer (mounted inside the ONE EngineSessionProvider, like
 * every cadedit surface) maps the session's entities into the viewer's intake
 * shape (engineIntake.js) and hands it to the viewer's own `applyVersion`,
 * the same seam a backend version push uses; when the document closes or the
 * worker dies it hands back `null`, and the console drawing returns. It
 * renders nothing, owns no session, constructs no boundary.
 *
 * Bounded: the mapper caps points and reports truncation; the viewer is
 * touched only when the entity list or the document actually changed.
 */
import { useEffect, useRef } from 'react'

import { engineIntake, hexHandle } from './engineIntake.js'
import { SESSION_ERROR } from './engineSession.js'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'

export default function EngineDocumentView({ viewerRef = null, onShown = null, selectedHandle = null, onSelectedHandleChange = null }) {
  const { session, highlightedIds } = useEngineSessionContext()
  const showing = session.engineParsed && session.errorKind !== SESSION_ERROR.CRASHED
  const entities = showing ? session.entities : null
  const documentId = showing ? session.documentId : ''
  const lastRef = useRef(null)
  // Both mirrors share this ref so each other's writes cannot echo as changes.
  const lastSelectedHandleRef = useRef(undefined)
  const lastEngineSelectionRef = useRef(undefined)
  const wasShownRef = useRef(false)
  /** one-way: console handle -> engine selection, on change */
  useEffect(() => {
    const previous = lastSelectedHandleRef.current
    if (previous === selectedHandle) return
    lastSelectedHandleRef.current = selectedHandle
    if (!entities || (previous === undefined && selectedHandle === null)) return
    if (selectedHandle === null) {
      session.actions.select(null)
      return
    }
    for (const entity of entities) {
      if (hexHandle(entity.id) === selectedHandle) {
        session.actions.select(entity.id)
        return
      }
    }
  }, [selectedHandle, entities, session.actions])
  useEffect(() => {
    const wasShown = wasShownRef.current
    wasShownRef.current = entities !== null
    if (entities === null) {
      if (wasShown && lastSelectedHandleRef.current != null) {
        lastSelectedHandleRef.current = null
        if (typeof onSelectedHandleChange === 'function') onSelectedHandleChange(null)
      }
      return
    }
    const selectedId = session.selectedId
    const previous = lastEngineSelectionRef.current
    lastEngineSelectionRef.current = selectedId
    if (typeof onSelectedHandleChange !== 'function' || selectedId === previous) return
    if (typeof selectedId === 'string' && selectedId !== '') {
      const hex = hexHandle(selectedId)
      if (hex === lastSelectedHandleRef.current) return
      lastSelectedHandleRef.current = hex
      onSelectedHandleChange(hex)
    } else if (selectedId === null || (selectedId === '' && typeof previous === 'string' && previous !== '')) {
      if (lastSelectedHandleRef.current === null) return
      lastSelectedHandleRef.current = null
      onSelectedHandleChange(null)
    }
  }, [session.selectedId, entities, onSelectedHandleChange])
  // The latest onShown, so the unmount cleanup (a closure from the first
  // render) tells the host the stamp is gone (kimi, #969).
  const onShownRef = useRef(onShown)
  onShownRef.current = onShown
  useEffect(() => {
    const viewer = viewerRef?.current
    if (!viewer || typeof viewer.applyVersion !== 'function') return undefined
    if (!entities) {
      if (lastRef.current !== null) {
        lastRef.current = null
        viewer.applyVersion(null)
        onShown?.(null)
      }
      return undefined
    }
    if (lastRef.current === entities) return undefined
    lastRef.current = entities
    const intake = engineIntake(entities, documentId)
    viewer.applyVersion(intake)
    viewer.setHighlight?.(Array.from(highlightedIds || []))
    onShown?.(intake)
    return undefined
  }, [viewerRef, entities, documentId, onShown, highlightedIds])
  // Unmount (the surface leaves): the console drawing comes back.
  useEffect(() => () => {
    if (lastRef.current === null) return
    lastRef.current = null
    const viewer = viewerRef?.current
    if (viewer && typeof viewer.applyVersion === 'function') viewer.applyVersion(null)
    onShownRef.current?.(null)
  }, [viewerRef])
  return null
}
