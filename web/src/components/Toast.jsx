// NT2 toast (crib §2): a completed event surfaces as ONE quiet bottom-center
// toast above the docked bar — newest replaces (App holds a single toast slot),
// auto-fades after ~5 s (180 ms exit per M1), at most one quiet action (View).
// Anatomy/colors come from styles.css (.toast, .toast .action, .enter/.exit).

import { useEffect, useState } from 'react'

const TOAST_MS = 5000 // visible window before the exit fade
const EXIT_MS = 180   // M1 exit fade

export default function Toast({ toast, onDone }) {
  const [exiting, setExiting] = useState(false)

  // Restart the lifecycle whenever a new toast lands (newest replaces).
  useEffect(() => {
    if (!toast) return undefined
    setExiting(false)
    const fade = setTimeout(() => setExiting(true), TOAST_MS)
    const done = setTimeout(() => onDone(toast.id), TOAST_MS + EXIT_MS)
    return () => { clearTimeout(fade); clearTimeout(done) }
  }, [toast, onDone])

  if (!toast) return null
  return (
    <div className={`toast ${exiting ? 'exit' : 'enter'}`} role="status">
      <span>{toast.text}</span>
      {toast.action && (
        <button
          type="button"
          className="action"
          onClick={() => { toast.action.onClick(); onDone(toast.id) }}
        >
          {toast.action.label || 'View'}
        </button>
      )}
    </div>
  )
}
