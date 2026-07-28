import { useCallback, useMemo, useRef, useState } from 'react'

const identity = (value) => value

function visibleLayerMap(intake) {
  const visible = {}
  for (const layer of intake?.layers || []) visible[layer] = true
  return visible
}

function drawingStateFrom(view, drawingId, previous) {
  const resolvedDrawingId = drawingId ?? view?.drawing_id ?? previous?.drawing_id ?? null
  const head = view?.head ?? previous?.head ?? null
  const latest = view?.latest ?? previous?.latest ?? head ?? null
  const version = view?.version ?? head ?? previous?.version ?? null

  if (resolvedDrawingId == null && head == null && latest == null && version == null) return null
  return { drawing_id: resolvedDrawingId, version, head, latest }
}

/**
 * Owns the drawing intake and version-chain state shared by the workspace
 * surfaces. Transport and presentation stay outside the hook: callers inject
 * adapters for reads and mutations, plus callbacks for viewer and notice work.
 */
export default function useDrawingVersionController({
  loadHead,
  loadVersion,
  loadVersions,
  undoVersion,
  redoVersion,
  onApplyIntake,
  onResetSelection,
  onVersionEvent,
  onError,
  formatError = identity,
  initialIntake = null,
  initialDrawingState = null,
} = {}) {
  const [intake, setIntake] = useState(initialIntake)
  const [versionIntake, setVersionIntake] = useState(null)
  const [visibleLayers, setVisibleLayers] = useState(() => visibleLayerMap(initialIntake))
  const [drawingState, setDrawingState] = useState(initialDrawingState)
  const [versionBusy, setVersionBusy] = useState(false)
  const [versionError, setVersionError] = useState(null)
  const [overlayStale, setOverlayStale] = useState(false)

  const [historyOpen, setHistoryOpen] = useState(false)
  const [history, setHistory] = useState(null)
  const [historyError, setHistoryError] = useState(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [previewing, setPreviewing] = useState(null)
  const [previewIntake, setPreviewIntake] = useState(null)

  const [refreshFailure, setRefreshFailure] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  // A restore can commit a new immutable head before its derived intake cache
  // is readable. This is drawing state, not drawer state: closing history must
  // not make the stale viewer eligible to mutate the newer server head.
  const [unreadableHead, setUnreadableHead] = useState(null)

  const shown = previewIntake || versionIntake || intake
  const activeVersion = previewing?.version ?? drawingState?.head ?? drawingState?.version ?? null
  const numericHead = Number(drawingState?.head)
  const numericLatest = Number(drawingState?.latest)
  const mutationsBlocked = unreadableHead != null
  const canUndo = !mutationsBlocked && Number.isFinite(numericHead) && numericHead > 1
  const canRedo = !mutationsBlocked && Number.isFinite(numericHead) && Number.isFinite(numericLatest) && numericHead < numericLatest

  const reportError = useCallback((error, operation) => {
    setVersionError(formatError(error))
    onError?.(error, { operation })
  }, [formatError, onError])

  // Conditional lock release: a seated view lifts the unreadable-head lock
  // ONLY when it proves the lock's target was reached — same drawing and a
  // head at or past the locked head. Round-2 finding: an unconditional clear
  // let a STALE in-flight head GET (started before the restore, e.g. the
  // job-completion read) seat old geometry and re-enable mutations against
  // the moved server head. Other drawing responses cannot prove this restored
  // head is seated, so they leave the lock unchanged.
  const releaseLockIfSeated = useCallback((view, seatedDrawingId) => {
    setUnreadableHead((lock) => {
      if (!lock) return null
      const seatedDrawing = view?.drawing_id ?? seatedDrawingId ?? null
      if (seatedDrawing !== lock.drawing_id) return lock
      // Two proofs, both required (round-3 findings):
      //  * COHERENCE — the SEATED GEOMETRY is the response's own head
      //    (version === head). A split read can report {intake v3, head 4}
      //    when a restore commits between the server's two reads; its head
      //    field proves nothing about what was seated.
      //  * POST-RESTORE — the response's `latest` watermark reaches the
      //    lock's. `latest` is monotone where `head` is not: a legitimate
      //    undo AFTER the restore lowers head but keeps latest at the
      //    restored version (this response must release, or the lock wedges
      //    with redo disabled), while a STALE pre-restore read necessarily
      //    reports the old, smaller latest.
      const seatedVersion = Number(view?.version)
      const responseHead = Number(view?.head)
      const responseLatest = Number(view?.latest)
      const coherent = Number.isFinite(seatedVersion) && Number.isFinite(responseHead)
        && seatedVersion === responseHead
      const postRestore = Number.isFinite(responseLatest)
        ? responseLatest >= Number(lock.latest ?? lock.head)
        : coherent && seatedVersion >= Number(lock.head)
      return coherent && postRestore ? null : lock
    })
  }, [])

  const resetPreview = useCallback(() => {
    setHistoryOpen(false)
    setHistory(null)
    setHistoryError(null)
    setPreviewing(null)
    setPreviewIntake(null)
  }, [])

  const reset = useCallback(() => {
    setIntake(null)
    setVersionIntake(null)
    setVisibleLayers({})
    setDrawingState(null)
    setVersionBusy(false)
    setVersionError(null)
    setOverlayStale(false)
    setHistoryOpen(false)
    setHistory(null)
    setHistoryError(null)
    setHistoryLoading(false)
    setPreviewing(null)
    setPreviewIntake(null)
    setRefreshFailure(null)
    setRefreshing(false)
    setUnreadableHead(null)
    restoreGenerationRef.current += 1 // abandon any in-flight restore completion
    onResetSelection?.({ source: 'reset' })
  }, [onResetSelection])

  const seatIntake = useCallback((nextIntake, options = {}) => {
    setIntake(nextIntake)
    setVersionIntake(null)
    setVisibleLayers(visibleLayerMap(nextIntake))
    setVersionError(null)
    setOverlayStale(false)
    setRefreshFailure(null)
    setUnreadableHead(null)
    restoreGenerationRef.current += 1 // drawing switch: abandon in-flight restore completions
    resetPreview()
    onResetSelection?.({ source: 'intake' })

    if (options.drawingState || options.drawingId != null) {
      const source = options.drawingState || {}
      setDrawingState(drawingStateFrom(source, options.drawingId, null))
    } else {
      setDrawingState(null)
    }

    if (options.apply) onApplyIntake?.(nextIntake, { source: 'intake' })
    return nextIntake
  }, [onApplyIntake, onResetSelection, resetPreview])

  const seatVersion = useCallback((view, options = {}) => {
    if (!view?.intake) throw new Error('A drawing version must include an intake.')

    const nextDrawingState = drawingStateFrom(view, options.drawingId, drawingState)
    setDrawingState(nextDrawingState)
    setVersionIntake(view.intake)
    setVersionError(null)
    setRefreshFailure(null)
    releaseLockIfSeated(view, options.drawingId)
    resetPreview()
    onResetSelection?.({ source: options.source || 'version' })
    onApplyIntake?.(view.intake, {
      source: options.source || 'version',
      drawingState: nextDrawingState,
    })
    onVersionEvent?.({
      event: options.event || null,
      view,
      drawingState: nextDrawingState,
    })
    return view
  }, [drawingState, onApplyIntake, onResetSelection, onVersionEvent, releaseLockIfSeated, resetPreview])

  const runVersionMutation = useCallback(async (operation, adapter, event, context) => {
    const drawingId = drawingState?.drawing_id
    if (drawingId == null || versionBusy || mutationsBlocked || typeof adapter !== 'function') return null

    setVersionBusy(true)
    setVersionError(null)
    setOverlayStale(true)
    try {
      const view = await adapter(drawingId, context)
      seatVersion(view, { drawingId, source: operation, event })
      return view
    } catch (error) {
      reportError(error, operation)
      return null
    } finally {
      setVersionBusy(false)
    }
  }, [drawingState, mutationsBlocked, reportError, seatVersion, versionBusy])

  const undo = useCallback(
    (context) => runVersionMutation('undo', undoVersion, 'undo', context),
    [runVersionMutation, undoVersion],
  )

  const redo = useCallback(
    (context) => runVersionMutation('redo', redoVersion, 'redo', context),
    [redoVersion, runVersionMutation],
  )

  const loadHistory = useCallback(async () => {
    const drawingId = drawingState?.drawing_id
    if (drawingId == null || typeof loadVersions !== 'function') return null

    setHistoryLoading(true)
    setHistoryError(null)
    try {
      // The drawer is the one surface that wants per-row delta chips; the
      // adapter forwards the flag to ?include_deltas=1 (server-side cost:
      // every version payload is loaded, so nothing else requests it).
      const nextHistory = await loadVersions(drawingId, { includeDeltas: true })
      setHistory(nextHistory)
      return nextHistory
    } catch (error) {
      setHistoryError(formatError(error))
      setHistory(null)
      onError?.(error, { operation: 'history' })
      return null
    } finally {
      setHistoryLoading(false)
    }
  }, [drawingState, formatError, loadVersions, onError])

  const toggleHistory = useCallback(async () => {
    if (historyOpen) {
      setHistoryOpen(false)
      return null
    }
    setHistoryOpen(true)
    return loadHistory()
  }, [historyOpen, loadHistory])

  const closeHistory = useCallback(() => {
    setHistoryOpen(false)
  }, [])

  const previewVersion = useCallback(async (version) => {
    const drawingId = drawingState?.drawing_id
    if (drawingId == null) return null

    const isHead = Object.is(version, drawingState.head)
    const adapter = isHead ? loadHead : loadVersion
    if (typeof adapter !== 'function') return null

    setHistoryError(null)
    try {
      const view = isHead
        ? await adapter(drawingId)
        : await adapter(drawingId, version)
      if (!view?.intake) throw new Error('A drawing preview must include an intake.')

      onApplyIntake?.(view.intake, { source: isHead ? 'head' : 'preview', version })
      onResetSelection?.({ source: isHead ? 'head' : 'preview', version })
      if (isHead) {
        setVersionIntake(view.intake)
        setDrawingState((previous) => drawingStateFrom(view, drawingId, previous))
        setPreviewIntake(null)
        setPreviewing(null)
        // Same conditional release as seatVersion: a stale head view started
        // before a restore must not lift the lock (round-2 finding).
        releaseLockIfSeated(view, drawingId)
      } else {
        setPreviewIntake(view.intake)
        setPreviewing({ version })
        setOverlayStale(true)
      }
      return view
    } catch (error) {
      setHistoryError(formatError(error))
      onError?.(error, { operation: 'preview', version })
      return null
    }
  }, [drawingState, formatError, loadHead, loadVersion, onApplyIntake, onError, onResetSelection, releaseLockIfSeated])

  const backToHead = useCallback(() => {
    if (drawingState?.head == null) return null
    return previewVersion(drawingState.head)
  }, [drawingState, previewVersion])

  // After a restore the SERVER head moved; the intake, head, and version
  // state this controller feeds the viewer must move with it, or the next
  // write operates on the restored head while the viewer still shows the old
  // drawing. seatVersion also closes the history drawer (resetPreview), which
  // is the intended landing: the user restored, show them the result.
  const refreshHead = useCallback(async () => {
    const drawingId = drawingState?.drawing_id
    if (drawingId == null || typeof loadHead !== 'function') return null
    try {
      const view = await loadHead(drawingId)
      seatVersion(view, { drawingId, source: 'restore' })
      return view
    } catch (error) {
      reportError(error, 'restore-refresh')
      return null
    }
  }, [drawingState, loadHead, reportError, seatVersion])

  // Generation token for the ASYNC restore completion (round-4 finding): the
  // awaited head read can resolve after another actor already settled the
  // lock (a parallel valid read cleared it, a drawing switch replaced the
  // context, a newer restore superseded this one). A stale completion must
  // neither seat over the newer context nor re-arm an obsolete lock.
  const restoreGenerationRef = useRef(0)

  const recordRestore = useCallback(async (result) => {
    const drawingId = result?.drawing_id ?? drawingState?.drawing_id
    if (drawingId == null || result?.head == null) return null
    const generation = restoreGenerationRef.current + 1
    restoreGenerationRef.current = generation
    const nextState = drawingStateFrom({
      drawing_id: drawingId,
      version: result.head,
      head: result.head,
      latest: result.latest ?? result.head,
    }, drawingId, drawingState)
    // Propagate the committed server state before any optional intake read.
    setDrawingState(nextState)
    setOverlayStale(true)
    setPreviewIntake(null)
    setPreviewing(null)

    if (result.restored_head_readable === false) {
      setUnreadableHead({
        drawing_id: drawingId,
        head: Number(result.head),
        latest: Number(result.latest ?? result.head),
        restored_from: result.restored_from,
        message: `Restored as v${result.head}, but the new head is not readable yet. Editing stays locked until its intake cache is repaired.`,
      })
      return result
    }

    // Even a READABLE restore moves the server head before this client can
    // read it: until the new head's intake actually seats, the stale viewer
    // must not mutate (undo or a write against a head it has not seen). The
    // lock is armed BEFORE the read — a pending GET window with mutations
    // enabled was the round-1 hole — and only seatVersion clears it.
    setUnreadableHead({
      drawing_id: drawingId,
      head: Number(result.head),
      latest: Number(result.latest ?? result.head),
      restored_from: result.restored_from,
      // pending: the routine post-restore load, rendered as calm progress —
      // never as a failure alert (round-2 MINOR).
      pending: true,
      message: `Restored as v${result.head}. Loading the new head…`,
    })
    if (typeof loadHead !== 'function') {
      // No reader available: the new head cannot be seated, so the lock
      // STAYS (fail-safe; every in-tree caller passes loadHead).
      return result
    }
    try {
      const view = await loadHead(drawingId)
      if (restoreGenerationRef.current !== generation) {
        // Superseded while awaiting (newer restore, drawing switch, reset):
        // this completion must not seat old context over the new one.
        return null
      }
      const seatedDrawing = view?.drawing_id ?? drawingId
      // Same coherence + watermark proof as releaseLockIfSeated: the SEATED
      // version must BE the response's own head (a split read can report
      // {intake v3, head 4}), and the response must post-date the restore
      // via the monotone `latest` (an undo landing after the restore lowers
      // head legitimately). An incoherent-but-successful read throws so the
      // catch escalates the pending lock to the retryable alert instead of
      // leaving calm progress stuck with no affordance.
      const seatedVersion = Number(view?.version ?? view?.head)
      const responseHead = Number(view?.head ?? view?.version)
      const responseLatest = Number(view?.latest)
      const coherent = seatedDrawing === drawingId
        && Number.isFinite(seatedVersion) && Number.isFinite(responseHead)
        && seatedVersion === responseHead
      const postRestore = Number.isFinite(responseLatest)
        ? responseLatest >= Number(result.latest ?? result.head)
        : seatedVersion >= Number(result.head)
      if (!coherent || !postRestore) {
        throw new Error(`The restored head v${result.head} is not readable yet.`)
      }
      // seatVersion clears unreadableHead itself — the lock lifts only once
      // the NEW head's intake is actually seated in the viewer.
      seatVersion(view, { drawingId, source: 'restore' })
      return view
    } catch (error) {
      if (restoreGenerationRef.current !== generation) return null
      // The server head moved but we could not read it: the stale viewer
      // must NOT become eligible to mutate the newer head. Re-arm ONLY if
      // the current lock is still THIS restore's (a parallel valid read may
      // already have released it; an obsolete failure must not resurrect an
      // obsolete lock).
      setUnreadableHead((lock) => {
        if (!lock || lock.drawing_id !== drawingId || Number(lock.head) !== Number(result.head)) return lock
        return {
          drawing_id: drawingId,
          head: Number(result.head),
          latest: Number(result.latest ?? result.head),
          restored_from: result.restored_from,
          message: `Restored as v${result.head}, but the new head could not be loaded. Editing stays locked until it loads.`,
        }
      })
      reportError(error, 'restore-refresh')
      return null
    }
  }, [drawingState, loadHead, reportError, seatVersion])

  const retryUnreadableHead = useCallback(async () => {
    const drawingId = unreadableHead?.drawing_id
    if (drawingId == null || refreshing || typeof loadHead !== 'function') return null
    setRefreshing(true)
    try {
      const view = await loadHead(drawingId)
      seatVersion(view, { drawingId, source: 'restore-repair' })
      return view
    } catch (error) {
      onError?.(error, { operation: 'restore-repair' })
      return null
    } finally {
      setRefreshing(false)
    }
  }, [loadHead, onError, refreshing, seatVersion, unreadableHead])

  const markRefreshFailure = useCallback((failure) => {
    setRefreshFailure(failure || null)
  }, [])

  const retryRefresh = useCallback(async () => {
    const drawingId = refreshFailure?.drawing_id ?? refreshFailure?.drawingId
    if (drawingId == null || refreshing || typeof loadHead !== 'function') return null

    setRefreshing(true)
    try {
      const view = await loadHead(drawingId)
      seatVersion(view, { drawingId, source: 'refresh' })
      setRefreshFailure(null)
      return view
    } catch (error) {
      onError?.(error, { operation: 'refresh' })
      return null
    } finally {
      setRefreshing(false)
    }
  }, [loadHead, onError, refreshFailure, refreshing, seatVersion])

  const actions = useMemo(() => ({
    reset,
    seatIntake,
    seatVersion,
    setVisibleLayers,
    setOverlayStale,
    clearVersionError: () => setVersionError(null),
    undo,
    redo,
    loadHistory,
    toggleHistory,
    closeHistory,
    previewVersion,
    backToHead,
    markRefreshFailure,
    retryRefresh,
    refreshHead,
    recordRestore,
    retryUnreadableHead,
  }), [
    backToHead,
    closeHistory,
    loadHistory,
    markRefreshFailure,
    previewVersion,
    redo,
    refreshHead,
    recordRestore,
    reset,
    retryRefresh,
    retryUnreadableHead,
    seatIntake,
    seatVersion,
    toggleHistory,
    undo,
  ])

  return {
    intake,
    versionIntake,
    shown,
    visibleLayers,
    drawingState,
    activeVersion,
    head: drawingState?.head ?? null,
    latest: drawingState?.latest ?? null,
    canUndo,
    canRedo,
    versionBusy,
    versionError,
    overlayStale,
    historyOpen,
    history,
    historyError,
    historyLoading,
    previewing,
    previewIntake,
    refreshFailure,
    refreshing,
    unreadableHead,
    mutationsBlocked,
    actions,
  }
}

export { drawingStateFrom, visibleLayerMap }
