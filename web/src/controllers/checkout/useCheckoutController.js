import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { getDrawingVersions, releaseCheckout, takeCheckout } from '../../api.js'
import {
  bootstrapCheckoutReloadHandoff,
  claimHolderId,
  holdCheckoutReloadAuthority,
  remintSessionHolderId,
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

/**
 * The shared single-writer checkout controller for BOTH shells.
 *
 * W2c moved three things in here that the console (App.jsx) used to hand-roll
 * beside its own copy of the lock state. They are protocol, not presentation,
 * so a second implementation of them is a second answer to "who may write":
 *
 *   1. THE HOLDER CLAIM. `claimHolderId` announces this runtime's holder id so
 *      a DUPLICATED tab — which inherits sessionStorage and therefore the
 *      incumbent's id — remints instead of impersonating it.
 *   2. THE CLAIM DEFERRAL. While a provisional reload handoff exists, the claim
 *      is HELD BACK. The handoff is keyed to the boot holder id, so a duplicate
 *      that won the claim race would push this runtime off exactly the id its
 *      staged capability names, and the redemption would be lost. The Web Lock
 *      arbitrates first; the claim then starts from whichever branch resolved,
 *      and the runtime that redeemed the handoff claims AS THE INCUMBENT.
 *   3. THE REFUSED-REDEMPTION REMINT. A refused redemption means the stored
 *      holder id may be a clone's (a reload and a duplication present
 *      identically — see checkoutIdentity.js). Mint a fresh one, tell the
 *      shell, and join the claim channel under it.
 *
 * `bootDrawingId` is the drawing this PAGE LOAD booted with. A shell that
 * knows it (the console: DrawingIdentityProvider seeds the console mode
 * unconditionally) gets the handoff bootstrapped in the render phase, so the
 * claim deferral is decided before the first effect runs. A shell that does
 * not (the operator stage boots with no drawing until one loads or uploads)
 * passes null and the bootstrap happens in the authority effect, exactly as
 * before — bootstrapping early with the wrong drawing id would CONSUME and
 * discard a valid handoff, because the consume is destructive by contract.
 */
export default function useCheckoutController({
  mock = false,
  drawingId = null,
  holder = null,
  bootDrawingId = null,
  deferForAuthCallback = false,
  onHolderRemint = null,
  services = defaultServices,
} = {}) {
  const controllerRef = useRef(null)
  if (!controllerRef.current) controllerRef.current = createCheckoutController({ mock, drawingId, holder, services })
  const controller = controllerRef.current
  const authorityRef = useRef(null)
  const authorityScopeRef = useRef(null)
  const claimRef = useRef(null)
  const remintRef = useRef(onHolderRemint)
  remintRef.current = onHolderRemint
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)

  // The identity this page load BOOTED with. The handoff is keyed to it, and a
  // remint must not change what we try to redeem.
  const bootHolderRef = useRef(null)
  if (!bootHolderRef.current && holder) bootHolderRef.current = holder
  const deferForAuthCallbackRef = useRef(null)
  if (deferForAuthCallbackRef.current === null) deferForAuthCallbackRef.current = !!deferForAuthCallback

  // `undefined` = not read yet; `null` = nothing to redeem, or already
  // resolved. The consume is DESTRUCTIVE and one-use, so it happens once per
  // runtime and every later reader sees the same answer.
  const provisionalRef = useRef(undefined)
  const readProvisional = (forHolder, forDrawingId) => {
    if (provisionalRef.current === undefined) {
      provisionalRef.current = bootstrapCheckoutReloadHandoff({
        holder: forHolder,
        drawingId: forDrawingId,
        deferForAuthCallback: deferForAuthCallbackRef.current,
      })
    }
    return provisionalRef.current
  }
  if (provisionalRef.current === undefined && bootDrawingId && bootHolderRef.current) {
    readProvisional(bootHolderRef.current, bootDrawingId)
  }

  const startClaim = useCallback((id, { incumbent = false } = {}) => {
    if (!id) return
    claimRef.current?.stop()
    claimRef.current = claimHolderId({
      id,
      onRemint: (next) => remintRef.current?.(next),
      // The runtime that redeemed the reload handoff IS the incumbent for this
      // holder: it owns the staged capability. Claiming at age 0 makes it win
      // every tie-break, so a duplicate that raced the reload steps aside
      // instead of pushing the real owner off its own id.
      ...(incumbent ? { now: () => 0 } : {}),
    })
  }, [])

  const abandonInheritedHolder = useCallback(() => {
    const next = remintSessionHolderId()
    remintRef.current?.(next)
    startClaim(next)
  }, [startClaim])

  useEffect(() => {
    controller.start()
    return () => controller.dispose()
  }, [controller])

  useEffect(() => {
    controller.setScope({ mock, drawingId, holder })
    controller.refresh()
  }, [controller, drawingId, holder, mock])

  useEffect(() => {
    // Deferred: the authority effect starts the claim from whichever branch
    // resolves the handoff. See (2) in the header.
    if (!provisionalRef.current) startClaim(bootHolderRef.current || holder)
    return () => {
      claimRef.current?.stop()
      claimRef.current = null
    }
    // Claim once per runtime. A remint must not restart the claim loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (mock || !drawingId || !holder) {
      // A VOIDED scope — a tenant switch resets the drawing identity — must not
      // leave the previous scope's origin-wide authority lock held by this
      // runtime: a duplicate queued behind it would wait forever.
      authorityRef.current?.stop()
      authorityRef.current = null
      authorityScopeRef.current = null
      return undefined
    }
    const scope = `${holder}\u0000${drawingId}`
    if (authorityScopeRef.current && authorityScopeRef.current !== scope) {
      authorityRef.current?.stop()
      authorityRef.current = null
      authorityScopeRef.current = null
    }

    const handoff = readProvisional(bootHolderRef.current || holder, drawingId)
    if (!handoff) return undefined
    const authority = holdCheckoutReloadAuthority({
      handoff,
      onAcquired: (owned) => {
        provisionalRef.current = null
        controller.restoreCapability(owned.capability)
        startClaim(owned.holder, { incumbent: true })
        controller.refresh()
      },
      onError: () => {
        provisionalRef.current = null
        controller.clearCapability()
        abandonInheritedHolder()
        controller.refresh()
      },
    })
    authorityRef.current = authority
    authorityScopeRef.current = scope
    if (!authority.active) {
      provisionalRef.current = null
      controller.clearCapability()
      abandonInheritedHolder()
      Promise.resolve()
        .then(() => services.release(drawingId, handoff.capability))
        .catch(() => {})
    }
    // The module runtime owns this lock. A StrictMode cleanup must not create a
    // release gap; explicit checkout release and scope changes stop it.
    return undefined
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [controller, drawingId, holder, mock, services, startClaim, abandonInheritedHolder])

  useEffect(() => {
    if (mock || !drawingId || !holder) return undefined
    const stage = () => {
      const provisional = provisionalRef.current
      // A handoff this runtime staged but has not yet redeemed is still the
      // only copy of that authority: re-stage it so a second reload in the
      // redemption window does not strand a live lease.
      const capability = controller.getCapability() || provisional?.capability
      if (!capability) return
      // Stop answering claims BEFORE staging. An outgoing runtime that still
      // replies `held` makes the reloading runtime remint away from the very
      // holder id the staged handoff is keyed to.
      claimRef.current?.stop()
      stageCheckoutReloadHandoff({
        capability,
        holder: provisional?.holder || holder,
        drawingId: provisional?.drawingId || drawingId,
      })
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
