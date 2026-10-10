// NT2 toast (crib §2): a completed event surfaces as ONE quiet bottom-center
// toast above the docked bar — newest replaces (App holds a single toast slot),
// auto-fades after ~5 s (8 s for Undo, 180 ms exit per M1), one quiet action.
// Anatomy/colors come from styles.css (.toast, .toast .action, .enter/.exit).

import { useEffect, useRef, useState } from 'react'
import { isCompositionKey } from '../lib/keyComposition.js'

const TOAST_MS = 5000 // visible window before the exit fade
const UNDO_MS = 8000
const EXIT_MS = 180   // M1 exit fade

export default function Toast({ toast, onDone }) {
  const [exiting, setExiting] = useState(false)
  const rootRef = useRef(null)
  const actionRef = useRef(null)
  const hoveredRef = useRef(false)
  const focusedRef = useRef(false)
  const returnFocusRef = useRef(null)
  const dragRef = useRef(null)
  const suppressClickRef = useRef(false)
  const lifecycleRef = useRef(null)
  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone

  function restoreFocus(root = rootRef.current) {
    const previous = returnFocusRef.current
    returnFocusRef.current = null
    if (previous?.isConnected && (root?.contains(document.activeElement) || document.activeElement === document.body)) {
      previous.focus()
    }
  }

  // A replacement/update gets a fresh lifetime. Changing the host's callback
  // alone must not reset the clock. Only time spent unpaused counts.
  useEffect(() => {
    if (!toast) {
      hoveredRef.current = false
      focusedRef.current = false
      dragRef.current = null
      return undefined
    }
    setExiting(false)
    focusedRef.current = rootRef.current?.contains(document.activeElement) || false
    dragRef.current = null
    suppressClickRef.current = false
    let remaining = toast.action?.undo === true ? UNDO_MS : TOAST_MS
    let startedAt = null
    let timer = null
    let fading = false
    let finished = false

    function dismiss() {
      if (finished) return
      finished = true
      clearTimeout(timer)
      restoreFocus()
      onDoneRef.current(toast.id)
    }

    function syncPause() {
      if (finished) return
      const paused = hoveredRef.current || focusedRef.current || document.hidden
      if (paused && startedAt !== null) {
        remaining = Math.max(0, remaining - (Date.now() - startedAt))
        startedAt = null
        clearTimeout(timer)
      } else if (!paused && startedAt === null) {
        startedAt = Date.now()
        timer = setTimeout(() => {
          startedAt = null
          if (fading) {
            dismiss()
          } else {
            fading = true
            remaining = EXIT_MS
            setExiting(true)
            syncPause()
          }
        }, remaining)
      }
    }

    lifecycleRef.current = { dismiss, syncPause }
    document.addEventListener('visibilitychange', syncPause)
    syncPause()
    return () => {
      finished = true
      clearTimeout(timer)
      lifecycleRef.current = null
      document.removeEventListener('visibilitychange', syncPause)
    }
  }, [toast])

  // Keyed updates keep their focus destination; closure/unmount returns to
  // the connected origin only if the user has not moved focus elsewhere.
  useEffect(() => {
    if (!toast) return undefined
    const root = rootRef.current
    const onKeyDown = (event) => {
      if (isCompositionKey(event)) return
      if (event.key !== 'F6' || event.altKey || event.ctrlKey || event.metaKey) return
      event.preventDefault()
      if (root.contains(document.activeElement) && returnFocusRef.current) {
        restoreFocus(root)
      } else {
        if (!root.contains(document.activeElement)) returnFocusRef.current = document.activeElement
        const target = actionRef.current || root
        target.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      restoreFocus(root)
    }
  }, [toast?.id])

  function finishDrag(event) {
    const drag = dragRef.current
    if (!drag || drag.id !== event.pointerId) return
    dragRef.current = null
    const dx = event.clientX - drag.x
    const dy = event.clientY - drag.y
    if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy)) {
      suppressClickRef.current = true
      event.preventDefault()
      lifecycleRef.current?.dismiss()
    }
  }

  if (!toast) return null
  return (
    <div
      ref={rootRef}
      className={`toast ${exiting ? 'exit' : 'enter'}`}
      role="status"
      tabIndex={-1}
      style={{ touchAction: 'pan-y' }}
      onMouseEnter={() => { hoveredRef.current = true; lifecycleRef.current?.syncPause() }}
      onMouseLeave={() => { hoveredRef.current = false; lifecycleRef.current?.syncPause() }}
      onFocusCapture={() => { focusedRef.current = true; lifecycleRef.current?.syncPause() }}
      onBlurCapture={(event) => {
        if (event.currentTarget.contains(event.relatedTarget)) return
        focusedRef.current = false
        lifecycleRef.current?.syncPause()
      }}
      onPointerDown={(event) => {
        if (event.button !== 0 || event.isPrimary === false) return
        suppressClickRef.current = false
        dragRef.current = { id: event.pointerId, x: event.clientX, y: event.clientY }
        event.currentTarget.setPointerCapture?.(event.pointerId)
      }}
      onPointerUp={finishDrag}
      onPointerCancel={() => { dragRef.current = null }}
      onLostPointerCapture={() => { dragRef.current = null }}
      onClickCapture={(event) => {
        if (!suppressClickRef.current) return
        event.preventDefault()
        event.stopPropagation()
        suppressClickRef.current = false
      }}
    >
      <span>{toast.text}</span>
      {toast.action && (
        <button
          ref={actionRef}
          type="button"
          className="action"
          onClick={() => {
            const dismiss = lifecycleRef.current?.dismiss
            toast.action.onClick()
            dismiss?.()
          }}
        >
          {toast.action.label || 'View'}
        </button>
      )}
    </div>
  )
}
