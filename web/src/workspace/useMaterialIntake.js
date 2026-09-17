import { useEffect, useRef, useSyncExternalStore } from 'react'
import { importUploadedDrawingVersion } from '../api.js'
import { createMaterialIntakeController } from './createMaterialIntakeController.js'

export default function useMaterialIntake({ upload, onAttached, onReady } = {}) {
  const callbacks = useRef({ onAttached, onReady })
  callbacks.current = { onAttached, onReady }
  const ref = useRef(null)
  if (!ref.current) ref.current = createMaterialIntakeController({ services: { importUpload: importUploadedDrawingVersion } })
  const controller = ref.current
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    let notified = null
    return controller.subscribe(() => {
      const next = controller.getSnapshot()
      if (!next.attached) notified = null
      if (next.attached && next !== notified) {
        notified = next
        callbacks.current.onAttached?.(next.drawing)
      }
    })
  }, [controller])
  useEffect(() => upload?.subscribeReady?.((result) => Promise.all([
    controller.onUploadReady(result), callbacks.current.onReady?.(result),
  ])), [controller, upload?.subscribeReady])
  useEffect(() => () => controller.dispose(), [controller])
  return { ...state, begin: controller.begin, retry: controller.retry, reset: controller.reset }
}
