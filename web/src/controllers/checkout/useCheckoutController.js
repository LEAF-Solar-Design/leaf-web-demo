import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { getDrawingVersions, releaseCheckout, takeCheckout } from '../../api.js'
import createCheckoutController from './createCheckoutController.js'

const defaultServices = {
  loadVersions: (drawingId) => getDrawingVersions(false, drawingId),
  take: (drawingId, holder) => takeCheckout(drawingId, holder),
  release: (drawingId, holder) => releaseCheckout(drawingId, holder),
}

export default function useCheckoutController({ mock = false, drawingId = null, holder = null, services = defaultServices } = {}) {
  const controllerRef = useRef(null)
  if (!controllerRef.current) controllerRef.current = createCheckoutController({ mock, drawingId, holder, services })
  const controller = controllerRef.current
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)

  useEffect(() => {
    controller.start()
    return () => controller.dispose()
  }, [controller])

  useEffect(() => {
    controller.setScope({ mock, drawingId, holder })
    controller.refresh()
  }, [controller, drawingId, holder, mock])

  const actions = useMemo(
    () => ({ refresh: controller.refresh, take: controller.take, release: controller.release }),
    [controller],
  )

  return { ...state, actions }
}
