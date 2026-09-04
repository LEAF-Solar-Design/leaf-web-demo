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
    // Slice 8a: the refusal copy names the header's credential panel only where
    // it is actually mounted, so a mode flip MUST re-reach the controller. A
    // missing dep here would freeze the honest-copy answer at first render.
    options.context?.credentialMountAvailable,
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
