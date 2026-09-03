// @vitest-environment node
//
// The ribbon's clusters as data (W4d Slice A): every group is a real
// command with honest gating — a disabled tool always carries its reason,
// an unavailable group its note, and no cluster is ever fabricated.
import { describe, expect, it, vi } from 'vitest'

import {
  CATALOG_TOOL_NOTE_ALL_PLACED,
  MAX_LAYER_TOOLS,
  REASONS,
  RIBBON_RATIONALE,
  authorCluster,
  catalogClusters,
  catalogTabClusters,
  layersCluster,
  railCluster,
  versionCluster,
  viewCluster,
} from './ribbonClusters.js'

const FAMS = [
  {
    family_id: 'measurement',
    label: 'Measurement',
    capabilities: [
      { name: 'count-by-layer', description: 'Counts entities per layer.', capabilities: ['drawing.read'] },
    ],
  },
  {
    family_id: 'custom',
    label: 'Custom authored tools',
    capabilities: [
      { name: 'delete-marked-panel', description: 'Deletes the marked panel.', capabilities: ['drawing.write'] },
    ],
  },
]

function toolsOf(cluster) {
  return Object.fromEntries(cluster.tools.map((t) => [t.id, t]))
}

describe('catalogClusters', () => {
  it('maps one family cluster per family and arms the catalog run path with ribbon attribution', () => {
    const onRequestRun = vi.fn()
    const clusters = catalogClusters(FAMS, { onRequestRun })
    expect(clusters.map((c) => [c.id, c.kind])).toEqual([['measurement', 'family'], ['custom', 'family']])
    const read = toolsOf(clusters[0])['count-by-layer']
    expect(read.disabled).toBe(false)
    expect(read.write).toBe(false)
    read.onClick()
    expect(onRequestRun).toHaveBeenCalledWith(FAMS[0].capabilities[0], null, RIBBON_RATIONALE, 'ribbon')
  })

  it('disables write tools under the single-writer lock with the reason, and leaves read tools live', () => {
    const clusters = catalogClusters(FAMS, { onRequestRun: () => {}, writeLocked: true })
    const write = toolsOf(clusters[1])['delete-marked-panel']
    expect(write.disabled).toBe(true)
    expect(write.reason).toBe(REASONS.writeLocked)
    expect(toolsOf(clusters[0])['count-by-layer'].disabled).toBe(false)
  })

  it('prefers the caller-supplied lock note and names the plan when unentitled', () => {
    const locked = catalogClusters(FAMS, { onRequestRun: () => {}, writeLocked: true, writeLockNote: 'held by ops' })
    expect(toolsOf(locked[1])['delete-marked-panel'].reason).toBe('held by ops')
    const unentitled = catalogClusters(FAMS, { onRequestRun: () => {}, writeEntitled: false })
    expect(toolsOf(unentitled[1])['delete-marked-panel'].reason).toBe(REASONS.writeUnentitled)
  })

  it('disables everything during a run or preview and names the distinct reason', () => {
    const running = catalogClusters(FAMS, { onRequestRun: () => {}, running: true })
    expect(running.flatMap((c) => c.tools).every((t) => t.disabled && t.reason === REASONS.running)).toBe(true)
    const previewing = catalogClusters(FAMS, { onRequestRun: () => {}, previewing: true })
    expect(previewing.flatMap((c) => c.tools).every((t) => t.disabled && t.reason === REASONS.previewing)).toBe(true)
  })

  it('an empty fold is ONE honest cluster with the sentence and zero tools', () => {
    for (const families of [[], null, undefined]) {
      const clusters = catalogClusters(families, { onRequestRun: () => {} })
      expect(clusters).toHaveLength(1)
      expect(clusters[0].tools).toEqual([])
      expect(clusters[0].note).toBe('No tools for this surface yet.')
    }
  })
})

describe('the rail affordances the band carries while the rail is hidden', () => {
  it('a family cluster label opens that family when a handler is given, and is decoration otherwise', () => {
    const onOpenFamily = vi.fn()
    const [cluster] = catalogClusters(FAMS, { onRequestRun: () => {}, onOpenFamily })
    expect(cluster.labelTitle).toBe('Open Measurement in the tool rail (1 tools)')
    cluster.onLabelClick()
    expect(onOpenFamily).toHaveBeenCalledWith(FAMS[0])
    const [plain] = catalogClusters(FAMS, { onRequestRun: () => {} })
    expect(plain.onLabelClick).toBeNull()
  })

  it('railCluster is one real command: expand the hidden rail', () => {
    const onExpand = vi.fn()
    const cluster = railCluster({ onExpand })
    expect(cluster.tools.map((t) => t.id)).toEqual(['rail-expand'])
    cluster.tools[0].onClick()
    expect(onExpand).toHaveBeenCalledTimes(1)
  })
})

describe('viewCluster', () => {
  it('drives the viewer ref surface and is disabled with a reason without a drawing', () => {
    const viewer = { setView: vi.fn(() => true), getPose: () => ({ zoom: 2 }) }
    const on = viewCluster({ viewerRef: { current: viewer }, hasDrawing: true })
    toolsOf(on).fit.onClick()
    expect(viewer.setView).toHaveBeenCalledWith('home')
    toolsOf(on)['zoom-in'].onClick()
    expect(viewer.setView).toHaveBeenLastCalledWith({ zoom: 2.5 })
    const off = viewCluster({ viewerRef: { current: null }, hasDrawing: false })
    expect(off.tools.every((t) => t.disabled && t.reason === REASONS.noDrawing)).toBe(true)
    // A missing viewer is a no-op, never a throw.
    expect(() => toolsOf(off).fit.onClick()).not.toThrow()
  })

  it('carries the Properties pane toggle only when the caller owns the pane, as a pressed-state tool', () => {
    expect(toolsOf(viewCluster({ viewerRef: { current: null }, hasDrawing: true }))['properties-pane']).toBeUndefined()
    const onTogglePane = vi.fn()
    const open = toolsOf(viewCluster({ viewerRef: { current: null }, hasDrawing: false, paneOpen: true, onTogglePane }))['properties-pane']
    expect(open.pressed).toBe(true)
    expect(open.disabled).toBeUndefined()
    open.onClick()
    expect(onTogglePane).toHaveBeenCalledTimes(1)
    const closed = toolsOf(viewCluster({ viewerRef: { current: null }, hasDrawing: true, paneOpen: false, onTogglePane }))['properties-pane']
    expect(closed.pressed).toBe(false)
    expect(closed.title).toMatch(/Open/)
  })
})

describe('versionCluster', () => {
  it('gates undo/redo/history exactly like the toolbar, each with its reason', () => {
    const onUndo = vi.fn(); const onRedo = vi.fn(); const onToggleHistory = vi.fn()
    const live = toolsOf(versionCluster({ hasVersions: true, canUndo: true, canRedo: false, onUndo, onRedo, onToggleHistory, historyOpen: true }))
    expect(live.undo.disabled).toBe(false)
    live.undo.onClick(); expect(onUndo).toHaveBeenCalledTimes(1)
    expect(live.redo.disabled).toBe(true)
    expect(live.redo.reason).toBe(REASONS.nothingToRedo)
    expect(live.history.expanded).toBe(true)
    live.history.onClick(); expect(onToggleHistory).toHaveBeenCalledTimes(1)
  })

  it('the shared reasons win in resolution order: no versions, busy, running, preview, blocked', () => {
    const base = { hasVersions: true, canUndo: true, canRedo: true }
    expect(toolsOf(versionCluster({ ...base, hasVersions: false })).undo.reason).toBe(REASONS.noVersions)
    expect(toolsOf(versionCluster({ ...base, versionBusy: true })).undo.reason).toBe(REASONS.versionBusy)
    expect(toolsOf(versionCluster({ ...base, running: true })).redo.reason).toBe(REASONS.running)
    expect(toolsOf(versionCluster({ ...base, previewing: true })).undo.reason).toBe(REASONS.previewing)
    expect(toolsOf(versionCluster({ ...base, mutationsBlocked: true })).undo.reason).toBe(REASONS.mutationsBlocked)
    // History ignores the mutation gates: reading the chain is never a write.
    expect(toolsOf(versionCluster({ ...base, previewing: true, mutationsBlocked: true })).history.disabled).toBe(false)
  })
})

describe('layersCluster', () => {
  it('one pressed toggle per layer, visible unless the map says false, calling the toggle by name', () => {
    const onToggle = vi.fn()
    const cluster = layersCluster({ layers: ['Panels', 'Roof'], counts: { Panels: 3 }, visibleLayers: { Roof: false }, onToggle })
    expect(cluster.tools.map((t) => [t.label, t.pressed])).toEqual([['Panels', true], ['Roof', false]])
    expect(cluster.tools[0].title).toBe('Hide Panels (3)')
    expect(cluster.tools[1].title).toBe('Show Roof (0)')
    cluster.tools[1].onClick()
    expect(onToggle).toHaveBeenCalledWith('Roof')
    expect(cluster.note).toBeNull()
  })

  it('bounds the strip and names the remainder; no layers is an honest note', () => {
    const layers = Array.from({ length: MAX_LAYER_TOOLS + 4 }, (_, i) => `L${i}`)
    const cluster = layersCluster({ layers, onToggle: () => {} })
    expect(cluster.tools).toHaveLength(MAX_LAYER_TOOLS)
    expect(cluster.note).toBe('+4 more in the Layers palette')
    const empty = layersCluster({ layers: undefined })
    expect(empty.tools).toEqual([])
    expect(empty.note).toBe(REASONS.noDrawing)
  })
})

describe('the tool record carries icon, size and tab (slice 3)', () => {
  // The regression this whole slice must not cause: a catalog whose tools
  // declare NOTHING renders byte-identically to before the record grew fields.
  it('is byte-identical for tools that declare no icon and no placement', () => {
    const onRequestRun = () => {}
    const clusters = catalogClusters(FAMS, { onRequestRun })
    expect(clusters.map((c) => ({
      id: c.id,
      label: c.label,
      kind: c.kind,
      tools: c.tools.map(({ onClick, ...rest }) => rest),
    }))).toEqual([
      {
        id: 'measurement',
        label: 'Measurement',
        kind: 'family',
        tools: [{
          id: 'count-by-layer',
          label: 'count-by-layer',
          text: 'count-by-layer',
          icon: 'toolbox',
          size: 'large',
          title: 'Counts entities per layer.',
          write: false,
          disabled: false,
          reason: '',
        }],
      },
      {
        id: 'custom',
        label: 'Custom authored tools',
        kind: 'family',
        tools: [{
          id: 'delete-marked-panel',
          label: 'delete-marked-panel',
          text: 'delete-marked-panel',
          icon: 'toolbox',
          size: 'large',
          title: 'Deletes the marked panel.',
          write: true,
          disabled: false,
          reason: '',
        }],
      },
    ])
    expect(catalogTabClusters(FAMS, { onRequestRun })).toEqual({})
  })

  it('reads the icon and the size off the record when it declares them', () => {
    const fams = [{
      family_id: 'measurement',
      label: 'Measurement',
      capabilities: [
        { name: 'count-by-layer', icon: 'layers', placement: { size: 'small' }, capabilities: [] },
        { name: 'measure-panel-area', capabilities: [] },
      ],
    }]
    const tools = toolsOf(catalogClusters(fams, { onRequestRun: () => {} })[0])
    expect([tools['count-by-layer'].icon, tools['count-by-layer'].size]).toEqual(['layers', 'small'])
    expect([tools['measure-panel-area'].icon, tools['measure-panel-area'].size]).toEqual(['toolbox', 'large'])
  })

  it('ignores an unknown size rather than rendering an unknown ribbon shape', () => {
    const fams = [{
      family_id: 'measurement', label: 'Measurement',
      capabilities: [{ name: 'count-by-layer', placement: { size: 'huge' }, capabilities: [] }],
    }]
    expect(toolsOf(catalogClusters(fams, { onRequestRun: () => {} })[0])['count-by-layer'].size).toBe('large')
  })

  it('moves a tool that names a tab OUT of the families panel and into that tab', () => {
    const onRequestRun = vi.fn()
    const fams = [{
      family_id: 'measurement',
      label: 'Measurement',
      capabilities: [
        { name: 'count-by-layer', placement: { tab: 'annotate' }, capabilities: [] },
        { name: 'measure-panel-area', capabilities: [] },
      ],
    }]
    const stay = catalogClusters(fams, { onRequestRun })
    expect(stay.map((c) => c.id)).toEqual(['measurement'])
    expect(Object.keys(toolsOf(stay[0]))).toEqual(['measure-panel-area'])

    const byTab = catalogTabClusters(fams, { onRequestRun })
    expect(Object.keys(byTab)).toEqual(['annotate'])
    expect(byTab.annotate.map((c) => [c.id, c.label])).toEqual([['measurement@annotate', 'Measurement']])
    const placed = toolsOf(byTab.annotate[0])['count-by-layer']
    placed.onClick()
    expect(onRequestRun).toHaveBeenCalledWith(
      fams[0].capabilities[0], null, RIBBON_RATIONALE, 'ribbon')
  })

  it('seats one family on two tabs as two distinct clusters', () => {
    const fams = [{
      family_id: 'measurement',
      label: 'Measurement',
      capabilities: [
        { name: 'a', placement: { tab: 'draw' }, capabilities: [] },
        { name: 'b', placement: { tab: 'view' }, capabilities: [] },
      ],
    }]
    const byTab = catalogTabClusters(fams, { onRequestRun: () => {} })
    expect(byTab.draw[0].id).toBe('measurement@draw')
    expect(byTab.view[0].id).toBe('measurement@view')
    // Every tool moved, so the families panel keeps no empty shell.
    expect(catalogClusters(fams, { onRequestRun: () => {} })).toEqual([
      { id: 'tools', label: 'Tools', kind: 'group', note: CATALOG_TOOL_NOTE_ALL_PLACED, tools: [] },
    ])
  })

  it('an unknown tab leaves the tool exactly where it renders today', () => {
    const fams = [{
      family_id: 'measurement', label: 'Measurement',
      capabilities: [{ name: 'count-by-layer', placement: { tab: 'model' }, capabilities: [] }],
    }]
    expect(Object.keys(toolsOf(catalogClusters(fams, { onRequestRun: () => {} })[0])))
      .toEqual(['count-by-layer'])
    expect(catalogTabClusters(fams, { onRequestRun: () => {} })).toEqual({})
  })

  it('carries the write gating onto a placed tool too', () => {
    const fams = [{
      family_id: 'custom', label: 'Custom authored tools',
      capabilities: [{
        name: 'delete-marked-panel', placement: { tab: 'draw' }, capabilities: ['drawing.write'],
      }],
    }]
    const byTab = catalogTabClusters(fams, { onRequestRun: () => {}, writeLocked: true })
    const tool = toolsOf(byTab.draw[0])['delete-marked-panel']
    expect([tool.write, tool.disabled, tool.reason]).toEqual([true, true, REASONS.writeLocked])
  })
})

describe('authorCluster', () => {
  it('one real command, disabled with the plan reason when authoring is not entitled', () => {
    const onOpen = vi.fn()
    const on = authorCluster({ onOpen, entitled: true })
    on.tools[0].onClick()
    expect(onOpen).toHaveBeenCalledTimes(1)
    const off = authorCluster({ onOpen, entitled: false })
    expect(off.tools[0].disabled).toBe(true)
    expect(off.tools[0].reason).toBe(REASONS.buildUnentitled)
    // Entitled but the authoring stage is off: a different fix, a different sentence.
    const dark = authorCluster({ onOpen, entitled: true, available: false })
    expect(dark.tools[0].disabled).toBe(true)
    expect(dark.tools[0].reason).toBe(REASONS.buildUnavailable)
    // The plan's answer outranks availability when both are false.
    expect(authorCluster({ onOpen, entitled: false, available: false }).tools[0].reason).toBe(REASONS.buildUnentitled)
  })

  it('carries no second tool until something has been published', () => {
    expect(authorCluster({ onOpen: () => {} }).tools.map((t) => t.id)).toEqual(['author-tool'])
    expect(authorCluster({ onOpen: () => {}, authored: null }).tools).toHaveLength(1)
  })

  it('shows a just-published tool with the record icon and runs it through onUseAuthored', () => {
    const onUseAuthored = vi.fn()
    const authored = { name: 'panel-audit', icon: 'layers', catalog_digest: 'sha256:abc' }
    const cluster = authorCluster({ onOpen: () => {}, authored, onUseAuthored })
    const tool = cluster.tools[1]
    expect([tool.id, tool.label, tool.icon, tool.size, tool.disabled, tool.reason])
      .toEqual(['authored:panel-audit', 'panel-audit', 'layers', 'large', false, ''])
    tool.onClick()
    expect(onUseAuthored).toHaveBeenCalledWith(authored)
  })

  it('falls back to the shared toolbox glyph when the published record names no icon', () => {
    const cluster = authorCluster({
      onOpen: () => {}, authored: { name: 'panel-audit', catalog_digest: 'sha256:abc' },
    })
    expect(cluster.tools[1].icon).toBe('toolbox')
  })

  it('says honestly that a digest-less publish is not runnable yet', () => {
    // Exact string, pinned: this is the sentence a user reads, and it is the
    // ribbon's half of publishedCatalogTool.js's fail-closed rule.
    expect(REASONS.publishing).toBe('publishing: not in the runnable catalog yet')
    for (const authored of [
      { name: 'panel-audit' },
      { name: 'panel-audit', catalog_digest: '' },
      { name: 'panel-audit', catalog_digest: 7 },
    ]) {
      const tool = authorCluster({ onOpen: () => {}, authored }).tools[1]
      expect(tool.disabled).toBe(true)
      expect(tool.reason).toBe(REASONS.publishing)
    }
  })

  it('a settled authored tool still yields to a run in flight or a version preview', () => {
    const authored = { name: 'panel-audit', catalog_digest: 'sha256:abc' }
    expect(authorCluster({ onOpen: () => {}, authored, running: true }).tools[1].reason)
      .toBe(REASONS.running)
    expect(authorCluster({ onOpen: () => {}, authored, previewing: true }).tools[1].reason)
      .toBe(REASONS.previewing)
  })
})
