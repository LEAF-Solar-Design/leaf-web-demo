import { useEffect, useRef, useSyncExternalStore } from 'react'
import { createCatalogController } from './createCatalogController.js'

export function useCatalogController(options) {
  const controllerRef = useRef(null)

  if (!controllerRef.current) {
    controllerRef.current = createCatalogController(options)
  }

  const controller = controllerRef.current

  useEffect(() => {
    controller.setContext(options.context || {})
  }, [
    controller,
    options.context?.mock,
    options.context?.entitlements,
    options.context?.running,
    options.context?.agentDisabled,
  ])

  useEffect(() => {
    controller.start()
    return () => controller.destroy()
  }, [controller])

  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
  )

  return { state, actions: controller.actions, controller }
}

export default useCatalogController
