// @vitest-environment node
//
// Slice 10b/10c: the palette's pure filtering, tested without a DOM (the
// same split composer.js's own tests use).
import { describe, expect, it } from 'vitest'

import {
  MAX_ACTION_ROWS,
  MAX_ARTIFACT_ROWS_PER_KIND,
  actionPaletteRows,
  findResultRows,
  sessionArtifactRows,
  toolArtifactRows,
  versionArtifactRows,
} from './palette.js'

describe('actionPaletteRows', () => {
  const actions = [
    { id: 'fit', label: 'fit', icon: 'fit', kbd: null, disabled: false, reason: '', onSelect: () => {} },
    { id: 'undo', label: 'undo', icon: 'undo', kbd: null, disabled: true, reason: 'nothing to undo', onSelect: () => {} },
    { id: 'bar:shortcuts', label: 'Keyboard shortcuts', icon: '', kbd: 'Shift+?', disabled: false, reason: '', onSelect: () => {} },
  ]

  it('carries the disabled row\'s real reason through unchanged, never a dash or a zero', () => {
    const rows = actionPaletteRows(actions, 'undo')
    expect(rows).toEqual([
      { kind: 'action', id: 'undo', label: 'undo', icon: 'undo', kbd: null, disabled: true, reason: 'nothing to undo', onSelect: actions[1].onSelect },
    ])
  })

  it('never invents a kbd cap: only the record that actually carries one shows one', () => {
    const rows = actionPaletteRows(actions, '')
    expect(rows.find((r) => r.id === 'fit').kbd).toBeNull()
    expect(rows.find((r) => r.id === 'bar:shortcuts').kbd).toBe('Shift+?')
  })

  it('ranks a label-prefix match ahead of a substring match', () => {
    const rows = actionPaletteRows([
      { id: 'a', label: 'zundo-like', onSelect: () => {} },
      { id: 'b', label: 'undo', onSelect: () => {} },
    ], 'undo')
    expect(rows.map((r) => r.id)).toEqual(['b', 'a'])
  })

  it('bounds the row count at MAX_ACTION_ROWS', () => {
    const many = Array.from({ length: MAX_ACTION_ROWS + 10 }, (_, i) => ({ id: `a${i}`, label: `action ${i}`, onSelect: () => {} }))
    expect(actionPaletteRows(many, '')).toHaveLength(MAX_ACTION_ROWS)
  })
})

describe('artifact rows', () => {
  it('versionArtifactRows filters on v-number, tool and note, and bounds per kind', () => {
    const payload = { versions: Array.from({ length: MAX_ARTIFACT_ROWS_PER_KIND + 5 }, (_, i) => ({ v: i + 1, tool: 'drawing.write', note: 'seed' })) }
    const rows = versionArtifactRows(payload, '')
    expect(rows).toHaveLength(MAX_ARTIFACT_ROWS_PER_KIND)
    expect(rows[0]).toEqual({ kind: 'version', id: 'version:1', label: 'v1', description: 'drawing.write · seed' })
    expect(versionArtifactRows(payload, 'nomatch')).toEqual([])
  })

  it('sessionArtifactRows reads an empty payload (a non-operator caller) as zero rows, not an error', () => {
    expect(sessionArtifactRows(undefined, '')).toEqual([])
    expect(sessionArtifactRows({ sessions: [] }, '')).toEqual([])
    const rows = sessionArtifactRows({ sessions: [{ session_id: 'opsess-1', profile: 'default', environment: 'staging', status: 'idle' }] }, 'opsess')
    expect(rows).toEqual([{ kind: 'session', id: 'session:opsess-1', label: 'opsess-1', description: 'default · staging · idle' }])
  })

  it('toolArtifactRows reuses the same tools list the "/" picker already holds', () => {
    const tools = [{ name: 'drawing.write', description: 'mutate the drawing' }, { name: 'other', description: '' }]
    expect(toolArtifactRows(tools, 'drawing')).toEqual([
      { kind: 'tool', id: 'tool:drawing.write', label: 'drawing.write', description: 'mutate the drawing' },
    ])
  })
})

describe('findResultRows', () => {
  it('reshapes the search endpoint payload and drops a malformed row rather than throwing', () => {
    const rows = findResultRows({
      results: [
        { kind: 'tool', id: 'tool:x', label: 'x', description: 'd' },
        { kind: 'version', id: 42, label: 'bad-id-type' },
        null,
      ],
    })
    expect(rows).toEqual([{ kind: 'tool', id: 'tool:x', label: 'x', description: 'd' }])
  })

  it('an absent payload is zero rows, never a throw', () => {
    expect(findResultRows(undefined)).toEqual([])
  })
})
