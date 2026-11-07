import { useEffect, useId, useRef, useState } from 'react'

export default function CanvasToolbar({ editor, editing, onEditing, outline, onOutline, settings, onSettings }) {
  const [confirmUntil, setConfirmUntil] = useState(0)
  const [narrow, setNarrow] = useState(() => globalThis.matchMedia?.('(max-width: 899px)').matches ?? false)
  const [actionsOpen, setActionsOpen] = useState(false)
  const [statusOpen, setStatusOpen] = useState(false)
  const holder = useRef(null)
  const actions = useRef(null), more = useRef(null), statusDetails = useRef(null), statusTrigger = useRef(null)
  const actionsId = useId(), statusId = useId()
  const { session, state } = editor
  const locked = !state.enabled || Boolean(state.busy)
  const message = state.error || state.validation || state.notice || state.status
  useEffect(() => {
    const media = globalThis.matchMedia?.('(max-width: 899px)')
    const change = () => setNarrow(media.matches)
    media?.addEventListener?.('change', change)
    return () => media?.removeEventListener?.('change', change)
  }, [])
  useEffect(() => {
    if (narrow && !actionsOpen && actions.current?.contains(document.activeElement)) more.current?.focus()
    if (!narrow && [more.current, statusTrigger.current].includes(document.activeElement)) holder.current?.querySelector('button')?.focus()
    if ((!narrow || !statusOpen) && statusDetails.current?.contains(document.activeElement)) {
      if (narrow) statusTrigger.current?.focus()
      else holder.current?.querySelector('button')?.focus()
    }
    const height = holder.current?.getBoundingClientRect().height
    if (height) holder.current?.closest('.browser-asset-viewport')?.style.setProperty('--canvas-toolbar-height', `${height}px`)
  })
  useEffect(() => {
    const toolbar = holder.current, surface = toolbar?.closest('.browser-asset-viewport')
    const measure = () => {
      const height = toolbar?.getBoundingClientRect().height
      if (height) surface?.style.setProperty('--canvas-toolbar-height', `${height}px`)
    }
    measure()
    window.addEventListener('resize', measure)
    const observer = globalThis.ResizeObserver ? new ResizeObserver(measure) : null
    if (toolbar) observer?.observe(toolbar)
    return () => { observer?.disconnect(); window.removeEventListener('resize', measure) }
  }, [])
  useEffect(() => {
    const surface = holder.current?.closest('.browser-asset-viewport')
    const key = event => {
      if (locked || !(event.ctrlKey || event.metaKey) || event.altKey || event.target.closest('input, textarea, select') || surface?.closest('[hidden], [inert]')) return
      if (event.key.toLowerCase() === 's') { event.preventDefault(); if (state.dirty && !state.validation && !state.conflict) session.save() }
      if (event.key.toLowerCase() === 'z') { event.preventDefault(); event.shiftKey ? session.redo() : session.undo() }
    }
    surface?.addEventListener('keydown', key)
    return () => surface?.removeEventListener('keydown', key)
  }, [session, state, locked])
  return <div ref={holder} className="canvas-toolbar" aria-label="Page tools" onKeyDown={event => {
    if (event.key !== 'Escape') return
    if (statusOpen && statusDetails.current?.contains(event.target)) { setStatusOpen(false); statusTrigger.current?.focus(); event.stopPropagation() }
    else if (narrow && actionsOpen) { setActionsOpen(false); more.current?.focus(); event.stopPropagation() }
  }}>
    <button type="button" aria-pressed={!editing} onClick={() => onEditing(false)}>View</button>
    <button type="button" aria-pressed={editing} onClick={() => onEditing(true)}>Edit</button>
    <button type="button" disabled={locked || !state.dirty || Boolean(state.validation) || state.conflict} onClick={session.save}>Save page</button>
    <button ref={more} type="button" hidden={!narrow} aria-label="More page tools" aria-expanded={actionsOpen} aria-controls={actionsId} onClick={() => setActionsOpen(!actionsOpen)}>More</button>
    <div ref={actions} id={actionsId} className="canvas-toolbar__actions" hidden={narrow && !actionsOpen}>
    <button type="button" disabled={locked || !state.canUndo} onClick={session.undo}>Undo</button>
    <button type="button" disabled={locked || !state.canRedo} onClick={session.redo}>Redo</button>
    <button type="button" disabled={locked || !state.dirty || !state.savedRevision} onClick={() => {
      if (Date.now() < confirmUntil) { session.discard(); setConfirmUntil(0) }
      else setConfirmUntil(Date.now() + 5000)
    }}>{Date.now() < confirmUntil ? 'Confirm discard unsaved' : 'Discard unsaved'}</button>
    <button type="button" disabled={locked} onClick={session.reload}>Reload saved</button>
    <button type="button" aria-expanded={outline} onClick={() => onOutline(!outline)}>Elements / Outline</button>
    <button type="button" aria-expanded={settings} onClick={() => onSettings(!settings)}>Settings</button>
    </div>
    <div className="canvas-toolbar__status">
      <span role="status">{message}</span>
      <button ref={statusTrigger} type="button" hidden={!narrow} aria-expanded={statusOpen} aria-controls={statusId} onClick={() => setStatusOpen(!statusOpen)}>Page status</button>
    </div>
    <div ref={statusDetails} id={statusId} className="canvas-toolbar__status-details" role="region" aria-label="Full page status" hidden={!narrow || !statusOpen}>
      <p>{message}</p>
      <button type="button" onClick={() => { setStatusOpen(false); statusTrigger.current?.focus() }}>Close page status</button>
    </div>
  </div>
}
