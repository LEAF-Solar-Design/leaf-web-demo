import { useCallback, useMemo, useState } from 'react'

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

  const shown = previewIntake || versionIntake || intake
  const activeVersion = previewing?.version ?? drawingState?.head ?? drawingState?.version ?? null
  const numericHead = Number(drawingState?.head)
  const numericLatest = Number(drawingState?.latest)
  const canUndo = Number.isFinite(numericHead) && numericHead > 1
  const canRedo = Number.isFinite(numericHead) && Number.isFinite(numericLatest) && numericHead < numericLatest

  const reportError = useCallback((error, operation) => {
    setVersionError(formatError(error))
    onError?.(error, { operation })
  }, [formatError, onError])

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
    onResetSelection?.({ source: 'reset' })
  }, [onResetSelection])

  const seatIntake = useCallback((nextIntake, options = {}) => {
    setIntake(nextIntake)
    setVersionIntake(null)
    setVisibleLayers(visibleLayerMap(nextIntake))
    setVersionError(null)
    setOverlayStale(false)
    setRefreshFailure(null)
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
  }, [drawingState, onApplyIntake, onResetSelection, onVersionEvent, resetPreview])

  const runVersionMutation = useCallback(async (operation, adapter, event, context) => {
    const drawingId = drawingState?.drawing_id
    if (drawingId == null || versionBusy || typeof adapter !== 'function') return null

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
  }, [drawingState, reportError, seatVersion, versionBusy])

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
  }, [drawingState, formatError, loadHead, loadVersion, onApplyIntake, onError, onResetSelection])

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
  }), [
    backToHead,
    closeHistory,
    loadHistory,
    markRefreshFailure,
    previewVersion,
    redo,
    refreshHead,
    reset,
    retryRefresh,
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
    actions,
  }
}

export { drawingStateFrom, visibleLayerMap }
