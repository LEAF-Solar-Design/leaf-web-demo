/**
 * W4g-1b: the console's own drawing opens in the browser engine.
 *
 * Until now the engine only ever received a DXF the drafter imported by
 * hand, so on a fresh visit every Draw/Modify tool was disabled with "opens
 * on an imported DXF". This consumer (mounted inside the ONE
 * EngineSessionProvider, beside EngineDocumentView) fetches the head of the
 * drawing the console shows (`GET /api/drawings/{id}/dxf`, W4g-1a) and hands
 * the bytes to the store's own open path, so the tools go live on the
 * drawing on screen and the canvas switches to the engine's view of it
 * (EngineDocumentView, unchanged). It renders nothing, owns no session,
 * constructs no boundary.
 *
 * Rules, each pinned by engineHeadOpener.test.jsx:
 *   - opens once per (drawing, head) when NO document is in the engine; a
 *     hand import always wins (a late fetch never replaces a document that
 *     appeared while it was in flight);
 *   - the head moving on the server (a tool run, undo/redo, restore) re-opens
 *     the engine's copy ONLY while it holds no unsaved edit; with unsaved
 *     edits it reports `stale` instead and touches nothing;
 *   - every failure is a sentence on the reach state (the ribbon's reason),
 *     never a thrown error or a silent retry loop: one attempt per
 *     (drawing, head), the next head or a reset tries again;
 *   - bounded: the fetch has its own budget and byte cap (api.js); the
 *     store's own size ceiling applies after it.
 */
import { useEffect, useRef } from 'react'

import { REACH_STATE, useEngineSessionContext } from './EngineSessionProvider.jsx'

export { REACH_STATE }

/** The engine document name the opener uses; the ribbon and the e2e read it. */
export function headDocumentId(drawingId, version) {
  return `${drawingId}-v${version}.dxf`
}

const HEAD_DOC = /-v\d+\.dxf$/

export default function EngineHeadOpener({ drawingId = null, enabled = false, headKey = null, fetchDxf = null }) {
  const { session, setReach } = useEngineSessionContext()
  const { openBytes } = session.actions
  // Latest session for the post-await checks (a state read inside the async
  // leg would be the render it was captured in).
  const sessionRef = useRef(session)
  sessionRef.current = session
  // What this opener has opened or tried: one attempt per (drawing, head).
  const attemptRef = useRef('')
  // Bumped on unmount and on every drawing switch; an async leg captured
  // before an await compares and abandons if it moved.
  const generationRef = useRef(0)
  const fetchRef = useRef(fetchDxf)
  fetchRef.current = fetchDxf

  const documentId = session.documentId
  const present = session.engineParsed || session.busy || documentId !== ''
  const holdsHead = HEAD_DOC.test(documentId) && documentId.startsWith(`${drawingId}-v`)
  const dirty = session.dirty === true

  useEffect(() => {
    generationRef.current += 1
    attemptRef.current = ''
  }, [drawingId])

  useEffect(() => {
    if (!enabled || !drawingId || typeof fetchRef.current !== 'function') return undefined
    // A hand-imported document is never replaced, and the reach reads idle
    // while it is open (the head's sentence would be stale under it). Checked
    // before the attempt key: an import that lands mid-fetch must win too.
    if (present && !holdsHead) {
      setReach({ state: REACH_STATE.IDLE, sentence: '' })
      return undefined
    }
    const key = `${drawingId}#${headKey}`
    if (attemptRef.current === key) return undefined
    // The head moved because THIS engine saved it: the engine already holds
    // exactly those bytes, so there is nothing to fetch and the undo history
    // is kept (a re-open would floor it at the save, which no drafter asked
    // for).
    if (present && holdsHead && Number.isInteger(session.savedVersion) && Number(headKey) === session.savedVersion) {
      attemptRef.current = key
      setReach({ state: REACH_STATE.OPEN, sentence: '', version: session.savedVersion, head: session.savedVersion, source: 'engine-save' })
      return undefined
    }
    if (present) {
      // The engine's own copy of the head follows a moved head only when
      // nothing would be lost.
      if (dirty) {
        attemptRef.current = key
        setReach({ state: REACH_STATE.STALE, sentence: 'the drawing moved on the server; save or discard the browser edits to open the new version' })
        return undefined
      }
      if (session.busy) return undefined
    }
    attemptRef.current = key
    const generation = generationRef.current
    setReach({ state: REACH_STATE.OPENING, sentence: `opening ${drawingId} in the browser engine...` })
    let cancelled = false
    ;(async () => {
      let answer
      try {
        answer = await fetchRef.current(drawingId)
      } catch (error) {
        if (cancelled || generation !== generationRef.current) return
        setReach({
          state: REACH_STATE.FAILED,
          sentence: `the drawing could not be opened in the browser engine: ${error?.message || 'fetch failed'}; import a DXF instead`,
        })
        return
      }
      if (cancelled || generation !== generationRef.current) return
      const latest = sessionRef.current
      // A document appeared while the bytes were in flight (a hand import,
      // or this opener's own earlier open): it wins, the bytes are dropped.
      if (latest.documentId !== '' && !(HEAD_DOC.test(latest.documentId) && latest.documentId.startsWith(`${drawingId}-v`))) {
        setReach({ state: REACH_STATE.IDLE, sentence: '' })
        return
      }
      if (latest.dirty === true) {
        setReach({ state: REACH_STATE.STALE, sentence: 'the drawing moved on the server; save or discard the browser edits to open the new version' })
        return
      }
      const bytes = answer?.bytes
      // The version comes from the answer's X-Leaf-Version header; on a
      // cross-origin API (the local stack, any split deployment) the browser
      // hides custom headers unless the server exposes them, so the head
      // number the host already holds (headKey, the drawing's head) is the
      // fallback: the fetch asked for `head`, and that is what it got.
      const answered = Number(answer?.version)
      const known = Number(headKey)
      const version = Number.isInteger(answered) && answered > 0
        ? answered
        : Number.isInteger(known) && known > 0 ? known : NaN
      // toString, not instanceof: a Uint8Array from another realm (a
      // worker, a test harness) is still bytes.
      if (Object.prototype.toString.call(bytes) !== '[object Uint8Array]' || !Number.isInteger(version) || version < 1) {
        setReach({ state: REACH_STATE.FAILED, sentence: 'the drawing could not be opened in the browser engine: the server answered without a document; import a DXF instead' })
        return
      }
      openBytes(bytes, headDocumentId(drawingId, version))
      setReach({ state: REACH_STATE.OPEN, sentence: '', version, head: Number(answer?.head) || version, source: String(answer?.source || '') })
    })()
    return () => { cancelled = true }
    // present/holdsHead/dirty/busy are read from the same session object the
    // effect keys on; listing the derived booleans keeps the deps honest.
  }, [enabled, drawingId, headKey, present, holdsHead, dirty, session.busy, session.savedVersion, openBytes, setReach])

  // Unmount: nothing in flight may report onto a provider that outlives it.
  useEffect(() => () => { generationRef.current += 1 }, [])
  return null
}
