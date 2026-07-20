import { useEffect, useState } from 'react'

// M1 exit fades (the standard: exits are 180 ms fades), generalized from
// Toast.jsx's exact pattern: the caller keeps passing its open state / data
// value; when it clears, the last truthy value is HELD for EXIT_MS so the
// .exit fade can play before unmount. Returns { shown, exiting } — render
// while `shown` is truthy, and swap the mount's `enter` class for `exit`
// while `exiting` (styles.css: .exit = m1-out, display-only).
// prefers-reduced-motion: styles.css zeroes every animation, so the surface
// holds still through the beat (freeze, never hide) and then unmounts.
export const EXIT_MS = 180

export default function useExit(value, ms = EXIT_MS) {
  const [held, setHeld] = useState(null) // last truthy value, kept through the fade
  const [exiting, setExiting] = useState(false)
  useEffect(() => {
    if (value) { setHeld(value); setExiting(false); return undefined }
    setExiting(true)
    const id = setTimeout(() => { setHeld(null); setExiting(false) }, ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return { shown: value || held, exiting: !value && exiting }
}
