import { useEffect, useRef, useSyncExternalStore } from 'react'

import {
  getClaudeGrant,
  getEntitlements,
  getHealth,
  getUsage,
  activateClaudeGrant,
  linkClaudeGrant,
  unlinkClaudeGrant,
} from '../../api.js'
import { humanizeError } from '../../errorHumanize.js'
import createPlatformTrustController from './createPlatformTrustController.js'
import { classifyHealth, entitlementAllowed, resolveQuotaStatus } from './platformTrustModel.js'

const defaultServices = {
  getClaudeGrant,
  getEntitlements,
  getHealth,
  getUsage,
  activateClaudeGrant,
  linkClaudeGrant,
  unlinkClaudeGrant,
}

/**
 * React adapter for the platform trust store. Services are injectable so fixture
 * and local proof tiers exercise the same controller with different transports.
 */
export default function usePlatformTrustController({
  mock = false,
  services = defaultServices,
  formatError = humanizeError,
  now,
  onAuthRequired,
  autoLoad = true,
  quotaResult = null,
  quotaAt = 0,
  quotaPollIntervalMs = 60_000,
} = {}) {
  const controllerRef = useRef(null)
  if (!controllerRef.current) {
    controllerRef.current = createPlatformTrustController({
      mock,
      services: { ...defaultServices, ...services },
      formatError,
      now,
      onAuthRequired,
    })
  }
  const controller = controllerRef.current

  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)

  useEffect(() => {
    controller.start()
    return () => controller.destroy()
  }, [controller])

  useEffect(() => {
    controller.setMock(mock)
    if (autoLoad && !mock) controller.refreshAll()
  }, [autoLoad, controller, mock])

  const quota = resolveQuotaStatus({
    result: quotaResult,
    conditionAt: quotaAt,
    usage: state.usage,
    usageAt: state.usageAt,
  })

  useEffect(() => {
    if (mock || state.authRequired || !quota.condition || quota.cleared) return undefined
    controller.loadUsage()
    const timer = setInterval(controller.loadUsage, quotaPollIntervalMs)
    return () => clearInterval(timer)
  }, [controller, mock, quota.cleared, quota.condition?.kind, quotaAt, quotaPollIntervalMs, state.authRequired])

  return {
    ...state,
    quota,
    healthStatus: classifyHealth(state.health),
    isEntitled: (key, fallback = true) => entitlementAllowed(state.entitlements, key, fallback),
    actions: {
      refreshAll: controller.refreshAll,
      loadUsage: controller.loadUsage,
      loadEntitlements: controller.loadEntitlements,
      loadHealth: controller.loadHealth,
      loadGrant: controller.loadGrant,
      linkClaude: controller.linkClaude,
      activateClaude: controller.activateClaude,
      unlinkClaude: controller.unlinkClaude,
      reportAuthRequired: controller.reportAuthRequired,
    },
  }
}
