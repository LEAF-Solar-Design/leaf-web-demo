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
 *   - `sourceKey` names where the head bytes come from (the live API or the
 *     static sample), so a source switch is a new head to open under the
 *     same import, dirty and busy protections as a moved head;
 *   - opens once per (drawing, source, head) when NO document is in the engine; a
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

/**
 * Provenance, not the filename, says the engine holds this drawing's head: the
 * name must have the head shape for THIS drawing, and the session must have
 * loaded it as the head, or be loading (or have refused) this opener's own
 * head load. A hand import named like the head is a hand import. Pure.
 */
export function holdsHeadDocument(session, drawingId, openedDocumentId) {
  const id = session?.documentId
  if (typeof id !== 'string' || !HEAD_DOC.test(id)) return false
  if (typeof drawingId !== 'string' || drawingId === '') return false
  // Exact: the remainder after `${drawingId}-v` is only the version, so a
  // longer drawing id sharing the prefix (rooftop_demo-villa) never matches.
  const prefix = `${drawingId}-v`
  if (!id.startsWith(prefix) || !/^\d+\.dxf$/.test(id.slice(prefix.length))) return false
  if (session.documentOrigin === 'head') return true
  return session.documentOrigin === null && id === openedDocumentId
}

export default function EngineHeadOpener({ drawingId = null, enabled = false, headKey = null, fetchDxf = null, sourceKey = '' }) {
  const { session, setReach } = useEngineSessionContext()
  const { openBytes } = session.actions
  // Latest session for the post-await checks (a state read inside the async
  // leg would be the render it was captured in).
  const sessionRef = useRef(session)
  sessionRef.current = session
  // What this opener has opened or tried: one attempt per (drawing, source, head).
  const attemptRef = useRef('')
  // The source of the last head this opener loaded (null until it loads
  // one); the engine-save shortcut applies only within that source.
  const openedSourceRef = useRef(null)
  // The session's savedVersion at the moment this opener last opened a head.
  const savedAtOpenRef = useRef(null)
  // The document name this opener instance last passed to `openBytes`.
  const openedDocumentRef = useRef(null)
  // At most one entry, { key, promise, pending }: the head fetch for the
  // current attempt key, shared by effect runs that were cancelled before they
  // settled. A run attaches only while the fetch is still pending; a settled
  // one (above all a rejected one) is fetched again, as before sharing.
  const inflightRef = useRef(null)
  // Bumped on unmount and on every drawing or source switch; an async leg
  // captured before an await compares and abandons if it moved.
  const generationRef = useRef(0)
  const fetchRef = useRef(fetchDxf)
  fetchRef.current = fetchDxf

  const documentId = session.documentId
  const present = session.engineParsed || session.busy || documentId !== ''
  const holdsHead = holdsHeadDocument(session, drawingId, openedDocumentRef.current)
  const dirty = session.dirty === true

  // A drawing or source switch abandons any fetch in flight for the old one.
  // The attempt key already carries the drawing id and the source, so it is
  // NOT cleared here: clearing it between StrictMode's two effect
  // invocations issued a second fetch on every dev mount (kimi, #1006).
  useEffect(() => {
    generationRef.current += 1
  }, [drawingId, sourceKey])

  useEffect(() => {
    if (!enabled || !drawingId || typeof fetchRef.current !== 'function') return undefined
    const key = `${drawingId}#${sourceKey}#${headKey}`
    // An entry for another key serves nothing now. Hygiene only (no row): the
    // pending check below already keeps a stale entry from being misused.
    if (inflightRef.current !== null && inflightRef.current.key !== key) inflightRef.current = null
    // A hand-imported document is never replaced, and the reach reads idle
    // while it is open (the head's sentence would be stale under it). Checked
    // before the attempt key: an import that lands mid-fetch must win too.
    if (present && !holdsHead) {
      // A hand import wins, so bytes still in flight will never be used.
      inflightRef.current = null
      setReach({ state: REACH_STATE.IDLE, sentence: '' })
      return undefined
    }
    if (attemptRef.current === key) return undefined
    // The head moved because THIS engine saved it: the engine already holds
    // exactly those bytes, so there is nothing to fetch and the undo history
    // is kept (a re-open would floor it at the save, which no drafter asked
    // for). Never across a source switch: the saved head is the old source's.
    // null means this opener instance has not opened a head (a remount over a
    // retained session), so the shortcut keeps its behaviour from before the
    // source existed; once it has opened one, a different source never matches.
    // A savedVersion already present when this opener opened its head is not
    // this head's save (the session keeps it across a document switch).
    if (present && holdsHead && (openedSourceRef.current === null || openedSourceRef.current === sourceKey) && session.savedVersion !== savedAtOpenRef.current &&Number.isInteger(session.savedVersion) && Number(headKey) === session.savedVersion) {
      attemptRef.current = key
      // Decided without a fetch, so no shared fetch serves this attempt.
      inflightRef.current = null
      setReach({ state: REACH_STATE.OPEN, sentence: '', version: session.savedVersion, head: session.savedVersion, source: 'engine-save' })
      return undefined
    }
    if (present) {
      // The engine's own copy of the head follows a moved head only when
      // nothing would be lost.
      if (dirty) {
        attemptRef.current = key
        // Decided without a fetch, so no shared fetch serves this attempt.
        inflightRef.current = null
        setReach({ state: REACH_STATE.STALE, sentence: 'the drawing moved on the server; save or discard the browser edits to open the new version' })
        return undefined
      }
      if (session.busy) return undefined
    }
    attemptRef.current = key
    const generation = generationRef.current
    const source = sourceKey
    setReach({ state: REACH_STATE.OPENING, sentence: `opening ${drawingId} in the browser engine...` })
    let cancelled = false
    // Settled = this attempt reached a terminal verdict (loaded, refused,
    // stood down). A cancel BEFORE that (the session changed under the
    // fetch: an edit went busy, an import landed) drops the bytes AND
    // re-arms the attempt, so the next quiet moment re-evaluates instead of
    // leaving the head unopened and the reach stuck at "opening".
    let settled = false
    // A settled attempt drops its shared fetch; a cancelled run leaves it for
    // the next run with the same key (StrictMode's second setup) to attach to.
    const release = () => {
      if (inflightRef.current?.key === key) inflightRef.current = null
    }
    let promise
    if (inflightRef.current?.key === key && inflightRef.current.pending === true) {
      promise = inflightRef.current.promise
    } else {
      // A synchronous throw in fetchDxf is a rejection. Called now, not in a
      // .then: a native promise passes through Promise.resolve unwrapped, so
      // the load lands on the same microtask it did before the fetch was shared.
      try {
        promise = Promise.resolve(fetchRef.current(drawingId))
      } catch (error) {
        promise = Promise.reject(error)
      }
      const entry = { key, promise, pending: true }
      // Also handles a rejection nobody is attached to (a cancelled run's).
      promise.then(() => { entry.pending = false }, () => { entry.pending = false })
      inflightRef.current = entry
    }
    ;(async () => {
      let answer
      try {
        answer = await promise
      } catch (error) {
        if (cancelled || generation !== generationRef.current) return
        settled = true
        release()
        setReach({
          state: REACH_STATE.FAILED,
          sentence: `the drawing could not be opened in the browser engine: ${error?.message || 'fetch failed'}; import a DXF instead`,
        })
        return
      }
      if (cancelled || generation !== generationRef.current) return
      settled = true
      release()
      const latest = sessionRef.current
      // A document appeared while the bytes were in flight (a hand import,
      // or this opener's own earlier open): it wins, the bytes are dropped.
      if (latest.documentId !== '' && !holdsHeadDocument(latest, drawingId, openedDocumentRef.current)) {
        setReach({ state: REACH_STATE.IDLE, sentence: '' })
        return
      }
      if (latest.dirty === true) {
        setReach({ state: REACH_STATE.STALE, sentence: 'the drawing moved on the server; save or discard the browser edits to open the new version' })
        return
      }
      // An edit, undo or redo is in flight (busy set synchronously, its
      // reply not yet landed, so `dirty` cannot say yet): loading now would
      // clobber it (kimi, #1006). Stand down and let the effect try again
      // when busy clears; by then dirty tells the truth.
      if (latest.busy) {
        attemptRef.current = ''
        setReach({ state: REACH_STATE.IDLE, sentence: '' })
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
      // W4g-3b: this document IS the head, so the store keeps the entity
      // list it loads as the base a save diffs against (the mutation plan).
      openedDocumentRef.current = headDocumentId(drawingId, version)
      openBytes(bytes, headDocumentId(drawingId, version), { committed: true, version })
      openedSourceRef.current = source
      savedAtOpenRef.current = sessionRef.current.savedVersion ?? null
      setReach({ state: REACH_STATE.OPEN, sentence: '', version, head: Number(answer?.head) || version, source: String(answer?.source || '') })
    })()
    return () => {
      cancelled = true
      if (!settled) attemptRef.current = ''
    }
    // present/holdsHead/dirty/busy are read from the same session object the
    // effect keys on; listing the derived booleans keeps the deps honest.
  }, [enabled, drawingId, sourceKey, headKey, present, holdsHead, dirty, session.busy, session.savedVersion, openBytes, setReach])

  // Unmount: nothing in flight may report onto a provider that outlives it.
  useEffect(() => () => { generationRef.current += 1 }, [])
  return null
}
