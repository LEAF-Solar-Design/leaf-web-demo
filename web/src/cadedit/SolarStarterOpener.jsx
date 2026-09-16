import { useEffect, useRef } from 'react'
import { REACH_STATE, useEngineSessionContext } from './EngineSessionProvider.jsx'
import { SESSION_ERROR } from './engineSessionErrors.js'

export const SOLAR_STARTER_DOCUMENT_ID = 'solar-starter.dxf'
// The bundled intake's top-level shape, with no console drawing or geometry.
export const SOLAR_STARTER_EMPTY_INTAKE = Object.freeze({
  dwg: SOLAR_STARTER_DOCUMENT_ID,
  layers: Object.freeze([]), polylines: Object.freeze([]), inserts: Object.freeze([]),
  faces3d: Object.freeze([]), blockdefs: Object.freeze({}), geodata: Object.freeze([]),
  images: Object.freeze([]), imageNames: Object.freeze([]),
})

export default function SolarStarterOpener({ enabled = false, drawingSeated = false, fetchDxf = null, retryKey = 0, onStarterState = null }) {
  const { session, setReach } = useEngineSessionContext()
  const latestRef = useRef(null)
  latestRef.current = { session, fetchDxf, onStarterState }
  const generationRef = useRef(0)
  const attemptRef = useRef(null)
  const edgeRef = useRef(0)
  const activeRef = useRef(false)
  const ownsRef = useRef(false)
  const refusedRef = useRef(false)

  useEffect(() => {
    if (enabled) edgeRef.current += 1
  }, [enabled])

  useEffect(() => {
    const report = (state, sentence = '', extra = {}) => {
      setReach({ state, sentence, ...extra })
      latestRef.current.onStarterState?.(state)
    }
    const starterOrEmpty = session.documentId === '' || session.documentId === SOLAR_STARTER_DOCUMENT_ID
    // A foreign operation ends ownership, even while this profile is disabled.
    // A same-name hand import without an intervening foreign document cannot be
    // distinguished: the store exposes a document name, but no operation origin.
    if (!starterOrEmpty) {
      ownsRef.current = false
      refusedRef.current = false
      if (activeRef.current) {
        activeRef.current = false
        report(REACH_STATE.IDLE)
      }
      return undefined
    }
    if (drawingSeated && ownsRef.current && session.documentId === SOLAR_STARTER_DOCUMENT_ID) {
      if (session.dirty) {
        report(REACH_STATE.OPEN, '', { source: 'sample-static' })
      } else if (!session.busy) {
        ownsRef.current = false
        activeRef.current = false
        refusedRef.current = false
        session.actions.reset()
        report(REACH_STATE.IDLE)
      }
      return undefined
    }
    if (!enabled) return undefined
    if (!ownsRef.current && session.documentId === SOLAR_STARTER_DOCUMENT_ID
      && (session.errorKind === SESSION_ERROR.REFUSED || session.errorKind === SESSION_ERROR.CRASHED)) return undefined
    if (ownsRef.current && starterOrEmpty && !session.engineParsed && !refusedRef.current
      && (session.errorKind === SESSION_ERROR.REFUSED || session.errorKind === SESSION_ERROR.CRASHED)) {
      refusedRef.current = true
      report(REACH_STATE.FAILED, 'the rooftop starter could not be opened: the engine refused the document; retry or import a DXF')
    }
    if (!starterOrEmpty || (session.engineParsed && !refusedRef.current)) {
      if (activeRef.current && !ownsRef.current) {
        activeRef.current = false
        report(REACH_STATE.IDLE)
      }
      return undefined
    }
    if (session.busy || session.dirty || typeof latestRef.current.fetchDxf !== 'function') return undefined
    const key = `${edgeRef.current}#${retryKey}`
    if (attemptRef.current === key) return undefined
    attemptRef.current = key
    activeRef.current = true
    ownsRef.current = false
    refusedRef.current = false
    const generation = ++generationRef.current
    let cancelled = false
    let settled = false
    const current = () => !cancelled && generation === generationRef.current
    report(REACH_STATE.OPENING, 'opening the rooftop starter...')
    ;(async () => {
      // StrictMode's first setup is cancelled before it can issue a fetch.
      await Promise.resolve()
      if (!current()) return
      try {
        const answer = await latestRef.current.fetchDxf(SOLAR_STARTER_DOCUMENT_ID)
        if (!current()) return
        const latest = latestRef.current.session
        if ((latest.documentId !== '' && latest.documentId !== SOLAR_STARTER_DOCUMENT_ID) || latest.engineParsed || latest.dirty) {
          settled = true
          activeRef.current = false
          report(REACH_STATE.IDLE)
          return
        }
        if (latest.busy) {
          attemptRef.current = null
          settled = true
          report(REACH_STATE.IDLE)
          return
        }
        if (Object.prototype.toString.call(answer?.bytes) !== '[object Uint8Array]') {
          settled = true
          report(REACH_STATE.FAILED, 'the rooftop starter could not be opened: the sample answered with no document; retry or import a DXF')
          return
        }
        settled = true
        ownsRef.current = true
        latest.actions.openBytes(answer.bytes, SOLAR_STARTER_DOCUMENT_ID)
        report(REACH_STATE.OPEN, '', { source: 'sample-static' })
      } catch (error) {
        if (!current()) return
        settled = true
        ownsRef.current = false
        const reason = Number.isFinite(error?.status) ? `HTTP ${error.status}` : 'fetch failed'
        report(REACH_STATE.FAILED, `the rooftop starter could not be opened: ${reason}; retry or import a DXF`)
      }
    })()
    return () => {
      cancelled = true
      generationRef.current += 1
      if (!settled) attemptRef.current = null
    }
  }, [enabled, drawingSeated, retryKey, session.documentId, session.engineParsed, session.busy, session.dirty, session.errorKind, setReach])

  useEffect(() => () => {
    generationRef.current += 1
    latestRef.current.onStarterState?.(REACH_STATE.IDLE)
  }, [])
  return null
}
