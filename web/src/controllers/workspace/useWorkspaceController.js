import { useEffect, useRef, useSyncExternalStore } from 'react'

import { createWorkspaceController } from './createWorkspaceController.js'
import { authConfigured } from '../../auth.js'
import { isWorkspaceBootstrapRequired } from '../../api.js'

/**
 * React adapter for the framework-neutral workspace controller.
 *
 * `autoProvision: { enabled, name, onProvisioned }` creates a signed-in
 * caller's first workspace without a prompt once the projects read proves
 * them unbound. The controller owns the once-only and in-flight guards;
 * onProvisioned runs after the org exists so the host can re-read tenant
 * state in place.
 */
export default function useWorkspaceController(options) {
  const onProvisionedRef = useRef(null)
  onProvisionedRef.current = options?.autoProvision?.onProvisioned
  const controllerRef = useRef(null)
  if (!controllerRef.current) {
    const callbacks = options?.callbacks || {}
    controllerRef.current = createWorkspaceController({
      authLive: authConfigured,
      isBootstrapRequired: isWorkspaceBootstrapRequired,
      ...options,
      callbacks: {
        ...callbacks,
        onAutoProvisioned: (org) => {
          callbacks.onAutoProvisioned?.(org)
          onProvisionedRef.current?.(org)
        },
      },
    })
  }
  const controller = controllerRef.current
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)

  useEffect(() => {
    // start() makes the lifecycle safe under StrictMode's setup-cleanup-setup probe.
    controller.start()
    return () => controller.dispose()
  }, [controller])

  useEffect(() => {
    controller.setMock(options?.mock)
    controller.setReadsPaused(options?.readsPaused)
    controller.loadProjects()
  }, [controller, options?.mock, options?.readsPaused, snapshot.orgId])

  const autoEnabled = !!options?.autoProvision?.enabled
  const autoName = options?.autoProvision?.name
  useEffect(() => {
    if (!autoEnabled || snapshot.orgId || snapshot.bootstrapState !== 'unbound'
      || snapshot.autoProvision !== 'idle') return
    void controller.autoProvisionOrg(autoName ? { name: autoName } : undefined)
  }, [controller, autoEnabled, autoName, snapshot.orgId, snapshot.bootstrapState, snapshot.autoProvision])

  return { ...snapshot, ...controller }
}
