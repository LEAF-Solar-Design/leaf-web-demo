import { useEffect, useRef, useSyncExternalStore } from 'react'
import { subscribeUnauthorized } from '../../api.js'
import { logout, subscribeTokenStored } from '../../auth.js'
import { createSessionController } from './createSessionController.js'

export default function useSessionController({ storage, endSession = logout } = {}) {
  const controllerRef = useRef(null)
  if (!controllerRef.current) {
    controllerRef.current = createSessionController({
      storage: storage || globalThis.localStorage,
      subscribeUnauthorized,
      subscribeTokenStored,
      endSession,
    })
  }
  const controller = controllerRef.current
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)

  useEffect(() => {
    controller.start()
    return () => controller.destroy()
  }, [controller])

  return { ...state, actions: controller.actions, controller }
}
