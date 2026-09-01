/**
 * W2a mechanical dedupe: pins the entity-selection resolver behavior each
 * shell shipped before the extraction (App.jsx's `selection` useMemo vs
 * site/ToolCast.jsx's `selectedEntity`) — they disagree on what an
 * UNRESOLVED handle (valid intake, no matching entity) returns.
 */
import { describe, expect, it } from 'vitest'

import { selectEntity } from './selectEntity.js'

const intake = {
  polylines: [{ handle: 'p1', layer: 'Roof' }],
  inserts: [{ handle: 'i1', layer: 'Blocks', name: 'Panel' }],
  faces3d: [{ handle: 'f1', layer: 'Surfaces' }],
}

describe('selectEntity', () => {
  it('resolves a matching polyline, insert, and 3dface', () => {
    expect(selectEntity(intake, 'p1')).toEqual({ handle: 'p1', kind: 'polyline', layer: 'Roof' })
    expect(selectEntity(intake, 'i1')).toEqual({ handle: 'i1', kind: 'insert', layer: 'Blocks', name: 'Panel' })
    expect(selectEntity(intake, 'f1')).toEqual({ handle: 'f1', kind: '3dface', layer: 'Surfaces' })
  })

  it('short-circuits to null with no handle or no intake, regardless of onUnresolved', () => {
    const generic = () => ({ handle: 'should-not-appear', kind: 'entity', layer: null })
    expect(selectEntity(intake, null, { onUnresolved: generic })).toBeNull()
    expect(selectEntity(intake, undefined, { onUnresolved: generic })).toBeNull()
    expect(selectEntity(null, 'p1', { onUnresolved: generic })).toBeNull()
    expect(selectEntity(undefined, 'p1', { onUnresolved: generic })).toBeNull()
  })

  it('defaults an unresolved handle to null — ToolCast.jsx original behavior', () => {
    expect(selectEntity(intake, 'does-not-exist')).toBeNull()
  })

  it('reproduces App.jsx original behavior: a generic entity descriptor on an unresolved handle', () => {
    const result = selectEntity(intake, 'does-not-exist', {
      onUnresolved: (handle) => ({ handle, kind: 'entity', layer: null }),
    })
    expect(result).toEqual({ handle: 'does-not-exist', kind: 'entity', layer: null })
  })

  it('handles a missing collection on the intake gracefully', () => {
    expect(selectEntity({}, 'p1')).toBeNull()
    expect(selectEntity({ polylines: undefined }, 'p1', {
      onUnresolved: (handle) => ({ handle, kind: 'entity', layer: null }),
    })).toEqual({ handle: 'p1', kind: 'entity', layer: null })
  })
})
