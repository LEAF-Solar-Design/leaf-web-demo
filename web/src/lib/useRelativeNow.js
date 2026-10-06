import { useSyncExternalStore } from 'react'

let now = Date.now()
let timer = null
const listeners = new Set()
const getSnapshot = () => now

function subscribe(listener) {
  if (listeners.size === 0) {
    now = Date.now()
    timer = setInterval(() => {
      now = Date.now()
      for (const notify of listeners) notify()
    }, 30_000)
  }
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) {
      clearInterval(timer)
      timer = null
    }
  }
}

// All consumers in the document share one clock and one interval. The final
// unsubscribe stops it, including React's development mount/unmount cycle.
export default function useRelativeNow() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
