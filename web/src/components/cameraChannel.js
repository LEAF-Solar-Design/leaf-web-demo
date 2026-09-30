/** One latest camera snapshot per frame; rebuilding a scene keeps subscribers. */
export function createCameraChannel({ schedule, cancel }) {
  const listeners = new Set()
  let latest = { pose: null, viewport: null }, pending = null, generation = 0, disposed = false
  const deliver = (listener) => { try { listener(latest) } catch { /* isolate consumers */ } }
  const cancelPending = () => {
    generation++
    if (pending !== null) cancel(pending)
    pending = null
  }
  return {
    subscribe(listener) {
      if (disposed) return () => {}
      listeners.add(listener)
      deliver(listener)
      return () => listeners.delete(listener)
    },
    publish(snapshot) {
      if (disposed) return
      latest = snapshot
      if (pending !== null) return
      const version = generation
      pending = schedule(() => {
        if (version !== generation || disposed) return
        pending = null
        for (const listener of listeners) deliver(listener)
      })
    },
    cancelPending,
    dispose() { cancelPending(); disposed = true; listeners.clear() },
  }
}
