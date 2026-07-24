import { useEffect, useRef, useSyncExternalStore } from 'react'

import { createWorkspaceController } from './createWorkspaceController.js'

/** React adapter for the framework-neutral workspace controller. */
export default function useWorkspaceController(options) {
  const controllerRef = useRef(null)
  if (!controllerRef.current) controllerRef.current = createWorkspaceController(options)
  const controller = controllerRef.current
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)

  useEffect(() => {
    // start() makes the lifecycle safe under StrictMode's setup-cleanup-setup probe.
    controller.start()
    return () => controller.dispose()
  }, [controller])

  useEffect(() => {
    controller.setMock(options?.mock)
    controller.loadProjects()
  }, [controller, options?.mock, snapshot.orgId])

  return { ...snapshot, ...controller }
}
