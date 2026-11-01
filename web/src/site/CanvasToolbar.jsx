import { useEffect, useRef, useState } from 'react'

export default function CanvasToolbar({ editor, editing, onEditing, outline, onOutline, settings, onSettings }) {
  const [confirmUntil, setConfirmUntil] = useState(0)
  const holder = useRef(null)
  const { session, state } = editor
  const locked = !state.enabled || Boolean(state.busy)
  useEffect(() => {
    const toolbar = holder.current, surface = toolbar?.closest('.browser-asset-viewport')
    const measure = () => {
      const height = toolbar?.getBoundingClientRect().height
      if (height) surface?.style.setProperty('--canvas-toolbar-height', `${height}px`)
    }
    measure()
    if (!globalThis.ResizeObserver) return
    const observer = new ResizeObserver(measure)
    if (toolbar) observer.observe(toolbar)
    return () => observer.disconnect()
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
  return <div ref={holder} className="canvas-toolbar" aria-label="Page tools">
    <button type="button" aria-pressed={!editing} onClick={() => onEditing(false)}>View</button>
    <button type="button" aria-pressed={editing} onClick={() => onEditing(true)}>Edit</button>
    <button type="button" disabled={locked || !state.canUndo} onClick={session.undo}>Undo</button>
    <button type="button" disabled={locked || !state.canRedo} onClick={session.redo}>Redo</button>
    <button type="button" disabled={locked || !state.dirty || Boolean(state.validation) || state.conflict} onClick={session.save}>Save page</button>
    <button type="button" disabled={locked || !state.dirty || !state.savedRevision} onClick={() => {
      if (Date.now() < confirmUntil) { session.discard(); setConfirmUntil(0) }
      else setConfirmUntil(Date.now() + 5000)
    }}>{Date.now() < confirmUntil ? 'Confirm discard unsaved' : 'Discard unsaved'}</button>
    <button type="button" disabled={locked} onClick={session.reload}>Reload saved</button>
    <button type="button" aria-expanded={outline} onClick={() => onOutline(!outline)}>Elements / Outline</button>
    <button type="button" aria-expanded={settings} onClick={() => onSettings(!settings)}>Settings</button>
    <span role="status">{state.error || state.validation || state.notice || state.status}</span>
  </div>
}
