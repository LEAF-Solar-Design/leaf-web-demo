import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { getDrawingVersions, releaseCheckout, takeCheckout } from '../../api.js'
import {
  bootstrapCheckoutReloadHandoff,
  holdCheckoutReloadAuthority,
  stageCheckoutReloadHandoff,
} from '../../checkoutIdentity.js'
import createCheckoutController from './createCheckoutController.js'

const defaultServices = {
  loadVersions: (drawingId) => getDrawingVersions(false, drawingId),
  take: (drawingId, holder, capability) => takeCheckout(drawingId, holder, undefined, capability),
  release: (drawingId, capability) => releaseCheckout(drawingId, capability),
}

export async function secureTakenCheckoutAuthority({
  controller,
  result,
  drawingId,
  holder,
  services,
  holdAuthority = holdCheckoutReloadAuthority,
} = {}) {
  const capability = result?.checkout_capability
  if (!result?.acquired || !capability || !drawingId || !holder) return null

  // The take response arrives before the origin-wide lock. Remove the bearer
  // proof during that gap so no render can issue a write without both proofs.
  controller.clearCapability()
  const authority = holdAuthority({
    handoff: { capability, holder, drawingId, createdAtMs: Date.now() },
    onAcquired: (owned) => controller.restoreCapability(owned.capability),
    onError: () => controller.clearCapability(),
  })
  const acquired = authority.active && await authority.acquired
  if (acquired) return authority

  authority.stop()
  controller.clearCapability()
  try { await services.release(drawingId, capability) } catch { /* fail closed */ }
  await controller.refresh()
  return null
}

export default function useCheckoutController({ mock = false, drawingId = null, holder = null, services = defaultServices } = {}) {
  const controllerRef = useRef(null)
  if (!controllerRef.current) controllerRef.current = createCheckoutController({ mock, drawingId, holder, services })
  const controller = controllerRef.current
  const authorityRef = useRef(null)
  const authorityScopeRef = useRef(null)
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)

  useEffect(() => {
    controller.start()
    return () => controller.dispose()
  }, [controller])

  useEffect(() => {
    controller.setScope({ mock, drawingId, holder })
    controller.refresh()
  }, [controller, drawingId, holder, mock])

  useEffect(() => {
    if (mock || !drawingId || !holder) return undefined
    const scope = `${holder}\u0000${drawingId}`
    if (authorityScopeRef.current && authorityScopeRef.current !== scope) {
      authorityRef.current?.stop()
      authorityRef.current = null
      authorityScopeRef.current = null
    }

    const handoff = bootstrapCheckoutReloadHandoff({ holder, drawingId })
    if (!handoff) return undefined
    const authority = holdCheckoutReloadAuthority({
      handoff,
      onAcquired: (owned) => controller.restoreCapability(owned.capability),
      onError: () => controller.clearCapability(),
    })
    authorityRef.current = authority
    authorityScopeRef.current = scope
    if (!authority.active) {
      controller.clearCapability()
      Promise.resolve()
        .then(() => services.release(drawingId, handoff.capability))
        .catch(() => {})
    }
    // The module runtime owns this lock. A StrictMode cleanup must not create a
    // release gap; explicit checkout release and scope changes stop it.
    return undefined
  }, [controller, drawingId, holder, mock, services])

  useEffect(() => {
    if (mock || !drawingId || !holder) return undefined
    const stage = () => {
      const capability = controller.getCapability()
      if (capability) stageCheckoutReloadHandoff({ capability, holder, drawingId })
    }
    window.addEventListener('beforeunload', stage)
    return () => window.removeEventListener('beforeunload', stage)
  }, [controller, drawingId, holder, mock])

  const actions = useMemo(() => {
    const take = async () => {
      const result = await controller.takeDeferred()
      let acquiredAuthority = null
      if (result?.acquired && result.checkout_capability && drawingId && holder) {
        const scope = `${holder}\u0000${drawingId}`
        authorityRef.current?.stop()
        acquiredAuthority = await secureTakenCheckoutAuthority({
          controller, result, drawingId, holder, services,
        })
        authorityRef.current = acquiredAuthority
        authorityScopeRef.current = authorityRef.current ? scope : null
      }
      if (!result) return result
      const safeResult = { ...result }
      delete safeResult.checkout_capability
      if (result.acquired && !acquiredAuthority) safeResult.acquired = false
      return safeResult
    }
    const release = async () => {
      const result = await controller.release()
      if (!controller.getCapability()) {
        authorityRef.current?.stop()
        authorityRef.current = null
        authorityScopeRef.current = null
      }
      return result
    }
    return {
      refresh: controller.refresh,
      take,
      release,
      getCapability: controller.getCapability,
    }
  }, [controller, drawingId, holder])

  return { ...state, actions }
}
