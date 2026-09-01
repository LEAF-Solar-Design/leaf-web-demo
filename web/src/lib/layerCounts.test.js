/**
 * W2a mechanical dedupe: pins the layer entity-count behavior shared by
 * App.jsx's and site/ToolCast.jsx's `layerCounts` useMemo — confirmed
 * identical against git history, so this is a pure merge, no options.
 */
import { describe, expect, it } from 'vitest'

import { countEntitiesByLayer } from './layerCounts.js'

describe('countEntitiesByLayer', () => {
  it('seeds every known layer to 0 before counting', () => {
    const shown = { layers: ['Roof', 'Blocks', 'Surfaces'], polylines: [], inserts: [], faces3d: [] }
    expect(countEntitiesByLayer(shown)).toEqual({ Roof: 0, Blocks: 0, Surfaces: 0 })
  })

  it('counts polylines, inserts, and 3dfaces together per layer', () => {
    const shown = {
      layers: ['Roof', 'Blocks', 'Surfaces'],
      polylines: [{ layer: 'Roof' }, { layer: 'Roof' }],
      inserts: [{ layer: 'Blocks' }],
      faces3d: [{ layer: 'Surfaces' }, { layer: 'Surfaces' }, { layer: 'Surfaces' }],
    }
    expect(countEntitiesByLayer(shown)).toEqual({ Roof: 2, Blocks: 1, Surfaces: 3 })
  })

  it('an insert/face-only layer never reads as a false 0 once seeded, and gains a real count', () => {
    // The regression App.jsx's comment names: fixture=edit's Blocks/Surfaces
    // layers carry only inserts/faces, never polylines.
    const shown = {
      layers: ['Blocks'],
      polylines: [],
      inserts: [{ layer: 'Blocks' }],
      faces3d: [],
    }
    expect(countEntitiesByLayer(shown)).toEqual({ Blocks: 1 })
  })

  it('counts an entity on a layer not in the seeded list too (no data loss)', () => {
    const shown = { layers: ['Roof'], polylines: [{ layer: 'Unlisted' }] }
    expect(countEntitiesByLayer(shown)).toEqual({ Roof: 0, Unlisted: 1 })
  })

  it('degrades to an empty object for a null/undefined intake', () => {
    expect(countEntitiesByLayer(null)).toEqual({})
    expect(countEntitiesByLayer(undefined)).toEqual({})
  })
})
