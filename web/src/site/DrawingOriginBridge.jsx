import { useLayoutEffect } from 'react'
import { useDrawingObjects } from './DrawingObjectsContext.jsx'

export default function DrawingOriginBridge({ onChange }) {
  const drawingKey = useDrawingObjects()?.index?.drawingKey ?? null
  useLayoutEffect(() => { onChange(drawingKey) }, [drawingKey, onChange])
  return null
}
