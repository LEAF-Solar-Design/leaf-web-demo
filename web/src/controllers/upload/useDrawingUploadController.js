import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { getDrawingUploadStatus, getGuestUploadPolicy, getUploadedDrawingIntake, uploadDrawing } from '../../api.js'
import createDrawingUploadController from './createDrawingUploadController.js'

const services = {
  policy: getGuestUploadPolicy,
  upload: uploadDrawing,
  status: getDrawingUploadStatus,
  intake: getUploadedDrawingIntake,
  wait: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
}

export default function useDrawingUploadController({ onReady } = {}) {
  const onReadyRef = useRef(onReady)
  onReadyRef.current = onReady
  const controllerRef = useRef(null)
  if (!controllerRef.current) {
    controllerRef.current = createDrawingUploadController({ services, onReady: (result) => onReadyRef.current?.(result) })
  }
  const controller = controllerRef.current
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.start()
    controller.loadPolicy()
    return () => controller.dispose()
  }, [controller])
  const actions = useMemo(
    () => ({ upload: controller.upload, cancel: controller.cancel, refreshPolicy: controller.loadPolicy }),
    [controller],
  )
  return { ...state, actions }
}
