import { useMemo, useSyncExternalStore } from 'react'

export function useProjectCheckoutController(projectClient) {
  const subscribe = useMemo(() => (listener) => projectClient.subscribe(listener), [projectClient])
  const getSnapshot = useMemo(() => () => projectClient.getSnapshot(), [projectClient])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const actions = useMemo(() => Object.freeze({
    refresh: (options) => projectClient.readCheckout(options),
    take: (options) => projectClient.acquireCheckout(options),
    release: (options) => projectClient.releaseCheckout(options),
  }), [projectClient])
  return { state: { binding: snapshot.binding, checkout: snapshot.checkout,
    ownsCheckout: snapshot.ownsCheckout, pending: snapshot.pending, error: snapshot.error }, actions }
}
