import { useEffect, useMemo } from 'react'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { SESSION_ERROR } from './engineSession.js'
import { hexHandle } from './engineIntake.js'
import { buildDrawingObjectIndex } from '../lib/drawingObjectIndex.js'
import { useDrawingObjects } from '../site/DrawingObjectsContext.jsx'

export default function EngineObjectBridge({ active = true, intake = null, solarGraph = null }) {
  const { session } = useEngineSessionContext()
  const { publish, publishSelection } = useDrawingObjects()
  const { documentId, entities, selectedIds, engineParsed, errorKind } = session
  const showing = active && engineParsed && errorKind !== SESSION_ERROR.CRASHED
  const drawingKey = `engine:${documentId}`
  const scopedIntake = intake?.source === 'engine' && intake.documentId === documentId ? intake : null
  const index = useMemo(() => showing ? buildDrawingObjectIndex({ drawingKey, entities, intake: scopedIntake, solarGraph }) : null,
    [showing, drawingKey, entities, scopedIntake, solarGraph])
  useEffect(() => index ? publish('engine', { index }) : undefined, [index, publish])
  useEffect(() => {
    if (showing) publishSelection('engine', (selectedIds || []).map(hexHandle))
  }, [showing, selectedIds, publishSelection])
  return null
}
