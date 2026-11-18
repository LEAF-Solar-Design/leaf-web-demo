// Canonical lineage of an unchanged project version projected by the engine.
// Rendering and overlay scene identity remain independent of this observer key.
export function projectEngineObservationBinding({ projectMode, scopeKey, currentScopeKey,
  engineIntake, engineDocument, engineDirty, canonicalVersionId, drawingKey } = {}) {
  if (projectMode !== true || engineDirty !== false || !scopeKey || scopeKey !== currentScopeKey
    || engineIntake?.source !== 'engine' || !engineIntake.documentId
    || engineDocument?.documentId !== engineIntake.documentId
    || engineDocument.documentOrigin !== 'head'
    || !canonicalVersionId || engineDocument.committedVersion !== canonicalVersionId
    || typeof drawingKey !== 'string' || !drawingKey.startsWith('project:')
    || !drawingKey.endsWith(`:${canonicalVersionId}`)) return null
  return Object.freeze({ documentId: engineIntake.documentId, drawingKey })
}

export function observationDrawingKey(sceneKey, intake, binding, parentDrawingKey) {
  return intake?.source === 'engine' && binding?.documentId === intake.documentId
    && typeof binding?.drawingKey === 'string' && binding.drawingKey.startsWith('project:')
    && binding.drawingKey === parentDrawingKey ? binding.drawingKey : sceneKey
}
