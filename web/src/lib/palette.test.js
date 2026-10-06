// @vitest-environment node
//
// Slice 10b/10c: the palette's pure filtering, tested without a DOM (the
// same split composer.js's own tests use).
import { describe, expect, it } from 'vitest'

import {
  MAX_ACTION_ROWS,
  MAX_ARTIFACT_ROWS_PER_KIND,
  actionPaletteRows,
  actionRow,
  drawingObjectRows,
  findResultRows,
  PALETTE_GROUP_LABELS,
  projectArtifactRows,
  sessionArtifactRows,
  toolArtifactRows,
  versionArtifactRows,
} from './palette.js'

describe('actionPaletteRows', () => {
  it('SSD1-A row3: actionRow maps the eight fields and actionPaletteRows still caps at MAX_ACTION_ROWS', () => {
    expect(actionRow({ id: 'x', label: 'X' })).toEqual({
      kind: 'action', id: 'x', label: 'X', icon: '', kbd: null, disabled: false, reason: '', onSelect: undefined,
    })
    const list = Array.from({ length: 30 }, (_, i) => ({ id: `a${i}`, label: `action ${i}` }))
    expect(actionPaletteRows(list, '')).toHaveLength(MAX_ACTION_ROWS)
  })

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
  it('keeps server version and session rows and pins their group labels', () => {
    const results = [
      { kind: 'version', id: 'version:2', label: 'v2', description: 'drawing.write · panel move' },
      { kind: 'session', id: 'session:opsess-1', label: 'opsess-1', description: 'default · staging · idle' },
    ]
    expect(findResultRows({ results })).toEqual(results)
    expect(PALETTE_GROUP_LABELS.version).toBe('Versions')
    expect(PALETTE_GROUP_LABELS.session).toBe('Sessions')
    expect(PALETTE_GROUP_LABELS['drawing-object']).toBe('Drawing objects')
    expect(PALETTE_GROUP_LABELS.project).toBe('Projects')
  })
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

describe('local find indexes', () => {
  it('matches drawing object names, paths, handles and aliases, capped per group', () => {
    const records = Array.from({ length: 12 }, (_, i) => ({ id: `h:${i}`, name: `Panel ${i}`, path: 'drawing / roof', aliases: ['module'] }))
    expect(drawingObjectRows({ records }, 'PANEL')).toHaveLength(MAX_ARTIFACT_ROWS_PER_KIND)
    expect(drawingObjectRows({ records }, 'roof')[0]).toEqual({ kind: 'drawing-object', id: 'h:0', label: 'Panel 0', description: 'drawing / roof' })
    expect(drawingObjectRows({ records }, 'module')).toHaveLength(MAX_ARTIFACT_ROWS_PER_KIND)
    expect(drawingObjectRows({ records }, 'h:11').map((r) => r.id)).toEqual(['h:11'])
    expect(drawingObjectRows({ records }, '')).toEqual([])
    expect(drawingObjectRows(null, 'panel')).toEqual([])
    expect(drawingObjectRows({ records }, 'missing')).toEqual([])
  })

  it('matches canonical projects without inventing a project from a drawing name', () => {
    const projects = Array.from({ length: 12 }, (_, i) => ({ project_id: `p${i}`, name: `Roof ${i}` }))
    expect(projectArtifactRows(projects, 'ROOF')).toHaveLength(MAX_ARTIFACT_ROWS_PER_KIND)
    expect(projectArtifactRows(projects, 'p11')).toEqual([{ kind: 'project', id: 'project:p11', label: 'Roof 11', description: '' }])
    expect(projectArtifactRows([null, { name: 'Roof' }, { project_id: 'p1' }], 'roof')).toEqual([])
    expect(projectArtifactRows(undefined, 'roof')).toEqual([])
    expect(projectArtifactRows(projects, '')).toEqual([])
  })
})
