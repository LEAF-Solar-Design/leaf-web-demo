import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { validObjectBounds } from '../lib/drawingObjectIndex.js'

const Context = createContext(null)
const EMPTY = []

export function DrawingObjectsProvider({ viewerRef, children }) {
  const state = useRef({ sources: new Map(), index: null, focusId: null, alive: true })
  const [, update] = useState(0)
  const viewer = useRef(viewerRef)
  viewer.current = viewerRef
  const refresh = useCallback(() => {
    const s = state.current
    if (!s.alive) return
    const entry = s.sources.get('engine') || s.sources.get('console')
    const next = entry?.index || null, previousFocus = s.focusId
    const changed = entry !== s.entry
    s.entry = entry
    const record = next?.byId.get(s.focusId)
    if (next?.drawingKey !== s.index?.drawingKey || (s.focusId && !validObjectBounds(record?.bounds))) {
      s.focusId = null
      viewer.current?.current?.setFocusMarker?.(null)
    }
    s.index = next
    const api = viewer.current?.current, scene = api?.getDrawingScene?.()
    if (scene?.ready && scene.drawingKey === next?.drawingKey && scene.intake === next?.intake) {
      api.setFocusMarker?.(s.focusId ? record.bounds : null)
    }
    if (changed || previousFocus !== s.focusId) update((n) => n + 1)
  }, [])
  const retract = useCallback((source, owner) => {
    const s = state.current
    if (!s.alive || (owner && s.sources.get(source) !== owner)) return
    s.sources.delete(source); refresh()
  }, [refresh])
  // The returned cleanup owns this publication, never a later publisher's state.
  const publish = useCallback((source, { index }) => {
    const s = state.current
    if (!s.alive) return () => {}
    const entry = { index, handles: s.sources.get(source)?.handles || EMPTY }
    s.sources.set(source, entry); refresh()
    return () => queueMicrotask(() => retract(source, entry))
  }, [refresh, retract])
  const publishSelection = useCallback((source, handles) => {
    const s = state.current, entry = s.sources.get(source)
    if (!s.alive || !entry) return
    entry.handles = [...handles]; update((n) => n + 1)
  }, [])
  const clearFocus = useCallback(() => {
    if (!state.current.alive) return
    state.current.focusId = null
    viewer.current?.current?.setFocusMarker?.(null)
    update((n) => n + 1)
  }, [])
  const focus = useCallback((id) => {
    const s = state.current, api = viewer.current?.current, record = s.index?.byId.get(id)
    const fail = (reason) => ({ ok: false, reason })
    if (!s.alive) return fail('viewer not ready')
    if (!record) return fail('unknown id')
    if (!validObjectBounds(record.bounds)) return fail('no bounds')
    if (!api?.frame || !api?.setFocusMarker || !api?.canSetFocusMarker || !api?.getDrawingScene) return fail('viewer not ready')
    const scene = api.getDrawingScene()
    if (!scene?.ready) return fail('viewer not ready')
    if (scene.drawingKey !== s.index.drawingKey || scene.intake !== s.index.intake) return fail('scene not showing this index\'s drawing')
    if (!api.canSetFocusMarker(record.bounds)) return fail('viewer not ready')
    if (!api.frame(record.bounds)) return fail('frame refused')
    api.setFocusMarker(record.bounds)
    s.focusId = id; update((n) => n + 1)
    return { ok: true }
  }, [])
  useEffect(() => {
    state.current.alive = true
    return () => { clearFocus(); state.current.alive = false; state.current.sources.clear() }
  }, [clearFocus])
  // Ref attachment can be delayed by lazy loading; App's attachment notification rerenders this provider.
  const subscribed = useRef(null)
  useEffect(() => {
    const api = viewerRef?.current
    if (subscribed.current?.api === api) return
    subscribed.current?.off?.()
    subscribed.current = { api, off: api?.subscribeCamera?.(refresh) }
  })
  useEffect(() => () => { subscribed.current?.off?.(); subscribed.current = null }, [])
  const s = state.current
  const selectedHandles = (s.sources.get('engine') || s.sources.get('console'))?.handles || EMPTY
  return <Context.Provider value={{ index: s.index, focusId: s.focusId, focus, clearFocus, selectedHandles,
    publish, publishSelection, retract }}>{children}</Context.Provider>
}

export function useDrawingObjects() { return useContext(Context) }
