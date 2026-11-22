import { describe, it, expect } from 'vitest'
import { projectEngineObservationBinding, observationDrawingKey } from './projectDrawingObservation.js'
const version = '11111111-1111-4111-8111-111111111111'
const drawingKey = `project:org:project:drawing:${version}`
const engineIntake = { source: 'engine', documentId: 'project.dxf' }
const valid = { projectMode: true, scopeKey: 'scope-A', currentScopeKey: 'scope-A', engineIntake,
  engineDocument: { documentId: 'project.dxf', documentOrigin: 'head', committedVersion: version },
  engineDirty: false, canonicalVersionId: version, drawingKey }
describe('project drawing observation lineage', () => {
  it('binds a clean engine projection of the exact committed canonical version', () => {
    const binding = projectEngineObservationBinding(valid)
    expect(binding).toEqual({ documentId: 'project.dxf', drawingKey })
    expect(Object.isFrozen(binding)).toBe(true)
    expect(observationDrawingKey('engine:project.dxf', engineIntake, binding, drawingKey)).toBe(drawingKey)
  })
  it.each([
    { projectMode: false }, { scopeKey: 'stale' }, { currentScopeKey: '' },
    { engineDirty: true }, { engineDirty: undefined }, { canonicalVersionId: 'other' },
    { engineIntake: { ...engineIntake, source: 'bridge' } },
    { engineDocument: { ...valid.engineDocument, documentId: 'other.dxf' } },
    { engineDocument: { ...valid.engineDocument, documentOrigin: 'import' } },
    { engineDocument: { ...valid.engineDocument, committedVersion: 'other' } },
    { drawingKey: 'standalone' }, { drawingKey: 'project:wrong-version' },
  ])('does not claim canonical identity for an unbound or changed projection: %o', (patch) => {
    expect(projectEngineObservationBinding({ ...valid, ...patch })).toBeNull()
  })
  it('keeps standalone, wrong document, and changed parent identities separate', () => {
    const binding = projectEngineObservationBinding(valid)
    expect(observationDrawingKey('engine:project.dxf', engineIntake, binding, 'standalone')).toBe('engine:project.dxf')
    expect(observationDrawingKey('engine:other', { ...engineIntake, documentId: 'other' }, binding, drawingKey)).toBe('engine:other')
    expect(observationDrawingKey('project:base', { source: 'server' }, binding, drawingKey)).toBe('project:base')
  })
})
